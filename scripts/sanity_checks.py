"""Sanity checks for the locally-pulled El Bagawat data subset.

Verifies that the rasters and vectors are mutually coherent before any
viewshed work: CRS (projected, metric, identical across assets), grid
alignment between the buildings DEM and the current base DEM, a plausible
building-height differential, and that the Marks_Brief2 viewpoints fall
inside the DEM extent.

Run:  .venv/bin/python scripts/sanity_checks.py
Exits nonzero if any check FAILs. WARNs don't fail the run.
"""

import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling
from rasterio.features import rasterize

ROOT = Path(__file__).resolve().parent.parent / "LAMP_DataStore" / "ElBagawat"

DEM_BUILDINGS = ROOT / "200_Projects/220_BuildingsToDEM/BuildingsToDEM.tif"
DEM_BASE_04 = ROOT / ("100_Data/150_DigitalElevationModel/Generated_DEMs/"
                      "Current_DEM/Bagawat-DEM-NewImageryOnly-0.4m-DEM.tif")
DEM_BASE_05 = ROOT / ("100_Data/150_DigitalElevationModel/Generated_DEMs/"
                      "Current_DEM/Bagawat-DEM-NewImageryOnly-0.5m-DEM.tif")
FOOTPRINTS = ROOT / ("100_Data/130_BuildingFootprintsVectorData/"
                     "BuildingTracesCurrent/Buildings_Mask.shp")
# newest regenerated 0.4m DEM-with-buildings, if built (see
# build_dem_with_buildings.py)
DEM_REGEN = max(
    (ROOT / "200_Projects/220_BuildingsToDEM").glob("DEMWithBuildings-0.4m-*.tif"),
    default=None)
VIEWPOINTS = ROOT / "100_Data/160_ViewpointMarks/Marks_Brief2.shp"
DOME_INVENTORY = ROOT / "200_Projects/220_BuildingsToDEM/dome_inventory.csv"
GPKG_DIR = ROOT / "200_Projects/220_BuildingsToDEM/Temp_Buildings_Explode/IndividualBuildings"

failures = []
warnings = []


