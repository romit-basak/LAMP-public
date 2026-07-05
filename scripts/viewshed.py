"""True-3D ray-casting viewshed engine for El Bagawat (build-order step 1).

Casts real 3D line-of-sight rays from the Marks_Brief2 observers against the
regenerated DEM-with-buildings *heightfield* (solid buildings, no apertures
yet). The LOS test lives behind a `Scene` interface so an aperture-capable
explicit-geometry scene can replace `HeightfieldScene` at step 2 without
touching the driver or graph builder.

Outputs per-observer visibility rasters (+ combined/count), a visibility graph
(observer-observer and observer-building), and QC overlays for visual audit.

Run (360° from the 3 sample observers, core window):
    .venv/bin/python scripts/viewshed.py
Directional cone from an arbitrary point (compass azimuth + pitch, full-width
horizontal fov and vertical vfov):
    .venv/bin/python scripts/viewshed.py --point 254210 2820958 \\
        --radius 200 --azimuth 90 --fov 60 --pitch 10 --vfov 40 --no-graph
Optional 3D visibility volume (coarse voxels; csv/ply/npy/las/laz):
    .venv/bin/python scripts/viewshed.py --point 254210 2820958 \\
        --radius 150 --volume --volume-format laz --no-graph
Include dome roofs in the ray-cast surface (needs build_dome_layer.py output;
off by default, experimental pending mentor visual review):
    .venv/bin/python scripts/viewshed.py --point 254210 2820958 \\
        --radius 150 --domes --no-graph
Any point file, 360°:
    .venv/bin/python scripts/viewshed.py --observers my_points.shp

Exits nonzero if any self-check fails.
"""

import os

# Set before torch is imported: lets ops that lack an MPS kernel fall back to
# CPU instead of raising, so the same code path runs on Apple Silicon and CUDA.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import argparse
import json
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
from rasterio.features import geometry_mask
from rasterio.windows import Window, transform as window_transform
from shapely.geometry import LineString

from sanity_checks import (ROOT, DEM_REGEN, FOOTPRINTS, VIEWPOINTS,
                           DOME_INVENTORY, check, warn, failures)
from build_dem_with_buildings import hillshade, core_window

EYE_HEIGHT = 1.5          # default eye height (m) above surface; late-antique
                          # skeletal baseline, override with --eye-height
STEP = 0.2                # ray-march sample spacing (~1/2 pixel at 0.4 m).
                          # Sub-pixel on purpose: a coarser step lets a thin
                          # wall fall between samples, so a ray would leak
                          # through a solid building at grazing angles.
EPS_ANG = 1e-6            # slope tolerance so a target sitting exactly on the
                          # accumulated horizon reads visible instead of losing
                          # to float rounding in the angle comparison.


def select_device():
    # Prefer a GPU but never hard-code one: CUDA on the remote box, MPS on the
    # Mac, CPU elsewhere — the check order encodes that preference.
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
        # Flat and resident on the device so the ray-march can gather the four
        # bilinear neighbours by flat index without re-uploading the DEM.
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
        # ~half a pixel diagonal (sqrt(2)/2). A target nearer than this shares
        # the observer's own cell, so nothing lies between to occlude it; the
        # march also skips distances below it to avoid dividing by ~0.
        self.d_min = d_min if d_min is not None else 0.71 * self.px

    def _pix(self, x, y):
        """World -> fractional cell-center pixel coords (col_f, row_f)."""
        # -0.5: the affine transform addresses pixel corners, but elevations are
        # sampled at cell centers, so shift by half a pixel.
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
        # Clamp so the neighbour gather never indexes out of bounds; `inb`
        # remembers which points were truly inside, so the clamped edge reads
        # get discarded as invalid below (clamp-then-mask).
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

        # March in coordinates relative to the observer's pixel. UTM eastings/
        # northings are ~1e6 and float32 (all MPS offers) holds ~7 digits, so
        # adding a 0.2 m step to an absolute coord rounds away. Keep the big
        # integer anchor (ci, ri) on host; only small offsets become tensors.
        col_eye, row_eye = self._pix(ex, ey)
        ci, ri = int(round(col_eye)), int(round(row_eye))
        cf, rf = col_eye - ci, row_eye - ri

        # Chunk the targets so a full-site grid (millions of cells) never has to
        # live in device memory all at once.
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
                # Hand-rolled bilinear gather via flat indices into dem_flat.
                # grid_sample is the natural tool, but its border-padding mode
                # has no MPS kernel, so the four corner reads are explicit.
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
                # Fold the highest terrain elevation-angle seen so far — the
                # running horizon. Done step-by-step because torch.cummax, the
                # obvious one-liner, has no MPS kernel.
                running = torch.maximum(running, ang)

            # Visible iff the target rises to or above the horizon accumulated
            # along its bearing.
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


