"""How high inside a chapel can an outside observer actually see?

The excavation report puts the painted scenes on the *dome* — "Adam and
Eve, which are in the east side of the dome, and I continue the
description of the scenes anti clockwise" (Chapel of Peace) — and on
the arcaded zone above the walls in the Chapel of Exodus. That makes
the obvious question about the frescoes a geometric one: a doorway's
head caps how steeply a sightline can rise once it is inside, so there
is a height above which nothing in the chamber can be seen from
outside at all, no matter where the observer stands.

This measures that ceiling instead of arguing about it. From the
standing station outside a chapel's door, a vertical ladder of points
is raised at two places inside — the chamber's centre, under where a
dome's apex would sit, and against the inner face of the wall facing
the entrance — and the ray-caster is asked which of them are visible.
The highest one that is gives the observer's reach.

The comparison is against the springing line: the top of the walls,
where a dome starts. Reach below springing means the entire dome, and
so the entire painted surface, is invisible from outside — a
conclusion that follows from the model's own geometry rather than from
an assumption about what people could see.

The bound is deliberately generous. The chamber is modelled empty, the
observer may stand anywhere on the door's axis rather than where a
person would, and the head height used is the registry's, which is
already the most favourable reading of the evidence. If the dome is
out of reach under those conditions it is out of reach in fact.
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

from sanity_checks import (FOOTPRINTS, DEM_BASE_04, DEM_REGEN,
                           check, warn, failures)
from aperture_registry import (APERTURES_DIR, INVENTORY, BUILDING_FABRIC,
                               largest_poly)
from build_aperture_walls import load_fabric, plane_fit
from viewshed import select_device, load_dem, HeightfieldScene
from scene3d import HybridScene, flatten_footprints, load_scene_meshes
from test_feature_visibility import build_openings

LADDER_STEP = 0.05         # vertical resolution of the reach probe (m)
MESH_DIR = APERTURES_DIR / "meshes_all_fabric"


def facing_wall(walls, door):
    """Index of the wall most nearly opposite the door's, or None."""
    nx, ny = door["n_in"]
    best, bi = -2.0, None
    for wi, ((x0, y0), (x1, y1)) in enumerate(walls):
        if wi == door["wall"]:
            continue
        L = math.hypot(x1 - x0, y1 - y0)
        if L < 1e-6:
            continue
        # Inward normal of this wall, oriented like the door's is.
        ux, uy = (x1 - x0) / L, (y1 - y0) / L
        d = nx * -uy + ny * ux
        if abs(d) > best:
            best, bi = abs(d), wi
    return bi


def reach(scene, eye, x, y, z0, z1):
    """Highest z at (x, y) visible from `eye`, or None if none is."""
    n = max(2, int((z1 - z0) / LADDER_STEP) + 1)
    zs = np.linspace(z0, z1, n)
    pts = np.column_stack([np.full(n, x), np.full(n, y), zs])
    vis = np.asarray(scene.visible_mask(eye, pts)) == 1
    return float(zs[vis].max()) if vis.any() else None


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--footprints", type=Path, default=FOOTPRINTS)
    p.add_argument("--registry", type=Path, default=INVENTORY)
    p.add_argument("--fabric", type=Path, default=BUILDING_FABRIC)
    p.add_argument("--paintings", type=Path,
                   default=APERTURES_DIR / "painting_inventory.csv")
    p.add_argument("--mesh-dir", type=Path, default=MESH_DIR)
    p.add_argument("--dem", type=Path, default=DEM_REGEN)
    p.add_argument("--bare-dem", type=Path, default=DEM_BASE_04)
    p.add_argument("--thickness", type=float, default=0.4)
    p.add_argument("--ids", nargs="+", type=int, default=None,
                   help="chapels to probe; defaults to every chapel "
                        "named in the painting inventory")
    p.add_argument("--all-chapels", action="store_true",
                   help="probe every chapel with a door instead, to "
                        "show the painted ones are not special")
    p.add_argument("--out-dir", type=Path,
                   default=APERTURES_DIR / "feature_visibility")
    return p


