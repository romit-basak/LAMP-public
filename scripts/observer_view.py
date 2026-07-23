"""First-person observer-view snapshots from the ray-casting engine.

Renders "what the observer actually sees": equirectangular panoramas
(compass azimuth across the image, elevation up it) and pinhole
perspective views, computed by the same HeightfieldScene ray-march that
produces the viewsheds (scripts/viewshed.py) — so every pixel is
auditable against the visibility products, and --domes / --eye-height
behave identically.

Three shadings are derived from one march per view:
    natural  sand-tinted hillshade at the hit (optional orthophoto
             drape, distance fog), sky gradient — the human-readable one
    depth    slant range to the hit, viridis + colorbar — the
             measurable one
    ids      building footprints colored by ID over gray ground —
             answers "which chapel is that?"
Other observers are drawn as markers: filled green when the viewgraph
LOS test reports them visible from this eye, hollow red when occluded —
a direct visual audit of the viewgraph_obs_obs edges.

Run (all Marks_Brief2 observers, full 360-degree pano, all modes):
    .venv/bin/python scripts/observer_view.py
One observer, eastward perspective, no panorama:
    .venv/bin/python scripts/observer_view.py --ids 1 --persp 90 --no-pano
With dome caps baked into the surface:
    .venv/bin/python scripts/observer_view.py --domes

Cost: the default pano is 1440x160 px = ~230k rays marched at 0.2 m —
the same order as one per-observer viewshed over the shared core window;
per-ray exit/ceiling bounds retire sky rays early, so it usually runs
faster. --pano-width, --max-range and --step-scale trade fidelity for
speed (audit-grade = the defaults).

Exits nonzero if any self-check fails.
"""

import os

# Set before torch is imported: lets ops that lack an MPS kernel fall back to
# CPU instead of raising, so the same code path runs on Apple Silicon and CUDA.
os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

import argparse
import math
import sys
import time
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
import numpy as np
import rasterio
from matplotlib.cm import ScalarMappable
from matplotlib.colors import LogNorm, Normalize, hsv_to_rgb
from matplotlib.ticker import FuncFormatter
from rasterio.features import rasterize

from sanity_checks import (ROOT, DEM_REGEN, FOOTPRINTS, VIEWPOINTS,
                           DOME_INVENTORY, check, warn, failures)
from viewshed import (EYE_HEIGHT, STEP, HeightfieldScene, select_device,
                      load_dem, load_observers, apply_dome_overlay)
from build_dem_with_buildings import hillshade
from build_dome_layer import ORTHO

SAND = np.array([0.86, 0.77, 0.62])          # ground tint, natural mode
SKY_HORIZON = np.array([0.87, 0.90, 0.94])
SKY_ZENITH = np.array([0.45, 0.62, 0.87])
ELEV_LIMIT = 85.0     # rays are parameterized by horizontal distance, which
                      # degenerates at +/-90 deg (tan -> inf); stay inside


def clamp_elev(lo, hi):
    if lo < -ELEV_LIMIT or hi > ELEV_LIMIT:
        warn("elevation span clipped",
             f"{lo:g}..{hi:g} deg -> +/-{ELEV_LIMIT:g} deg")
    return max(lo, -ELEV_LIMIT), min(hi, ELEV_LIMIT)


def pano_rays(azimuth, fov, pitch, vfov, width):
    """Equirectangular ray grid: columns sweep compass azimuth left to
    right (full 0..360 when azimuth is None, else centered on it), rows
    sweep elevation with the top row highest. Square angular pixels, so
    the height follows from width * vspan / fov. Returns (ux, uy, slope)
    as [H, W] arrays plus the degree extent (az_lo, az_hi, el_lo, el_hi)."""
    if azimuth is None:
        if fov < 360:
            warn("--fov without --azimuth ignored", "rendering full 360")
        az_lo, fov = 0.0, 360.0
    else:
        az_lo = azimuth - fov / 2
    az_hi = az_lo + fov
    el_lo, el_hi = clamp_elev(pitch - vfov / 2, pitch + vfov / 2)
    height = max(int(round(width * (el_hi - el_lo) / fov)), 2)
    az = np.deg2rad(az_lo + (np.arange(width) + 0.5) * fov / width)
    el = np.deg2rad(el_hi - (np.arange(height) + 0.5)
                    * (el_hi - el_lo) / height)
    ux = np.broadcast_to(np.sin(az)[None, :], (height, width))
    uy = np.broadcast_to(np.cos(az)[None, :], (height, width))
    slope = np.broadcast_to(np.tan(el)[:, None], (height, width))
    return ux, uy, slope, (az_lo, az_hi, el_lo, el_hi)


