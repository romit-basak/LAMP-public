"""True-3D ray-casting viewshed engine for El Bagawat (build-order step 1).

Casts real 3D line-of-sight rays from the Marks_Brief2 observers against the
regenerated DEM-with-buildings *heightfield* (solid buildings, no apertures
yet). The LOS test lives behind a `Scene` interface so an aperture-capable
explicit-geometry scene can replace `HeightfieldScene` at step 2 without
touching the driver or graph builder.

Outputs per-observer visibility rasters (+ combined/count), a visibility graph
(observer-observer and observer-building), and QC overlays for visual audit.

Run:  .venv/bin/python scripts/viewshed.py
Exits nonzero if any self-check fails.
"""

import os

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

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
import torch
from rasterio.windows import transform as window_transform
from shapely.geometry import LineString, Point

from sanity_checks import ROOT, DEM_REGEN, FOOTPRINTS, VIEWPOINTS, check, warn, failures
from build_dem_with_buildings import hillshade, core_window

EYE_HEIGHT = 1.5          # meters above surface (CLAUDE.md mandate; do not change)
STEP = 0.2                # ray-march sample spacing (~1/2 pixel at 0.4 m)
EPS_ANG = 1e-6            # dimensionless slope tolerance


def select_device():
    if torch.cuda.is_available():
        return torch.device("cuda")
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


class HeightfieldScene:
    """DEM-as-heightfield LOS scene. Geometry only — eye height is the
    driver's responsibility. Implements the Scene contract used by the
    viewshed driver and graph builder: surface_z, visible_mask, is_visible."""

    def __init__(self, dem_array, transform, nodata, device, step=STEP,
                 eps_ang=EPS_ANG, d_min=None):
        self.dem_np = np.asarray(dem_array, dtype=np.float32)
        self.H, self.W = self.dem_np.shape
        self.dem_flat = torch.as_tensor(
            self.dem_np.reshape(-1), dtype=torch.float32, device=device)
        self.device = device
        self.a = float(transform.a)        # +px width
        self.e = float(transform.e)        # -px height
        self.x0 = float(transform.c)       # top-left corner x
        self.y0 = float(transform.f)       # top-left corner y
        self.px = abs(self.a)
        self.nodata = nodata
        self.step = step
        self.eps_ang = eps_ang
        self.d_min = d_min if d_min is not None else 0.71 * self.px

    def _pix(self, x, y):
        """World -> fractional cell-center pixel coords (col_f, row_f)."""
        return (x - self.x0) / self.a - 0.5, (y - self.y0) / self.e - 0.5

    def surface_z(self, x, y):
        """Bilinear surface elevation at world points (numpy, host). nodata
        and out-of-bounds -> NaN."""
        x = np.atleast_1d(np.asarray(x, dtype=np.float64))
        y = np.atleast_1d(np.asarray(y, dtype=np.float64))
        col_f, row_f = self._pix(x, y)
        c0 = np.floor(col_f).astype(int)
        r0 = np.floor(row_f).astype(int)
        wc = col_f - c0
        wr = row_f - r0
        inb = (c0 >= 0) & (c0 < self.W - 1) & (r0 >= 0) & (r0 < self.H - 1)
        c0c, r0c = np.clip(c0, 0, self.W - 2), np.clip(r0, 0, self.H - 2)
        z00 = self.dem_np[r0c, c0c]
        z01 = self.dem_np[r0c, c0c + 1]
        z10 = self.dem_np[r0c + 1, c0c]
        z11 = self.dem_np[r0c + 1, c0c + 1]
        valid = inb
        if self.nodata is not None:
            valid &= ~np.any(np.stack([z00, z01, z10, z11]) == self.nodata, axis=0)
        top = z00 * (1 - wc) + z01 * wc
        bot = z10 * (1 - wc) + z11 * wc
        z = top * (1 - wr) + bot * wr
        return np.where(valid, z, np.nan)

    def visible_mask(self, eye_xyz, targets_xyz, chunk=200_000):
        """LOS from eye (3,) to each target (N,3 world; tz is the endpoint
        surface). Returns a bool numpy array [N]. Per-bearing ray-march with a
        running-max elevation-angle reduction, in eye-relative pixel coords."""
        ex, ey, ez = (float(v) for v in eye_xyz)
        targets = np.asarray(targets_xyz, dtype=np.float64)
        n = len(targets)
        out = np.zeros(n, dtype=bool)

        col_eye, row_eye = self._pix(ex, ey)
        ci, ri = int(round(col_eye)), int(round(row_eye))
        cf, rf = col_eye - ci, row_eye - ri

        for s in range(0, n, chunk):
            tx = targets[s:s + chunk, 0]
            ty = targets[s:s + chunk, 1]
            tz = targets[s:s + chunk, 2]
            dx, dy = tx - ex, ty - ey
            D = np.hypot(dx, dy)
            near = D < self.d_min
            with np.errstate(invalid="ignore", divide="ignore"):
                ux, uy = dx / D, dy / D
                ang_t = (tz - ez) / D

            t = lambda arr: torch.as_tensor(  # noqa: E731
                arr, dtype=torch.float32, device=self.device)
            D_t, ang_t_t = t(D), t(ang_t)
            ucol = t(ux / self.a)          # rel-pixel cols per meter along ray
            urow = t(uy / self.e)
            running = torch.full((len(tx),), -math.inf, device=self.device)

            d_max = float(np.nanmax(D)) if len(D) else 0.0
            k_max = int(math.ceil(d_max / self.step)) + 1
            for k in range(1, k_max):
                d_k = k * self.step
                if d_k < self.d_min:
                    continue
                active = D_t - self.d_min >= d_k
                if not bool(active.any()):
                    break
                rel_col = cf + ucol * d_k
                rel_row = rf + urow * d_k
                fc = torch.floor(rel_col)
                fr = torch.floor(rel_row)
                wc = rel_col - fc
                wr = rel_row - fr
                c0 = ci + fc.to(torch.long)
                r0 = ri + fr.to(torch.long)
                inb = (c0 >= 0) & (c0 < self.W - 1) & (r0 >= 0) & (r0 < self.H - 1)
                c0c = c0.clamp(0, self.W - 2)
                r0c = r0.clamp(0, self.H - 2)
                base = r0c * self.W + c0c
                z00 = self.dem_flat[base]
                z01 = self.dem_flat[base + 1]
                z10 = self.dem_flat[base + self.W]
                z11 = self.dem_flat[base + self.W + 1]
                valid = inb & active
                if self.nodata is not None:
                    nod = ((z00 == self.nodata) | (z01 == self.nodata) |
                           (z10 == self.nodata) | (z11 == self.nodata))
                    valid &= ~nod
                top = z00 * (1 - wc) + z01 * wc
                bot = z10 * (1 - wc) + z11 * wc
                z = top * (1 - wr) + bot * wr
                ang = (z - ez) / d_k
                ang = torch.where(valid, ang, torch.full_like(ang, -math.inf))
                running = torch.maximum(running, ang)

            vis = (ang_t_t >= running - self.eps_ang).cpu().numpy()
            vis |= near                      # target == observer cell
            vis &= np.isfinite(ang_t)        # tz nodata -> not visible
            out[s:s + chunk] = vis
        return out

    def is_visible(self, eye_xyz, target_xyz):
        return bool(self.visible_mask(eye_xyz, np.asarray(target_xyz)[None, :])[0])


