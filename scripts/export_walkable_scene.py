"""Export a walkable Blender scene: bare ground plus the real chapels.

`export_scene_bundle.py` exports a heightmap for a *rendered still* — the
buildings are baked into the surface, so there are no interiors to enter.
This exports the scene you can walk through instead: the **bare-earth**
DEM as ground, with the aperture meshes standing on it as real geometry
with real doorways and light apertures.

Using the bare DEM is the whole trick. The canonical ray-casting surface
is the DEM *with buildings*, where each chapel is an extruded block of
solid heightfield — walk into one and you are inside rock. The aperture
meshes already model those chapels properly, so the ground underneath
them has to be the un-extruded earth or the two would occupy the same
space and every interior would be filled in.

Coordinates are the second trick. The site sits at UTM eastings around
254,000 and northings around 2,821,000, and Blender stores mesh
coordinates in single precision: at that magnitude the spacing between
representable floats is roughly a quarter of a metre, so a wall 0.17 m
thick would collapse into noise. Everything is therefore shifted to a
local origin, written to `scene.json` so the offset is recoverable and
nothing here is silently un-georeferenced.

Writes `terrain.obj` (cropped, decimated, local origin) and `scene.json`
listing the building meshes and spawn points; `blender/build_walkable_
scene.py` consumes both. Presentation tier — this is for looking and
walking, never for evidence.
"""

import argparse
import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.windows import from_bounds

from sanity_checks import (FOOTPRINTS, DEM_BASE_04, VIEWPOINTS, ROOT,
                           check, warn, failures)
from viewshed import load_observers
from aperture_registry import APERTURES_DIR, INVENTORY

EYE_HEIGHT = 1.5


def crop_dem(path, bounds, margin):
    """Bare DEM cropped to the footprints' extent plus a margin."""
    with rasterio.open(path) as src:
        x0, y0, x1, y1 = bounds
        win = from_bounds(x0 - margin, y0 - margin, x1 + margin,
                          y1 + margin, src.transform)
        arr = src.read(1, window=win)
        tr = src.window_transform(win)
        nod = src.nodata
    z = arr.astype(np.float64)
    if nod is not None:
        z = np.where(z == nod, np.nan, z)
    return z, tr


def write_terrain_obj(z, transform, origin, step, out):
    """Ground as a triangle grid, decimated by `step`, local origin.

    NaN cells (nodata at the crop edge) are filled with the surrounding
    median rather than dropped, so the ground stays a single closed
    sheet — a hole in the terrain is a place a walker falls through."""
    zz = z[::step, ::step]
    if np.isnan(zz).any():
        fill = float(np.nanmedian(zz))
        n_nan = int(np.isnan(zz).sum())
        zz = np.where(np.isnan(zz), fill, zz)
        warn("terrain nodata filled", f"{n_nan} cells -> {fill:.2f} m")
    rows, cols = zz.shape
    a, b, c, d, e, f = (transform.a, transform.b, transform.c,
                        transform.d, transform.e, transform.f)
    jj, ii = np.meshgrid(np.arange(cols), np.arange(rows))
    px, py = jj * step + 0.5, ii * step + 0.5
    x = a * px + b * py + c - origin[0]
    y = d * px + e * py + f - origin[1]
    zl = zz - origin[2]

    with open(out, "w") as fh:
        fh.write("# El Bagawat bare ground, local origin\n")
        for xr, yr, zr in zip(x, y, zl):
            for xv, yv, zv in zip(xr, yr, zr):
                fh.write(f"v {xv:.3f} {yv:.3f} {zv:.3f}\n")
        # Two triangles per cell, 1-based indices, wound counter-
        # clockwise seen from above so the ground faces up.
        for i in range(rows - 1):
            base = i * cols + 1
            nxt = base + cols
            for j in range(cols - 1):
                v0, v1 = base + j, base + j + 1
                v2, v3 = nxt + j, nxt + j + 1
                fh.write(f"f {v0} {v2} {v1}\n")
                fh.write(f"f {v1} {v2} {v3}\n")
    return rows * cols, 2 * (rows - 1) * (cols - 1)


