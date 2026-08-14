"""Aperture-capable hybrid scene: heightfield terrain + triangle meshes.

Step 2 of the build order. A heightfield stores one elevation per (x, y)
column, so it cannot represent a wall that is solid, then open (a door
or window), then solid again along the same vertical line. Buildings
that need real openings are therefore modeled as explicit triangle
meshes (OBJ), and `HybridScene` composes them with the validated
`HeightfieldScene`:

    visible_mask : heightfield result AND segment-clear-of-triangles
    first_hit    : elementwise min of the two hit distances
    surface_z    : heightfield only (eye placement / grid semantics
                   unchanged; mesh-aware standing surfaces are a noted
                   deferred refinement)

The composition wraps an already-constructed HeightfieldScene rather
than subclassing it, so the r.viewshed-validated kernel in viewshed.py
is never touched — runs without --mesh are byte-identical to before.

Intersection is batched two-sided Moller-Trumbore in torch (same
device-agnostic cuda->mps->cpu stack as the heightfield march; no mesh
library, per the project's dependency-light stance). Triangles are
translated to eye-relative coordinates in float64 on the host before
the float32 upload — the same precision idiom as the heightfield's
eye-anchored march (UTM magnitudes ~1e6 overwhelm float32). The
many-observer path cannot use a single eye as that anchor, so it
translates to a frame local to the ray bundle instead.

Run as a script, this executes the aperture self-checks against the
synthetic assets from make_test_building.py:

    .venv/bin/python scripts/scene3d.py \
        --assets viewshed_runs/synthetic_building/assets
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

import numpy as np
import torch

from sanity_checks import check, failures
from volume_mesh import load_obj, check_soup

# Barycentric tolerance: accept hits marginally outside an edge so a ray
# crossing exactly on the shared edge of two adjacent triangles cannot
# slip through the crack between them.
EPS_BARY = 1e-6
# Determinant cutoff below which a ray is treated as parallel to the
# triangle plane (no usable intersection).
EPS_DET = 1e-9
# Cap on rays x triangles per batched intersection (transient tensors
# are this many float32 elements x3 for the cross products).
BATCH_ELEMS = 8_000_000


def load_scene_meshes(paths):
    """Load OBJ file(s) into [(tris (T,3,3) float64, aabb (2,3))].

    Each file keeps its own entry and bounding box so a query can skip
    whole buildings its rays cannot reach; check_soup drops degenerate
    triangles and reports soup problems into the audit transcript."""
    meshes = []
    n_open = 0
    # Wall-panel meshes are open by design (they are facades, not
    # solids), so a per-file boundary-edge warning is pure noise once a
    # few hundred buildings are loaded — report the total once instead.
    quiet = len(paths) > 1
    for path in paths:
        verts, faces = load_obj(path)
        tris = check_soup(verts, faces, label=Path(path).name,
                          report_open_edges=not quiet)
        n_open += getattr(check_soup, "last_open_edges", 0)
        aabb = np.stack([tris.reshape(-1, 3).min(axis=0),
                         tris.reshape(-1, 3).max(axis=0)])
        meshes.append((tris, aabb))
    if quiet and n_open:
        print(f"  mesh: {n_open:,} open boundary edges across "
              f"{len(paths)} files (expected for wall panels)")
    return meshes


def _aabb_reachable(aabb, eye, reach):
    """Can a ray of 3D length `reach` from `eye` touch this box at all?"""
    if not np.isfinite(reach):
        return True
    gap = np.maximum(np.maximum(aabb[0] - eye, eye - aabb[1]), 0.0)
    return float(np.linalg.norm(gap)) <= reach


def _mt_min_t(eye, dirs, t_lo, t_hi, tris, device):
    """Earliest ray-triangle intersection parameter per ray.

    Batched two-sided Moller-Trumbore. `dirs` (N,3) need not be unit —
    the returned t is in units of |dir| (segments pass target-eye and
    read t in [0,1]; first-hit rays pass (ux,uy,slope) with (ux,uy)
    unit horizontal, making t the horizontal distance directly).
    `t_lo`/`t_hi` (N,) bound the acceptance window per ray. inf = miss.

    With eye-relative coordinates the ray origin is the zero vector, so
    the tvec/qvec terms of the classic formulation become per-triangle
    constants — computed once per triangle chunk, not per ray pair."""
    n = len(dirs)
    out = np.full(n, np.inf)
    if n == 0 or len(tris) == 0:
        return out
    tris_local = np.ascontiguousarray(tris - eye, dtype=np.float32)
    chunk_tris = min(len(tris), 4096)
    chunk_rays = max(1, BATCH_ELEMS // chunk_tris)

    t32 = lambda a: torch.as_tensor(  # noqa: E731
        np.asarray(a, dtype=np.float32), device=device)
    tris_t = t32(tris_local)
    v0, e1 = tris_t[:, 0], tris_t[:, 1] - tris_t[:, 0]
    e2 = tris_t[:, 2] - tris_t[:, 0]
    tvec = -v0                                   # origin - v0, origin = 0
    qvec = torch.linalg.cross(tvec, e1)
    tq = (e2 * qvec).sum(-1)                     # per-triangle constant

    for s in range(0, n, chunk_rays):
        d_t = t32(dirs[s:s + chunk_rays])
        lo = t32(t_lo[s:s + chunk_rays])[:, None]
        hi = t32(t_hi[s:s + chunk_rays])[:, None]
        best = torch.full((len(d_t),), math.inf, device=device)
        for ts in range(0, len(tris_t), chunk_tris):
            e1c, e2c = e1[ts:ts + chunk_tris], e2[ts:ts + chunk_tris]
            tv, qv = tvec[ts:ts + chunk_tris], qvec[ts:ts + chunk_tris]
            tqc = tq[ts:ts + chunk_tris]
            pvec = torch.linalg.cross(d_t[:, None, :], e2c[None, :, :])
            det = (pvec * e1c[None]).sum(-1)
            inv = 1.0 / det
            u = (pvec * tv[None]).sum(-1) * inv
            v = (d_t @ qv.T) * inv
            t = tqc[None] * inv
            ok = ((det.abs() > EPS_DET)
                  & (u >= -EPS_BARY) & (v >= -EPS_BARY)
                  & (u + v <= 1.0 + EPS_BARY)
                  & (t > lo) & (t < hi))
            t = torch.where(ok, t, torch.full_like(t, math.inf))
            best = torch.minimum(best, t.amin(dim=1))
        out[s:s + chunk_rays] = best.cpu().numpy().astype(np.float64)
    return out


def _mt_min_t_multi(origins, dirs, t_lo, t_hi, tris, device):
    """As `_mt_min_t`, but every ray carries its own origin.

    Folding the origin into per-triangle constants is cheaper per pair
    and ties one call to one observer. Here the origin varies per ray,
    so tvec/qvec become per-pair terms — about 1.7x the arithmetic —
    and in exchange the whole site is tested in a handful of launches
    instead of one per observer. Measurement drives the trade: with a
    few thousand triangles the per-observer form spends ~99% of its
    time in launch, graph-compile and device-sync overhead rather than
    arithmetic, so paying more arithmetic to launch less is a large
    net win.

    `origins` and `tris` must already be in a common local frame
    (translated on the host in float64), because the float32 upload
    cannot hold UTM magnitudes.
    """
    n = len(dirs)
    out = np.full(n, np.inf)
    if n == 0 or len(tris) == 0:
        return out

    t32 = lambda a: torch.as_tensor(  # noqa: E731
        np.asarray(a, dtype=np.float32), device=device)
    tris_t = t32(tris)
    v0 = tris_t[:, 0]
    e1, e2 = tris_t[:, 1] - v0, tris_t[:, 2] - v0

    chunk_tris = min(len(tris), 4096)
    # The per-pair terms are 3-vectors and several are live at once, so
    # the ray block is sized against a fraction of the element budget.
    chunk_rays = max(1, BATCH_ELEMS // (4 * chunk_tris))

    for s in range(0, n, chunk_rays):
        d_t = t32(dirs[s:s + chunk_rays])[:, None, :]
        o_t = t32(origins[s:s + chunk_rays])[:, None, :]
        lo = t32(t_lo[s:s + chunk_rays])[:, None]
        hi = t32(t_hi[s:s + chunk_rays])[:, None]
        best = torch.full((d_t.shape[0],), math.inf, device=device)
        for ts in range(0, len(tris_t), chunk_tris):
            v0c = v0[ts:ts + chunk_tris][None]
            e1c = e1[ts:ts + chunk_tris][None]
            e2c = e2[ts:ts + chunk_tris][None]
            pvec = torch.linalg.cross(d_t.expand(-1, e2c.shape[1], -1),
                                      e2c.expand(d_t.shape[0], -1, -1))
            det = (pvec * e1c).sum(-1)
            inv = 1.0 / det
            tvec = o_t - v0c
            u = (pvec * tvec).sum(-1) * inv
            qvec = torch.linalg.cross(tvec, e1c.expand_as(tvec))
            v = (d_t * qvec).sum(-1) * inv
            t = (e2c * qvec).sum(-1) * inv
            ok = ((det.abs() > EPS_DET)
                  & (u >= -EPS_BARY) & (v >= -EPS_BARY)
                  & (u + v <= 1.0 + EPS_BARY)
                  & (t > lo) & (t < hi))
            t = torch.where(ok, t, torch.full_like(t, math.inf))
            best = torch.minimum(best, t.amin(dim=1))
        out[s:s + chunk_rays] = best.cpu().numpy().astype(np.float64)
    return out


def _gather_reachable(meshes, eye, reach):
    """One triangle array for every mesh a ray of length `reach` could
    touch, or None.

    Concatenating matters: dispatching a separate intersection call per
    mesh file costs a tensor upload and kernel launch each time, and
    the graph builder issues one visible_mask call per building — with
    a few hundred buildings loaded that overhead dominates the actual
    arithmetic by orders of magnitude."""
    keep = [tris for tris, aabb in meshes
            if _aabb_reachable(aabb, eye, reach)]
    if not keep:
        return None
    return keep[0] if len(keep) == 1 else np.concatenate(keep, axis=0)


def segment_blocked(eye, targets, meshes, device, d_min, back=0.05):
    """Does any triangle cut the open segment eye->target? bool [N].

    The acceptance window mirrors the heightfield's conventions:
    ignore hits within `d_min` of the eye (the observer's own cell
    cannot occlude) and within `back` metres of the target, so a
    target lying exactly on a wall face reads visible rather than
    being occluded by the surface it sits on."""
    eye = np.asarray(eye, dtype=np.float64)
    targets = np.asarray(targets, dtype=np.float64)
    dirs = targets - eye
    L = np.linalg.norm(dirs, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        t_lo = d_min / L
        t_hi = (L - back) / L
    reach = float(np.nanmax(L)) if len(L) else 0.0
    tris = _gather_reachable(meshes, eye, reach)
    if tris is None:
        return np.zeros(len(targets), dtype=bool)
    return np.isfinite(_mt_min_t(eye, dirs, t_lo, t_hi, tris, device))


def segments_blocked_multi(eyes, targets, eye_index, meshes, device,
                           d_min, back=0.05):
    """`segment_blocked` for rays leaving many different eyes.

    Same acceptance window and the same two-sided test; only the
    scheduling differs. Two things are given up to get one launch:
    the per-eye AABB cull, so every ray is tested against the whole
    site's triangles, and the eye-relative frame, replaced by a frame
    local to the ray bundle. Neither is free, and both were measured
    before being taken — the cull saves ~4x arithmetic on a problem
    whose arithmetic is not the cost, and the shared frame keeps the
    site within a few hundred metres of the origin, so float32 holds
    positions to ~1e-4 m against acceptance windows of 0.05 m and up.

    That frame is taken from the triangles, never from the rays. A
    ray-derived anchor (the mean eye, say) shifts when the caller casts
    a subset, so a memoising caller casting only its misses would
    disagree with an exhaustive one on rays grazing a triangle edge —
    from rounding alone, with no error in either. Keyed on geometry,
    the frame is the same for every batch drawn against one scene.
    """
    eyes = np.asarray(eyes, dtype=np.float64)
    targets = np.asarray(targets, dtype=np.float64)
    idx = np.asarray(eye_index, dtype=np.int64)
    if len(targets) == 0:
        return np.zeros(0, dtype=bool)

    keep = [tris for tris, _aabb in meshes if len(tris)]
    if not keep:
        return np.zeros(len(targets), dtype=bool)
    tris = keep[0] if len(keep) == 1 else np.concatenate(keep, axis=0)

    org = eyes[idx]
    dirs = targets - org
    L = np.linalg.norm(dirs, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        t_lo = d_min / L
        t_hi = (L - back) / L

    flat = tris.reshape(-1, 3)
    ref = (flat.min(axis=0) + flat.max(axis=0)) / 2.0
    return np.isfinite(_mt_min_t_multi(org - ref, dirs, t_lo, t_hi,
                                       tris - ref, device))


def mesh_first_hit(eye, ux, uy, slope, meshes, device, d_min, t_hi=None):
    """Horizontal distance to the first triangle hit per ray; inf = miss.

    Rays are (ux, uy, slope) with (ux, uy) unit horizontal — identical
    to HeightfieldScene.first_hit — so the raw intersection parameter
    is already the horizontal distance. `t_hi` (N,) caps the search
    (callers pass the heightfield hit: a mesh hit beyond it can never
    win the min)."""
    eye = np.asarray(eye, dtype=np.float64)
    ux = np.asarray(ux, dtype=np.float64).ravel()
    uy = np.asarray(uy, dtype=np.float64).ravel()
    slope = np.asarray(slope, dtype=np.float64).ravel()
    dirs = np.column_stack([ux, uy, slope])
    t_lo = np.full(len(dirs), d_min)
    if t_hi is None:
        t_hi = np.full(len(dirs), np.inf)
    finite = t_hi[np.isfinite(t_hi)]
    # |dir| >= 1, so horizontal reach t_hi understates 3D length; scale
    # by the steepest slope present for a still-cheap conservative cull.
    reach = (float(finite.max()) * float(np.sqrt(1 + slope ** 2).max())
             if len(finite) == len(t_hi) and len(finite) else np.inf)
    tris = _gather_reachable(meshes, eye, reach)
    if tris is None:
        return np.full(len(dirs), np.inf)
    return _mt_min_t(eye, dirs, t_lo, t_hi, tris, device)


class HybridScene:
    """Heightfield + triangle meshes behind the same Scene contract.

    Wraps an already-built HeightfieldScene by composition: the
    heightfield answers first and the meshes can only further occlude
    (visible AND clear; min of hit distances). Grid attributes and
    surface_z forward to the base, so every raster/QC consumer sees
    the same grid semantics as before."""

    has_mesh = True

    def __init__(self, base, meshes):
        self._base = base
        self._meshes = meshes

    def __getattr__(self, name):
        # dem_np, H, W, a, e, x0, y0, px, nodata, device, step, d_min,
        # surface_z, _pix — anything not overridden below.
        return getattr(self._base, name)

    def visible_mask(self, eye_xyz, targets_xyz, chunk=200_000):
        targets = np.asarray(targets_xyz, dtype=np.float64)
        vis = self._base.visible_mask(eye_xyz, targets, chunk=chunk)
        idx = np.nonzero(vis)[0]
        if len(idx):
            # AND is order-free, so the mesh test only ever runs on the
            # heightfield's survivors — pure savings.
            eye = np.asarray(eye_xyz, dtype=np.float64)
            blocked = segment_blocked(eye, targets[idx], self._meshes,
                                      self._base.device, self._base.d_min)
            vis[idx[blocked]] = False
        return vis

    def visible_mask_multi(self, eyes_xyz, targets_xyz, eye_index):
        """Many-observer LOS: both halves batched over all observers.

        Terrain and mesh are still ANDed, so the mesh pass only ever
        runs on the heightfield's survivors. Both halves are launched
        once for the whole bundle rather than once per observer, which
        is what the cost actually consists of: on this site a draw of
        ~200 observers spent ~99% of its time in per-call overhead and
        ~1% in the intersection arithmetic itself."""
        eyes = np.asarray(eyes_xyz, dtype=np.float64)
        targets = np.asarray(targets_xyz, dtype=np.float64)
        idx = np.asarray(eye_index, dtype=np.int64)
        vis = self._base.visible_mask_multi(eyes, targets, idx)
        sel = np.flatnonzero(vis)
        if len(sel):
            blocked = segments_blocked_multi(
                eyes, targets[sel], idx[sel], self._meshes,
                self._base.device, self._base.d_min)
            vis[sel[blocked]] = False
        return vis

    def first_hit(self, eye_xyz, ux, uy, slope, max_range=None,
                  chunk=200_000):
        d_hf = self._base.first_hit(eye_xyz, ux, uy, slope,
                                    max_range=max_range, chunk=chunk)
        t_hi = d_hf if max_range is None else np.minimum(d_hf, max_range)
        d_mesh = mesh_first_hit(np.asarray(eye_xyz, dtype=np.float64),
                                ux, uy, slope, self._meshes,
                                self._base.device, self._base.d_min,
                                t_hi=t_hi)
        return np.minimum(d_hf, d_mesh)

    def is_visible(self, eye_xyz, target_xyz):
        return bool(self.visible_mask(
            eye_xyz, np.asarray(target_xyz)[None, :])[0])


def flatten_footprints(dem_np, transform, nodata, geoms, bare_dem_path):
    """Un-extrude selected buildings: overwrite their footprint cells
    with the bare-earth DEM so a mesh-represented building does not
    double-occlude (once as its extruded heightfield block, once as
    triangles). Wired for when real chapel models arrive; the
    synthetic experiment sidesteps it with a flat DEM. Returns
    (new dem, cells replaced)."""
    import rasterio
    from rasterio.features import geometry_mask

    with rasterio.open(bare_dem_path) as src:
        bare = src.read(1)
        same = (bare.shape == dem_np.shape
                and src.transform == transform)
    check(same, "bare-earth DEM matches the working grid",
          str(bare_dem_path))
    if not same:
        return dem_np, 0
    inside = ~geometry_mask(geoms, out_shape=dem_np.shape,
                            transform=transform, invert=False)
    if nodata is not None:
        inside &= (dem_np != nodata) & (bare != nodata)
    out = dem_np.copy()
    out[inside] = bare[inside]
    return out, int(inside.sum())


def run_scene3d_checks(scene, solid_scene, prm):
    """Aperture ground truths on the synthetic cube+dome+door building.

    Every probe is derived from the generator's parameters (params.json)
    so the expected outcome is analytic, not eyeballed. `scene` carries
    building.obj (with door), `solid_scene` building_solid.obj."""
    cx, cy = prm["center"]
    gz, wh = prm["ground_z"], prm["wall_height"]
    half = prm["size"] / 2.0                 # south outer wall: y = cy - half
    thick = prm["wall_thickness"]
    head = gz + prm["door_head"]
    dome_r = prm["dome_radius"]
    eye = (cx, cy - half - 10.0, gz + 1.5)   # 10 m south of the door, on axis

    p_in = (cx, cy, gz + 1.0)                # chest height, building centre
    check(scene.is_visible(eye, p_in),
          "door-centre sightline passes", f"eye z {eye[2]:.1f} < head {head:.1f}")

    p_high = (cx, cy, gz + wh - 0.2)         # near the ceiling
    check(not scene.is_visible(eye, p_high),
          "above-door-head sightline blocked by the header")

    p_side = (cx + 2.5, cy, gz + 1.0)        # crosses the wall beside the door
    check(not scene.is_visible(eye, p_side),
          "off-door sightline blocked by the blank wall")

    check(not solid_scene.is_visible(eye, p_in),
          "solid variant blocks the same door-centre sightline")

    # Dome occlusion: an elevated segment clearing the wall tops but not
    # the dome apex must be blocked; raised above the apex it must pass.
    apex = gz + wh + dome_r
    e_air = (cx, cy - half - 10.0, gz + wh + 1.0)
    p_air = (cx, cy + half + 10.0, gz + wh + 1.0)
    check(not scene.is_visible(e_air, p_air),
          "dome occludes an over-the-walls sightline",
          f"segment z {e_air[2]:.1f} < apex {apex:.1f}")
    e_top = (cx, cy - half - 10.0, apex + 0.5)
    p_top = (cx, cy + half + 10.0, apex + 0.5)
    check(scene.is_visible(e_top, p_top),
          "sightline above the dome apex passes")

    # Reciprocity: the triangle test is symmetric, so both directions of
    # the same segment must agree — through the door and against a wall.
    check(scene.is_visible(eye, p_in) == scene.is_visible(p_in, eye),
          "reciprocity through the door")
    check(scene.is_visible(eye, p_side) == scene.is_visible(p_side, eye),
          "reciprocity against the wall")

    # Analytic first-hit distances, due north from the outside eye
    # (az 0 -> uy=+1). On the door axis the ray passes through the
    # opening, crosses the interior, and lands on the inner face of the
    # north wall; offset beyond the jamb it stops at the south face.
    d = scene.first_hit(eye, np.array([0.0, 0.0]), np.array([1.0, 1.0]),
                        np.array([0.0, 0.0]))
    d_thru = 10.0 + 2 * half - thick          # north inner face
    check(abs(d[0] - d_thru) < 1e-3,
          "first-hit through the door lands on the far inner wall",
          f"{d[0]:.4f} vs {d_thru:.4f} m")
    eye_off = (cx + 2.5, eye[1], eye[2])
    d_off = scene.first_hit(eye_off, np.array([0.0]), np.array([1.0]),
                            np.array([0.0]))
    check(abs(d_off[0] - 10.0) < 1e-3,
          "first-hit beside the door stops at the south face",
          f"{d_off[0]:.4f} vs 10.0000 m")

    # Kernel consistency: for a fan of horizontal rays at eye height over
    # flat ground, visible_mask at range R and first_hit agree exactly
    # (blocked iff something is hit nearer than R).
    az = np.linspace(0.0, 2 * math.pi, 720, endpoint=False)
    ux, uy = np.sin(az), np.cos(az)
    r_fan = 2 * half + 14.0
    tgts = np.column_stack([eye[0] + ux * r_fan, eye[1] + uy * r_fan,
                            np.full(az.size, eye[2])])
    vis = scene.visible_mask(np.asarray(eye), tgts)
    dhit = scene.first_hit(eye, ux, uy, np.zeros(az.size))
    agree = float(np.mean(vis == (dhit >= r_fan - 0.1)))
    check(agree >= 0.99, "visible_mask/first_hit fan consistency",
          f"{agree:.4f} agreement over {az.size} rays")


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--assets", type=Path,
                   default=Path("viewshed_runs/synthetic_building/assets"),
                   help="asset folder written by make_test_building.py "
                        "(params.json, flat_dem.tif, building*.obj)")
    return p


def main():
    args = build_parser().parse_args()
    prm = json.loads((args.assets / "params.json").read_text())
    # Imported here, not at module top: viewshed.py lazy-imports this
    # module when --mesh is passed, so a top-level import back into
    # viewshed would be one refactor away from a cycle.
    from viewshed import HeightfieldScene, select_device, load_dem

    device = select_device()
    dem, transform, _, nodata, _ = load_dem(args.assets / "flat_dem.tif")
    base = HeightfieldScene(dem, transform, nodata, device)
    print("=" * 70)
    print(f"SCENE3D SELF-CHECKS   device: {device}   assets: {args.assets}")
    print("=" * 70)
    scene = HybridScene(base, load_scene_meshes(
        [args.assets / "building.obj"]))
    solid = HybridScene(base, load_scene_meshes(
        [args.assets / "building_solid.obj"]))
    run_scene3d_checks(scene, solid, prm)
    if failures:
        print(f"\n{len(failures)} check(s) failed")
        sys.exit(1)
    print("\nall scene3d checks passed")


if __name__ == "__main__":
    main()