def persp_basis(azimuth, pitch):
    """Camera basis in (E, N, U): forward toward compass `azimuth` at
    elevation `pitch`, right 90 deg clockwise of it, up completing the
    frame (E = sin az, N = cos az — the project's compass convention)."""
    azr, pr = math.radians(azimuth), math.radians(pitch)
    fwd = np.array([math.sin(azr) * math.cos(pr),
                    math.cos(azr) * math.cos(pr),
                    math.sin(pr)])
    right = np.array([math.cos(azr), -math.sin(azr), 0.0])
    up = np.cross(right, fwd)
    return fwd, right, up


def persp_rays(azimuth, pitch, hfov, width, height):
    """Pinhole ray grid pointing at (azimuth, pitch); the vertical fov
    follows from the aspect ratio (square pixels on the image plane).
    Returns (ux, uy, slope) as [H, W] arrays."""
    fwd, right, up = persp_basis(azimuth, pitch)
    tan_h = math.tan(math.radians(hfov) / 2)
    tan_v = tan_h * height / width
    xs = ((np.arange(width) + 0.5) / width * 2 - 1) * tan_h
    ys = (1 - (np.arange(height) + 0.5) / height * 2) * tan_v
    d = (fwd[None, None, :]
         + xs[None, :, None] * right[None, None, :]
         + ys[:, None, None] * up[None, None, :])
    horiz = np.hypot(d[..., 0], d[..., 1])
    ok = horiz > 1e-12
    ux = np.divide(d[..., 0], horiz, out=np.zeros_like(horiz), where=ok)
    uy = np.divide(d[..., 1], horiz, out=np.ones_like(horiz), where=ok)
    slope = np.divide(d[..., 2], horiz, out=np.zeros_like(horiz), where=ok)
    lim = math.tan(math.radians(ELEV_LIMIT))
    n_clip = int((np.abs(slope) > lim).sum() + (~ok).sum())
    if n_clip:
        warn("perspective pixels clipped to +/-85 deg elevation",
             f"{n_clip} px — lower --pitch or widen the image")
        slope = np.clip(slope, -lim, lim)
    return ux, uy, slope


def render_hits(scene, eye, ux, uy, slope, max_range, chunk):
    """March one ray grid. Returns per-pixel [H, W] fields: d (horizontal
    distance, inf = sky), r (slant range), hx/hy (world hit coords, NaN
    on sky), hit mask, elevation angle (deg) and the ray directions."""
    shape = ux.shape
    d = scene.first_hit(eye, ux.ravel(), uy.ravel(), slope.ravel(),
                        max_range=max_range, chunk=chunk).reshape(shape)
    hit = np.isfinite(d)
    d_safe = np.where(hit, d, 0.0)
    r = d * np.sqrt(1.0 + np.asarray(slope) ** 2)     # slant range
    hx = np.where(hit, eye[0] + ux * d_safe, np.nan)
    hy = np.where(hit, eye[1] + uy * d_safe, np.nan)
    elev = np.degrees(np.arctan(slope))
    return {"d": d, "r": r, "hx": hx, "hy": hy, "hit": hit,
            "elev": elev, "ux": ux, "uy": uy}


