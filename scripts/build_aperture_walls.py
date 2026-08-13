"""Build per-building OBJ wall meshes from the aperture registry.

The registry (`aperture_inventory.csv`, seeded by extract_site_plan.py
and hand-edited from the QC tiles / report plates / DXF plots) anchors
each opening to a canonical footprint wall. This script turns that
into the triangle geometry `viewshed.py --mesh` consumes: per wall an
outer face, an inner face offset by the wall thickness, and the door
reveal (jambs + header soffit + sill) connecting them — so oblique
sightlines through an opening are clipped by the wall depth, exactly
the effect the aperture analysis is after — plus a roof cap (the
heightfield block is flattened away by --mesh-clear-ids, so the mesh
must own its roofline) and the chapel's dome cap where the dome layer
records one.

One OBJ per building (`meshes/building_<ID>.obj`): that granularity is
what the hybrid scene's per-file bounding-box culling expects. The
companion `mesh_args.txt` holds the ready-to-paste engine arguments.

`--self-test` proves the whole registry->mesh->engine chain on a
synthetic square building over a flat in-memory DEM, asserting the
same analytic sightlines the synthetic-building experiment validated
(through-door passes, above-head blocked, blank-wall blocked, exact
first-hit distances). Run it after any change here.

This script never writes the registry; `--calibrate` reports measured
opening statistics (rows with source_dims=plate) so the documented
defaults can be updated deliberately.
"""

import argparse
import csv
import math
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
import numpy as np
import rasterio

from sanity_checks import (FOOTPRINTS, DEM_BASE_04, DOME_INVENTORY,
                           ROOT, check, warn, failures)
from aperture_registry import (APERTURES_DIR, INVENTORY, BUILDING_FABRIC,
                               DOOR_WIDTH, DOOR_HEAD, DOOR_SILL,
                               canonical_walls, largest_poly, resolve_wall,
                               row_kind, row_perforates, row_face,
                               row_depth, opening_rect)
from make_test_building import rect, dome
from volume_mesh import load_obj, check_soup, write_obj

ORTHO = (ROOT / "100_Data/150_DigitalElevationModel/Generated_DEMs/"
         "Current_DEM/Bagawat-DEM-NewImageryOnly-0.4m-ORTHOPHOTO.tif")
DOME_SINK = 0.35          # dome centre sits this fraction of r below
                          # the roofline (the dome layer's convention)
OPENING_MODES = ("none", "doors", "perforating", "all")
THICKNESS_MODES = ("legacy", "fabric")


def built_thickness(geom, nominal, rule_t):
    """The thickness a footprint is actually built at: `nominal`, or 0.

    Circular footprints (densified rings, so many vertices that an
    inward offset self-intersects) and footprints too small to hold a
    3x-thickness core fall back to single-sheet walls. `rule_t` is the
    thickness the *predicate* runs on, which is deliberately allowed to
    differ from the one built, so a fabric run can be compared against
    legacy without the two effects riding on each other.

    Anything reasoning about a wall's depth — a recess, a target inside
    one — has to agree with the mesh about which walls have none, so
    the rule lives here rather than inline at its one caller."""
    poly = largest_poly(geom)
    thin = (len(poly.exterior.coords) > 30
            or poly.buffer(-3 * rule_t).is_empty)
    return 0.0 if thin else nominal


def load_fabric(path, check, warn):
    """building id -> wall thickness (m) from the fabric table.

    Kept separate from the aperture registry because thickness is a
    property of the building, not of any one opening, and because a
    building with no opening still needs one."""
    if not path.exists():
        check(False, "fabric table present", str(path))
        return None
    out = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                bid, t = int(r["ID"]), float(r["wall_thickness_m"])
            except (KeyError, TypeError, ValueError):
                continue
            if t <= 0:
                warn(f"ID {bid} has a non-positive thickness", str(t))
                continue
            out[bid] = t
    check(bool(out), "fabric table has usable rows",
          f"{len(out)} buildings")
    return out