def spawn_points(fp, reg_ids, origin, ground, n=6):
    """A few places worth standing, biased to chapels with openings."""
    out = []
    sub = fp[fp["ID"].astype(int).isin(reg_ids)]
    for _, r in sub.iterrows():
        c = r.geometry.centroid
        out.append((int(r["ID"]), c.x, c.y))
    out.sort(key=lambda t: t[0])
    picks, step = [], max(1, len(out) // n)
    for bid, x, y in out[::step][:n]:
        gz = ground(x, y)
        if not np.isfinite(gz):
            continue
        picks.append(dict(name=f"outside_chapel_{bid}",
                          x=round(x - origin[0], 3),
                          y=round(y - origin[1], 3),
                          z=round(gz - origin[2] + EYE_HEIGHT, 3)))
    return picks


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bare-dem", type=Path, default=DEM_BASE_04,
                   help="ground surface; must be the BARE earth, not "
                        "the DEM with buildings extruded into it")
    p.add_argument("--footprints", type=Path, default=FOOTPRINTS)
    p.add_argument("--observers", type=Path, default=VIEWPOINTS)
    p.add_argument("--registry", type=Path, default=INVENTORY)
    p.add_argument("--mesh-dir", type=Path,
                   default=APERTURES_DIR / "meshes_windows_fabric",
                   help="which built chapel meshes to place")
    p.add_argument("--margin", type=float, default=60.0,
                   help="ground margin (m) beyond the footprints")
    p.add_argument("--step", type=int, default=1,
                   help="terrain decimation; 1 = native 0.4 m")
    p.add_argument("--out-dir", type=Path,
                   default=ROOT / "200_Projects/260_WalkableScene")
    return p


def main():
    args = build_parser().parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    fp = gpd.read_file(args.footprints)
    fp["ID"] = fp["ID"].astype(int)
    bounds = fp.total_bounds
    check(args.bare_dem.exists(), "bare DEM exists", str(args.bare_dem))
    check(args.mesh_dir.exists(), "mesh directory exists",
          str(args.mesh_dir))
    if failures:
        sys.exit(1)

    objs = sorted(args.mesh_dir.glob("building_*.obj"),
                  key=lambda p: int(p.stem.split("_")[1]))
    check(bool(objs), "chapel meshes found", f"{len(objs)} in "
          f"{args.mesh_dir.name}")

    z, tr = crop_dem(args.bare_dem, bounds, args.margin)
    origin = (round(float(bounds[0]), 1), round(float(bounds[1]), 1),
              round(float(np.nanmin(z)), 1))
    print(f"\nlocal origin (UTM): {origin}")
    print(f"ground crop: {z.shape[1]} x {z.shape[0]} cells "
          f"({z.shape[1] * 0.4:.0f} x {z.shape[0] * 0.4:.0f} m)")

    terrain = args.out_dir / "terrain.obj"
    nv, nf = write_terrain_obj(z, tr, origin, args.step, terrain)
    print(f"terrain.obj: {nv:,} verts, {nf:,} tris "
          f"(step {args.step} = {0.4 * args.step:.1f} m)")

    src = rasterio.open(args.bare_dem)
    nodata = src.nodata

    def ground(x, y):
        """Bare-earth z, or NaN off the raster.

        The nodata sentinel here is -1e6, which is a perfectly finite
        float — testing the result with isfinite alone lets it through
        and puts whatever used it a kilometre underground."""
        v = float(next(src.sample([(float(x), float(y))]))[0])
        if nodata is not None and v == nodata:
            return float("nan")
        return v

    reg_ids = {int(p.stem.split("_")[1]) for p in objs}
    spawns = spawn_points(fp, reg_ids, origin, ground)

    obs = []
    if args.observers.exists():
        # Marks_Brief2 is lat/lon MultiPoint while the DEM and
        # footprints are UTM, so reuse the engine's own loader rather
        # than reading the file raw — subtracting a UTM origin from a
        # longitude produces coordinates that look plausible in a JSON
        # file and are wrong by the width of the planet.
        for oid, x, y in load_observers(args.observers, fp.crs)[0]:
            gz = ground(x, y)
            if np.isfinite(gz):
                obs.append(dict(name=f"mark_{oid}",
                                x=round(x - origin[0], 3),
                                y=round(y - origin[1], 3),
                                z=round(gz - origin[2] + EYE_HEIGHT, 3)))
            else:
                warn(f"observer {oid} is off the bare DEM", "skipped")

    meta = dict(
        crs=str(fp.crs), origin_utm=dict(x=origin[0], y=origin[1],
                                         z=origin[2]),
        note="Add origin_utm back to any coordinate to georeference it.",
        eye_height_m=EYE_HEIGHT,
        terrain="terrain.obj",
        terrain_step_m=round(0.4 * args.step, 2),
        mesh_dir=str(args.mesh_dir),
        buildings=[dict(id=int(p.stem.split("_")[1]), path=str(p))
                   for p in objs],
        spawns=spawns, observers=obs)
    (args.out_dir / "scene.json").write_text(json.dumps(meta, indent=2))

    print(f"buildings: {len(objs)} chapel meshes referenced")
    print(f"spawns: {len(spawns)}, observer marks: {len(obs)}")
    print(f"\nwrote {terrain.name} and scene.json to {args.out_dir}")
    print("next: blender --python blender/build_walkable_scene.py")
    if failures:
        print(f"\n{len(failures)} check(s) failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