def sample_grid(grid, scene, x, y, nearest=False):
    """Sample a raster on the scene's grid at world points (host numpy).
    Bilinear by default; nearest for categorical grids (returns 0
    outside, NaN outside for bilinear)."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    bad = ~(np.isfinite(x) & np.isfinite(y))
    col_f, row_f = scene._pix(np.where(bad, scene.x0, x),
                              np.where(bad, scene.y0, y))
    if nearest:
        c = np.round(col_f).astype(int)
        r = np.round(row_f).astype(int)
        inb = (c >= 0) & (c < scene.W) & (r >= 0) & (r < scene.H) & ~bad
        cc = np.clip(c, 0, scene.W - 1)
        rc = np.clip(r, 0, scene.H - 1)
        return np.where(inb, grid[rc, cc], 0)
    c0 = np.floor(col_f).astype(int)
    r0 = np.floor(row_f).astype(int)
    wc, wr = col_f - c0, row_f - r0
    inb = (c0 >= 0) & (c0 < scene.W - 1) & (r0 >= 0) & (r0 < scene.H - 1)
    c0c = np.clip(c0, 0, scene.W - 2)
    r0c = np.clip(r0, 0, scene.H - 2)
    top = grid[r0c, c0c] * (1 - wc) + grid[r0c, c0c + 1] * wc
    bot = grid[r0c + 1, c0c] * (1 - wc) + grid[r0c + 1, c0c + 1] * wc
    val = top * (1 - wr) + bot * wr
    return np.where(inb & ~bad, val, np.nan)


def shade_natural(res, scene, hs_grid, ortho_grid, fog_len):
    """Human-readable render: sand-tinted hillshade at the hit (vertical
    DEM walls keep the azimuth-dependent Lambert term, so facades shade
    by facing), optional orthophoto drape, exponential distance fog
    toward the horizon color, sky by elevation gradient."""
    t = np.clip(res["elev"] / 90.0, 0.0, 1.0)[..., None]
    img = (SKY_HORIZON[None, None, :] * (1 - t)
           + SKY_ZENITH[None, None, :] * t).astype(np.float32)
    hit = res["hit"]
    if hit.any():
        shade = sample_grid(hs_grid, scene, res["hx"][hit], res["hy"][hit])
        shade = np.where(np.isfinite(shade), shade, 0.5)
        tone = shade
        if ortho_grid is not None:
            o = sample_grid(ortho_grid, scene, res["hx"][hit], res["hy"][hit])
            o = np.where(np.isfinite(o), o, 0.6)
            tone = shade * (0.35 + 0.65 * o)
        ground = SAND[None, :] * (0.15 + 0.85 * tone[:, None])
        if fog_len > 0:
            f = (1.0 - np.exp(-res["r"][hit] / fog_len))[:, None]
            ground = ground * (1 - f) + SKY_HORIZON[None, :] * f
        img[hit] = ground
    return np.clip(img, 0.0, 1.0)


def shade_depth(res):
    """Slant range to the first hit -> viridis; sky = light gray.
    Log-scaled: ranges span ~3 m (own feet) to ~1 km (street gaps), so a
    linear ramp would leave everything nearby a single dark shade.
    Returns (rgb, norm) so the caller can attach a matching colorbar."""
    rgb = np.full(res["d"].shape + (3,), 0.92, dtype=np.float32)
    hit = res["hit"]
    norm = Normalize(0.0, 1.0)
    if hit.any():
        r = res["r"][hit]
        norm = LogNorm(vmin=max(float(r.min()), 0.1), vmax=float(r.max()))
        rgb[hit] = plt.get_cmap("viridis")(norm(r))[:, :3]
    return rgb, norm


def shade_ids(res, scene, id_grid, hs_grid):
    """Footprint-ID color at the hit over gray hillshade ground. The
    sample point is nudged half a pixel *along* the ray so wall hits on
    a building's bilinear ramp read the building, not the ground cell
    just in front of it. Returns (rgb, {ID: pixel share})."""
    h, w = res["d"].shape
    rgb = np.full((h, w, 3), 0.92, dtype=np.float32)
    hit = res["hit"]
    if not hit.any():
        return rgb, {}
    nx = res["hx"][hit] + np.asarray(res["ux"])[hit] * scene.px * 0.5
    ny = res["hy"][hit] + np.asarray(res["uy"])[hit] * scene.px * 0.5
    bid = sample_grid(id_grid, scene, nx, ny, nearest=True)
    shade = sample_grid(hs_grid, scene, res["hx"][hit], res["hy"][hit])
    shade = np.where(np.isfinite(shade), shade, 0.5)
    # Golden-ratio hue cycling: neighbouring IDs land far apart on the
    # wheel, and a fixed saturation keeps every building color distinct
    # from the gray terrain (tab20's gray entries collide with it).
    hue = (bid * 0.6180339887) % 1.0
    colors = hsv_to_rgb(np.stack([hue, np.full_like(hue, 0.65),
                                  np.full_like(hue, 0.95)], axis=-1))
    ground = np.repeat((0.35 + 0.55 * shade)[:, None], 3, axis=1)
    lit = colors * (0.55 + 0.45 * shade[:, None])
    rgb[hit] = np.where((bid > 0)[:, None], lit, ground)
    ids, cnt = np.unique(bid[bid > 0], return_counts=True)
    share = {int(i): float(c) / res["d"].size for i, c in zip(ids, cnt)}
    return rgb, share


def observer_marks(scene, eye, label, eyes, labels):
    """Bearing/elevation of every *other* observer from this eye plus
    the same eye-point LOS test build_viewgraph runs, so the markers
    audit the viewgraph_obs_obs edges one-to-one."""
    others = [(l, e) for l, e in zip(labels, eyes) if l != label]
    if not others:
        return []
    vis = scene.visible_mask(eye, np.array([e for _, e in others]))
    marks = []
    for (l, e), v in zip(others, vis):
        dx, dy = e[0] - eye[0], e[1] - eye[1]
        dist = math.hypot(dx, dy)
        if dist < scene.d_min:
            continue
        marks.append({"label": l, "visible": bool(v), "dist": dist,
                      "bearing": math.degrees(math.atan2(dx, dy)) % 360,
                      "elev": math.degrees(math.atan2(e[2] - eye[2], dist))})
    return marks


def persp_mark_xy(mark, azimuth, pitch, hfov, width, height):
    """Project a mark's (bearing, elev) direction through the pinhole
    camera; returns image (x, y) in pixels or None when out of frame."""
    fwd, right, up = persp_basis(azimuth, pitch)
    b = math.radians(mark["bearing"])
    el = math.radians(mark["elev"])
    v = np.array([math.sin(b) * math.cos(el),
                  math.cos(b) * math.cos(el),
                  math.sin(el)])
    depth = float(v @ fwd)
    if depth <= 1e-9:                      # behind the camera
        return None
    tan_h = math.tan(math.radians(hfov) / 2)
    tan_v = tan_h * height / width
    x = float(v @ right) / depth / tan_h   # [-1, 1] across the frame
    y = float(v @ up) / depth / tan_v
    if not (-1 <= x <= 1 and -1 <= y <= 1):
        return None
    return (x + 1) / 2 * width, (1 - y) / 2 * height


def save_view_png(path, rgb, title, marks, extent=None, colorbar=None,
                  xlabel="", ylabel=""):
    h, w = rgb.shape[:2]
    fig_w = 15.0
    fig, ax = plt.subplots(figsize=(fig_w, max(fig_w * h / w + 1.2, 3.0)),
                           dpi=150)
    ax.imshow(rgb, extent=extent, interpolation="nearest")
    if extent is not None:
        # Compass labels fold past-360 ticks (a pano centered near north
        # spans the 0/360 seam).
        ax.xaxis.set_major_formatter(
            FuncFormatter(lambda v, _: f"{v % 360:g}"))
    for m in marks:
        mfc = "lime" if m["visible"] else "none"
        mec = "black" if m["visible"] else "red"
        ax.plot(m["x"], m["y"], "o", markersize=9, markerfacecolor=mfc,
                markeredgecolor=mec, markeredgewidth=1.8)
        ax.annotate(m["label"], (m["x"], m["y"]), xytext=(5, 7),
                    textcoords="offset points", fontsize=9, color="white",
                    path_effects=[pe.withStroke(linewidth=2,
                                                foreground="black")])
    if colorbar is not None:
        cmap, norm, clabel = colorbar
        fig.colorbar(ScalarMappable(norm=norm, cmap=cmap), ax=ax,
                     fraction=0.03, pad=0.02, label=clabel)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def view_stats(name, res, share, seconds):
    hitf = float(res["hit"].mean())
    line = f"  {name}: {hitf:.1%} surface"
    r = res["r"][res["hit"]]
    if r.size:
        line += (f", slant {r.min():.1f}/{np.median(r):.1f}/"
                 f"{r.max():.1f} m (min/med/max)")
    print(line + f"  [{seconds:.1f} s]")
    if share:
        tops = [(i, s) for i, s in sorted(share.items(), key=lambda kv: -kv[1])
                if s >= 0.001][:8]
        if tops:
            print("    buildings in view: "
                  + ", ".join(f"ID {i} {s:.1%}" for i, s in tops))


def run_self_checks(scene, views):
    """Physical invariants over the rendered views. No ground truth
    exists, so the load-bearing check cross-validates first_hit against
    visible_mask — the kernel the viewshed products were validated with."""
    print("\n" + "=" * 70)
    print("SELF-VERIFICATION")
    print("=" * 70)
    diag = math.hypot(scene.W * scene.px, scene.H * scene.px)
    for v in views:
        res = v["res"]
        d = res["d"][res["hit"]]
        check(d.size > 0, f"{v['name']}: some rays hit the surface")
        if d.size:
            check(float(d.min()) > 0
                  and float(d.max()) <= diag + scene.step,
                  f"{v['name']}: hit distances inside the DEM",
                  f"{d.min():.2f}..{d.max():.1f} m (diag {diag:.0f} m)")
        check(v["finite"], f"{v['name']}: rendered images all finite")
        if v["min_elev"] <= -10.0:
            bot = res["hit"][-1]
            frac = float(bot.mean())
            med = float(np.median(res["r"][-1][bot])) if bot.any() else np.inf
            # Eye 1.5 m above ground looking 10+ deg down meets the
            # surface within a few m on any plausible slope.
            check(frac >= 0.95 and med < 30.0,
                  f"{v['name']}: steepest-down rays grounded nearby",
                  f"{frac:.0%} hit, median slant {med:.1f} m")
        top_hit = float(res["hit"][0].mean())
        if top_hit > 0.5:
            warn(f"{v['name']}: top row mostly surface",
                 f"{top_hit:.0%} hit — observer may face a wall")

    # Cross-validation: every first-hit point is, by construction, the
    # first visible surface along its ray, so visible_mask must agree it
    # is visible. Tolerance: a silhouette-edge hit sits on the bilinear
    # wall ramp where the two kernels' final samples can differ by up to
    # one march step, flipping the boundary comparison.
    v = max(views, key=lambda v: int(v["res"]["hit"].sum()))
    res = v["res"]
    idx = np.flatnonzero(res["hit"].ravel())
    if idx.size:
        rng = np.random.default_rng(0)
        sel = rng.choice(idx, size=min(2000, idx.size), replace=False)
        hx = res["hx"].ravel()[sel]
        hy = res["hy"].ravel()[sel]
        hz = scene.surface_z(hx, hy)
        ok = np.isfinite(hz)
        vis = scene.visible_mask(v["eye"],
                                 np.column_stack([hx[ok], hy[ok], hz[ok]]))
        frac = float(vis.mean()) if len(vis) else 1.0
        check(frac >= 0.98,
              "first-hit points visible to the validated LOS kernel",
              f"{frac:.2%} of {len(vis)} sampled hits ({v['name']})")
        if frac < 0.995:
            warn("hit/LOS agreement below 99.5%", f"{frac:.3%}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dem", type=Path, default=DEM_REGEN)
    p.add_argument("--footprints", type=Path, default=FOOTPRINTS)
    p.add_argument("--observers", type=Path, default=VIEWPOINTS)
    p.add_argument("--out-dir", type=Path,
                   default=ROOT / "200_Projects/220_BuildingsToDEM/observer_views")
    p.add_argument("--eye-height", type=float, default=EYE_HEIGHT,
                   help=f"observer eye height (m) above surface "
                        f"(default {EYE_HEIGHT:g})")
    p.add_argument("--point", type=float, nargs=2, action="append",
                   metavar=("X", "Y"),
                   help="observer in the DEM CRS; repeatable; overrides --observers")
    p.add_argument("--ids", type=int, nargs="+",
                   help="select observers from --observers by their id field; "
                        "omit to run every point in the file")
    p.add_argument("--azimuth", type=float, default=None,
                   help="pano center, compass degrees (0=N, 90=E, clockwise); "
                        "omit for a full 360 panorama")
    p.add_argument("--fov", type=float, default=360.0,
                   help="pano horizontal span in degrees (with --azimuth; "
                        "default 360)")
    p.add_argument("--pitch", type=float, default=0.0,
                   help="view center elevation angle, degrees "
                        "(0=horizontal, + up; default 0)")
    p.add_argument("--vfov", type=float, default=40.0,
                   help="pano vertical span in degrees — the image's angular "
                        "height, unlike viewshed.py's cone filter "
                        "(default 40 = +/-20 around --pitch)")
    p.add_argument("--pano-width", type=int, default=1440,
                   help="panorama width in px (default 1440 = 0.25 deg/px "
                        "over 360)")
    p.add_argument("--no-pano", action="store_true",
                   help="skip the panorama (e.g. perspective-only runs)")
    p.add_argument("--persp", type=float, nargs="+", metavar="AZ",
                   help="also render pinhole perspective view(s) at these "
                        "compass azimuths")
    p.add_argument("--persp-fov", type=float, default=60.0,
                   help="perspective horizontal fov in degrees (default 60)")
    p.add_argument("--persp-size", type=int, nargs=2, default=[720, 480],
                   metavar=("W", "H"),
                   help="perspective image size in px (default 720 480)")
    p.add_argument("--modes", nargs="+", default=["natural", "depth", "ids"],
                   choices=["natural", "depth", "ids"],
                   help="shading modes to render (default: all three)")
    p.add_argument("--ortho", type=Path, default=ORTHO,
                   help="orthophoto to drape in natural mode (must share "
                        "the DEM grid)")
    p.add_argument("--no-ortho", action="store_true",
                   help="natural mode without the orthophoto drape")
    p.add_argument("--fog", type=float, default=300.0,
                   help="exponential distance-fog length (m) in natural "
                        "mode; 0 disables (default 300)")
    p.add_argument("--max-range", type=float, default=None,
                   help="stop rays at this distance (m); default marches "
                        "to the DEM edge")
    p.add_argument("--step-scale", type=float, default=1.0,
                   help="march-step multiplier; >1 is faster but can step "
                        "over thin walls (audit-grade = 1.0)")
    p.add_argument("--no-markers", action="store_true",
                   help="skip the other-observer visibility markers")
    p.add_argument("--chunk", type=int, default=200_000,
                   help="ray-batch size for the march (memory guard)")
    p.add_argument("--domes", action="store_true",
                   help="bake dome caps into the surface before rendering "
                        "(off by default; needs dome_inventory.csv)")
    p.add_argument("--dome-inventory", type=Path, default=DOME_INVENTORY,
                   help="dome inventory CSV to use with --domes")
    args = p.parse_args()

    device = select_device()
    print("=" * 70)
    print(f"DEVICE: {device}   DEM: {Path(args.dem).name}"
          f"{'  + domes' if args.domes else ''}")
    print("=" * 70)

    dem, transform, crs, nodata, _ = load_dem(args.dem)
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
    if args.step_scale > 1.0:
        warn("march step coarsened", f"x{args.step_scale:g} — thin walls "
             "may be stepped over; use 1.0 for audit-grade output")
    scene = HeightfieldScene(dem, transform, nodata, device,
                             step=STEP * args.step_scale)

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
    check(not args.no_pano or bool(args.persp), "something to render",
          "--no-pano without --persp leaves no views")
    if failures:
        sys.exit(1)

    labels = [o[0] for o in observers]
    eyes = []
    for label, x, y in observers:
        sz = float(scene.surface_z(x, y)[0])
        if not np.isfinite(sz):
            warn("observer outside DEM / on nodata", f"{label} ({x:.1f}, {y:.1f})")
        eyes.append((x, y, sz + args.eye_height))
    for label, e in zip(labels, eyes):
        print(f"  {label}: ({e[0]:.1f}, {e[1]:.1f})  eye_z {e[2]:.2f} m")

    modes = list(dict.fromkeys(args.modes))
    # One full-grid hillshade serves every view (and doubles as facade
    # shading: on a vertical DEM wall it degenerates to the
    # azimuth-dependent Lambert term).
    hs_grid = hillshade(scene.dem_np, scene.px).astype(np.float32)
    ortho_grid = None
    if "natural" in modes and not args.no_ortho:
        if args.ortho.exists():
            with rasterio.open(args.ortho) as src:
                same = ((src.height, src.width) == (scene.H, scene.W)
                        and np.allclose(tuple(src.transform)[:6],
                                        tuple(transform)[:6]))
                if same:
                    o = src.read(1).astype(np.float32)
                    ov = o[o != src.nodata] if src.nodata is not None else o
                    lo, hi = np.percentile(ov, [2, 98])
                    ortho_grid = np.clip((o - lo) / max(hi - lo, 1e-6), 0, 1)
                else:
                    warn("orthophoto grid differs from DEM; drape skipped",
                         str(args.ortho))
        else:
            warn("orthophoto not found; hillshade-only natural mode",
                 str(args.ortho))
    id_grid = None
    if "ids" in modes:
        id_grid = rasterize(
            [(geom, int(bid)) for geom, bid
             in zip(footprints.geometry, footprints["ID"])],
            out_shape=(scene.H, scene.W), transform=transform,
            fill=0, dtype="int32")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    dome_sfx = "_domes" if args.domes else ""
    eye_txt = f"eye {args.eye_height:g} m{' + domes' if args.domes else ''}"
    views = []
    print("\n" + "=" * 70)
    print("OBSERVER VIEWS")
    print("=" * 70)
    for label, eye in zip(labels, eyes):
        marks = ([] if args.no_markers
                 else observer_marks(scene, eye, label, eyes, labels))
        jobs = []
        if not args.no_pano:
            jobs.append(("pano", None,
                         *pano_rays(args.azimuth, args.fov, args.pitch,
                                    args.vfov, args.pano_width)))
        for az in (args.persp or []):
            w, h = args.persp_size
            jobs.append(("persp", az,
                         *persp_rays(az, args.pitch, args.persp_fov, w, h),
                         None))
        for kind, az, ux, uy, slope, extent in jobs:
            t0 = time.perf_counter()
            res = render_hits(scene, eye, ux, uy, slope,
                              args.max_range, args.chunk)
            dt = time.perf_counter() - t0
            h, w = res["d"].shape

            if kind == "pano":
                az_lo, az_hi, el_lo, el_hi = extent
                mlist = []
                for m in marks:
                    mx = az_lo + ((m["bearing"] - az_lo) % 360)
                    if mx <= az_hi and el_lo <= m["elev"] <= el_hi:
                        mlist.append({**m, "x": mx, "y": m["elev"]})
                name = f"{label} pano"
                title = (f"{label} — panorama az {az_lo:g}..{az_hi:g} deg, "
                         f"elev {el_lo:g}..{el_hi:g} deg, {eye_txt}")
                xlabel, ylabel = "compass azimuth (deg)", "elevation (deg)"
            else:
                mlist = []
                for m in marks:
                    xy = persp_mark_xy(m, az, args.pitch, args.persp_fov, w, h)
                    if xy is not None:
                        mlist.append({**m, "x": xy[0], "y": xy[1]})
                name = f"{label} persp az{az:g}"
                title = (f"{label} — perspective az {az:g} deg, "
                         f"fov {args.persp_fov:g} deg, pitch {args.pitch:g} "
                         f"deg, {eye_txt}")
                xlabel = ylabel = ""

            share = None
            finite = True
            for mode in modes:
                colorbar = None
                if mode == "natural":
                    rgb = shade_natural(res, scene, hs_grid, ortho_grid,
                                        args.fog)
                elif mode == "depth":
                    rgb, norm = shade_depth(res)
                    colorbar = ("viridis", norm, "slant range (m)")
                else:
                    rgb, share = shade_ids(res, scene, id_grid, hs_grid)
                finite &= bool(np.isfinite(rgb).all())
                if kind == "pano":
                    fname = f"view_{label}_pano_{mode}{dome_sfx}.png"
                else:
                    fname = (f"view_{label}_persp{int(round(az)) % 360:03d}"
                             f"_{mode}{dome_sfx}.png")
                save_view_png(args.out_dir / fname, rgb,
                              f"{title} — {mode}", mlist, extent=extent,
                              colorbar=colorbar, xlabel=xlabel, ylabel=ylabel)
            view_stats(name, res, share, dt)
            views.append({"name": name, "res": res, "eye": eye,
                          "finite": finite,
                          "min_elev": float(np.min(res["elev"]))})

    run_self_checks(scene, views)

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
    n_png = len(views) * len(modes)
    print(f"Observer views complete; {n_png} images in {loc}")


if __name__ == "__main__":
    main()