def wall_panel(p0, p1, zb, zt, verts, faces, holes=()):
    """One vertical wall face from 2D endpoints, with openings.

    `zb`/`zt` are (z_at_p0, z_at_p1) pairs — base and top interpolate
    linearly along the wall, so sloped ground and a fitted roof plane
    both come out as sheared quads (rect takes arbitrary corners).
    `holes` = [(s0, s1, z_lo, z_hi)] in metres along p0->p1, absolute
    z; they must lie inside the panel but may share an s-range as long
    as their z-ranges are disjoint.

    Column sweep: the hole s-boundaries cut the panel into vertical
    columns, and within each column the union of the holes spanning it
    is subtracted from the full height, leaving the complementary
    bands. Walking holes left-to-right instead — emitting a header and
    a sill per hole — is the obvious approach and is wrong as soon as
    two openings stack: the lower one's header spans from its head to
    the roof and fills in the upper one, which then silently does not
    exist. That case is not hypothetical here; the excavation report
    repeatedly puts a light aperture directly above a niche."""
    p0 = np.asarray(p0, float)
    u = np.asarray(p1, float) - p0
    L = float(np.linalg.norm(u))
    u = u / L

    def at(s):
        return p0 + u * s

    def zb_at(s):
        return zb[0] + (zb[1] - zb[0]) * s / L

    def zt_at(s):
        return zt[0] + (zt[1] - zt[0]) * s / L

    def band(s0, s1, z0a, z0b, z1a, z1b):
        if s1 - s0 <= 1e-9 or (z1a - z0a <= 1e-9 and z1b - z0b <= 1e-9):
            return
        a, b = at(s0), at(s1)
        rect((a[0], a[1], z0a), (b[0], b[1], z0b),
             (b[0], b[1], z1b), (a[0], a[1], z1a), verts, faces)

    # Clamp the cuts into the panel: a wall shorter than its own
    # opening (the registry has a few, where the nudge bounds cross)
    # yields an s outside [0, L], and an unclamped cut would emit a
    # band hanging off the end of the wall.
    cuts = sorted({0.0, L} | {min(max(s, 0.0), L)
                              for h in holes for s in h[:2]})
    for c0, c1 in zip(cuts, cuts[1:]):
        if c1 - c0 <= 1e-9:
            continue
        mid = 0.5 * (c0 + c1)
        spans = sorted((z_lo, z_hi) for s0, s1, z_lo, z_hi in holes
                       if s0 - 1e-9 <= mid <= s1 + 1e-9)
        # Merge the column's occluded z-intervals, then emit what is
        # left between the base and the top.
        merged = []
        for lo, hi in spans:
            if merged and lo <= merged[-1][1] + 1e-9:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        bands, z = [], None
        for lo, hi in merged:
            bands.append((zb_at(c0), zb_at(c1), lo, lo) if z is None
                         else (z, z, lo, lo))
            z = hi
        bands.append((zb_at(c0), zb_at(c1), zt_at(c0), zt_at(c1))
                     if z is None else (z, z, zt_at(c0), zt_at(c1)))
        # Top band first, then the rest bottom-up. Any order builds the
        # same surface, but this one reproduces the previous
        # header-then-sill emission exactly, which keeps the frozen
        # mesh hashes a usable regression gate for everything after it.
        for quad in [bands[-1]] + bands[:-1]:
            band(c0, c1, *quad)


def rects_overlap(a, b, tol=0.01):
    """Do two (s0, s1, z_lo, z_hi) wall rectangles share area?

    `tol` in metres: openings that merely touch at an edge are a
    digitizing artifact, not a conflict."""
    a0, a1, az0, az1 = a
    b0, b1, bz0, bz1 = b
    return (min(a1, b1) - max(a0, b0) > tol
            and min(az1, bz1) - max(az0, bz0) > tol)


def reveal(a_out, b_out, n_dir, depth, zl, zh, verts, faces,
           sill_z=None, cap=False):
    """The returns around one opening: two jambs, a head soffit, and
    optionally a sill and a back panel.

    A door's reveal and a niche's recess are the same four surfaces —
    the door's runs the full wall thickness and opens onto the far
    side, the niche's stops short and is closed by a back panel. Shared
    so the two cannot drift apart, and so a recess is never silently
    built as a hole. `n_dir` is the unit direction the opening recedes
    along (into the wall), `depth` how far."""
    a_in = a_out + n_dir * depth
    b_in = b_out + n_dir * depth
    rect((a_out[0], a_out[1], zl), (a_in[0], a_in[1], zl),
         (a_in[0], a_in[1], zh), (a_out[0], a_out[1], zh), verts, faces)
    rect((b_out[0], b_out[1], zl), (b_in[0], b_in[1], zl),
         (b_in[0], b_in[1], zh), (b_out[0], b_out[1], zh), verts, faces)
    rect((a_out[0], a_out[1], zh), (b_out[0], b_out[1], zh),
         (b_in[0], b_in[1], zh), (a_in[0], a_in[1], zh), verts, faces)
    if sill_z is not None and zl > sill_z + 0.05:
        rect((a_out[0], a_out[1], zl), (b_out[0], b_out[1], zl),
             (b_in[0], b_in[1], zl), (a_in[0], a_in[1], zl), verts, faces)
    if cap:
        rect((a_in[0], a_in[1], zl), (b_in[0], b_in[1], zl),
             (b_in[0], b_in[1], zh), (a_in[0], a_in[1], zh), verts, faces)


