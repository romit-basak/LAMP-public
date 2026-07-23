"""Build the dome layer for the 3D visualization.

Joins the excavation-report chapel typology (xlsx `Type`, legend in the site
report Ch. III: types 4/5/6/7/9 are domed; 1/2/3 flat, 8 composite, 10
barrel-vaulted) onto the building footprints, measures each dome's center and
radius from the orthophoto (domes read as the largest bright blob on the
roof), and emits:

  - dome_inventory.csv   editable registry (rerun with --from-inventory to
                         honor hand edits instead of re-detecting)
  - domes.gpkg           PointZ dome centers at the *rendered* roofline (a
                         least-squares plane through vertex ground + height,
                         matching vertex-bound extrusion), sunk 0.35 x radius
                         so sphere symbols read as domes (the roofline hides
                         the lower bulge, incl. on slope-sheared roofs);
                         one layer per 0.5 m radius class (QGIS 3D sphere
                         radius cannot be data-defined, so each class is
                         styled separately), plus `domes_all` for 2D audit
  - dome_qc.png          orthophoto + footprints + dome circles for visual
                         audit (yellow = ortho-measured, red = fallback)

Untyped chapels are left untouched (listed, no dome). Barrel-vaulted (10) and
composite (8) stay flat this pass.

Run:
    .venv/bin/python scripts/build_dome_layer.py
    .venv/bin/python scripts/build_dome_layer.py --from-inventory \\
        <out-dir>/dome_inventory.csv

Exits nonzero if any self-check fails.
"""

import argparse
import math
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds
from scipy import ndimage
from shapely.geometry import Point
from shapely.ops import polylabel

from sanity_checks import ROOT, DEM_BASE_04, FOOTPRINTS, check, warn, failures

ORTHO = ROOT / ("100_Data/150_DigitalElevationModel/Generated_DEMs/"
                "Current_DEM/Bagawat-DEM-NewImageryOnly-0.4m-ORTHOPHOTO.tif")
XLSX = ROOT / "100_Data/120_SiteReport/Bagawat Data From Excavation Report.xlsx"

DOMED_TYPES = {4, 5, 6, 7, 9}     # site report Ch. III (pp. 20-23 of the PDF)
# Types 1/2/3 (flat, wooden-beam) and 8/10 (composite/barrel-vaulted) are the
# rest of the typology; they never reach the domed branch below, so there is
# no separate "flat types" set to check against.
R_MIN = 0.8                       # smallest credible dome radius (m)
R_FRAC_MAX = 0.95                 # radius cap as a fraction of the inradius
CLASS_STEP = 0.5                  # gpkg layer binning (m)
DOME_SINK = 0.35                  # sphere center sits this fraction of the
                                  # radius below the roof: the roof plane then
                                  # hides the lower bulge, so the ball reads
                                  # as a dome even on slope-sheared roofs


def load_typology(path):
    """Chapel # -> Type (int) from Sheet1; NaN kept as None."""
    df = pd.read_excel(path, sheet_name="Sheet1")
    out = {}
    for _, row in df.iterrows():
        t = row["Type"]
        out[int(row["Chapel #"])] = int(t) if pd.notna(t) else None
    return out


def _largest_poly(geom):
    """The largest sub-polygon of a (possibly multi-part) footprint. A
    handful of buildings are digitized as MultiPolygon (a detached
    outbuilding sharing one ID); the dome math below only ever cares
    about the main chamber, so it always operates on the biggest part."""
    return (max(geom.geoms, key=lambda g: g.area)
            if geom.geom_type == "MultiPolygon" else geom)


def inradius_point(geom):
    """(polylabel point, its clearance) — center of the largest inscribed
    circle and the distance to the boundary."""
    poly = _largest_poly(geom)
    c = polylabel(poly, tolerance=0.05)
    return c, poly.exterior.distance(c)


def clearance(geom, x, y):
    """Distance from (x, y) to the footprint wall (largest part's boundary)."""
    return _largest_poly(geom).exterior.distance(Point(x, y))