def load_dem(path):
    with rasterio.open(path) as src:
        return (src.read(1), src.transform, src.crs, src.nodata, src.profile.copy())


def load_observers(path, crs):
    pts = (gpd.read_file(path).explode(index_parts=False)
           .reset_index(drop=True).to_crs(crs))
    return [(p.x, p.y) for p in pts.geometry]


def target_grid(scene, win, transform):
    """Cell-center world coords + surface z for every cell in the window."""
    cs, rs = int(win.col_off), int(win.row_off)
    w = min(int(win.width), scene.W - cs)
    h = min(int(win.height), scene.H - rs)
    rows = rs + np.arange(h)
    cols = cs + np.arange(w)
    xs = scene.x0 + (cols + 0.5) * scene.a
    ys = scene.y0 + (rows + 0.5) * scene.e
    X, Y = np.meshgrid(xs, ys)
    Z = scene.dem_np[rs:rs + h, cs:cs + w]
    return X, Y, Z, (h, w), (rs, cs)


def compute_viewshed(scene, eye_xyz, X, Y, Z, shape):
    targets = np.column_stack([X.ravel(), Y.ravel(), Z.ravel()])
    vis = scene.visible_mask(eye_xyz, targets).reshape(shape)
    out = vis.astype(np.uint8)
    if scene.nodata is not None:
        out[Z == scene.nodata] = 255
    return out


