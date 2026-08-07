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
from aperture_registry import (APERTURES_DIR, INVENTORY, DOOR_WIDTH,
                               DOOR_HEAD, DOOR_SILL, canonical_walls,
                               largest_poly, resolve_wall)
from make_test_building import rect, dome
from volume_mesh import load_obj, check_soup, write_obj

ORTHO = (ROOT / "100_Data/150_DigitalElevationModel/Generated_DEMs/"
         "Current_DEM/Bagawat-DEM-NewImageryOnly-0.4m-ORTHOPHOTO.tif")
DOME_SINK = 0.35          # dome centre sits this fraction of r below
                          # the roofline (the dome layer's convention)


def wall_panel(p0, p1, zb, zt, verts, faces, holes=()):
    """One vertical wall face from 2D endpoints, with openings.

    `zb`/`zt` are (z_at_p0, z_at_p1) pairs — base and top interpolate
    linearly along the wall, so sloped ground and a fitted roof plane
    both come out as sheared quads (rect takes arbitrary corners).
    `holes` = [(s0, s1, z_lo, z_hi)] in metres along p0->p1, absolute
    z; they must be pre-sorted, non-overlapping and inside the panel.
    Emits full-height strips between holes and header/sill bands over
    them — the multi-hole generalization of the synthetic builder's
    axis-aligned wall_x_span."""
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

    s_prev = 0.0
    for s0, s1, z_lo, z_hi in holes:
        band(s_prev, s0, zb_at(s_prev), zb_at(s0),
             zt_at(s_prev), zt_at(s0))
        band(s0, s1, z_hi, z_hi, zt_at(s0), zt_at(s1))    # header
        band(s0, s1, zb_at(s0), zb_at(s1), z_lo, z_lo)    # sill band
        s_prev = s1
    band(s_prev, L, zb_at(s_prev), zb_at(L), zt_at(s_prev), zt_at(L))


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
                        thickness, dome_row=None):
    """All triangles for one building. Returns (verts, faces, stats).

    `ground_z(pts)` samples the bare DEM; `plane(x, y)` evaluates the
    fitted roofline. Outer faces sit on the footprint ring; inner
    faces are offset by `thickness` along the inward normal, extended
    half a thickness at both ends so corners overlap instead of
    leaking (overlap is invisible to an any-hit occlusion test).
    `thickness=0` builds single-face walls (the circular-footprint
    fallback)."""
    verts, faces = [], []
    embed = 0.3               # sink bases below grade: no daylight gaps
    hole_area = 0.0
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
        wall_panel(p0, p1, zb, zt, verts, faces, holes)
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
            holes_in = [(s0 + thickness, s1 + thickness, zl, zh)
                        for s0, s1, zl, zh in holes]
            wall_panel(q0, q1, zbq, ztq, verts, faces, holes_in)
            hole_area += sum((s1 - s0) * (zh - zl)
                             for s0, s1, zl, zh in holes_in)
            for s0, s1, zl, zh in holes:        # door reveals
                a_out = p0 + u * s0
                b_out = p0 + u * s1
                a_in = a_out + n_in * thickness
                b_in = b_out + n_in * thickness
                rect((a_out[0], a_out[1], zl), (a_in[0], a_in[1], zl),
                     (a_in[0], a_in[1], zh), (a_out[0], a_out[1], zh),
                     verts, faces)
                rect((b_out[0], b_out[1], zl), (b_in[0], b_in[1], zl),
                     (b_in[0], b_in[1], zh), (b_out[0], b_out[1], zh),
                     verts, faces)
                rect((a_out[0], a_out[1], zh), (b_out[0], b_out[1], zh),
                     (b_in[0], b_in[1], zh), (a_in[0], a_in[1], zh),
                     verts, faces)              # header soffit
                zg = float(ground_z([(a_out + b_out) / 2])[0])
                if zl > zg + 0.05:
                    rect((a_out[0], a_out[1], zl),
                         (b_out[0], b_out[1], zl),
                         (b_in[0], b_in[1], zl),
                         (a_in[0], a_in[1], zl), verts, faces)

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
            {"hole_area": hole_area})


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
                   help="wall thickness (m); doors get real reveals")
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
                   help="build the SAME geometry with every opening "
                        "omitted — the doorless control. Comparing "
                        "against plain extruded blocks instead would "
                        "confound apertures with the roof planes and "
                        "dome caps these meshes also add")
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
        n_orig = len(largest_poly(geom).exterior.coords)
        thin = (n_orig > 30 or largest_poly(geom).buffer(
            -3 * args.thickness).is_empty)
        th = 0.0 if thin else args.thickness
        if thin:
            warn(f"ID {bid} built zero-thickness",
                 "circular or too small for wall offsetting")

        holes_by_wall, srcs = {}, set()
        ok_rows = True
        for r in sorted(by_id[bid], key=lambda r: (int(r["wall"]),
                                                   float(r["s_m"]))):
            wi = resolve_wall(walls, r, check, warn)
            if wi is None:
                ok_rows = False
                continue
            p0, p1 = walls[wi]
            L = math.hypot(p1[0] - p0[0], p1[1] - p0[1])
            w = (float(r["width_m"]) if r.get("width_m", "") != ""
                 else args.default_door_width)
            sill = (float(r["sill_m"]) if r.get("sill_m", "") != ""
                    else args.default_sill)
            head = (float(r["head_m"]) if r.get("head_m", "") != ""
                    else args.default_door_head)
            s = min(max(float(r["s_m"]), w / 2 + 0.05),
                    L - w / 2 - 0.05)
            if abs(s - float(r["s_m"])) > 0.01:
                warn(f"ID {bid} ap {r['ap_id']} opening nudged "
                     "inside its wall", f"s {r['s_m']} -> {s:.2f}")
            hx, hy = (p0[0] + (p1[0] - p0[0]) * s / L,
                      p0[1] + (p1[1] - p0[1]) * s / L)
            zg = float(ground_z([(hx, hy)])[0])
            z_top = plane(hx, hy)
            z_hi = min(zg + head, z_top - 0.1)
            if z_hi < zg + head:
                warn(f"ID {bid} ap {r['ap_id']} head clipped below "
                     "roofline", f"{zg + head:.2f} -> {z_hi:.2f}")
            holes_by_wall.setdefault(wi, []).append(
                (s - w / 2, s + w / 2, zg + sill, z_hi))
            srcs.add(r.get("source_dims", ""))
        for wi, hs in holes_by_wall.items():
            hs.sort()
            for (a0, a1, *_), (b0, b1, *_) in zip(hs, hs[1:]):
                if b0 < a1:
                    check(False, f"ID {bid} wall {wi} openings overlap",
                          f"{a0:.2f}-{a1:.2f} vs {b0:.2f}-{b1:.2f}")
                    ok_rows = False
        if not ok_rows:
            continue

        verts, faces, stats = build_building_mesh(
            walls, {} if args.no_openings else holes_by_wall,
            ground_z, plane, th, dome_rows.get(bid))
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