def apply_dome_overlay(dem_np, transform, nodata, csv_path, footprints):
    """Raise `dem_np` with hemispherical dome caps from a dome-layer CSV
    (`scripts/build_dome_layer.py`). A dome is a heightfield bump — solid,
    springing from the surface already at its center — so this only ever
    raises cells (`np.maximum`), needing no change to the Scene/ray-marching
    code that consumes the array afterwards. The springing height is sampled
    fresh from `dem_np` itself (not the CSV's stored `roof_z`), so the overlay
    stays consistent with whatever `--dem` raster is actually loaded. Each cap
    is also clipped to its own footprint polygon, so an over-generous CSV
    radius can never spill the dome onto neighboring ground or buildings.
    Returns (dem_np, n_domes_applied, n_cells_raised)."""
    df = pd.read_csv(csv_path)
    df = df[df["has_dome"].astype(str).str.lower() == "true"]
    geom_by_id = {int(r["ID"]): r.geometry for _, r in footprints.iterrows()}
    H, W = dem_np.shape
    a, e, x0, y0 = transform.a, transform.e, transform.c, transform.f
    px = abs(a)
    out = dem_np.copy()
    n_applied, n_cells = 0, 0
    for _, row in df.iterrows():
        bid = int(row["ID"])
        cx, cy, r = float(row["cx"]), float(row["cy"]), float(row["radius_m"])
        ci = int(round((cx - x0) / a - 0.5))
        ri = int(round((cy - y0) / e - 0.5))
        if not (0 <= ri < H and 0 <= ci < W):
            warn("dome outside DEM extent", f"ID {bid} ({cx:.1f}, {cy:.1f})")
            continue
        spring = float(dem_np[ri, ci])
        if nodata is not None and spring == nodata:
            warn("dome center on nodata", f"ID {bid}")
            continue
        geom = geom_by_id.get(bid)
        if geom is None:
            warn("dome has no matching footprint", f"ID {bid}")
            continue
        rpix = int(math.ceil(r / px)) + 1
        r0, r1 = max(ri - rpix, 0), min(ri + rpix + 1, H)
        c0, c1 = max(ci - rpix, 0), min(ci + rpix + 1, W)
        rows_ix, cols_ix = np.mgrid[r0:r1, c0:c1]
        xs = x0 + (cols_ix + 0.5) * a
        ys = y0 + (rows_ix + 0.5) * e
        d2 = (xs - cx) ** 2 + (ys - cy) ** 2
        inside = d2 <= r * r
        win_transform = window_transform(Window(c0, r0, c1 - c0, r1 - r0), transform)
        inside &= ~geometry_mask([geom], (r1 - r0, c1 - c0), win_transform,
                                 invert=False)
        if nodata is not None:
            inside &= out[r0:r1, c0:c1] != nodata
        cap = spring + np.sqrt(np.maximum(r * r - d2, 0.0))
        patch = out[r0:r1, c0:c1]
        raised = inside & (cap > patch)
        patch[raised] = cap[raised]
        n_applied += 1
        n_cells += int(raised.sum())
    return out, n_applied, n_cells


def load_observers(path, crs):
    """Return ([(oid, x, y), ...], has_id). oid is the file's `id` field
    (case-insensitive) when present, else the 1-based row index."""
    gdf = (gpd.read_file(path).explode(index_parts=False)
           .reset_index(drop=True).to_crs(crs))
    id_col = next((c for c in gdf.columns if c.lower() == "id"), None)
    obs = []
    for i, row in gdf.iterrows():
        oid = (int(row[id_col]) if id_col is not None and pd.notna(row[id_col])
               else i + 1)
        obs.append((oid, row.geometry.x, row.geometry.y))
    return obs, id_col is not None


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


def observer_window(scene, eye_xy, radius):
    """Pixel window for the disc of `radius` m around an observer (clamped)."""
    ex, ey = eye_xy
    rpix = int(math.ceil(radius / scene.px))
    c = (ex - scene.x0) / scene.a
    r = (ey - scene.y0) / scene.e
    return Window(max(int(c - rpix), 0), max(int(r - rpix), 0), 2 * rpix, 2 * rpix)