def main():
    args = build_parser().parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    device = select_device()

    fp = gpd.read_file(args.footprints)
    fp["ID"] = fp["ID"].astype(int)
    reg = list(csv.DictReader(open(args.registry)))
    fabric = load_fabric(args.fabric, check, warn)

    if args.ids:
        want = set(args.ids)
    elif args.all_chapels:
        want = None
    else:
        pv = pd.read_csv(args.paintings)
        want = set(pv["ID"].astype(int))
        check(bool(want), "painted chapels loaded",
              f"{sorted(want)}")

    bare = rasterio.open(args.bare_dem)

    def ground_z(pts):
        return np.array([v[0] for v in bare.sample(
            [(float(x), float(y)) for x, y in pts])], float)

    aps_by_id = build_openings(fp, reg, fabric, ground_z, args.thickness)
    mesh_paths = sorted(args.mesh_dir.glob("building_*.obj"))
    check(len(mesh_paths) > 100, "building meshes found",
          f"{len(mesh_paths)}")
    if failures:
        sys.exit(1)
    mesh_ids = {int(p.stem.split("_")[1]) for p in mesh_paths}

    dem, tr, _c, nod, _p = load_dem(args.dem)
    geoms = [r.geometry for _, r in fp.iterrows()
             if int(r["ID"]) in mesh_ids]
    dem, _n = flatten_footprints(dem, tr, nod, geoms,
                                 bare_dem_path=args.bare_dem)
    scene = HybridScene(HeightfieldScene(dem, tr, nod, device),
                        load_scene_meshes(mesh_paths))

    from aperture_registry import canonical_walls
    rows = []
    for _, frow in fp.iterrows():
        bid = int(frow["ID"])
        if want is not None and bid not in want:
            continue
        aps = aps_by_id.get(bid, [])
        doors = [a for a in aps if a["kind"] == "door"]
        if not doors or bid not in mesh_ids:
            continue
        door = doors[0]
        geom = largest_poly(frow.geometry)
        walls = canonical_walls(geom)
        elev = (float(frow["Elevation"])
                if np.isfinite(frow.get("Elevation", np.nan)) else 0.0)
        plane = plane_fit(geom, elev if elev > 0 else 3.5, ground_z)

        eye = door["eye"]
        c = geom.representative_point()
        gz_c = float(ground_z([(c.x, c.y)])[0])
        springing = float(plane(c.x, c.y))
        centre_reach = reach(scene, eye, c.x, c.y, gz_c + 0.2,
                             springing + 3.0)

        wi = facing_wall(walls, door)
        far_reach, far_gz, far_top = None, float("nan"), float("nan")
        if wi is not None:
            (x0, y0), (x1, y1) = walls[wi]
            mx, my = (x0 + x1) / 2, (y0 + y1) / 2
            # Step just inside the far wall's inner face so the probe
            # sits in the room, not buried in the panel itself.
            th = fabric.get(bid, args.thickness)
            vx, vy = c.x - mx, c.y - my
            vl = math.hypot(vx, vy) or 1.0
            px = mx + vx / vl * (th + 0.05)
            py = my + vy / vl * (th + 0.05)
            far_gz = float(ground_z([(px, py)])[0])
            far_top = float(plane(px, py))
            far_reach = reach(scene, eye, px, py, far_gz + 0.2,
                              far_top - 0.05)

        rows.append(dict(
            ID=bid, door_ap=door["ap_id"],
            door_head_m=round(door["rect"][3] - gz_c, 2),
            springing_m=round(springing - gz_c, 2),
            centre_reach_m=(round(centre_reach - gz_c, 2)
                            if centre_reach is not None else ""),
            far_wall=wi if wi is not None else "",
            far_reach_m=(round(far_reach - far_gz, 2)
                         if far_reach is not None else ""),
            dome_reachable=bool(centre_reach is not None
                                and centre_reach >= springing)))

    df = pd.DataFrame(rows)
    check(len(df) > 0, "chapels probed", f"{len(df)}")
    out = args.out_dir / "painting_reach.csv"
    df.to_csv(out, index=False)

    print(f"\nREACH from outside the doorway ({len(df)} chapels)")
    print(df.to_string(index=False) if len(df) <= 25
          else df.head(25).to_string(index=False))
    n_dome = int(df["dome_reachable"].sum())
    print(f"\ndome/springing level reachable from outside: "
          f"{n_dome}/{len(df)} chapels")
    heights = pd.to_numeric(df["centre_reach_m"], errors="coerce")
    spring = pd.to_numeric(df["springing_m"], errors="coerce")
    ok = heights.notna() & spring.notna()
    if ok.any():
        print(f"median reach at the chamber centre "
              f"{heights[ok].median():.2f} m, median springing "
              f"{spring[ok].median():.2f} m, median shortfall "
              f"{(spring[ok] - heights[ok]).median():.2f} m")
    print(f"\nwrote {out.name}")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
