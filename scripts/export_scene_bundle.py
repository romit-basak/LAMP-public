"""Scene-bundle exporter for external renderers (Blender and Unity).

Writes a self-contained folder that carries everything an external 3D
tool needs to rebuild the site — heightfield, orthophoto texture,
observer cameras and dome geometry — in a *local* coordinate frame:
UTM eastings/northings (~1e6 m) overflow the float32 both engines use
internally, so every coordinate is shifted by a whole-meter origin
recorded in meta.json (local = UTM - origin).

Bundle contents:
    heightmap.npy   float32 [H, W] elevations, nodata filled (see meta)
    ortho.png       8-bit grayscale drape texture on the same grid
    meta.json       origin, grid geometry, axes/row-order notes
    observers.json  eye positions (local + UTM), ground/eye z
    domes.json      dome centers (local), springing z, radius
    heightmap_unity.raw   optional --unity 16-bit RAW for Unity Terrain

Domes are exported as geometry, never baked into the heightmap — the
consumer adds spheres (blender/build_bagawat_scene.py --domes), so a
bundle can't double-dome.

Run (necropolis core + 60 m, the default):
    .venv/bin/python scripts/export_scene_bundle.py
Whole DEM, with the Unity terrain heightmap:
    .venv/bin/python scripts/export_scene_bundle.py --full --unity

Consumers: blender/build_bagawat_scene.py (docs/BLENDER.md) and the
Unity Terrain import walkthrough (docs/UNITY.md).

Exits nonzero if any self-check fails.
"""

import argparse
import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from PIL import Image
from rasterio.windows import Window
from scipy.ndimage import map_coordinates

from sanity_checks import (ROOT, DEM_REGEN, FOOTPRINTS, VIEWPOINTS,
                           DOME_INVENTORY, check, warn, failures)
from viewshed import load_dem, load_observers
from build_dem_with_buildings import core_window
from build_dome_layer import ORTHO


def bilinear(dem, transform, nodata, x, y):
    """Cell-center bilinear sample of the DEM at world points; NaN
    outside or on nodata (HeightfieldScene.surface_z without needing a
    torch scene just for the export)."""
    x = np.atleast_1d(np.asarray(x, dtype=np.float64))
    y = np.atleast_1d(np.asarray(y, dtype=np.float64))
    h, w = dem.shape
    col_f = (x - transform.c) / transform.a - 0.5
    row_f = (y - transform.f) / transform.e - 0.5
    c0 = np.floor(col_f).astype(int)
    r0 = np.floor(row_f).astype(int)
    wc, wr = col_f - c0, row_f - r0
    inb = (c0 >= 0) & (c0 < w - 1) & (r0 >= 0) & (r0 < h - 1)
    c0c, r0c = np.clip(c0, 0, w - 2), np.clip(r0, 0, h - 2)
    q = np.stack([dem[r0c, c0c], dem[r0c, c0c + 1],
                  dem[r0c + 1, c0c], dem[r0c + 1, c0c + 1]])
    valid = inb
    if nodata is not None:
        valid &= ~np.any(q == nodata, axis=0)
    top = q[0] * (1 - wc) + q[1] * wc
    bot = q[2] * (1 - wc) + q[3] * wc
    return np.where(valid, top * (1 - wr) + bot * wr, np.nan)


def export_ortho(ortho_path, dem_shape, dem_transform, cs, rs, w, h, out):
    """Percentile-stretched 8-bit grayscale drape texture for the same
    window. Skipped (with a warn) when the orthophoto is missing or not
    on the DEM grid — the bundle stays usable via the height ramp."""
    if not ortho_path.exists():
        warn("orthophoto not found; bundle has no drape texture",
             str(ortho_path))
        return False
    with rasterio.open(ortho_path) as src:
        same = ((src.height, src.width) == dem_shape
                and np.allclose(tuple(src.transform)[:6],
                                tuple(dem_transform)[:6]))
        if not same:
            warn("orthophoto grid differs from DEM; drape skipped",
                 str(ortho_path))
            return False
        o = src.read(1, window=Window(cs, rs, w, h)).astype(np.float32)
        ov = o[o != src.nodata] if src.nodata is not None else o
    lo, hi = np.percentile(ov, [2, 98])
    o8 = (np.clip((o - lo) / max(hi - lo, 1e-6), 0, 1) * 255).astype(np.uint8)
    Image.fromarray(o8, mode="L").save(out)
    return True