def apply_view_constraints(mask, X, Y, Z, eye_xyz, azimuth, fov, radius,
                           pitch=0.0, vfov=180.0):
    """Zero out visible cells outside the sight radius, the horizontal azimuth
    sector, and/or the vertical pitch sector. Azimuth is compass degrees
    (0=N, 90=E, clockwise); fov is the full horizontal cone width. Pitch is the
    elevation angle in degrees (0=horizontal, + up); vfov is the full vertical
    cone width (180 = unconstrained). Modifies `mask` in place and returns it."""
    ex, ey, ez = eye_xyz
    if radius is not None:
        mask[(mask == 1) & (np.hypot(X - ex, Y - ey) > radius)] = 0
    if azimuth is not None and fov < 360:
        # arctan2(dx, dy) with x first yields a compass bearing (0=N, CW). The
        # +180 %360 -180 folds the difference into [-180, 180] so the sector
        # test holds across the 0/360 seam (e.g. a cone centered on north).
        bearing = np.degrees(np.arctan2(X - ex, Y - ey)) % 360
        diff = (bearing - azimuth + 180) % 360 - 180
        mask[(mask == 1) & (np.abs(diff) > fov / 2)] = 0
    if vfov < 180:
        dist = np.hypot(X - ex, Y - ey)
        with np.errstate(invalid="ignore"):
            elev = np.degrees(np.arctan2(Z - ez, dist))
        # the observer's own cell (dist 0) has no defined elevation; keep it
        sector = (np.abs(elev - pitch) <= vfov / 2) | (dist == 0)
        mask[(mask == 1) & ~sector] = 0
    return mask


