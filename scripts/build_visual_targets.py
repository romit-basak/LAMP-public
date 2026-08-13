"""Named visual targets — the things inside a chapel worth seeing.

The visibility graph currently asks whether a building's *centroid* is
visible, which is a proxy for the building, not for anything anyone
would have wanted to look at. This builds the real targets: a point on
the entrance axis just inside the door, and (once the interior
extraction lands) apses and niches. Those are what a doorway frames,
and whether they were arranged to be seen is the question the project
exists to ask.

**Target heights never come from a heightfield.** This is the trap that
already cost this project a result: `target_grid()` pins each target's
z to the surface DEM, so a point inside a footprint sits at the
*extruded block's roof*, and the raster door-effect measured with it was
meaningless (fixed 2026-08-10). A named target's z is instead derived
from the registry — bare ground plus the opening's mid-height — and
recorded as `z_source`. Every target is then required to lie below its
chapel's roof plane, because a target that floats above the roof is
visible from everywhere and would manufacture exactly the effect this
is meant to detect. Seven chapels standing under about 2 m fail that
and are dropped rather than clamped: a ruin has no interior left to be
hidden, so counting one as "seen" would measure the ruin.

Writes `target_inventory.csv` and `visual_targets.gpkg` (PointZ, one
layer per kind) *before* any visibility run, so the z column can be
eyeballed in QGIS in ten seconds rather than trusted.
"""

import argparse
import csv
import math
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from shapely.geometry import Point

from sanity_checks import (FOOTPRINTS, DEM_BASE_04, check, warn,
                           failures)
from aperture_registry import (APERTURES_DIR, INVENTORY, BUILDING_FABRIC,
                               DOOR_HEAD, DOOR_SILL, canonical_walls,
                               largest_poly, resolve_wall, row_depth,
                               row_face)
from build_aperture_walls import (built_thickness, load_fabric,
                                  plane_fit)

TARGET_COLS = ["ID", "target_id", "kind", "x", "y", "z", "z_source",
               "height_m", "headroom_m", "wall", "source",
               "confidence", "notes"]

# How far inside the doorway the entrance-axis target sits. Far enough
# to be genuinely interior (a point in the door plane is visible
# wherever the door is), close enough that it is not behind whatever
# stood in the middle of the chamber.
AXIS_INSET_M = 0.5


def wall_axis(geom, row):
    """(opening centre, inward unit normal, wall index), or None.

    The normal is oriented by testing an interior point rather than by
    trusting the ring's winding, so a footprint digitised clockwise
    does not silently place every target outside its own chapel."""
    walls = canonical_walls(geom)
    wi = resolve_wall(walls, row, check, warn)
    if wi is None:
        return None
    (x0, y0), (x1, y1) = walls[wi]
    L = math.hypot(x1 - x0, y1 - y0)
    ux, uy = (x1 - x0) / L, (y1 - y0) / L
    s = float(row["s_m"])
    cx, cy = x0 + ux * s, y0 + uy * s
    nx, ny = -uy, ux
    inside = largest_poly(geom).representative_point()
    if (inside.x - cx) * nx + (inside.y - cy) * ny < 0:
        nx, ny = -nx, -ny        # point inward
    return (cx, cy), (nx, ny), wi


def at_height(tx, ty, wi, row, ground_z, plane, sill_d, head_d):
    """Finish a target: bare ground plus the opening's mid-height."""
    sill = (float(row["sill_m"]) if row.get("sill_m", "") != ""
            else sill_d)
    head = (float(row["head_m"]) if row.get("head_m", "") != ""
            else head_d)
    gz = float(ground_z([(tx, ty)])[0])
    if not np.isfinite(gz):
        return None
    h = (sill + head) / 2.0
    return dict(x=tx, y=ty, z=gz + h, z_source="registry", height_m=h,
                wall=wi, roof_z=plane(tx, ty))


def entrance_axis_target(geom, row, ground_z, plane):
    """Point on the door's inward axis, at the opening's mid-height."""
    frame = wall_axis(geom, row)
    if frame is None:
        return None
    (cx, cy), (nx, ny), wi = frame
    tx, ty = cx + nx * AXIS_INSET_M, cy + ny * AXIS_INSET_M
    return at_height(tx, ty, wi, row, ground_z, plane, DOOR_SILL,
                     DOOR_HEAD)