def build_viewgraph(scene, eyes, footprints):
    """Observer-observer (symmetric matrix) + observer-building edges."""
    n = len(eyes)
    obs = np.zeros((n, n), dtype=bool)
    for i in range(n):
        for j in range(n):
            if i == j:
                obs[i, j] = True
                continue
            obs[i, j] = scene.is_visible(eyes[i], (eyes[j][0], eyes[j][1], eyes[j][2]))

    cent = footprints.geometry.centroid
    roof_z = scene.surface_z(cent.x.values, cent.y.values)   # extruded roof
    edges = []
    for i, eye in enumerate(eyes):
        cen_t = np.column_stack([cent.x.values, cent.y.values, roof_z])
        vis_cent = scene.visible_mask(eye, cen_t)
        vis_any = vis_cent.copy()
        for bi, (_, row) in enumerate(footprints.iterrows()):
            verts = np.asarray(row.geometry.exterior.coords)
            vz = np.full(len(verts), roof_z[bi])
            vt = np.column_stack([verts[:, 0], verts[:, 1], vz])
            vis_any[bi] = bool(scene.visible_mask(eye, vt).any()) or vis_cent[bi]
            edges.append({
                "src_id": i + 1, "dst_type": "building",
                "dst_id": int(row["ID"]),
                "visible": bool(vis_cent[bi]),
                "visible_any_vertex": bool(vis_any[bi]),
                "distance_m": float(math.hypot(cent.x.values[bi] - eye[0],
                                               cent.y.values[bi] - eye[1])),
                "eye_z": round(eye[2], 3), "tgt_z": round(float(roof_z[bi]), 3),
                "geometry": LineString([(eye[0], eye[1]),
                                        (cent.x.values[bi], cent.y.values[bi])]),
            })
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            edges.append({
                "src_id": i + 1, "dst_type": "observer", "dst_id": j + 1,
                "visible": bool(obs[i, j]), "visible_any_vertex": bool(obs[i, j]),
                "distance_m": float(math.hypot(eyes[j][0] - eyes[i][0],
                                               eyes[j][1] - eyes[i][1])),
                "eye_z": round(eyes[i][2], 3), "tgt_z": round(eyes[j][2], 3),
                "geometry": LineString([(eyes[i][0], eyes[i][1]),
                                        (eyes[j][0], eyes[j][1])]),
            })
    return obs, edges


def write_viewshed_tif(mask, win, transform, crs, profile, path):
    prof = profile.copy()
    prof.update(driver="GTiff", dtype="uint8", count=1, nodata=255,
                height=mask.shape[0], width=mask.shape[1], crs=crs,
                transform=window_transform(win, transform),
                compress="lzw", tiled=True)
    with rasterio.open(path, "w", **prof) as dst:
        dst.write(mask, 1)