def ear_clip(ring):
    """Triangulate a simple CCW ring (2D) by ear clipping. Returns
    index triples into `ring`. Small and quadratic — fine for
    footprint rings (max ~16 canonical vertices)."""
    idx = list(range(len(ring)))
    tris = []

    def cross(o, a, b):
        return ((a[0] - o[0]) * (b[1] - o[1])
                - (a[1] - o[1]) * (b[0] - o[0]))

    def inside(p, a, b, c):
        return (cross(a, b, p) >= -1e-12 and cross(b, c, p) >= -1e-12
                and cross(c, a, p) >= -1e-12)

    guard = 0
    while len(idx) > 3 and guard < 10000:
        guard += 1
        for k in range(len(idx)):
            i, j, l = (idx[k - 1], idx[k], idx[(k + 1) % len(idx)])
            a, b, c = ring[i], ring[j], ring[l]
            if cross(a, b, c) <= 1e-12:
                continue                       # reflex or degenerate
            if any(inside(ring[m], a, b, c) for m in idx
                   if m not in (i, j, l)):
                continue
            tris.append((i, j, l))
            idx.pop(k)
            break
        else:
            break                              # no ear found — bail
    if len(idx) == 3:
        tris.append(tuple(idx))
    return tris


def build_building_mesh(walls, holes_by_wall, ground_z, plane,
                        thickness, dome_row=None, recess_by_wall=None):
    """All triangles for one building. Returns (verts, faces, stats).

    `ground_z(pts)` samples the bare DEM; `plane(x, y)` evaluates the
    fitted roofline. Outer faces sit on the footprint ring; inner
    faces are offset by `thickness` along the inward normal, extended
    half a thickness at both ends so corners overlap instead of
    leaking (overlap is invisible to an any-hit occlusion test).
    `thickness=0` builds single-face walls (the circular-footprint
    fallback).

    `holes_by_wall` perforates: [(s0, s1, z_lo, z_hi)]. `recess_by_wall`
    does not: [(s0, s1, z_lo, z_hi, depth, face)] with face in
    {"in", "out"} — a niche or apse, cut into one face and closed by a
    back panel, so a sightline stops in it instead of passing through.
    Keeping the two apart here is what stops an interior feature from
    silently becoming a window."""
    verts, faces = [], []
    embed = 0.3               # sink bases below grade: no daylight gaps
    hole_area = 0.0
    recess_area = 0.0
    recess_by_wall = recess_by_wall or {}
    for wi, (p0, p1) in enumerate(walls):
        p0 = np.asarray(p0, float)
        p1 = np.asarray(p1, float)
        u = p1 - p0
        L = float(np.linalg.norm(u))
        u = u / L
        n_in = np.array([-u[1], u[0]])          # CCW ring: interior left
        zb = tuple(float(z) - embed for z in ground_z([p0, p1]))
        zt = (plane(*p0), plane(*p1))
        holes = holes_by_wall.get(wi, [])
        # A zero-thickness wall is a single sheet with no depth to
        # recess into, so recesses there are dropped by the caller.
        recs = recess_by_wall.get(wi, []) if thickness > 0 else []
        out_cut = holes + [(s0, s1, zl, zh) for s0, s1, zl, zh, _, f
                           in recs if f == "out"]
        wall_panel(p0, p1, zb, zt, verts, faces, sorted(out_cut))
        hole_area += sum((s1 - s0) * (zh - zl)
                         for s0, s1, zl, zh in holes)
        if thickness > 0:
            q0 = p0 + n_in * thickness - u * thickness
            q1 = p1 + n_in * thickness + u * thickness
            zbq = tuple(float(z) - embed for z in ground_z([q0, q1]))
            ztq = (plane(*q0), plane(*q1))
            # Holes carry over at the same s: the inner wall is a
            # translate, but its parametrization starts one thickness
            # earlier, so shift s by +thickness.
            in_cut = holes + [(s0, s1, zl, zh) for s0, s1, zl, zh, _, f
                              in recs if f == "in"]
            holes_in = [(s0 + thickness, s1 + thickness, zl, zh)
                        for s0, s1, zl, zh in sorted(in_cut)]
            wall_panel(q0, q1, zbq, ztq, verts, faces, holes_in)
            hole_area += sum((s1 - s0) * (zh - zl)
                             for s0, s1, zl, zh in holes)
            for s0, s1, zl, zh in holes:        # door reveals
                a_out = p0 + u * s0
                b_out = p0 + u * s1
                zg = float(ground_z([(a_out + b_out) / 2])[0])
                reveal(a_out, b_out, n_in, thickness, zl, zh,
                       verts, faces, sill_z=zg)
            for s0, s1, zl, zh, d, f in recs:
                # The opening sits on whichever face it is cut into and
                # recedes toward the other one.
                base = (p0 if f == "out" else
                        p0 + n_in * thickness)
                a_out = base + u * s0
                b_out = base + u * s1
                reveal(a_out, b_out, n_in if f == "out" else -n_in,
                       d, zl, zh, verts, faces, cap=True)
                recess_area += (s1 - s0) * (zh - zl)

    ring = [w[0] for w in walls]
    for i, j, l in ear_clip(ring):
        k0 = len(verts)
        for m in (i, j, l):
            x, y = ring[m]
            verts.append((x, y, plane(x, y)))
        faces.append([k0, k0 + 1, k0 + 2])

    if dome_row is not None:
        r = float(dome_row["radius_m"])
        dome(float(dome_row["cx"]), float(dome_row["cy"]),
             float(dome_row["roof_z"]) - DOME_SINK * r, r, 24, 8,
             verts, faces)

    return (np.asarray(verts, float), np.asarray(faces, np.int64),
            {"hole_area": hole_area, "recess_area": recess_area})