def recess_target(geom, row, kind, ground_z, plane, thickness):
    """Point inside a niche or apse pocket, at its mid-depth.

    A recess is not a point on a wall face; it is a pocket, and what
    would have stood in one — a lamp, an offering, an image — sits
    inside it. Placing the target out in the room instead, as an
    entrance-axis point does, would make the pocket's own reveal
    irrelevant and count the feature as seen from oblique angles that
    in fact only ever saw the wall beside it.

    Geometry mirrors the mesh builder exactly: the outer face is the
    footprint ring, the inner face is one thickness in, an `in` recess
    is cut into the inner face and recedes back toward the ring, and an
    `out` recess does the reverse. A zero-thickness wall has no depth
    to recess into, so the builder drops the pocket and this returns
    None rather than leaving a target floating in a solid sheet."""
    if thickness <= 0:
        return None
    frame = wall_axis(geom, row)
    if frame is None:
        return None
    (cx, cy), (nx, ny), wi = frame
    depth = row_depth(row, kind, thickness, warn)
    if depth is None or depth <= 0:
        return None
    face = row_face(row, kind)
    off = thickness - depth / 2.0 if face == "in" else depth / 2.0
    return at_height(cx + nx * off, cy + ny * off, wi, row, ground_z,
                     plane, DOOR_SILL, DOOR_HEAD)


def centroid_target(geom, ground_z, plane, height_m):
    """Interior centroid at a fixed height — the existing graph's test,
    rebuilt here so the new targets can be compared against it."""
    p = largest_poly(geom).representative_point()
    gz = float(ground_z([(p.x, p.y)])[0])
    if not np.isfinite(gz):
        return None
    return dict(x=p.x, y=p.y, z=gz + height_m, z_source="ground+fixed",
                height_m=height_m, wall="", roof_z=plane(p.x, p.y))


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--registry", type=Path, default=INVENTORY)
    p.add_argument("--footprints", type=Path, default=FOOTPRINTS)
    p.add_argument("--bare-dem", type=Path, default=DEM_BASE_04)
    p.add_argument("--fabric", type=Path, default=BUILDING_FABRIC,
                   help="per-building wall thickness; sets how deep a "
                        "recess target sits inside its wall")
    p.add_argument("--thickness", type=float, default=0.4,
                   help="fallback thickness for buildings absent from "
                        "the fabric table")
    p.add_argument("--kinds", nargs="+",
                   default=["entrance_axis", "centroid", "apse", "niche"],
                   choices=["entrance_axis", "centroid", "apse", "niche"],
                   help="apse/niche need interior rows in the registry; "
                        "they are emitted only for rows that exist")
    p.add_argument("--centroid-height", type=float, default=1.0,
                   help="height (m) above interior ground for the "
                        "centroid target")
    p.add_argument("--min-headroom", type=float, default=0.3,
                   help="drop targets with less than this clearance "
                        "below their chapel's roof plane. These are "
                        "ruins standing under about 2 m, where a target "
                        "at eye level is at or above the surviving "
                        "fabric: there is no interior left to be hidden, "
                        "so counting one as 'seen' measures the ruin and "
                        "not the architecture")
    p.add_argument("--out-dir", type=Path, default=APERTURES_DIR)
    return p