def qc_png(scene, mask, win, transform, footprints, eye_xy, title, path):
    wt = window_transform(win, transform)
    rs, cs = int(win.row_off), int(win.col_off)
    h, w = mask.shape
    dem_win = scene.dem_np[rs:rs + h, cs:cs + w]
    hs = hillshade(dem_win, scene.px)

    fig, ax = plt.subplots(figsize=(10, 14), dpi=200)
    ax.imshow(hs, cmap="gray")
    overlay = np.ma.masked_where(mask != 1, mask)
    ax.imshow(overlay, cmap="autumn", alpha=0.4, vmin=0, vmax=1)

    def to_px(x, y):
        return (x - wt.c) / wt.a - 0.5, (y - wt.f) / wt.e - 0.5

    for geom in footprints.geometry:
        polys = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
        for poly in polys:
            xs, ys = poly.exterior.xy
            px, py = to_px(np.asarray(xs), np.asarray(ys))
            ax.plot(px, py, color="cyan", linewidth=0.4)
    epx, epy = to_px(*eye_xy)
    ax.plot(epx, epy, "b*", markersize=14, markeredgecolor="white")
    ax.set_title(title)
    ax.axis("off")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def run_self_checks(scene, eyes, masks, obs, X, Y):
    print("\n" + "=" * 70)
    print("SELF-VERIFICATION")
    print("=" * 70)
    # 1. observer sees its own neighborhood
    eye = eyes[0]
    near = np.column_stack([
        [eye[0] + dx for dx in (-1, 0, 1, 0)],
        [eye[1] + dy for dy in (0, 1, 0, -1)],
        [scene.surface_z(eye[0] + dx, eye[1] + dy)[0]
         for dx, dy in ((-1, 0), (0, 1), (1, 0), (0, -1))]])
    check(scene.visible_mask(eye, near).all(),
          "observer sees its immediate neighbors")
    # 2. obs-obs symmetric
    check(bool((obs == obs.T).all()), "observer-observer matrix symmetric")
    # 3. monotonic in eye height (check-only second run on a subsample)
    sub = np.column_stack([X.ravel()[::97], Y.ravel()[::97],
                           scene.surface_z(X.ravel()[::97], Y.ravel()[::97])])
    lo = scene.visible_mask(eyes[0], sub).sum()
    eye_hi = (eyes[0][0], eyes[0][1], eyes[0][2] + 1.0)
    hi = scene.visible_mask(eye_hi, sub).sum()
    check(hi >= lo, "raising the eye never reduces visible count",
          f"1.5 m: {lo}  +1 m: {hi}")
    # 4. reciprocity spot-check on a visible cell
    vis_idx = np.argwhere(masks[0] == 1)
    if len(vis_idx):
        r, c = vis_idx[len(vis_idx) // 2]
        tx, ty = X[r, c], Y[r, c]
        tz = scene.surface_z(tx, ty)[0]
        fwd = scene.is_visible(eyes[0], (tx, ty, tz))
        rev = scene.is_visible((tx, ty, tz + EYE_HEIGHT),
                               (eyes[0][0], eyes[0][1], eyes[0][2]))
        check(fwd == rev, "forward/reverse LOS agree on a sample cell")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dem", type=Path, default=DEM_REGEN)
    p.add_argument("--footprints", type=Path, default=FOOTPRINTS)
    p.add_argument("--observers", type=Path, default=VIEWPOINTS)
    p.add_argument("--out-dir", type=Path,
                   default=ROOT / "200_Projects/220_BuildingsToDEM")
    p.add_argument("--margin", type=float, default=60.0)
    p.add_argument("--chunk", type=int, default=200_000)
    args = p.parse_args()

    device = select_device()
    print("=" * 70)
    print(f"DEVICE: {device}   DEM: {Path(args.dem).name}")
    print("=" * 70)

    dem, transform, crs, nodata, profile = load_dem(args.dem)
    check(crs is not None and crs.is_projected, "DEM CRS projected", str(crs))
    scene = HeightfieldScene(dem, transform, nodata, device)

    footprints = gpd.read_file(args.footprints).to_crs(crs)
    obs_xy = load_observers(args.observers, crs)
    check(len(obs_xy) == 3, "3 observers loaded", f"{len(obs_xy)}")
    eyes = [(x, y, float(scene.surface_z(x, y)[0]) + EYE_HEIGHT) for x, y in obs_xy]
    for i, e in enumerate(eyes, 1):
        print(f"  observer {i}: ({e[0]:.1f}, {e[1]:.1f})  eye_z {e[2]:.2f} m")

    win, _ = core_window(footprints.total_bounds, transform, margin=args.margin)
    X, Y, Z, shape, _ = target_grid(scene, win, transform)
    print(f"  analysis window: {shape[0]}x{shape[1]} px ({shape[0]*shape[1]:,} cells)")

    print("\n" + "=" * 70)
    print("PER-OBSERVER VIEWSHEDS")
    print("=" * 70)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    masks = []
    for i, eye in enumerate(eyes, 1):
        mask = compute_viewshed(scene, eye, X, Y, Z, shape)
        masks.append(mask)
        n_vis = int((mask == 1).sum())
        print(f"  observer {i}: {n_vis:,} visible "
              f"({100*n_vis/(shape[0]*shape[1]):.1f}% of window)")
        tif = args.out_dir / f"viewshed_obs{i}.tif"
        write_viewshed_tif(mask, win, transform, crs, profile, tif)
        qc_png(scene, mask, win, transform, footprints, (eye[0], eye[1]),
               f"Viewshed — observer {i}", args.out_dir / f"viewshed_obs{i}.png")

    stack = np.stack(masks)
    valid = stack != 255
    combined = np.where(valid.any(0),
                        (stack == 1).any(0).astype(np.uint8), 255).astype(np.uint8)
    count = np.where(valid.any(0),
                     (stack == 1).sum(0).astype(np.uint8), 255).astype(np.uint8)
    write_viewshed_tif(combined, win, transform, crs, profile,
                       args.out_dir / "viewshed_combined_max.tif")
    write_viewshed_tif(count, win, transform, crs, profile,
                       args.out_dir / "viewshed_count.tif")
    qc_png(scene, combined, win, transform, footprints, (eyes[0][0], eyes[0][1]),
           "Viewshed — combined (any observer)",
           args.out_dir / "viewshed_combined.png")

    print("\n" + "=" * 70)
    print("VISIBILITY GRAPH")
    print("=" * 70)
    obs, edges = build_viewgraph(scene, eyes, footprints)
    gdf = gpd.GeoDataFrame(edges, crs=crs)
    gdf.to_file(args.out_dir / "viewgraph_edges.geojson", driver="GeoJSON")
    gdf.drop(columns="geometry").to_csv(
        args.out_dir / "viewgraph_edges.csv", index=False)
    pd.DataFrame(obs.astype(int),
                 index=[f"obs{i+1}" for i in range(len(eyes))],
                 columns=[f"obs{i+1}" for i in range(len(eyes))]).to_csv(
        args.out_dir / "viewgraph_obs_obs.csv")
    b = gdf[gdf.dst_type == "building"]
    print(f"  observer-observer visible pairs:\n{obs.astype(int)}")
    print(f"  building edges: {len(b)}  visible(centroid): "
          f"{int(b.visible.sum())}  visible(any vertex): "
          f"{int(b.visible_any_vertex.sum())}")

    run_self_checks(scene, eyes, masks, obs, X, Y)

    print("\n" + "=" * 70)
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"Viewshed engine complete; outputs in {args.out_dir.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