def build_viewgraph(scene, eyes, footprints, labels):
    """Observer-observer (symmetric matrix) + observer-building edges.
    `labels` (one per eye) are used as the graph node ids."""
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
        # Two visibility criteria per building: the strict centroid test and
        # the lenient any-vertex test — a tomb whose center is hidden by its
        # own near wall can still have a corner in plain view. Record both;
        # let the analysis choose.
        for bi, (_, row) in enumerate(footprints.iterrows()):
            verts = np.asarray(row.geometry.exterior.coords)
            vz = np.full(len(verts), roof_z[bi])
            vt = np.column_stack([verts[:, 0], verts[:, 1], vz])
            vis_any[bi] = bool(scene.visible_mask(eye, vt).any()) or vis_cent[bi]
            edges.append({
                "src_id": labels[i], "dst_type": "building",
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
                "src_id": labels[i], "dst_type": "observer", "dst_id": labels[j],
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
    h, w = mask.shape
    tiled = h >= 256 and w >= 256          # tiny windows can't carry 256-tiles
    prof.update(driver="GTiff", dtype="uint8", count=1, nodata=255,
                height=h, width=w, crs=crs,
                transform=window_transform(win, transform),
                compress="lzw", tiled=tiled)
    if tiled:
        prof.update(blockxsize=256, blockysize=256)
    else:
        prof.pop("blockxsize", None)
        prof.pop("blockysize", None)
    with rasterio.open(path, "w", **prof) as dst:
        dst.write(mask, 1)


def qc_png(scene, mask, win, transform, footprints, eye_xy, title, path,
           azimuth=None, fov=360.0, radius=None):
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
    if azimuth is not None and fov < 360:
        rad_px = (radius / scene.px) if radius else 0.9 * max(h, w)
        angs = np.deg2rad(np.linspace(azimuth - fov / 2, azimuth + fov / 2, 64))
        wx = epx + np.sin(angs) * rad_px      # compass: +col = East = sin(az)
        wy = epy - np.cos(angs) * rad_px      # +row is South, so North = -cos(az)
        ax.plot(np.r_[epx, wx, epx], np.r_[epy, wy, epy],
                color="yellow", linewidth=1.0, linestyle="--")
    ax.plot(epx, epy, "b*", markersize=14, markeredgecolor="white")
    ax.set_title(title)
    ax.axis("off")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def volume_extent(scene, eye_xy, radius, voxel):
    """Horizontal sample coords (xs east->, ys north->south) for the volume:
    a disc of `radius` around the eye, else the whole DEM footprint."""
    ex, ey = eye_xy
    if radius is not None:
        x_lo, x_hi = ex - radius, ex + radius
        y_lo, y_hi = ey - radius, ey + radius
    else:
        x_lo, x_hi = scene.x0, scene.x0 + scene.W * scene.a
        ya, yb = scene.y0, scene.y0 + scene.H * scene.e
        y_lo, y_hi = min(ya, yb), max(ya, yb)
    xs = np.arange(x_lo, x_hi + voxel / 2, voxel)
    ys = np.arange(y_hi, y_lo - voxel / 2, -voxel)
    return xs, ys


def compute_volume(scene, eye_xyz, xs, ys, levels, azimuth, fov, radius,
                   pitch=0.0, vfov=180.0, chunk=200_000):
    """Visibility of a 3D voxel grid. Columns sit on (xs, ys); each carries
    `levels` heights above its own ground. Returns (ground, vis) where ground
    is the per-column surface (nrows, ncols) and vis is bool (nrows, ncols,
    nlevels), already trimmed by the radius and the horizontal/vertical cones.
    Voxels over nodata ground are not visible (handled by visible_mask)."""
    ex, ey, ez = eye_xyz
    X, Y = np.meshgrid(xs, ys)
    nrows, ncols = X.shape
    nlev = len(levels)
    ground = scene.surface_z(X.ravel(), Y.ravel()).reshape(nrows, ncols)
    zabs = ground[:, :, None] + levels[None, None, :]
    targets = np.column_stack([
        np.repeat(X[:, :, None], nlev, axis=2).ravel(),
        np.repeat(Y[:, :, None], nlev, axis=2).ravel(),
        zabs.ravel()])
    vis = scene.visible_mask(eye_xyz, targets, chunk=chunk).reshape(
        nrows, ncols, nlev)

    dx, dy = X - ex, Y - ey
    dist = np.hypot(dx, dy)
    keep = np.ones((nrows, ncols), dtype=bool)
    if radius is not None:
        keep &= dist <= radius
    if azimuth is not None and fov < 360:
        bearing = np.degrees(np.arctan2(dx, dy)) % 360
        diff = (bearing - azimuth + 180) % 360 - 180
        keep &= np.abs(diff) <= fov / 2
    vis &= keep[:, :, None]
    if vfov < 180:
        with np.errstate(invalid="ignore"):
            elev = np.degrees(np.arctan2(zabs - ez, dist[:, :, None]))
        vis &= np.abs(elev - pitch) <= vfov / 2
    return ground, vis


def volume_points(xs, ys, levels, ground, vis):
    """(N,3) world coords of visible voxel centers + their (r,c,l) indices."""
    r, c, lv = np.nonzero(vis)
    pts = np.column_stack([xs[c], ys[r], ground[r, c] + levels[lv]])
    return pts, (r, c, lv)


def write_volume_csv(path, pts, count=None):
    cols = ["x", "y", "z"] + (["count"] if count is not None else [])
    data = pts if count is None else np.column_stack([pts, count])
    pd.DataFrame(data, columns=cols).to_csv(path, index=False)


def write_volume_ply(path, pts, count=None):
    header = ["ply", "format ascii 1.0", f"element vertex {len(pts)}",
              "property float x", "property float y", "property float z"]
    if count is not None:
        header.append("property int count")
    header.append("end_header")
    with open(path, "w") as f:
        f.write("\n".join(header) + "\n")
        if count is None:
            np.savetxt(f, pts, fmt="%.3f")
        else:
            np.savetxt(f, np.column_stack([pts, count]),
                       fmt=["%.3f", "%.3f", "%.3f", "%d"])


def write_volume_npy(path, vis, xs, ys, levels, crs):
    """Boolean/int voxel grid (rows->ys, cols->xs, depth->levels) + a JSON
    sidecar carrying the georeferencing needed to rebuild world coords."""
    np.save(path, vis)
    meta = {
        "x0": float(xs[0]), "y0": float(ys[0]),
        "xstep": float(xs[1] - xs[0]) if len(xs) > 1 else 0.0,
        "ystep": float(ys[1] - ys[0]) if len(ys) > 1 else 0.0,
        "levels": [float(v) for v in levels],
        "z_is_above_local_ground": True,
        "axes": "vis[row, col, level]; row->y, col->x, level->height AGL",
        "crs": str(crs),
    }
    with open(Path(path).with_suffix(".json"), "w") as f:
        json.dump(meta, f, indent=2)


def write_volume_las(path, pts, crs, count=None):
    """Visible voxel centers as a LAS/LAZ point cloud (.las/.laz chosen by the
    path suffix). `count` (combined volume) is stored as intensity. The CRS is
    written into the header so QGIS georeferences it. Requires laspy, plus the
    lazrs backend for .laz; missing deps are reported, not raised."""
    try:
        import laspy
        from pyproj import CRS as PyCRS
    except ImportError as exc:
        warn("LAS/LAZ skipped", f"install laspy (+lazrs for .laz): {exc}")
        return
    if len(pts) == 0:
        warn("LAS/LAZ skipped", f"no visible points for {Path(path).name}")
        return
    header = laspy.LasHeader(point_format=3, version="1.4")
    header.offsets = pts.min(axis=0)
    header.scales = [0.001, 0.001, 0.001]   # mm precision on large UTM coords
    if crs is not None:
        header.add_crs(PyCRS.from_user_input(str(crs)))
    las = laspy.LasData(header)
    las.x, las.y, las.z = pts[:, 0], pts[:, 1], pts[:, 2]
    if count is not None:
        las.intensity = np.asarray(count, dtype=np.uint16)
    las.write(str(path))


def volume_qc_png(xs, ys, levels, ground, vis, footprints, eye_xy, title, path):
    """Top-down audit: hillshade of the (coarse) ground with the highest
    visible height above ground per column overlaid."""
    voxel = float(xs[1] - xs[0]) if len(xs) > 1 else 1.0
    ystep = float(ys[1] - ys[0]) if len(ys) > 1 else -voxel
    anyvis = vis.any(axis=2)
    top = np.where(vis, np.arange(len(levels))[None, None, :], -1).max(axis=2)
    height = np.where(anyvis, levels[np.clip(top, 0, None)], np.nan)

    base = np.nan_to_num(ground, nan=float(np.nanmin(ground)))
    hs = hillshade(base, voxel)
    fig, ax = plt.subplots(figsize=(10, 14), dpi=200)
    ax.imshow(hs, cmap="gray")
    im = ax.imshow(np.ma.masked_invalid(height), cmap="viridis", alpha=0.6)
    fig.colorbar(im, ax=ax, fraction=0.046, label="max visible height AGL (m)")

    def to_px(x, y):
        return (x - xs[0]) / voxel, (y - ys[0]) / ystep

    for geom in footprints.geometry:
        polys = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
        for poly in polys:
            gx, gy = poly.exterior.xy
            cx, cy = to_px(np.asarray(gx), np.asarray(gy))
            ax.plot(cx, cy, color="cyan", linewidth=0.4)
    epx, epy = to_px(*eye_xy)
    ax.plot(epx, epy, "r*", markersize=14, markeredgecolor="white")
    ax.set_title(title)
    ax.axis("off")
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def run_self_checks(scene, eyes, masks, obs, X, Y, eye_height=EYE_HEIGHT):
    print("\n" + "=" * 70)
    print("SELF-VERIFICATION")
    print("=" * 70)
    # 1. observer is not blind to its own surroundings. Require >=1 of the 8
    #    immediate neighbours visible (total blindness => sign/index bug); a
    #    building-adjacent observer can legitimately have some sides occluded,
    #    so don't demand all of them.
    eye = eyes[0]
    dirs = [(-1, 0), (1, 0), (0, 1), (0, -1),
            (-1, -1), (-1, 1), (1, -1), (1, 1)]
    near = np.column_stack([
        [eye[0] + dx for dx, dy in dirs],
        [eye[1] + dy for dx, dy in dirs],
        [scene.surface_z(eye[0] + dx, eye[1] + dy)[0] for dx, dy in dirs]])
    nvis = int(scene.visible_mask(eye, near).sum())
    check(nvis >= 1, "observer sees its immediate surroundings", f"{nvis}/8 neighbours")
    # 2. obs-obs symmetric (only when the graph was built)
    if obs is not None:
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
        rev = scene.is_visible((tx, ty, tz + eye_height),
                               (eyes[0][0], eyes[0][1], eyes[0][2]))
        check(fwd == rev, "forward/reverse LOS agree on a sample cell")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dem", type=Path, default=DEM_REGEN)
    p.add_argument("--footprints", type=Path, default=FOOTPRINTS)
    p.add_argument("--observers", type=Path, default=VIEWPOINTS)
    p.add_argument("--out-dir", type=Path,
                   default=ROOT / "200_Projects/220_BuildingsToDEM")
    p.add_argument("--margin", type=float, default=60.0,
                   help="core-window margin (m); ignored when --radius is set")
    p.add_argument("--chunk", type=int, default=200_000)
    p.add_argument("--eye-height", type=float, default=EYE_HEIGHT,
                   help=f"observer eye height (m) above surface "
                        f"(default {EYE_HEIGHT:g})")
    p.add_argument("--point", type=float, nargs=2, action="append",
                   metavar=("X", "Y"),
                   help="observer in the DEM CRS; repeatable; overrides --observers")
    p.add_argument("--ids", type=int, nargs="+",
                   help="select observers from --observers by their id field; "
                        "omit to run every point in the file")
    p.add_argument("--radius", type=float, default=None,
                   help="per-observer sight radius (m); window follows the point")
    p.add_argument("--azimuth", type=float, default=None,
                   help="view-cone center, compass degrees (0=N, 90=E, clockwise)")
    p.add_argument("--fov", type=float, default=360.0,
                   help="horizontal view-cone full width in degrees "
                        "(default 360 = omni)")
    p.add_argument("--pitch", type=float, default=0.0,
                   help="vertical view-cone center, elevation degrees "
                        "(0=horizontal, + up; default 0)")
    p.add_argument("--vfov", type=float, default=180.0,
                   help="vertical view-cone full width in degrees "
                        "(default 180 = unconstrained)")
    p.add_argument("--no-graph", action="store_true",
                   help="skip the visibility graph (useful for single/point runs)")
    p.add_argument("--volume", action="store_true",
                   help="also compute a 3D visibility volume (off by default)")
    p.add_argument("--voxel", type=float, default=2.0,
                   help="volume horizontal spacing (m); coarse default 2.0")
    p.add_argument("--zmin", type=float, default=0.0,
                   help="volume lowest sample height above ground (m)")
    p.add_argument("--zmax", type=float, default=30.0,
                   help="volume highest sample height above ground (m)")
    p.add_argument("--zstep", type=float, default=2.0,
                   help="volume vertical spacing (m); coarse default 2.0")
    p.add_argument("--volume-fullres", action="store_true",
                   help="sample the volume at native DEM resolution "
                        "(voxel = zstep = pixel size); can be very large")
    p.add_argument("--volume-format", nargs="+", default=["csv"],
                   choices=["csv", "ply", "npy", "las", "laz", "all"],
                   help="volume output format(s); default csv. las/laz are "
                        "point clouds for QGIS/CloudCompare (need laspy); "
                        "'all' = csv,ply,npy")
    p.add_argument("--domes", action="store_true",
                   help="bake dome caps into the ray-casting surface before "
                        "analysis (off by default; needs dome_inventory.csv "
                        "from scripts/build_dome_layer.py)")
    p.add_argument("--dome-inventory", type=Path, default=DOME_INVENTORY,
                   help="dome inventory CSV to use with --domes")
    args = p.parse_args()

    device = select_device()
    print("=" * 70)
    print(f"DEVICE: {device}   DEM: {Path(args.dem).name}"
          f"{'  + domes' if args.domes else ''}")
    print("=" * 70)

    dem, transform, crs, nodata, profile = load_dem(args.dem)
    check(crs is not None and crs.is_projected, "DEM CRS projected", str(crs))
    footprints = gpd.read_file(args.footprints).to_crs(crs)
    if args.domes:
        check(args.dome_inventory.exists(), "dome inventory exists",
              f"{args.dome_inventory} — run scripts/build_dome_layer.py first")
        if failures:
            sys.exit(1)
        dem, n_domes, n_cells = apply_dome_overlay(
            dem, transform, nodata, args.dome_inventory, footprints)
        print(f"  domes: {n_domes} chapels overlaid, {n_cells:,} cells raised")
    scene = HeightfieldScene(dem, transform, nodata, device)
    if args.point:
        observers = [(f"obs{i}", x, y) for i, (x, y) in enumerate(args.point, 1)]
        print(f"  observers: {len(observers)} inline --point(s)")
        if args.ids:
            warn("--ids ignored", "it applies to --observers, not --point")
    else:
        obs_list, has_id = load_observers(args.observers, crs)
        if args.ids:
            if not has_id:
                check(False, "--ids needs an 'id' field in the observers file",
                      str(args.observers))
                sys.exit(1)
            present = {o[0] for o in obs_list}
            missing = [i for i in args.ids if i not in present]
            if missing:
                check(False, "all requested --ids present", f"missing {missing}")
                sys.exit(1)
            want = set(args.ids)
            obs_list = [o for o in obs_list if o[0] in want]
            print(f"  observers: {len(obs_list)} selected by --ids {sorted(want)}")
        else:
            print(f"  observers: all {len(obs_list)} points from "
                  f"{Path(args.observers).name}")
        tag = "id" if has_id else "obs"
        observers = [(f"{tag}{oid}", x, y) for oid, x, y in obs_list]
    check(len(observers) >= 1, "at least one observer", f"{len(observers)}")

    labels = [o[0] for o in observers]
    eye_height = args.eye_height
    eyes = []
    for label, x, y in observers:
        sz = float(scene.surface_z(x, y)[0])
        if not np.isfinite(sz):
            warn("observer outside DEM / on nodata", f"{label} ({x:.1f}, {y:.1f})")
        eyes.append((x, y, sz + eye_height))
    for label, e in zip(labels, eyes):
        print(f"  {label}: ({e[0]:.1f}, {e[1]:.1f})  eye_z {e[2]:.2f} m")

    h_sector = args.azimuth is not None and args.fov < 360
    v_sector = args.vfov < 180
    sector = h_sector or v_sector
    mode = f"radius {args.radius:g} m" if args.radius else f"core window +{args.margin:g} m"
    if h_sector:
        mode += f"  |  az {args.azimuth:g}° fov {args.fov:g}°"
    if v_sector:
        mode += f"  |  pitch {args.pitch:g}° vfov {args.vfov:g}°"
    print(f"  mode: {mode}")

    # A shared analysis window exists only when no per-observer radius is set;
    # with a radius each observer gets its own window centered on the point, so
    # there is no common grid to stack into the combined/count layers.
    shared = args.radius is None
    if shared:
        base_win, _ = core_window(footprints.total_bounds, transform, margin=args.margin)
        gX, gY, gZ, gshape, _ = target_grid(scene, base_win, transform)
        print(f"  analysis window: {gshape[0]}x{gshape[1]} px "
              f"({gshape[0]*gshape[1]:,} cells)")

    print("\n" + "=" * 70)
    print("PER-OBSERVER VIEWSHEDS")
    print("=" * 70)
    sect_bits = []
    if h_sector:
        sect_bits.append(f"az {args.azimuth:g}° fov {args.fov:g}°")
    if v_sector:
        sect_bits.append(f"pitch {args.pitch:g}° vfov {args.vfov:g}°")
    sect_txt = f" — {', '.join(sect_bits)}" if sect_bits else ""
    args.out_dir.mkdir(parents=True, exist_ok=True)
    masks, grids = [], []
    for label, eye in zip(labels, eyes):
        if shared:
            win, X, Y, Z, shape = base_win, gX, gY, gZ, gshape
        else:
            win = observer_window(scene, eye[:2], args.radius)
            X, Y, Z, shape, _ = target_grid(scene, win, transform)
        mask = compute_viewshed(scene, eye, X, Y, Z, shape)
        apply_view_constraints(mask, X, Y, Z, eye, args.azimuth, args.fov,
                               args.radius, args.pitch, args.vfov)
        masks.append(mask)
        grids.append((X, Y, shape))
        n_vis = int((mask == 1).sum())
        print(f"  {label}: {n_vis:,} visible of {shape[0]*shape[1]:,} window cells")
        write_viewshed_tif(mask, win, transform, crs, profile,
                           args.out_dir / f"viewshed_{label}.tif")
        qc_png(scene, mask, win, transform, footprints, eye[:2],
               f"Viewshed — point {label}{sect_txt}",
               args.out_dir / f"viewshed_{label}.png",
               azimuth=args.azimuth, fov=args.fov, radius=args.radius)

    if shared and len(eyes) > 1:
        stack = np.stack(masks)
        valid = stack != 255
        combined = np.where(valid.any(0),
                            (stack == 1).any(0).astype(np.uint8), 255).astype(np.uint8)
        count = np.where(valid.any(0),
                         (stack == 1).sum(0).astype(np.uint8), 255).astype(np.uint8)
        write_viewshed_tif(combined, base_win, transform, crs, profile,
                           args.out_dir / "viewshed_combined_max.tif")
        write_viewshed_tif(count, base_win, transform, crs, profile,
                           args.out_dir / "viewshed_count.tif")
        qc_png(scene, combined, base_win, transform, footprints, eyes[0][:2],
               "Viewshed — combined (any observer)",
               args.out_dir / "viewshed_combined.png",
               azimuth=args.azimuth, fov=args.fov, radius=args.radius)
    elif not shared:
        print("  (radius mode: per-observer windows differ — no combined/count layer)")

    if args.volume:
        print("\n" + "=" * 70)
        print("3D VISIBILITY VOLUME")
        print("=" * 70)
        voxel = scene.px if args.volume_fullres else args.voxel
        zstep = scene.px if args.volume_fullres else args.zstep
        levels = np.arange(args.zmin, args.zmax + zstep / 2, zstep)
        formats = ({"csv", "ply", "npy"} if "all" in args.volume_format
                   else set(args.volume_format))
        print(f"  voxel {voxel:g} m  |  z {args.zmin:g}..{args.zmax:g} m "
              f"step {zstep:g} ({len(levels)} levels)  |  formats "
              f"{','.join(sorted(formats))}")
        combine = shared and len(eyes) > 1
        count = None
        base = None
        for label, eye in zip(labels, eyes):
            xs, ys = volume_extent(scene, eye[:2], args.radius, voxel)
            npts = len(xs) * len(ys) * len(levels)
            if npts > 5_000_000:
                warn("large volume", f"{label}: {npts:,} sample points")
            ground, vis = compute_volume(
                scene, eye, xs, ys, levels, args.azimuth, args.fov,
                args.radius, args.pitch, args.vfov, args.chunk)
            nvis = int(vis.sum())
            print(f"  {label}: {nvis:,} visible voxels of {npts:,}")
            pts, _ = volume_points(xs, ys, levels, ground, vis)
            stem = args.out_dir / f"viewshed_volume_{label}"
            if "csv" in formats:
                write_volume_csv(stem.with_suffix(".csv"), pts)
            if "ply" in formats:
                write_volume_ply(stem.with_suffix(".ply"), pts)
            if "npy" in formats:
                write_volume_npy(stem.with_suffix(".npy"), vis, xs, ys, levels, crs)
            if "las" in formats:
                write_volume_las(stem.with_suffix(".las"), pts, crs)
            if "laz" in formats:
                write_volume_las(stem.with_suffix(".laz"), pts, crs)
            volume_qc_png(xs, ys, levels, ground, vis, footprints, eye[:2],
                          f"Volume — point {label}{sect_txt}",
                          stem.with_suffix(".png"))
            if combine:
                if count is None:
                    count = vis.astype(np.int32)
                    base = (xs, ys, ground)
                else:
                    count += vis
        if combine and count is not None:
            xs, ys, ground = base
            cvis = count > 0
            pts, (r, c, lv) = volume_points(xs, ys, levels, ground, cvis)
            cvals = count[r, c, lv]
            stem = args.out_dir / "viewshed_volume_combined"
            if "csv" in formats:
                write_volume_csv(stem.with_suffix(".csv"), pts, cvals)
            if "ply" in formats:
                write_volume_ply(stem.with_suffix(".ply"), pts, cvals)
            if "npy" in formats:
                write_volume_npy(stem.with_suffix(".npy"), count, xs, ys, levels, crs)
            if "las" in formats:
                write_volume_las(stem.with_suffix(".las"), pts, crs, cvals)
            if "laz" in formats:
                write_volume_las(stem.with_suffix(".laz"), pts, crs, cvals)
            print(f"  combined: {int(cvis.sum()):,} voxels visible to "
                  f"≥1 observer")

    obs = None
    if not args.no_graph:
        print("\n" + "=" * 70)
        print("VISIBILITY GRAPH")
        print("=" * 70)
        obs, edges = build_viewgraph(scene, eyes, footprints, labels)
        gdf = gpd.GeoDataFrame(edges, crs=crs)
        gdf.to_file(args.out_dir / "viewgraph_edges.geojson", driver="GeoJSON")
        gdf.drop(columns="geometry").to_csv(
            args.out_dir / "viewgraph_edges.csv", index=False)
        pd.DataFrame(obs.astype(int), index=labels, columns=labels).to_csv(
            args.out_dir / "viewgraph_obs_obs.csv")
        b = gdf[gdf.dst_type == "building"]
        print(f"  observer-observer visible pairs:\n{obs.astype(int)}")
        print(f"  building edges: {len(b)}  visible(centroid): "
              f"{int(b.visible.sum())}  visible(any vertex): "
              f"{int(b.visible_any_vertex.sum())}")
        if sector:
            print("  (graph LOS is omnidirectional; --azimuth/--fov/--pitch/"
                  "--vfov apply to rasters and volume only)")

    X0, Y0, _ = grids[0]
    run_self_checks(scene, eyes, masks, obs, X0, Y0, eye_height)

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
    print(f"Viewshed engine complete; outputs in {loc}")


if __name__ == "__main__":
    main()
