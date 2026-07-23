"""Regenerate the building-extruded DEM at the current base-DEM resolution.

Implements the recipe documented in 220_BuildingsToDEM/README.md against the
*current* 0.4 m DEM (the legacy BuildingsToDEM.tif is 1.5 m with a ~78 m
vertical-reference offset and must not be used for heights):

  1. rasterize footprint heights (Buildings_Mask 'Elevation' field) onto the
     base-DEM grid, 0 outside footprints
  2. add to the base DEM (nodata preserved, never invented)
  3. self-verify the written file and emit QC images for visual audit

Run:  .venv/bin/python scripts/build_dem_with_buildings.py
Exits nonzero if validation or self-verification fails.
"""

import argparse
import sys
from datetime import date
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from rasterio.features import rasterize
from shapely import make_valid

from sanity_checks import ROOT, DEM_BASE_04, FOOTPRINTS, check, warn, failures


def hillshade(z, res, azimuth=315.0, altitude=45.0):
    """Plain NumPy hillshade (avoids a GDAL dependency)."""
    gy, gx = np.gradient(z, res)
    slope = np.pi / 2 - np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    # Compass azimuth (CW from north) -> math angle (CCW from east) for trig.
    az = np.deg2rad(360.0 - azimuth + 90.0)
    alt = np.deg2rad(altitude)
    shaded = (np.sin(alt) * np.sin(slope) +
              np.cos(alt) * np.cos(slope) * np.cos(az - aspect))
    return np.clip(shaded, 0, 1)