def plane_fit(geom, elevation, ground_z):
    """Roofline plane z = ax + by + c through footprint-vertex ground
    + building height — the dome layer's roof_plane_z, factored to
    return an evaluator instead of a point sample (wall tops need it
    at every vertex)."""
    vx, vy = zip(*largest_poly(geom).exterior.coords[:-1])
    vz = ground_z(list(zip(vx, vy)))
    ok = np.isfinite(vz)
    if ok.sum() >= 3:
        A = np.column_stack([np.array(vx)[ok], np.array(vy)[ok],
                             np.ones(int(ok.sum()))])
        coef, *_ = np.linalg.lstsq(A, vz[ok] + elevation, rcond=None)
        return lambda x, y: float(coef[0] * x + coef[1] * y + coef[2])
    cx, cy = largest_poly(geom).centroid.coords[0]
    base = float(ground_z([(cx, cy)])[0]) + elevation
    return lambda x, y: base


def tri_areas(verts, faces):
    t = verts[faces]
    return np.linalg.norm(np.cross(t[:, 1] - t[:, 0],
                                   t[:, 2] - t[:, 0]), axis=1) / 2


def load_registry(path):
    rows = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def qc_render(verts, faces, bid, n_holes, path):
    fig = plt.figure(figsize=(8, 6))
    ax = fig.add_subplot(projection="3d")
    c = verts.mean(axis=0)
    v = verts - c
    ax.add_collection3d(Poly3DCollection(
        v[faces], facecolor="#d9c79a", edgecolor="#6b5a3e",
        linewidth=0.25, alpha=0.95))
    r = float(np.abs(v).max()) + 1
    ax.set_xlim(-r, r); ax.set_ylim(-r, r); ax.set_zlim(-r, r)
    ax.set_box_aspect((1, 1, 1))
    ax.set_title(f"building {bid} — {len(faces):,} tris, "
                 f"{n_holes} opening(s)")
    ax.view_init(elev=28, azim=-120)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def self_test():
    """Registry row -> mesh -> hybrid engine, on synthetic data.

    A 10 m square building (walls az 0/90/180/270), flat 100 m ground,
    one 1.2 m door centred on the south wall — every expected
    sightline is analytic, mirroring the synthetic-building checks."""
    from rasterio.transform import from_origin
    from shapely.geometry import box
    from viewshed import HeightfieldScene, select_device
    from scene3d import HybridScene

    cx, cy, gz, half, th = 254000.0, 2820500.0, 100.0, 5.0, 0.4
    walls = canonical_walls(box(cx - half, cy - half,
                                cx + half, cy + half))
    south = next(i for i, (p0, p1) in enumerate(walls)
                 if abs(p0[1] - (cy - half)) < 1e-6
                 and abs(p1[1] - (cy - half)) < 1e-6)
    p0 = walls[south][0]
    s_mid = math.hypot(cx - p0[0], (cy - half) - p0[1])
    holes = {south: [(s_mid - 0.6, s_mid + 0.6, gz, gz + 2.2)]}

    def ground_z(pts):
        return np.full(len(pts), gz)

    verts, faces, stats = build_building_mesh(
        walls, holes, ground_z, lambda x, y: gz + 4.0, th)
    check(abs(stats["hole_area"] - 2 * 1.2 * 2.2) < 1e-9,
          "self-test hole area exact (outer+inner)",
          f"{stats['hole_area']:.4f} vs {2 * 1.2 * 2.2:.4f}")

    n = 256
    dem = np.full((n, n), gz, dtype=np.float32)
    tf = from_origin(cx - n * 0.4 / 2, cy + n * 0.4 / 2, 0.4, 0.4)
    scene = HybridScene.__new__(HybridScene)
    base = HeightfieldScene(dem, tf, None, select_device())
    tris = verts[faces]
    aabb = np.stack([tris.reshape(-1, 3).min(0),
                     tris.reshape(-1, 3).max(0)])
    HybridScene.__init__(scene, base, [(tris, aabb)])

    eye = (cx, cy - half - 10.0, gz + 1.5)
    check(scene.is_visible(eye, (cx, cy, gz + 1.0)),
          "self-test: door sightline passes")
    check(not scene.is_visible(eye, (cx, cy, gz + 3.7)),
          "self-test: above-head sightline blocked")
    check(not scene.is_visible(eye, (cx + 2.5, cy, gz + 1.0)),
          "self-test: blank-wall sightline blocked")
    d = scene.first_hit(eye, np.array([0.0, 0.0]), np.array([1.0, 1.0]),
                        np.array([0.0, 0.0]))
    d_far = 10.0 + 2 * half - th
    check(abs(d[0] - d_far) < 1e-3,
          "self-test: through-door first hit on far inner wall",
          f"{d[0]:.4f} vs {d_far:.4f} m")
    eye_off = (cx + 2.5, eye[1], eye[2])
    d2 = scene.first_hit(eye_off, np.array([0.0]), np.array([1.0]),
                         np.array([0.0]))
    check(abs(d2[0] - 10.0) < 1e-3,
          "self-test: blank-wall first hit at the facade",
          f"{d2[0]:.4f} vs 10.0000 m")

    # --- interior features -------------------------------------------
    # A niche must NOT perforate. This is the direct regression test for
    # the bug the kind gate exists to prevent: before it, every registry
    # row cut a hole, so an interior niche would have opened a window
    # through the wall and inflated the aperture effect being measured.
    def scene_for(v, f):
        sc = HybridScene.__new__(HybridScene)
        t = v[f]
        bb = np.stack([t.reshape(-1, 3).min(0), t.reshape(-1, 3).max(0)])
        HybridScene.__init__(sc, HeightfieldScene(
            dem, tf, None, select_device()), [(t, bb)])
        return sc

    nd = 0.15
    recs = {south: [(s_mid - 0.3, s_mid + 0.3, gz + 1.0, gz + 1.6,
                     nd, "in")]}
    v_n, f_n, st_n = build_building_mesh(
        walls, {}, ground_z, lambda x, y: gz + 4.0, th,
        recess_by_wall=recs)
    check(st_n["hole_area"] == 0.0 and st_n["recess_area"] > 0,
          "self-test: niche books as recess, not hole",
          f"hole {st_n['hole_area']:.4f}, recess "
          f"{st_n['recess_area']:.4f}")
    sc_n = scene_for(v_n, f_n)
    check(not sc_n.is_visible(eye, (cx, cy, gz + 1.3)),
          "self-test: niche does not perforate the wall")

    # An on-axis recess is a dead end: the ray stops at its back panel,
    # one niche-depth short of where the bare wall face would be.
    recs_axis = {south: [(s_mid - 0.3, s_mid + 0.3, gz + 1.0, gz + 1.6,
                          nd, "out")]}
    v_a, f_a, _ = build_building_mesh(
        walls, {}, ground_z, lambda x, y: gz + 4.0, th,
        recess_by_wall=recs_axis)
    d3 = scene_for(v_a, f_a).first_hit(
        (cx, cy - half - 10.0, gz + 1.3), np.array([0.0]),
        np.array([1.0]), np.array([0.0]))
    check(abs(d3[0] - (10.0 + nd)) < 1e-3,
          "self-test: outward recess deepens the first hit by its depth",
          f"{d3[0]:.4f} vs {10.0 + nd:.4f} m")

    # Two openings sharing an s-span but not a z-span — the light
    # aperture above a niche the report keeps describing. The old
    # left-to-right panel walk filled the upper one in; the column
    # sweep must keep both.
    stacked = {south: [(s_mid - 0.3, s_mid + 0.3, gz + 0.2, gz + 0.8),
                       (s_mid - 0.3, s_mid + 0.3, gz + 1.6, gz + 2.2)]}
    v_s, f_s, st_s = build_building_mesh(
        walls, stacked, ground_z, lambda x, y: gz + 4.0, th)
    check(abs(st_s["hole_area"] - 2 * 2 * 0.6 * 0.6) < 1e-9,
          "self-test: stacked openings both cut",
          f"{st_s['hole_area']:.4f} vs {2 * 2 * 0.6 * 0.6:.4f}")
    sc_s = scene_for(v_s, f_s)
    check(sc_s.is_visible((cx, cy - half - 10.0, gz + 1.9),
                          (cx, cy, gz + 1.9)),
          "self-test: upper stacked opening is open")
    check(not sc_s.is_visible((cx, cy - half - 10.0, gz + 1.2),
                              (cx, cy, gz + 1.2)),
          "self-test: solid band between stacked openings blocks")

    # Overlap detection, both axes.
    check(rects_overlap((0, 1, 0, 1), (0.5, 1.5, 0.5, 1.5)),
          "self-test: overlapping rects detected")
    check(not rects_overlap((0, 1, 0, 1), (0, 1, 1.5, 2.5)),
          "self-test: same span, disjoint heights allowed")
    check(not rects_overlap((0, 1, 0, 1), (2, 3, 0, 1)),
          "self-test: disjoint spans allowed")

    # An unrecognised kind must fail rather than default to a hole.
    # row_kind takes its checker as an argument, so pass a recording
    # stub: the rejection is what's under test, and routing it through
    # the real check() would print a FAIL and register a failure for
    # behaviour that is correct.
    seen = []
    bad = row_kind({"ID": 0, "ap_id": 0, "kind": "buttress"},
                   lambda ok, label, detail="": seen.append(bool(ok)))
    check(bad is None and seen == [False],
          "self-test: unknown kind is rejected, not defaulted",
          f"returned {bad!r}, checker saw {seen}")

    # Thinner walls admit a wider cone through the same door.
    fan = np.linspace(-0.9, 0.9, 361)
    seen = {}
    for t_wall in (0.40, 0.17):
        v_t, f_t, _ = build_building_mesh(
            walls, holes, ground_z, lambda x, y: gz + 4.0, t_wall)
        sc_t = scene_for(v_t, f_t)
        tgt = np.column_stack([cx + fan, np.full(fan.size, cy),
                               np.full(fan.size, gz + 1.0)])
        seen[t_wall] = int(sc_t.visible_mask(eye, tgt).sum())
    check(seen[0.17] > seen[0.40],
          "self-test: thinner wall admits a wider cone",
          f"0.17 m: {seen[0.17]} rays vs 0.40 m: {seen[0.40]}")


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inventory", type=Path, default=INVENTORY,
                   help="aperture registry CSV (never written here)")
    p.add_argument("--footprints", type=Path, default=FOOTPRINTS)
    p.add_argument("--dem", type=Path, default=DEM_BASE_04,
                   help="bare-earth DEM for wall base / roofline fit")
    p.add_argument("--dome-inventory", type=Path, default=DOME_INVENTORY,
                   help="dome layer CSV; domed chapels get their cap "
                        "in the mesh (they lose the heightfield one "
                        "to --mesh-clear-ids)")
    p.add_argument("--no-domes", action="store_true",
                   help="skip dome caps in the meshes")
    p.add_argument("--thickness", type=float, default=0.4,
                   help="uniform wall thickness (m); doors get real "
                        "reveals. Used for every building unless "
                        "--thickness-mode fabric")
    p.add_argument("--fabric", type=Path, default=BUILDING_FABRIC,
                   help="per-building wall fabric CSV, read only when "
                        "--thickness-mode fabric")
    p.add_argument("--thickness-mode", choices=THICKNESS_MODES,
                   default="legacy",
                   help="legacy = one --thickness for every building, "
                        "the published baseline; fabric = per-building "
                        "measured or typology-derived thickness")
    p.add_argument("--thin-rule", choices=THICKNESS_MODES,
                   default="legacy",
                   help="which thickness decides the zero-thickness "
                        "fallback. Pinning it to legacy keeps the same "
                        "buildings solid, so a fabric run's delta is "
                        "thickness alone and not a change of "
                        "representation")
    p.add_argument("--ids", type=int, nargs="+",
                   help="build only these building ids")
    p.add_argument("--out-dir", type=Path,
                   default=APERTURES_DIR / "meshes",
                   help="where building_<ID>.obj files land")
    p.add_argument("--default-door-width", type=float,
                   default=DOOR_WIDTH,
                   help="width (m) for rows with a blank width_m")
    p.add_argument("--default-door-head", type=float, default=DOOR_HEAD,
                   help="head height (m) for rows with a blank head_m")
    p.add_argument("--default-sill", type=float, default=DOOR_SILL,
                   help="sill height (m) for rows with a blank sill_m")
    p.add_argument("--no-openings", action="store_true",
                   help="alias for --openings none: build the SAME "
                        "geometry with every opening omitted — the "
                        "doorless control. Comparing against plain "
                        "extruded blocks instead would confound "
                        "apertures with the roof planes and dome caps "
                        "these meshes also add")
    p.add_argument("--openings", choices=OPENING_MODES, default="doors",
                   help="which registry rows to build. none = the "
                        "doorless control; doors = doors only (the "
                        "published baseline); perforating = doors and "
                        "windows; all = also niches and apses as "
                        "recesses. Kept as a mode rather than a "
                        "boolean so the doorless control keeps its "
                        "exact meaning as new kinds are added")
    p.add_argument("--calibrate", action="store_true",
                   help="print measured-opening stats "
                        "(source_dims=plate) and exit")
    p.add_argument("--self-test", action="store_true",
                   help="synthetic registry->mesh->engine check, no "
                        "real data touched")
    return p