def roof_plane_z(geom, elevation, dem, x, y):
    """Rendered-roofline height at (x, y): least-squares plane through the
    footprint's vertex roof points (vertex ground + building height). A
    vertex-bound, terrain-clamped extrusion shears the roof with the terrain,
    so ground-at-center + height rides above the rendered roof wherever the
    center sits on a local high — anchoring to the vertex plane keeps dome
    spheres flush with the roof QGIS actually draws."""
    poly = _largest_poly(geom)
    verts = np.asarray(poly.exterior.coords)[:-1]
    vz = np.array([v[0] for v in dem.sample([(vx, vy) for vx, vy in verts])],
                  dtype=float)
    ok = np.isfinite(vz)
    if dem.nodata is not None:
        ok &= vz != dem.nodata
    if ok.sum() < 3:
        gz = float(next(dem.sample([(x, y)]))[0])
        return gz + elevation
    A = np.column_stack([verts[ok, 0], verts[ok, 1], np.ones(int(ok.sum()))])
    coef, *_ = np.linalg.lstsq(A, vz[ok] + elevation, rcond=None)
    return float(coef[0] * x + coef[1] * y + coef[2])


def detect_dome(ortho, transform, geom):
    """Largest bright blob inside the footprint. Returns (cx, cy, radius_m)
    or None. Bright = upper quartile of the footprint's own brightness (domes
    catch the sun; local threshold is robust to scene-wide contrast)."""
    b = geom.bounds
    try:
        win = from_bounds(b[0] - 1, b[1] - 1, b[2] + 1, b[3] + 1, transform)
        img = ortho.read(1, window=win)
        wt = ortho.window_transform(win)
    except (ValueError, rasterio.errors.RasterioIOError):
        return None
    if img.size < 25:
        return None
    inside = ~geometry_mask([geom], img.shape, wt, invert=False)
    vals = img[inside]
    if vals.size < 25 or not np.isfinite(vals).any():
        return None
    thresh = np.nanpercentile(vals, 75)
    bright = inside & (img >= thresh)
    labels, n = ndimage.label(bright)
    if n == 0:
        return None
    sizes = ndimage.sum(bright, labels, range(1, n + 1))
    biggest = int(np.argmax(sizes)) + 1
    px_area = float(sizes[biggest - 1])
    px = abs(wt.a)
    radius = math.sqrt(px_area * px * px / math.pi)   # equivalent circle
    if radius < R_MIN:
        return None
    ri, ci = ndimage.center_of_mass(labels == biggest)
    cx = wt.c + (ci + 0.5) * wt.a
    cy = wt.f + (ri + 0.5) * wt.e
    if not geom.contains(Point(cx, cy)):
        return None
    return cx, cy, radius


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--footprints", type=Path, default=FOOTPRINTS,
                   help="building footprint polygons (carries the ID and "
                        "Elevation/height fields)")
    p.add_argument("--xlsx", type=Path, default=XLSX,
                   help="excavation-report spreadsheet; source of the "
                        "chapel-typology Type column")
    p.add_argument("--ortho", type=Path, default=ORTHO,
                   help="grayscale orthophoto used to measure dome "
                        "center/radius by brightness")
    p.add_argument("--dem", type=Path, default=DEM_BASE_04,
                   help="bare-earth DEM used for the vertex roof-plane "
                        "height fit")
    p.add_argument("--out-dir", type=Path,
                   default=ROOT / "200_Projects/220_BuildingsToDEM",
                   help="where dome_inventory.csv / domes.gpkg / "
                        "dome_qc.png are written")
    p.add_argument("--from-inventory", type=Path, default=None,
                   help="skip detection; rebuild gpkg/QC from an edited "
                        "dome_inventory.csv")
    args = p.parse_args()

    print("=" * 70)
    print("DOME LAYER (typology + orthophoto)")
    print("=" * 70)
    fp = gpd.read_file(args.footprints)
    check(len(fp) > 0, "footprints load", f"{len(fp)} polygons")
    crs = fp.crs
    typology = load_typology(args.xlsx)
    check(len(typology) == len(fp), "one typology row per footprint",
          f"{len(typology)} vs {len(fp)}")

    ortho = rasterio.open(args.ortho)
    dem = rasterio.open(args.dem)
    check(str(ortho.crs) == str(crs), "orthophoto CRS matches footprints",
          f"{ortho.crs}")

    rows = []
    if args.from_inventory:
        inv = pd.read_csv(args.from_inventory)
        check({"ID", "has_dome", "cx", "cy", "radius_m", "roof_z"}.issubset(
            inv.columns), "inventory has required columns",
            ",".join(inv.columns))
        if failures:
            sys.exit(1)
        rows = inv.to_dict("records")
        print(f"  inventory: {len(rows)} rows from "
              f"{args.from_inventory.name} (detection skipped)")
    else:
        ratios = []
        detected, fallback = [], []
        for _, frow in fp.iterrows():
            bid = int(frow["ID"])
            geom = frow.geometry
            btype = typology.get(bid)
            base = {"ID": bid, "type": btype if btype is not None else "",
                    "has_dome": False, "source": "", "cx": np.nan,
                    "cy": np.nan, "radius_m": np.nan, "roof_z": np.nan,
                    "notes": ""}
            if btype is None:
                base["notes"] = "untyped - left untouched"
                rows.append(base)
                continue
            if btype not in DOMED_TYPES:
                base["notes"] = ("barrel-vaulted (flat this pass)"
                                 if btype == 10 else
                                 "composite (flat this pass)"
                                 if btype == 8 else "flat roof")
                rows.append(base)
                continue
            center, inr = inradius_point(geom)
            hit = detect_dome(ortho, ortho.transform, geom)
            if hit is not None:
                cx, cy, r = hit
                # clamp by the wall clearance at the *detected* center — the
                # polylabel inradius describes a different, wider point and
                # would let the sphere poke through the nearest wall
                r_wall = R_FRAC_MAX * clearance(geom, cx, cy)
                if r_wall < R_MIN:
                    hit = None       # blob hugs a wall; recenter below
                else:
                    r = min(r, r_wall)
                    base.update(source="ortho")
                    ratios.append(r / inr if inr > 0 else np.nan)
                    detected.append(bid)
            if hit is None:
                cx, cy = center.x, center.y
                r = np.nan                       # filled from median ratio
                base.update(source="fallback")
                fallback.append(bid)
            if pd.notna(frow["Elevation"]):
                elev = float(frow["Elevation"])
            else:
                warn("domed building missing Elevation; treated as 0 m",
                     f"ID {bid}")
                elev = 0.0
            base.update(has_dome=True, cx=cx, cy=cy, radius_m=r,
                        roof_z=roof_plane_z(geom, elev, dem, cx, cy))
            rows.append(base)
        med_ratio = float(np.nanmedian(ratios)) if ratios else 0.7
        for row in rows:
            if row["has_dome"] and not np.isfinite(row["radius_m"]):
                _, inr = inradius_point(
                    fp.loc[fp["ID"] == row["ID"]].geometry.iloc[0])
                # wall clearance caps the floor too: R_MIN must not push the
                # sphere through a narrow footprint's wall
                row["radius_m"] = min(max(R_MIN, med_ratio * inr),
                                      R_FRAC_MAX * inr)
        print(f"  domed chapels: {sum(r['has_dome'] for r in rows)} "
              f"(ortho-measured {len(detected)}, fallback {len(fallback)})")
        if ratios:
            q = np.nanpercentile(ratios, [25, 50, 75])
            print(f"  dome/inradius ratio: p25 {q[0]:.2f}  median {q[1]:.2f}"
                  f"  p75 {q[2]:.2f}  (fallback uses {med_ratio:.2f})")

    inv = pd.DataFrame(rows)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.out_dir / "dome_inventory.csv"
    if not args.from_inventory:
        inv.to_csv(csv_path, index=False)
        print(f"  wrote {csv_path.name}")

    domes = inv[inv["has_dome"] == True].copy()          # noqa: E712
    domes["size_class"] = (np.round(domes["radius_m"] / CLASS_STEP)
                           * CLASS_STEP).clip(lower=CLASS_STEP)
    gdf = gpd.GeoDataFrame(
        domes[["ID", "type", "source", "radius_m", "size_class", "roof_z"]],
        geometry=[Point(x, y, z - DOME_SINK * r) for x, y, z, r in
                  zip(domes.cx, domes.cy, domes.roof_z, domes.radius_m)],
        crs=crs)
    gpkg = args.out_dir / "domes.gpkg"
    gpkg.unlink(missing_ok=True)     # drop stale class layers from prior runs
    gdf.to_file(gpkg, layer="domes_all", driver="GPKG")
    classes = sorted(gdf["size_class"].unique())
    for cls in classes:
        sub = gdf[gdf["size_class"] == cls]
        name = f"domes_r{cls:.1f}".replace(".", "_")
        sub.to_file(gpkg, layer=name, driver="GPKG")
    print(f"  wrote {gpkg.name}: domes_all + {len(classes)} class layers "
          f"({', '.join(f'{c:g}m' for c in classes)})")

    # QC PNG over the orthophoto
    b = fp.total_bounds
    win = from_bounds(b[0] - 15, b[1] - 15, b[2] + 15, b[3] + 15,
                      ortho.transform)
    img = ortho.read(1, window=win)
    wt = ortho.window_transform(win)
    v1, v2 = np.nanpercentile(img, [2, 98])
    fig, ax = plt.subplots(figsize=(10, 16), dpi=200)
    ax.imshow(img, cmap="gray", vmin=v1, vmax=v2)

    def to_px(x, y):
        return (x - wt.c) / wt.a - 0.5, (y - wt.f) / wt.e - 0.5

    for geom in fp.geometry:
        polys = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
        for poly in polys:
            xs, ys = poly.exterior.xy
            px, py = to_px(np.asarray(xs), np.asarray(ys))
            ax.plot(px, py, color="cyan", linewidth=0.35)
    for _, row in gdf.iterrows():
        cx, cy = to_px(row.geometry.x, row.geometry.y)
        rad = row["radius_m"] / abs(wt.a)
        color = "yellow" if row["source"] == "ortho" else "red"
        ax.add_patch(plt.Circle((cx, cy), rad, fill=False,
                                color=color, linewidth=0.6))
    ax.set_title("Dome layer QC — yellow: ortho-measured, red: fallback")
    ax.axis("off")
    qc = args.out_dir / "dome_qc.png"
    fig.savefig(qc, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {qc.name}")

    print("\n" + "=" * 70)
    print("SELF-VERIFICATION")
    print("=" * 70)
    check(len(inv) == len(fp), "one inventory row per footprint",
          f"{len(inv)} rows")
    n_dome = int((inv["has_dome"] == True).sum())        # noqa: E712
    check(80 <= n_dome <= 140, "domed count in plausible range (~118)",
          f"{n_dome}")
    check(bool(np.isfinite(gdf["roof_z"]).all()), "roof_z finite for all domes")
    check(bool((gdf["radius_m"] > 0).all()), "all radii positive",
          f"min {gdf['radius_m'].min():.2f} m")
    geom_by_id = {int(r["ID"]): r.geometry for _, r in fp.iterrows()}
    clear = gdf.apply(lambda r: clearance(geom_by_id[int(r["ID"])],
                                          r.geometry.x, r.geometry.y), axis=1)
    n_over = int((gdf["radius_m"] > R_FRAC_MAX * clear + 1e-6).sum())
    check(n_over == 0, "no dome radius exceeds its own wall clearance",
          f"{n_over} overshoot" if n_over else
          f"max radius/clearance {float((gdf['radius_m'] / clear).max()):.2f}")
    joined = gpd.sjoin(gdf, fp[["ID", "geometry"]].rename(
        columns={"ID": "fp_id"}), predicate="within", how="left")
    check(bool((joined["fp_id"] == joined["ID"]).all()),
          "every dome center inside its own footprint")
    for bid, label in [(30, "Chapel of Exodus"), (80, "Chapel of Peace"),
                       (181, "circular structure")]:
        row = inv[inv["ID"] == bid]
        ok = bool(len(row)) and bool(row.iloc[0]["has_dome"])
        check(ok, f"#{bid} ({label}) is domed",
              f"type {row.iloc[0]['type']}" if len(row) else "missing")

    ortho.close()
    dem.close()
    print("\n" + "=" * 70)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    try:
        loc = args.out_dir.relative_to(ROOT)
    except ValueError:
        loc = args.out_dir
    print(f"Dome layer complete; outputs in {loc}")


if __name__ == "__main__":
    main()
