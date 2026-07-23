"""Promote a saved 3D-visibility-volume CSV to other formats.

The viewshed engine writes the volume as a lightweight CSV of visible voxel
centers (x, y, z [, count]). This converts that CSV — without recomputing the
ray-casting — into:
  - a PLY point cloud (always reliable; opens in CloudCompare/QGIS/MeshLab),
  - a LAS/LAZ point cloud for QGIS point-cloud layers (height-ramp coloring)
    and CloudCompare (needs laspy, plus lazrs for .laz),
  - a dense voxel-occupancy NumPy grid (.npy) on a regular absolute grid
    inferred from the point spacing, plus a JSON sidecar,
  - per-z-slice GeoTIFFs for QGIS (occupancy at each elevation band), and/or
  - the volume's boundary surface as a PLY triangle mesh (--to mesh, styles
    via --mesh-style). Caveat: the CSV's z is absolute but re-binned at
    --zstep here, so this mesh is a staircase approximation; the faithful
    terrain-following shape comes from viewshed.py --volume-format mesh.

Horizontal spacing (--voxel) is inferred from the point lattice unless
given explicitly; vertical spacing (--zstep) is never inferred (the
CSV's z values are absolute elevations, not a clean lattice) — it
always falls back to a fixed 1 m bin unless given explicitly.

Run:
    .venv/bin/python scripts/volume_convert.py viewshed_volume_id7.csv --to ply
    .venv/bin/python scripts/volume_convert.py viewshed_volume_id7.csv \\
        --to laz --crs EPSG:32636
    .venv/bin/python scripts/volume_convert.py viewshed_volume_id7.csv \\
        --to npy tif --voxel 2 --zstep 2 --crs EPSG:32636

Exits nonzero if any self-check fails.
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import rasterio
from rasterio.transform import from_origin

from sanity_checks import check, warn, failures
# volume_mesh is torch-free by design, so importing it here keeps this
# converter's startup light (unlike importing viewshed.py would). It also
# now supplies the plain PLY/LAS point-cloud writers: they used to be
# duplicated byte-for-byte between this file and viewshed.py, which had
# quietly drifted apart (this file was missing an empty-point-set guard
# viewshed.py's copy had) — one shared copy means one place to fix.
from volume_mesh import (blocky_mesh, smooth_mesh, index_to_world,
                         run_mesh_checks, write_mesh_ply, write_ply,
                         write_las)


def infer_step(values, given):
    """Smallest positive spacing between sorted unique coords, or `given`."""
    if given is not None:
        return float(given)
    u = np.unique(values)
    if len(u) < 2:
        return 1.0
    diffs = np.diff(u)
    return float(diffs[diffs > 0].min())


Z_BIN = 1.0          # default absolute-z bin (m) when --zstep is not given
# Cap the dense grid so a fine --voxel over a large extent fails cleanly instead
# of trying to allocate multiple GB and OOM-ing the machine.
MAX_CELLS = 80_000_000


def voxelize(df, voxel, zstep):
    """Snap scattered points onto a regular *absolute* grid. Returns
    (grid[rows, cols, zslices], meta). Rows index descending y (north->south),
    cols ascending x, slices ascending z. Cell value = count if present else 1.

    x/y sit on a clean voxel lattice so their spacing is inferred; z in the CSV
    is absolute elevation (voxels are layered above each column's local ground),
    so it is *binned* at `zstep` (default `Z_BIN`) rather than inferred."""
    x, y, z = df["x"].to_numpy(), df["y"].to_numpy(), df["z"].to_numpy()
    dx = infer_step(x, voxel)
    dy = infer_step(y, voxel)
    dz = float(zstep) if zstep is not None else Z_BIN
    x0, y0, z0 = x.min(), y.max(), z.min()        # top-left, lowest slice
    cols = np.round((x - x0) / dx).astype(int)
    rows = np.round((y0 - y) / dy).astype(int)
    slc = np.round((z - z0) / dz).astype(int)
    shape = (int(rows.max()) + 1, int(cols.max()) + 1, int(slc.max()) + 1)
    ncells = shape[0] * shape[1] * shape[2]
    if not check(ncells <= MAX_CELLS, "voxel grid within size limit",
                 f"{ncells:,} cells (limit {MAX_CELLS:,}; raise --voxel/--zstep)"):
        return None, None
    grid = np.zeros(shape, dtype=np.int32)
    vals = df["count"].to_numpy() if "count" in df.columns else np.ones(len(df))
    grid[rows, cols, slc] = vals
    meta = {"x0": float(x0), "y0": float(y0), "z0": float(z0),
            "dx": dx, "dy": dy, "dz": dz,
            "axes": "grid[row, col, slice]; row->y(desc), col->x, slice->z(asc)"}
    return grid, meta


def write_tif_slices(grid, meta, crs, stem):
    transform = from_origin(meta["x0"] - meta["dx"] / 2,
                            meta["y0"] + meta["dy"] / 2, meta["dx"], meta["dy"])
    nz = grid.shape[2]
    for k in range(nz):
        z = meta["z0"] + k * meta["dz"]
        path = f"{stem}_z{z:g}.tif"
        with rasterio.open(
                path, "w", driver="GTiff", height=grid.shape[0],
                width=grid.shape[1], count=1, dtype="int32", nodata=0,
                crs=crs, transform=transform, compress="lzw") as dst:
            dst.write(grid[:, :, k].astype(np.int32), 1)
    return nz


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("csv", type=Path, help="volume CSV from viewshed.py")
    p.add_argument("--to", nargs="+", required=True,
                   choices=["ply", "npy", "tif", "las", "laz", "mesh"],
                   help="target format(s); las/laz need laspy (+lazrs for "
                        "laz); mesh = boundary-surface PLY triangle mesh "
                        "(staircase approximation — see the docstring)")
    p.add_argument("--mesh-style", choices=["blocky", "smooth"],
                   default="blocky",
                   help="mesh style (with --to mesh): blocky = exact voxel "
                        "boundary; smooth = marching-cubes isosurface "
                        "(needs scikit-image)")
    p.add_argument("--out-stem", type=Path,
                   help="output path stem (default: input stem)")
    p.add_argument("--voxel", type=float, default=None,
                   help="horizontal spacing (m); inferred from data if omitted")
    p.add_argument("--zstep", type=float, default=None,
                   help=f"vertical bin size (m); unlike --voxel this is "
                        f"NOT inferred from the data (the CSV's z values "
                        f"aren't on a clean lattice) — defaults to "
                        f"{Z_BIN:g} m")
    p.add_argument("--crs", default=None,
                   help="CRS for npy sidecar / GeoTIFF / LAS (e.g. EPSG:32636)")
    args = p.parse_args()

    check(args.csv.exists(), "input CSV exists", str(args.csv))
    if failures:
        sys.exit(1)
    df = pd.read_csv(args.csv)
    check({"x", "y", "z"}.issubset(df.columns), "CSV has x,y,z columns",
          ",".join(df.columns))
    if failures:
        sys.exit(1)
    print(f"  {len(df):,} points read from {args.csv.name}")

    stem = args.out_stem or args.csv.with_suffix("")
    pts = df[["x", "y", "z"]].to_numpy()
    count = df["count"].to_numpy() if "count" in df.columns else None

    if "ply" in args.to:
        out = Path(f"{stem}.ply")
        write_ply(out, pts, count)
        print(f"  wrote {out.name}")
    for fmt in ("las", "laz"):
        if fmt in args.to:
            if args.crs is None:
                warn("no --crs", f".{fmt} written without a CRS")
            out = Path(f"{stem}.{fmt}")
            write_las(out, pts, args.crs, count)
            if not failures:
                print(f"  wrote {out.name}")
    if {"npy", "tif", "mesh"} & set(args.to):
        grid, meta = voxelize(df, args.voxel, args.zstep)
        if grid is None:
            print(f"FAILURES ({len(failures)}): " + "; ".join(failures))
            sys.exit(1)
        meta["crs"] = str(args.crs)
        print(f"  voxel grid {grid.shape} (rows, cols, z-slices)  "
              f"dx={meta['dx']:g} dy={meta['dy']:g} dz={meta['dz']:g}")
        if "npy" in args.to:
            out = Path(f"{stem}.npy")
            np.save(out, grid)
            with open(out.with_suffix(".json"), "w") as f:
                json.dump(meta, f, indent=2)
            print(f"  wrote {out.name} (+ .json)")
        if "tif" in args.to:
            if args.crs is None:
                warn("no --crs", "GeoTIFF slices written without a CRS")
            nz = write_tif_slices(grid, meta, args.crs, str(stem))
            print(f"  wrote {nz} GeoTIFF slice(s) {stem}_z*.tif")
        if "mesh" in args.to:
            occ = grid > 0
            nvis = int(occ.sum())
            verts, faces = (blocky_mesh(occ)
                            if args.mesh_style == "blocky"
                            else smooth_mesh(occ))
            if verts is not None:
                # The grid is already absolute-z (no ground warp), but z
                # was re-binned by voxelize — the mesh is a staircase
                # approximation of the terrain-following shape.
                verts, faces = index_to_world(
                    verts, faces, meta["x0"], meta["dx"],
                    meta["y0"], -meta["dy"], meta["z0"], meta["dz"])
                nr, nc, nl = grid.shape
                bounds = (meta["x0"],
                          meta["x0"] + (nc - 1) * meta["dx"],
                          meta["y0"] - (nr - 1) * meta["dy"], meta["y0"],
                          meta["z0"],
                          meta["z0"] + (nl - 1) * meta["dz"])
                vol = run_mesh_checks(
                    verts, faces, nvis,
                    meta["dx"] * meta["dy"] * meta["dz"], bounds,
                    (meta["dx"], meta["dy"], meta["dz"]), args.mesh_style)
                out = Path(f"{stem}_mesh.ply")
                write_mesh_ply(out, verts, faces,
                               comment=f"style={args.mesh_style} "
                                       f"dx={meta['dx']:g} "
                                       f"dz={meta['dz']:g} nvis={nvis} "
                                       f"crs={args.crs}")
                print(f"  wrote {out.name} ({len(verts):,} verts, "
                      f"{len(faces):,} faces, {vol:,.1f} m^3)")

    if failures:
        print(f"FAILURES ({len(failures)}): " + "; ".join(failures))
        sys.exit(1)
    print("Conversion complete.")


if __name__ == "__main__":
    main()