def core_window(total_bounds, transform, margin=60.0):
    """Pixel window + crop slice for the necropolis core (footprint bounds +
    margin), clipped to a non-negative origin. Returns (window, crop_slice)."""
    l, b, r, t = total_bounds
    win = rasterio.windows.from_bounds(
        l - margin, b - margin, r + margin, t + margin, transform)
    rs, cs = max(int(win.row_off), 0), max(int(win.col_off), 0)
    crop = np.s_[rs:rs + int(win.height), cs:cs + int(win.width)]
    return win, crop


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-dem", type=Path, default=DEM_BASE_04,
                   help="bare-earth DEM to extrude buildings onto")
    p.add_argument("--footprints", type=Path, default=FOOTPRINTS,
                   help="footprint polygons carrying the height field")
    p.add_argument("--height-field", default="Elevation",
                   help="footprint column holding building height (m)")
    p.add_argument("--out", type=Path,
                   default=ROOT / "200_Projects/220_BuildingsToDEM" /
                   f"DEMWithBuildings-0.4m-{date.today():%Y%m%d}.tif",
                   help="output raster path (default: date-stamped in "
                        "220_BuildingsToDEM/)")
    p.add_argument("--all-touched", action="store_true",
                   help="burn every pixel touched by a footprint (dilates "
                        "thin features by up to one pixel)")
    args = p.parse_args()

    print("=" * 70)
    print("INPUT VALIDATION")
    print("=" * 70)
    gdf = gpd.read_file(args.footprints)
    with rasterio.open(args.base_dem) as src:
        profile = src.profile
        base = src.read(1)
        transform, crs, res = src.transform, src.crs, src.res[0]
        nodata = src.nodata

    check(crs is not None and crs.is_projected, "base DEM CRS projected", str(crs))
    check(gdf.crs == crs, "footprints CRS matches base DEM",
          f"{gdf.crs} vs {crs}")
    if not check(args.height_field in gdf.columns,
                 f"height field '{args.height_field}' present",
                 f"columns: {list(gdf.columns)}"):
        sys.exit(1)

    h = gdf[args.height_field]
    bad = gdf[h.isna() | (h <= 0)]
    if len(bad):
        warn("features with null/zero/negative height skipped",
             f"IDs {bad['ID'].tolist()}")
        gdf = gdf.drop(bad.index)
    print(f"  features: {len(gdf)}  height range "
          f"{gdf[args.height_field].min():.2f}–{gdf[args.height_field].max():.2f} m")
    if "Type" in gdf.columns:
        print(f"  Type counts: {gdf['Type'].value_counts(dropna=False).to_dict()}")

    n_invalid = int((~gdf.geometry.is_valid).sum())
    if n_invalid:
        warn("invalid geometries repaired with make_valid", f"{n_invalid} features")
        gdf.geometry = gdf.geometry.apply(make_valid)

    print("\n" + "=" * 70)
    print("RASTERIZE + ADD")
    print("=" * 70)
    # ascending height so the taller building wins where footprints overlap
    # (rasterize burns in order; last wins)
    gdf = gdf.sort_values(args.height_field)
    heights = rasterize(
        zip(gdf.geometry, gdf[args.height_field].astype("float32")),
        out_shape=base.shape, transform=transform, fill=0.0,
        all_touched=args.all_touched, dtype="float32")

    # per-feature coverage audit: every footprint must survive rasterization
    id_grid = rasterize(
        zip(gdf.geometry, gdf["ID"].astype("int32")),
        out_shape=base.shape, transform=transform, fill=0,
        all_touched=args.all_touched, dtype="int32")
    burned_ids = set(np.unique(id_grid)) - {0}
    missing = sorted(set(gdf["ID"].astype(int)) - burned_ids)
    check(not missing, "every footprint covers >=1 pixel",
          f"missing IDs {missing} — consider --all-touched" if missing else
          f"{len(burned_ids)} features burned")

    fp_mask = heights > 0
    valid = base != nodata if nodata is not None else np.ones_like(base, bool)
    n_nodata_under = int((fp_mask & ~valid).sum())
    if n_nodata_under:
        warn("footprint pixels over nodata terrain left as nodata",
             f"{n_nodata_under} px")

    out = base.copy()
    # Extrude only on real terrain; adding height over nodata would invent
    # ground the ray-caster could then "see".
    out[fp_mask & valid] += heights[fp_mask & valid]
    print(f"  footprint pixels: {int(fp_mask.sum()):,} "
          f"({100 * fp_mask.mean():.2f}% of grid)")

    # tiled=True: downstream scripts (viewshed.py's core-window crop,
    # build_dome_layer.py's per-footprint ortho window reads) only read
    # small windows out of this raster, and GeoTIFF can only do that
    # efficiently against internal tiles, not a single strip. lzw is a
    # lossless, no-downside compression for a float32 elevation grid.
    profile.update(dtype="float32", compress="lzw", tiled=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(args.out, "w", **profile) as dst:
        dst.write(out.astype("float32"), 1)
    print(f"  wrote {args.out.relative_to(ROOT)}")

    print("\n" + "=" * 70)
    print("SELF-VERIFICATION (re-read written file)")
    print("=" * 70)
    with rasterio.open(args.out) as src:
        written = src.read(1)
        check(src.transform == transform and src.crs == crs and
              written.shape == base.shape,
              "output grid identical to base DEM")
    diff = written - base
    off = diff[~fp_mask & valid]
    on_err = np.abs(diff[fp_mask & valid] - heights[fp_mask & valid])
    check(float(np.abs(off).max()) == 0.0,
          "off-footprint pixels unchanged", f"max |diff| {np.abs(off).max()}")
    check(float(on_err.max()) < 1e-3,
          "on-footprint pixels = base + height",
          f"max error {on_err.max():.2e} m")
    nod_in = int((written[~valid] != nodata).sum()) if nodata is not None else 0
    check(nod_in == 0, "nodata preserved")

    print("\n" + "=" * 70)
    print("QC IMAGES")
    print("=" * 70)
    # crop to the necropolis core (footprint bounds + margin) so buildings
    # are actually visible at PNG resolution
    _, crop = core_window(gdf.total_bounds, transform, margin=60)

    hs = hillshade(written[crop], res)
    fig, ax = plt.subplots(figsize=(10, 14), dpi=200)
    ax.imshow(hs, cmap="gray")
    ax.set_title(f"Hillshade — {args.out.name} (necropolis core)")
    ax.axis("off")
    hs_path = args.out.with_name(args.out.stem + "_hillshade.png")
    fig.savefig(hs_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {hs_path.relative_to(ROOT)}")

    fig, ax = plt.subplots(figsize=(10, 14), dpi=200)
    im = ax.imshow(np.where(fp_mask, diff, np.nan)[crop], cmap="viridis",
                   vmin=0, vmax=float(gdf[args.height_field].max()))
    fig.colorbar(im, ax=ax, shrink=0.5, label="extruded height (m)")
    ax.set_title("Differential (output − base) on footprints")
    ax.axis("off")
    diff_path = args.out.with_name(args.out.stem + "_diff.png")
    fig.savefig(diff_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {diff_path.relative_to(ROOT)}")

    print("\n" + "=" * 70)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("Build complete; all self-checks passed.")


if __name__ == "__main__":
    main()