def check(ok, label, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(f"{label}: {detail}")
    return ok


def warn(label, detail=""):
    print(f"  [WARN] {label}" + (f" — {detail}" if detail else ""))
    warnings.append(f"{label}: {detail}")


def describe_raster(path):
    with rasterio.open(path) as src:
        print(f"\n--- {path.relative_to(ROOT)}")
        print(f"  size: {src.width}x{src.height}  dtype: {src.dtypes[0]}  "
              f"nodata: {src.nodata}")
        print(f"  crs: {src.crs}")
        print(f"  res: {src.res}  bounds: {tuple(round(b, 1) for b in src.bounds)}")
        check(src.crs is not None and src.crs.is_projected,
              "CRS is projected/metric", str(src.crs))
        return {"crs": src.crs, "bounds": src.bounds, "res": src.res,
                "transform": src.transform, "shape": (src.height, src.width)}


def main():
    print("=" * 70)
    print("RASTERS")
    print("=" * 70)
    rasters = {}
    for name, path in [("buildings", DEM_BUILDINGS),
                       ("base04", DEM_BASE_04),
                       ("base05", DEM_BASE_05)]:
        if not check(path.exists(), f"{name} exists", str(path)):
            continue
        rasters[name] = describe_raster(path)

    print("\n" + "=" * 70)
    print("CROSS-RASTER CONSISTENCY")
    print("=" * 70)
    crss = {name: r["crs"] for name, r in rasters.items()}
    ref_crs = crss.get("base04")
    for name, crs in crss.items():
        check(crs == ref_crs, f"{name} CRS matches base04", str(crs))

    if DEM_REGEN is not None:
        print(f"\n--- {DEM_REGEN.relative_to(ROOT)} (regenerated)")
        with rasterio.open(DEM_REGEN) as rsrc, rasterio.open(DEM_BASE_04) as dsrc:
            check(rsrc.crs == dsrc.crs and
                  rsrc.transform == dsrc.transform and
                  rsrc.shape == dsrc.shape,
                  "regenerated DEM grid identical to base04")
            rd = rsrc.read(1) - dsrc.read(1)
            nz = rd[rd != 0]
            check(nz.size > 0 and 0.5 <= np.median(nz) <= 15,
                  "regenerated differential = plausible building heights",
                  f"median {np.median(nz):.2f} m over {nz.size:,} px")
    else:
        warn("no regenerated DEMWithBuildings-0.4m-*.tif found",
             "run scripts/build_dem_with_buildings.py")

    if "buildings" in rasters and "base04" in rasters:
        b, d = rasters["buildings"], rasters["base04"]
        same_res = np.allclose(b["res"], d["res"])
        if same_res and b["transform"].almost_equals(d["transform"]):
            print("  [INFO] BuildingsToDEM is on the same grid as the 0.4m DEM")
        else:
            warn("BuildingsToDEM grid differs from current 0.4m DEM",
                 f"res {b['res']} vs {d['res']} — it was built from an older/"
                 "coarser DEM; the differential below resamples to compare. "
                 "Regenerating the extruded DEM from Current_DEM is likely "
                 "needed (mentor question).")
        ovl_ok = not (b["bounds"].right < d["bounds"].left or
                      b["bounds"].left > d["bounds"].right or
                      b["bounds"].top < d["bounds"].bottom or
                      b["bounds"].bottom > d["bounds"].top)
        check(ovl_ok, "buildings DEM overlaps base04 extent")

    print("\n" + "=" * 70)
    print("HEIGHT DIFFERENTIAL (BuildingsToDEM − base DEM, on buildings grid)")
    print("=" * 70)
    with rasterio.open(DEM_BUILDINGS) as bsrc, rasterio.open(DEM_BASE_04) as dsrc:
        bld = bsrc.read(1, masked=True).filled(np.nan).astype("float64")
        # The two DEMs sit on different grids, so resample the base onto the
        # buildings grid before differencing — otherwise misaligned cells
        # would be subtracted from each other.
        base = np.full(bld.shape, np.nan)
        reproject(
            source=rasterio.band(dsrc, 1), destination=base,
            src_transform=dsrc.transform, src_crs=dsrc.crs,
            dst_transform=bsrc.transform, dst_crs=bsrc.crs,
            resampling=Resampling.bilinear, dst_nodata=np.nan)
        diff = bld - base
        valid = np.isfinite(diff)
        check(valid.any(), "differential has valid overlap pixels")

        gdf = gpd.read_file(FOOTPRINTS).to_crs(bsrc.crs)
        fp_mask = rasterize(
            ((geom, 1) for geom in gdf.geometry),
            out_shape=bld.shape, transform=bsrc.transform, fill=0,
            dtype="uint8").astype(bool)

        on = diff[valid & fp_mask]
        off = diff[valid & ~fp_mask]
        print(f"  on-footprint  ({on.size:>9,} px): "
              f"median {np.median(on):6.2f} m   p5 {np.percentile(on, 5):6.2f}   "
              f"p95 {np.percentile(on, 95):6.2f}")
        print(f"  off-footprint ({off.size:>9,} px): "
              f"median {np.median(off):6.2f} m   p5 {np.percentile(off, 5):6.2f}   "
              f"p95 {np.percentile(off, 95):6.2f}")
        offset = np.median(off)
        rel = np.median(on) - offset
        print(f"  vertical offset between rasters (off-footprint median): "
              f"{offset:.2f} m;  building signal (on − off): {rel:.2f} m")
        if abs(offset) > 0.5:
            warn("off-footprint differential not ~0",
                 f"median {offset:.2f} m — the two rasters use different "
                 "vertical references / source DEMs. Do NOT difference them "
                 "for building heights; regenerate the extruded DEM from "
                 "Current_DEM + footprint heights (mentor question)")
        check(0.5 <= rel <= 15,
              "offset-corrected on-footprint differential looks like "
              "building heights", f"{rel:.2f} m")

    print("\n" + "=" * 70)
    print("VECTORS")
    print("=" * 70)
    gdf = gpd.read_file(FOOTPRINTS)
    print(f"\n--- {FOOTPRINTS.relative_to(ROOT)}")
    print(f"  features: {len(gdf)}  crs: {gdf.crs}")
    print(f"  columns: {list(gdf.columns)}")
    check(gdf.crs == ref_crs, "footprints CRS matches rasters", str(gdf.crs))
    height_cols = [c for c in gdf.columns
                   if "height" in c.lower() or "elev" in c.lower()
                   or c.lower() in ("h", "z")]
    if height_cols:
        col = height_cols[0]
        vals = gdf[col].dropna()
        print(f"  height field '{col}': min {vals.min():.2f}  "
              f"median {vals.median():.2f}  max {vals.max():.2f}")
        check(0 < vals.median() < 15, f"footprint '{col}' values plausible (m)")
    else:
        warn("no height-like field found on footprints",
             "extrusion heights must come from elsewhere")

    vp = gpd.read_file(VIEWPOINTS)
    print(f"\n--- {VIEWPOINTS.relative_to(ROOT)}")
    print(f"  features: {len(vp)}  crs: {vp.crs}  geom: {vp.geom_type.unique()}")
    pts = vp.explode(index_parts=False).reset_index(drop=True)
    check(len(pts) == 3, "Marks_Brief2 has exactly 3 points (proposal sample)",
          f"found {len(pts)}")
    check(pts.crs is not None, "viewpoints have a defined CRS", str(pts.crs))
    if pts.crs != ref_crs:
        warn("viewpoints CRS differs from rasters",
             f"{pts.crs} vs {ref_crs} — fine; reproject with to_crs() on load")
    with rasterio.open(DEM_BASE_04) as dsrc:
        inside = [dsrc.bounds.left <= p.x <= dsrc.bounds.right and
                  dsrc.bounds.bottom <= p.y <= dsrc.bounds.top
                  for p in pts.to_crs(dsrc.crs).geometry]
        check(all(inside), "all viewpoints inside 0.4m DEM extent",
              f"{sum(inside)}/{len(inside)} inside")
        coords = [(p.x, p.y) for p in pts.to_crs(dsrc.crs).geometry]
        for (x, y), z in zip(coords, dsrc.sample(coords)):
            print(f"  viewpoint ({x:.1f}, {y:.1f}) -> ground elev {z[0]:.2f} m")

    gpkgs = sorted(GPKG_DIR.glob("ID_*.gpkg"))
    print(f"\n--- {GPKG_DIR.relative_to(ROOT)}")
    check(len(gpkgs) == 263, "263 per-building gpkg files", f"found {len(gpkgs)}")
    for sample in (gpkgs[0], gpkgs[len(gpkgs) // 2], gpkgs[-1]):
        g = gpd.read_file(sample)
        ok = g.crs == ref_crs and len(g) >= 1
        check(ok, f"{sample.name} loads, CRS matches",
              f"{len(g)} feature(s), {g.crs}")

    print("\n" + "=" * 70)
    if warnings:
        print(f"WARNINGS ({len(warnings)}):")
        for w in warnings:
            print(f"  - {w}")
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("All checks passed." + (" (with warnings above)" if warnings else ""))


if __name__ == "__main__":
    main()