def main():
    args = build_parser().parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    fp = gpd.read_file(args.footprints)
    fp["ID"] = fp["ID"].astype(int)
    reg = list(csv.DictReader(open(args.registry)))
    by_id = {}
    for r in reg:
        by_id.setdefault(int(r["ID"]), []).append(r)
    check(bool(by_id), "registry loaded", f"{len(by_id)} chapels")
    fabric = load_fabric(args.fabric, check, warn)

    src = rasterio.open(args.bare_dem)

    def ground_z(pts):
        return np.array([v[0] for v in src.sample(
            [(float(x), float(y)) for x, y in pts])], float)

    rows, n_no_pocket = [], 0
    for _, frow in fp.iterrows():
        bid = int(frow["ID"])
        geom = frow.geometry
        elev = (float(frow["Elevation"])
                if np.isfinite(frow.get("Elevation", np.nan)) else 0.0)
        if elev <= 0:
            elev = 3.5
        plane = plane_fit(geom, elev, ground_z)
        nominal = fabric.get(bid, args.thickness)
        thickness = built_thickness(geom, nominal, nominal)

        if "centroid" in args.kinds:
            t = centroid_target(geom, ground_z, plane,
                                args.centroid_height)
            if t:
                rows.append(dict(ID=bid, target_id=f"{bid}-centroid",
                                 kind="centroid", source="footprint",
                                 confidence="ref",
                                 notes="comparability baseline", **t))

        for r in by_id.get(bid, []):
            kind = r["kind"]
            if kind == "door" and "entrance_axis" in args.kinds:
                t = entrance_axis_target(geom, r, ground_z, plane)
                if t is None:
                    warn(f"ID {bid}: entrance axis not resolved", "")
                    continue
                rows.append(dict(
                    ID=bid, target_id=f"{bid}-axis-{r['ap_id']}",
                    kind="entrance_axis", source=r.get("source_pos", ""),
                    confidence=r.get("confidence", ""),
                    notes=f"{AXIS_INSET_M} m inside the doorway", **t))
            elif kind in ("apse", "niche") and kind in args.kinds:
                t = recess_target(geom, r, kind, ground_z, plane,
                                  thickness)
                if t is None:
                    n_no_pocket += 1
                    continue
                rows.append(dict(
                    ID=bid, target_id=f"{bid}-{kind}-{r['ap_id']}",
                    kind=kind, source=r.get("source_pos", ""),
                    confidence=r.get("confidence", ""),
                    notes=f"inside the {kind} pocket, at mid-depth", **t))

    check(bool(rows), "targets built", f"{len(rows)} rows")
    if n_no_pocket:
        print(f"{n_no_pocket} recess target(s) skipped: their wall is "
              "built zero-thickness, so the mesh has no pocket either")
    if failures:
        sys.exit(1)

    df = pd.DataFrame(rows)
    # The filter that matters: nothing may sit at or above its own roof,
    # or it would be visible from everywhere and fake the effect.
    df["headroom_m"] = (df["roof_z"] - df["z"]).round(3)
    low = df[df["headroom_m"] < args.min_headroom]
    for bid in sorted(low["ID"].unique()):
        warn(f"ID {bid}: target dropped, no headroom under the roof",
             f"{float(low[low['ID'] == bid]['headroom_m'].min()):.2f} m")
    df = df[df["headroom_m"] >= args.min_headroom].reset_index(drop=True)
    print(f"\ndropped {len(low)} target(s) on "
          f"{low['ID'].nunique()} ruined chapel(s) "
          f"(< {args.min_headroom} m headroom)")

    check((df["z"] < df["roof_z"]).all(),
          "every kept target sits below its chapel's roof plane")
    check((df["z_source"] != "heightfield").all(),
          "no target height sampled from a heightfield")

    print("\nTARGETS by kind")
    for kind, grp in df.groupby("kind"):
        clr = grp["roof_z"] - grp["z"]
        print(f"  {kind:14s} {len(grp):>4} targets, "
              f"{grp['ID'].nunique():>3} chapels, "
              f"headroom {clr.min():.2f}-{clr.max():.2f} m")

    out_csv = args.out_dir / "target_inventory.csv"
    df[TARGET_COLS].to_csv(out_csv, index=False)

    gdf = gpd.GeoDataFrame(
        df[TARGET_COLS],
        geometry=[Point(x, y, z) for x, y, z in
                  zip(df["x"], df["y"], df["z"])],
        crs=fp.crs)
    gpkg = args.out_dir / "visual_targets.gpkg"
    gpkg.unlink(missing_ok=True)
    gdf.to_file(gpkg, layer="targets_all", driver="GPKG")
    for kind, grp in gdf.groupby("kind"):
        grp.to_file(gpkg, layer=f"targets_{kind}", driver="GPKG")
    print(f"\nwrote {out_csv.name} and {gpkg.name} "
          f"(targets_all + {df['kind'].nunique()} kind layers)")

    if failures:
        print(f"\n{len(failures)} check(s) failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
