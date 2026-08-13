"""Generate a fresh GRASS r.viewshed baseline at native 0.4 m resolution.

Task_2's existing r.viewshed baseline (`viewshed_mark{1,2,3}_curr.tif`)
was produced by the user outside this repo, on their own 1.5 m subset,
with observer height / max-distance / curvature settings that were
never recorded (`comparison_report.md`'s "For mentors" section still
asks). Rather than guess those settings retroactively, this generates
an independent, fully-documented baseline at the project's own
canonical 0.4 m surface, swept over the same two eye heights used
everywhere else in this project (1.5 m default, 1.75 m GIS-default
sensitivity), so `compare_apertures.py` can compare like-for-like at
both resolutions.

GRASS itself is not on this machine and has no Homebrew formula; it is
installed as a one-off dev tool via conda-forge (`mamba create -n grass
-c conda-forge grass`), the same precedent as LibreDWG in PROGRESS.md
(2026-08-08) — not a pipeline dependency, just how this one artifact
gets made. This script shells out to `mamba run -n grass grass
--tmp-project ... --exec` rather than requiring a GRASS Python binding
in the project's own venv.

Deliberately mirrors the engine's own assumptions rather than
r.viewshed's raw defaults where they would otherwise diverge: no earth
curvature or refraction flags (`comparison_report.md` already notes
both are negligible over this ROI's ~100 m span, and HeightfieldScene
models neither), target_elevation=0 (ray endpoint at the target cell's
own ground/roof height, matching `compute_viewshed`'s target grid).
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import rasterio

from sanity_checks import check, failures
from viewshed import load_observers

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TASK2 = PROJECT_ROOT / "Task_2"
GRASS_ENV = "grass"


def run_grass(dem_path, obs_xy, heights, out_dir):
    """One tmp GRASS project per invocation, importing the DEM once and
    looping every (mark, height) r.viewshed inside it — cheaper than a
    fresh project per run and keeps the raster import a single step."""
    with rasterio.open(dem_path) as src:
        crs = src.crs
    lines = [
        "set -e",
        f'r.in.gdal input="{dem_path}" output=dem --overwrite',
        "g.region raster=dem",
    ]
    outputs = {}
    for h in heights:
        for i, (x, y) in enumerate(obs_xy, 1):
            name = f"vis_m{i}_h{h}"
            out_path = out_dir / f"viewshed_mark{i}_04m_h{h}.tif"
            lines += [
                f"r.viewshed input=dem output={name} "
                f"coordinates={x},{y} observer_elevation={h} "
                "target_elevation=0 --overwrite",
                f'r.out.gdal input={name} output="{out_path}" '
                "format=GTiff createopt=COMPRESS=LZW --overwrite",
            ]
            outputs[(i, h)] = out_path
    script = "\n".join(lines)
    cmd = ["mamba", "run", "-n", GRASS_ENV, "grass", "--tmp-project",
           str(crs), "--exec", "bash", "-c", script]
    print(f"  running GRASS for {len(heights)} height(s) x "
         f"{len(obs_xy)} observer(s)...")
    result = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True,
                            text=True)
    if result.returncode != 0:
        print(result.stdout[-4000:])
        print(result.stderr[-4000:])
    check(result.returncode == 0, "GRASS batch run exits cleanly",
         f"exit {result.returncode}")
    return outputs


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dem", type=Path,
                  default=TASK2 / "DEM_Subset-WithBuildings-04m.tif")
    p.add_argument("--observers", type=Path,
                  default=TASK2 / "Marks_Brief2.shp")
    p.add_argument("--eye-heights", type=float, nargs="+",
                  default=[1.5, 1.75])
    p.add_argument("--out-dir", type=Path, default=TASK2)
    return p


def main():
    args = build_parser().parse_args()
    check(shutil.which("mamba") is not None,
         "mamba is on PATH to reach the grass env", "")
    if failures:
        sys.exit(1)

    with rasterio.open(args.dem) as src:
        crs = src.crs
    obs_list, _ = load_observers(args.observers, crs)
    obs_xy = [(x, y) for _, x, y in obs_list]
    check(len(obs_xy) == 3, "3 observers", f"{len(obs_xy)}")
    for i, (x, y) in enumerate(obs_xy, 1):
        print(f"  observer {i} -> mark{i}: ({x:.1f}, {y:.1f})")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    outputs = run_grass(args.dem, obs_xy, args.eye_heights, args.out_dir)

    print("\nVALIDATING OUTPUTS")
    for (i, h), path in outputs.items():
        with rasterio.open(path) as src:
            arr = src.read(1)
        vis = np.isfinite(arr)
        check(0 < vis.sum() < vis.size,
             f"mark{i} h={h}: binarizes sanely",
             f"{int(vis.sum())}/{vis.size} visible")

    if failures:
        print(f"\n{len(failures)} check(s) failed")
        sys.exit(1)
    print(f"\nOK — baseline written to {args.out_dir}")


if __name__ == "__main__":
    main()