def export_unity_raw(hm, z_lo, z_hi, res, out):
    """Unity Terrain heightmap: square (2^n)+1 16-bit little-endian RAW,
    normalized to the bundle's z range. Written south-up (row 0 =
    south) because Unity terrain sample [0, 0] sits at the terrain's
    local origin corner; the import dialog's Flip Vertically toggle
    covers the disagreeing case (see docs/UNITY.md)."""
    h, w = hm.shape
    rows = np.linspace(0, h - 1, res)
    cols = np.linspace(0, w - 1, res)
    rr, cc = np.meshgrid(rows, cols, indexing="ij")
    hs = map_coordinates(hm, [rr, cc], order=1, mode="nearest")
    norm = (hs - z_lo) / max(z_hi - z_lo, 1e-6)
    u16 = np.flipud((np.clip(norm, 0, 1) * 65535).astype("<u2"))
    out.write_bytes(u16.tobytes())
    return u16.nbytes


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dem", type=Path, default=DEM_REGEN)
    p.add_argument("--footprints", type=Path, default=FOOTPRINTS)
    p.add_argument("--observers", type=Path, default=VIEWPOINTS)
    p.add_argument("--ids", type=int, nargs="+",
                   help="select observers from --observers by their id "
                        "field; omit to export every point")
    p.add_argument("--eye-height", type=float, default=1.5,
                   help="camera eye height (m) above surface (default 1.5)")
    p.add_argument("--dome-inventory", type=Path, default=DOME_INVENTORY)
    p.add_argument("--margin", type=float, default=60.0,
                   help="window margin (m) around the footprint bounds")
    p.add_argument("--full", action="store_true",
                   help="export the whole DEM instead of the core window")
    p.add_argument("--unity", action="store_true",
                   help="also write a 16-bit RAW heightmap for Unity Terrain")
    p.add_argument("--unity-res", type=int, default=1025,
                   choices=[513, 1025, 2049, 4097],
                   help="Unity heightmap resolution, (2^n)+1 (default 1025)")
    p.add_argument("--out-dir", type=Path,
                   default=ROOT / "200_Projects/220_BuildingsToDEM/scene_bundle")
    args = p.parse_args()

    print("=" * 70)
    print(f"SCENE BUNDLE   DEM: {Path(args.dem).name}   "
          f"window: {'full' if args.full else f'core +{args.margin:g} m'}")
    print("=" * 70)

    dem, transform, crs, nodata, _ = load_dem(args.dem)
    check(crs is not None and crs.is_projected, "DEM CRS projected", str(crs))
    footprints = gpd.read_file(args.footprints).to_crs(crs)
    px = abs(transform.a)

    if args.full:
        rs, cs = 0, 0
        sub = dem
    else:
        _, crop = core_window(footprints.total_bounds, transform,
                              margin=args.margin)
        rs, cs = crop[0].start, crop[1].start
        sub = dem[crop]
    h, w = sub.shape
    valid = sub != nodata if nodata is not None else np.ones_like(sub, bool)
    check(bool(valid.any()), "window contains valid elevations")
    z_lo = float(sub[valid].min())
    z_hi = float(sub[valid].max())
    fill = z_lo - 2.0
    hm = np.where(valid, sub, fill).astype(np.float32)

    # Whole-meter local origin at the window center: consumers work in
    # meters near zero, where their float32 math is exact enough.
    ox = float(round(transform.c + (cs + w / 2) * transform.a))
    oy = float(round(transform.f + (rs + h / 2) * transform.e))
    x_first = transform.c + (cs + 0.5) * transform.a   # col 0 cell center
    y_first = transform.f + (rs + 0.5) * transform.e   # row 0 cell center

    args.out_dir.mkdir(parents=True, exist_ok=True)
    np.save(args.out_dir / "heightmap.npy", hm)
    print(f"  heightmap: {h}x{w} px @ {px:g} m  z {z_lo:.1f}..{z_hi:.1f} m"
          f"  ({h * px:.0f} x {w * px:.0f} m)")

    has_ortho = export_ortho(ORTHO, dem.shape, transform, cs, rs, w, h,
                             args.out_dir / "ortho.png")

    obs_list, has_id = load_observers(args.observers, crs)
    if args.ids:
        if not has_id:
            check(False, "--ids needs an 'id' field in the observers file",
                  str(args.observers))
            sys.exit(1)
        missing = [i for i in args.ids if i not in {o[0] for o in obs_list}]
        if missing:
            check(False, "all requested --ids present", f"missing {missing}")
            sys.exit(1)
        obs_list = [o for o in obs_list if o[0] in set(args.ids)]
    tag = "id" if has_id else "obs"
    observers = []
    for oid, x, y in obs_list:
        ground = float(bilinear(dem, transform, nodata, x, y)[0])
        if not np.isfinite(ground):
            warn("observer outside DEM / on nodata; skipped",
                 f"{tag}{oid} ({x:.1f}, {y:.1f})")
            continue
        observers.append({
            "id": f"{tag}{oid}",
            "x_local": round(x - ox, 3), "y_local": round(y - oy, 3),
            "ground_z": round(ground, 3),
            "eye_z": round(ground + args.eye_height, 3),
            "x_utm": x, "y_utm": y,
        })
    with open(args.out_dir / "observers.json", "w") as f:
        json.dump(observers, f, indent=2)
    print(f"  observers: {len(observers)}")

    domes, n_dome_rows = [], 0
    if args.dome_inventory.exists():
        df = pd.read_csv(args.dome_inventory)
        df = df[df["has_dome"].astype(str).str.lower() == "true"]
        n_dome_rows = len(df)
        for _, row in df.iterrows():
            cx, cy = float(row["cx"]), float(row["cy"])
            # Springing height resampled from the loaded DEM, not the
            # CSV's stored roof_z — same anti-staleness rationale as
            # viewshed.apply_dome_overlay.
            spring = float(bilinear(dem, transform, nodata, cx, cy)[0])
            if not np.isfinite(spring):
                warn("dome center off the DEM; skipped", f"ID {row['ID']}")
                continue
            domes.append({
                "id": int(row["ID"]),
                "cx_local": round(cx - ox, 3),
                "cy_local": round(cy - oy, 3),
                "spring_z": round(spring, 3),
                "radius_m": round(float(row["radius_m"]), 3),
            })
    else:
        warn("dome inventory missing; bundle has no domes.json entries",
             str(args.dome_inventory))
    with open(args.out_dir / "domes.json", "w") as f:
        json.dump(domes, f, indent=2)
    print(f"  domes: {len(domes)} of {n_dome_rows} inventory rows")

    meta = {
        "source_dem": Path(args.dem).name,
        "crs": str(crs),
        "px_m": px,
        "height_px": h, "width_px": w,
        "origin_utm": [ox, oy],
        "x_first_local": round(x_first - ox, 3),
        "y_first_local": round(y_first - oy, 3),
        "row0_is_north": True,
        "axes": "+X = east, +Y = north, z = elevation (m); vertex "
                "(row, col) at x = x_first_local + col*px_m, "
                "y = y_first_local - row*px_m",
        "z_min": z_lo, "z_max": z_hi, "nodata_fill": fill,
        "eye_height_m": args.eye_height,
        "ortho": "ortho.png" if has_ortho else None,
    }
    if args.unity:
        raw = args.out_dir / "heightmap_unity.raw"
        nbytes = export_unity_raw(hm, z_lo, z_hi, args.unity_res, raw)
        meta["unity"] = {
            "raw": raw.name,
            "resolution": args.unity_res,
            "depth_bits": 16, "byte_order": "little-endian",
            "row0_is_south": True,
            "terrain_width_m": round(w * px, 1),    # X (east)
            "terrain_length_m": round(h * px, 1),   # Z (north)
            "terrain_height_m": round(z_hi - z_lo, 2),
            "y_offset_m": z_lo,
        }
        check(nbytes == args.unity_res ** 2 * 2, "Unity RAW size matches",
              f"{nbytes:,} bytes @ {args.unity_res}^2 x 2")
        print(f"  unity raw: {args.unity_res}x{args.unity_res} px 16-bit")
    with open(args.out_dir / "meta.json", "w") as f:
        json.dump(meta, f, indent=2)

    print("\n" + "=" * 70)
    print("SELF-VERIFICATION")
    print("=" * 70)
    if observers:
        o = observers[0]
        # local coords are stored to the mm, so the round-trip is exact
        # only to that precision.
        check(abs((o["x_local"] + ox) - o["x_utm"]) < 1e-3
              and abs((o["y_local"] + oy) - o["y_utm"]) < 1e-3,
              "local + origin round-trips to UTM", o["id"])
        x_lo_l = meta["x_first_local"]
        y_hi_l = meta["y_first_local"]
        inside = all(
            x_lo_l <= ob["x_local"] <= x_lo_l + (w - 1) * px
            and y_hi_l - (h - 1) * px <= ob["y_local"] <= y_hi_l
            for ob in observers)
        check(inside, "all exported observers inside the window")
    check(1.0 < z_hi - z_lo < 200.0, "elevation span plausible",
          f"{z_hi - z_lo:.1f} m")
    if n_dome_rows:
        check(len(domes) == n_dome_rows, "every inventory dome exported",
              f"{len(domes)} of {n_dome_rows}")

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
    print(f"Scene bundle complete; files in {loc}")


if __name__ == "__main__":
    main()
