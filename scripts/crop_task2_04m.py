"""Crop the canonical site-wide 0.4 m rasters to the Task_2 ROI.

`compare_apertures.py` runs on Task_2's own 1.5 m subset
(`DEM_Subset-WithBuildings.tif`), which is on an older datum and was
never generated at any other resolution — there is no higher-res
version of that particular raster to crop. The canonical 0.4 m
ray-casting surface (`DEMWithBuildings-0.4m-*.tif`) and its bare-earth
counterpart (`Bagawat-DEM-NewImageryOnly-0.4m-DEM.tif`) are a
*different* raster pair, generated together and pixel-identical to
each other (verified: same shape/transform/CRS). This script crops
that pair, plus the matching orthophoto, to the same real-world extent
as the Task_2 ROI, so the 3D-vs-baseline comparison can be rerun at
native resolution without the resampling or datum mismatch a naive
downsample of the 1.5 m file would introduce.

Output lands in Task_2/ itself, named like the originals with a
-04m suffix, so compare_apertures.py can point --dem/--bare-dem/--ortho
at them directly.
"""

import argparse
from pathlib import Path

import rasterio
from rasterio.windows import from_bounds

from sanity_checks import check, failures

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TASK2 = PROJECT_ROOT / "Task_2"
DATASTORE = PROJECT_ROOT / "LAMP_DataStore/ElBagawat"
DEM_04M = (DATASTORE / "200_Projects/220_BuildingsToDEM"
           / "DEMWithBuildings-0.4m-20260612.tif")
BARE_04M = (DATASTORE / "100_Data/150_DigitalElevationModel"
            / "Generated_DEMs/Current_DEM"
            / "Bagawat-DEM-NewImageryOnly-0.4m-DEM.tif")
ORTHO_04M = (DATASTORE / "100_Data/150_DigitalElevationModel"
             / "Generated_DEMs/Current_DEM"
             / "Bagawat-DEM-NewImageryOnly-0.4m-ORTHOPHOTO.tif")


def crop_to(src_path, roi_bounds, out_path):
    """Crop a raster to a bounding box, returning (shape, transform).

    Window offsets and lengths are rounded to whole pixels before the
    read. Without that the crops of the two DEMs and the orthophoto
    can land on fractionally different grids, and every later
    comparison then silently resamples one against the other."""
    with rasterio.open(src_path) as src:
        win = from_bounds(*roi_bounds, transform=src.transform).round_lengths(
            pixel_precision=0).round_offsets(pixel_precision=0)
        transform = src.window_transform(win)
        data = src.read(window=win)
        profile = src.profile.copy()
        profile.update(height=data.shape[1], width=data.shape[2],
                       transform=transform)
        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(data)
        return data.shape[1:], transform


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--roi-dem", type=Path,
                   default=TASK2 / "DEM_Subset-WithBuildings.tif",
                   help="raster whose bounds define the ROI to crop to "
                        "(horizontal extent only — its own, older, "
                        "vertical datum is irrelevant here)")
    p.add_argument("--out-dir", type=Path, default=TASK2)
    return p


def main():
    args = build_parser().parse_args()
    with rasterio.open(args.roi_dem) as roi:
        roi_crs = roi.crs
        roi_bounds = roi.bounds
    print(f"ROI bounds ({roi_crs}): {tuple(round(v, 1) for v in roi_bounds)}")

    outputs = [
        (DEM_04M, args.out_dir / "DEM_Subset-WithBuildings-04m.tif"),
        (BARE_04M, args.out_dir / "DEM_Subset-Original-04m.tif"),
        (ORTHO_04M, args.out_dir / "OrthoImage_Subset-04m.tif"),
    ]
    shapes = []
    for src_path, out_path in outputs:
        with rasterio.open(src_path) as src:
            check(src.crs == roi_crs, f"{src_path.name} CRS matches ROI",
                  f"{src.crs} vs {roi_crs}")
        shape, transform = crop_to(src_path, roi_bounds, out_path)
        shapes.append(shape)
        print(f"  wrote {out_path.relative_to(PROJECT_ROOT)}  "
             f"{shape[0]}x{shape[1]} @ {transform.a} m")

    check(len(set(shapes)) == 1, "all three crops share one grid",
          f"shapes: {shapes}")
    with rasterio.open(outputs[0][1]) as a, rasterio.open(outputs[1][1]) as b:
        check(a.transform.almost_equals(b.transform, precision=1e-6),
              "DEM/bare-DEM crops share the exact transform",
              f"{a.transform} vs {b.transform}")

    if failures:
        print(f"\n{len(failures)} check(s) failed")
        raise SystemExit(1)
    print("OK")


if __name__ == "__main__":
    main()