def main():
    args = build_parser().parse_args()
    if args.self_test:
        self_test()
        if failures:
            print(f"\n{len(failures)} check(s) failed")
            sys.exit(1)
        print("\nself-test passed")
        return

    check(args.inventory.exists(), "aperture registry exists",
          f"{args.inventory} — run scripts/extract_site_plan.py first")
    if failures:
        sys.exit(1)
    rows = load_registry(args.inventory)

    if args.calibrate:
        for field in ("width_m", "sill_m", "head_m"):
            vals = [float(r[field]) for r in rows
                    if r.get("source_dims") == "plate"
                    and r.get(field, "") != ""]
            if vals:
                print(f"  {field}: n={len(vals)} median="
                      f"{np.median(vals):.2f} IQR "
                      f"{np.percentile(vals, 25):.2f}-"
                      f"{np.percentile(vals, 75):.2f}")
            else:
                print(f"  {field}: no plate-measured rows yet")
        return

    fp = gpd.read_file(args.footprints)
    dome_rows = {}
    if not args.no_domes and args.dome_inventory.exists():
        with open(args.dome_inventory, newline="") as f:
            for r in csv.DictReader(f):
                if str(r.get("has_dome", "")).lower() == "true":
                    dome_rows[int(float(r["ID"]))] = r

    by_id = {}
    for r in rows:
        by_id.setdefault(int(r["ID"]), []).append(r)
    ids = sorted(args.ids if args.ids else by_id)
    missing = [i for i in ids if i not in by_id]
    check(not missing, "all requested ids have registry rows",
          f"missing {missing}" if missing else "")
    if missing:
        sys.exit(1)

    # --no-openings predates --openings and is load-bearing: the
    # doorless control and compare_apertures' mesh-dir naming both use
    # it. Keep it as an alias rather than redefining it.
    openings_mode = "none" if args.no_openings else args.openings
    print(f"  openings: {openings_mode}")

    fabric = (load_fabric(args.fabric, check, warn)
              if args.thickness_mode == "fabric" else None)
    print(f"  thickness: {args.thickness_mode} "
          f"(thin rule: {args.thin_rule})")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    dem_src = rasterio.open(args.dem)

    def ground_z(pts):
        return np.array([v[0] for v in dem_src.sample(
            [(float(x), float(y)) for x, y in pts])], float)

    built, provenance = [], {}
    for bid in ids:
        frow = fp[fp["ID"] == bid]
        if not check(len(frow) == 1, f"ID {bid} in footprints"):
            continue
        frow = frow.iloc[0]
        geom = frow.geometry
        elev = float(frow["Elevation"]) if np.isfinite(
            frow["Elevation"]) else 0.0
        if elev <= 0:
            warn(f"ID {bid} has no Elevation", "wall height 3.5 m "
                 "assumed")
            elev = 3.5
        walls = canonical_walls(geom)
        plane = plane_fit(geom, elev, ground_z)

        # Zero-thickness fallback: circular footprints (densified
        # rings) and buildings too small for a 3x-thickness core.
        # Which thickness decides that is a separate choice from which
        # thickness gets built, so a fabric run can be compared against
        # legacy without the two effects riding on each other.
        nominal = (fabric.get(bid, args.thickness) if fabric
                   else args.thickness)
        rule_t = args.thickness if args.thin_rule == "legacy" else nominal
        th = built_thickness(geom, nominal, rule_t)
        thin = th == 0.0
        if thin:
            warn(f"ID {bid} built zero-thickness",
                 "circular or too small for wall offsetting")

        holes_by_wall, recess_by_wall, srcs = {}, {}, set()
        ok_rows = True
        for r in sorted(by_id[bid], key=lambda r: (int(r["wall"]),
                                                   float(r["s_m"]))):
            kind = row_kind(r, check)
            if kind is None:
                ok_rows = False
                continue
            perforates = row_perforates(r, kind)
            if openings_mode == "doors" and kind != "door":
                continue
            if openings_mode == "perforating" and not perforates:
                continue
            wi = resolve_wall(walls, r, check, warn)
            if wi is None:
                ok_rows = False
                continue
            p0, p1 = walls[wi]
            rect_sz = opening_rect(p0, p1, r, ground_z, plane,
                                   args.default_door_width,
                                   args.default_sill,
                                   args.default_door_head, warn)
            if perforates:
                holes_by_wall.setdefault(wi, []).append(rect_sz)
            elif th <= 0:
                warn(f"ID {bid} ap {r['ap_id']}: recess dropped",
                     "zero-thickness wall has no depth to recess into")
            else:
                depth = row_depth(r, kind, th, warn)
                recess_by_wall.setdefault(wi, []).append(
                    (*rect_sz, depth, row_face(r, kind)))
            srcs.add(r.get("source_dims", ""))
        # Openings compete for wall surface in BOTH axes. Comparing only
        # adjacent pairs after sorting by s misses a fully-nested pair,
        # and comparing only s rejects the arrangement the excavation
        # report describes most often — a light aperture directly above
        # a niche, same span, different heights. All-pairs over s x z;
        # k is at most a handful per wall, so the cost is nil.
        for wi in set(holes_by_wall) | set(recess_by_wall):
            marks = ([(*h, None, "through") for h in holes_by_wall.get(wi, [])]
                     + list(recess_by_wall.get(wi, [])))
            for i, a in enumerate(marks):
                for b in marks[i + 1:]:
                    if not rects_overlap(a[:4], b[:4]):
                        continue
                    if a[5] != b[5] or "through" in (a[5], b[5]):
                        check(False,
                              f"ID {bid} wall {wi}: openings overlap",
                              f"{a[5]} {a[0]:.2f}-{a[1]:.2f}"
                              f"/{a[2]:.2f}-{a[3]:.2f} vs {b[5]} "
                              f"{b[0]:.2f}-{b[1]:.2f}/{b[2]:.2f}-{b[3]:.2f}")
                        ok_rows = False
                    elif a[4] + b[4] >= th - 1e-9:
                        # Back-to-back recesses that meet through the
                        # wall are a hole, which is the one thing this
                        # gate exists to prevent.
                        check(False,
                              f"ID {bid} wall {wi}: opposed recesses "
                              "meet through the wall",
                              f"{a[4]:.2f} + {b[4]:.2f} >= {th:.2f} m")
                        ok_rows = False
        if not ok_rows:
            continue

        build_holes = {} if openings_mode == "none" else holes_by_wall
        build_recs = recess_by_wall if openings_mode == "all" else {}
        verts, faces, _ = build_building_mesh(
            walls, build_holes, ground_z, plane, th,
            dome_rows.get(bid), recess_by_wall=build_recs)
        areas = tri_areas(verts, faces)
        check(bool((areas > 1e-9).all()),
              f"ID {bid}: no degenerate triangles",
              f"{len(faces):,} tris")
        out = args.out_dir / f"building_{bid}.obj"
        write_obj(out, verts, faces,
                  comment=f"aperture walls, building {bid}")
        v2, f2 = load_obj(out)
        check_soup(v2, f2, label=out.name)
        n_holes = sum(len(v) for v in holes_by_wall.values())
        qc_render(verts, faces, bid, n_holes,
                  args.out_dir / f"building_{bid}_qc.png")
        built.append(bid)
        provenance[bid] = ("plate" if "plate" in srcs else
                           "dxf" if "dxf" in srcs else "default")
        print(f"  ID {bid}: {len(walls)} walls, {n_holes} opening(s), "
              f"{len(faces):,} tris ({provenance[bid]} dims"
              f"{', dome' if dome_rows.get(bid) else ''}"
              f"{', thin' if th == 0 else ''})")

    # Site coverage figure: which chapels have meshes, and how their
    # dimensions are sourced.
    if built:
        with rasterio.open(ORTHO) as osrc:
            minx, miny, maxx, maxy = fp.total_bounds
            win = rasterio.windows.from_bounds(
                minx - 15, miny - 15, maxx + 15, maxy + 15,
                osrc.transform)
            img = osrc.read(1, window=win)
            wt = osrc.window_transform(win)
        lo, hi = np.nanpercentile(img, (2, 98))
        fig, ax = plt.subplots(figsize=(10, 16))
        ax.imshow(img, cmap="gray", vmin=lo, vmax=hi)

        def to_px(x, y):
            return ((x - wt.c) / wt.a - 0.5, (y - wt.f) / wt.e - 0.5)

        colors = {"plate": "lime", "dxf": "orange", "default": "y"}
        for _, r in fp.iterrows():
            xs, ys = r.geometry.exterior.xy
            px, py = zip(*[to_px(x, y) for x, y in zip(xs, ys)])
            bid = int(r["ID"])
            if bid in provenance:
                ax.plot(px, py, color=colors[provenance[bid]], lw=0.9)
            else:
                ax.plot(px, py, color="0.5", lw=0.35)
        ax.set_title("aperture meshes — green: plate dims, orange: "
                     "dxf dims, yellow: default dims, gray: none")
        ax.axis("off")
        fig.savefig(args.out_dir / "aperture_coverage.png", dpi=200,
                    bbox_inches="tight")
        plt.close(fig)

        argsf = args.out_dir / "mesh_args.txt"
        rel = [str(args.out_dir / f"building_{b}.obj") for b in built]
        argsf.write_text(
            "--mesh " + " ".join(rel)
            + " --mesh-clear-ids " + " ".join(str(b) for b in built)
            + f" --bare-dem {args.dem}\n")
        print(f"  wrote {argsf.name} ({len(built)} buildings)")

    dem_src.close()
    if failures:
        print(f"\n{len(failures)} check(s) failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
