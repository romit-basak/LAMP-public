"""From where outside can you actually see inside a chapel?

The clear-width condition through a doorway, w*cos(t) > thickness*sin(t),
puts a hard bound on the off-normal angle at atan(w/t): 65 deg for the
old uniform 0.40 m wall, 79 deg for a Type 1 wall at one brick. That
bound is about the *opening*, though, not about the building — it says
where a ray can pass, not what it lands on once through. This script
measures the real thing, by casting rays in the actual scene:

  - The chapel's own far wall clips the view long before the geometric
    bound does. Standing far off-axis, a ray that clears the reveal has
    already crossed the chamber and buries itself in the return wall,
    so the visible floor shrinks to nothing well inside atan(w/t).
  - Neighbouring chapels occlude, and at El Bagawat they are close.
  - Distance costs area, not reach. Any one interior point stays
    visible however far back you stand — whether a ray clears the
    reveal does not change as you slide along that same ray. What
    shrinks is the *set* of reachable points: the interior you can see
    is the doorway projected from your eye, and as the eye recedes that
    projection narrows towards the door's own width. So the envelope
    has to be reported against range as well as angle, and the useful
    reading is the visible fraction rather than a yes/no.

So the answer is an envelope, not a number: for each (range, off-normal
angle) outside the door, what fraction of the interior floor is
visible. Writes a CSV of that grid, a polar figure, and the headline
angles. `--mesh-dir` selects which wall fabric to measure, so the
legacy uniform 0.40 m and the evidence-based per-building thickness can
be put side by side on the same chapel.
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
import numpy as np
import rasterio
from shapely.geometry import Point

from sanity_checks import (FOOTPRINTS, DEM_BASE_04, DEM_REGEN,
                           check, warn, failures)
from aperture_registry import (APERTURES_DIR, INVENTORY, BUILDING_FABRIC,
                               DOOR_WIDTH, canonical_walls, largest_poly,
                               resolve_wall)
from viewshed import select_device, load_dem
from viewshed import HeightfieldScene
from scene3d import load_scene_meshes, HybridScene, flatten_footprints

EYE_HEIGHT = 1.5
TARGET_HEIGHT = 1.0       # interior sample height above the chapel floor
FLOOR_SPACING = 0.25      # interior floor sample grid (m)


def door_frame(geom, row, thickness):
    """(centre on the outer face, outward unit normal, along unit, width).

    The door's outward normal is the wall normal pointing away from the
    footprint interior — the direction an observer has to come from."""
    walls = canonical_walls(geom)
    wi = resolve_wall(walls, row, check, warn)
    if wi is None:
        return None
    (x0, y0), (x1, y1) = walls[wi]
    L = math.hypot(x1 - x0, y1 - y0)
    ux, uy = (x1 - x0) / L, (y1 - y0) / L
    # s_m is the opening's centre, not its start — the builder cuts
    # (s - w/2, s + w/2). Offsetting by another half width here would
    # put the "on-axis" observer half a doorway off the axis.
    s = float(row["s_m"])
    w = float(row["width_m"]) if row.get("width_m", "") else DOOR_WIDTH
    cx, cy = x0 + ux * s, y0 + uy * s
    nx, ny = -uy, ux
    inside = largest_poly(geom).representative_point()
    if (inside.x - cx) * nx + (inside.y - cy) * ny > 0:
        nx, ny = -nx, -ny        # flip so the normal points outward
    return (cx, cy), (nx, ny), (ux, uy), w, thickness


def interior_targets(geom, inset, floor_z, spacing=FLOOR_SPACING):
    """Grid of interior floor points, inset from the footprint edge.

    The inset defaults to the wall thickness, which is the honest floor
    for a single run. Comparing two fabrics needs it pinned instead:
    insetting each by its own thickness would give the runs different
    floors, so the percentages would share no denominator and a thinner
    wall would appear to change what is visible head-on when all that
    changed was which points were being counted."""
    poly = largest_poly(geom)
    inner = poly.buffer(-max(inset, 0.05))
    if inner.is_empty:
        inner = poly.buffer(-0.05)
    if inner.is_empty:
        return np.empty((0, 3))
    x0, y0, x1, y1 = inner.bounds
    xs = np.arange(x0 + spacing / 2, x1, spacing)
    ys = np.arange(y0 + spacing / 2, y1, spacing)
    pts = [(x, y) for x in xs for y in ys
           if inner.contains(Point(x, y))]
    if not pts:
        return np.empty((0, 3))
    a = np.array(pts, float)
    z = np.full(len(a), floor_z + TARGET_HEIGHT)
    return np.column_stack([a, z])


def sweep(scene, frame, targets, ground_z, ranges, angles):
    """(visible-target fraction grid, widest ray angle actually used).

    The two are different quantities and only the second is bounded by
    atan(w/t). An observer standing at 76 deg off the door's normal can
    still see an interior point if the *ray* to that point leaves at 60
    deg, because the target is not the door centre — the standing angle
    and the ray angle only coincide on axis. Reporting the standing
    angle against the clear-width bound would look like a violation of
    the geometry when it is just two different angles."""
    (cx, cy), (nx, ny), _, _, _ = frame
    out = np.zeros((len(ranges), len(angles)))
    widest = 0.0
    for i, d in enumerate(ranges):
        for j, a in enumerate(angles):
            t = math.radians(a)
            # rotate the outward normal by the off-normal angle
            dx = nx * math.cos(t) - ny * math.sin(t)
            dy = nx * math.sin(t) + ny * math.cos(t)
            ex, ey = cx + dx * d, cy + dy * d
            gz = ground_z(ex, ey)
            if not np.isfinite(gz):
                out[i, j] = np.nan
                continue
            eye = (float(ex), float(ey), float(gz) + EYE_HEIGHT)
            vis = np.asarray(scene.visible_mask(eye, targets)) == 1
            out[i, j] = float(np.mean(vis))
            if vis.any():
                v = targets[vis, :2] - np.array([ex, ey])
                n = np.hypot(v[:, 0], v[:, 1])
                good = n > 1e-9
                if good.any():
                    cosang = ((v[good, 0] * -nx + v[good, 1] * -ny)
                              / n[good])
                    widest = max(widest, float(np.max(np.degrees(
                        np.arccos(np.clip(cosang, -1, 1))))))
    return out, widest


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--id", type=int, required=True,
                   help="chapel to stand outside of")
    p.add_argument("--registry", type=Path, default=INVENTORY)
    p.add_argument("--footprints", type=Path, default=FOOTPRINTS)
    p.add_argument("--dem", type=Path, default=DEM_REGEN)
    p.add_argument("--bare-dem", type=Path, default=DEM_BASE_04)
    p.add_argument("--fabric", type=Path, default=BUILDING_FABRIC)
    p.add_argument("--mesh-dir", type=Path,
                   default=APERTURES_DIR / "meshes",
                   help="which wall-fabric mesh set to measure")
    p.add_argument("--target-inset", type=float,
                   help="inset (m) from the footprint edge for the "
                        "interior floor samples; defaults to the wall "
                        "thickness. Pin it to a common value when "
                        "comparing two fabrics, so both measure the "
                        "same floor")
    p.add_argument("--thickness", type=float,
                   help="wall thickness (m) the meshes were built with; "
                        "defaults to the fabric table. Must be set to "
                        "0.4 when measuring the legacy uniform meshes, "
                        "or the interior inset and the reported "
                        "clear-width bound describe a different wall "
                        "than the one being ray-cast")
    p.add_argument("--neighbour-radius", type=float, default=40.0,
                   help="mesh every chapel within this radius too, so "
                        "the envelope includes real occlusion")
    p.add_argument("--max-range", type=float, default=30.0)
    p.add_argument("--range-step", type=float, default=1.0)
    p.add_argument("--angle-step", type=float, default=2.0)
    p.add_argument("--out-dir", type=Path,
                   default=APERTURES_DIR / "envelopes")
    return p


def main():
    args = build_parser().parse_args()
    device = select_device()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    fp = gpd.read_file(args.footprints)
    fp["ID"] = fp["ID"].astype(int)
    row_fp = fp[fp["ID"] == args.id]
    check(len(row_fp) == 1, f"chapel {args.id} in footprints")
    if failures:
        sys.exit(1)
    geom = row_fp.iloc[0].geometry

    reg = list(csv.DictReader(open(args.registry)))
    doors = [r for r in reg
             if int(r["ID"]) == args.id and r["kind"] == "door"]
    check(bool(doors), f"chapel {args.id} has a door row")
    if failures:
        sys.exit(1)
    door = doors[0]

    fabric = {int(r["ID"]): float(r["wall_thickness_m"])
              for r in csv.DictReader(open(args.fabric))}
    thickness = (args.thickness if args.thickness is not None
                 else fabric.get(args.id, 0.4))

    # Neighbours get meshed too — an envelope measured against a bare
    # chapel would be a statement about the doorway, not about the site.
    c = geom.centroid
    near = fp[fp.geometry.distance(c) <= args.neighbour_radius]
    mesh_ids = [i for i in near["ID"]
                if (args.mesh_dir / f"building_{i}.obj").exists()]
    check(args.id in mesh_ids, f"chapel {args.id} has a mesh in "
          f"{args.mesh_dir.name}")
    if failures:
        sys.exit(1)

    dem, transform, _crs, nodata, _prof = load_dem(args.dem)
    geoms = [r.geometry for _, r in fp.iterrows()
             if int(r["ID"]) in set(mesh_ids)]
    dem, n_cleared = flatten_footprints(dem, transform, nodata, geoms,
                                        bare_dem_path=args.bare_dem)
    base = HeightfieldScene(dem, transform, nodata, device)
    meshes = load_scene_meshes(
        [args.mesh_dir / f"building_{i}.obj" for i in mesh_ids])
    scene = HybridScene(base, meshes)
    print(f"  chapel {args.id}: wall {thickness:.3f} m, "
          f"{len(mesh_ids)} meshed chapels, {n_cleared} cells flattened")

    src = rasterio.open(args.bare_dem)

    def ground_z(x, y):
        return float(next(src.sample([(float(x), float(y))]))[0])

    floor_z = ground_z(c.x, c.y)
    inset = (args.target_inset if args.target_inset is not None
             else thickness)
    targets = interior_targets(geom, inset, floor_z)
    check(len(targets) >= 4, "interior floor sampled",
          f"{len(targets)} points at {FLOOR_SPACING} m, "
          f"inset {inset:.2f} m")
    if failures:
        sys.exit(1)

    frame = door_frame(geom, door, thickness)
    check(frame is not None, "door resolved to a wall")
    if failures:
        sys.exit(1)
    _, _, _, w, _ = frame
    geo_limit = math.degrees(math.atan(w / thickness)) if thickness else 90.0

    ranges = np.arange(args.range_step, args.max_range + 1e-9,
                       args.range_step)
    angles = np.arange(-88, 88 + 1e-9, args.angle_step)
    grid, widest_ray = sweep(scene, frame, targets, ground_z, ranges,
                             angles)

    tag = f"{args.id}_{args.mesh_dir.name}"
    csv_path = args.out_dir / f"envelope_{tag}.csv"
    with open(csv_path, "w", newline="") as f:
        wtr = csv.writer(f)
        wtr.writerow(["range_m", "angle_deg", "visible_fraction"])
        for i, d in enumerate(ranges):
            for j, a in enumerate(angles):
                wtr.writerow([d, a, round(float(grid[i, j]), 4)])

    seen = np.nan_to_num(grid) > 0
    any_seen = seen.any(axis=0)
    lim = (float(np.max(np.abs(angles[any_seen])))
           if any_seen.any() else float("nan"))
    best = np.nanmax(grid) if np.isfinite(grid).any() else float("nan")
    print(f"\n  door width {w:.2f} m, wall {thickness:.3f} m")
    print(f"  geometric clear-width bound  : +/-{geo_limit:.1f} deg")
    print(f"  widest ray actually admitted : {widest_ray:.1f} deg")
    print(f"  standing position, any seen  : +/-{lim:.1f} deg")
    print(f"  best interior fraction seen  : {100 * best:.1f}%")
    for frac, name in ((0.5, "half"), (0.25, "a quarter")):
        m = (np.nan_to_num(grid) >= frac).any(axis=0)
        if m.any():
            print(f"  {name} the floor visible within "
                  f"+/-{float(np.max(np.abs(angles[m]))):.1f} deg")
    rng_any = np.nan_to_num(grid).max(axis=1) > 0
    if rng_any.any():
        print(f"  interior visible from {ranges[rng_any].min():.0f} m "
              f"out to {ranges[rng_any].max():.0f} m (swept limit)")
    check(widest_ray <= geo_limit + 1.0,
          "no admitted ray beats the clear-width bound",
          f"{widest_ray:.1f} vs {geo_limit:.1f} deg")

    fig, ax = plt.subplots(figsize=(9, 4.2))
    im = ax.pcolormesh(angles, ranges, 100 * grid, cmap="magma",
                       vmin=0, shading="nearest")
    ax.axvline(geo_limit, color="cyan", ls="--", lw=1.2,
               label=f"clear-width bound {geo_limit:.0f} deg")
    ax.axvline(-geo_limit, color="cyan", ls="--", lw=1.2)
    ax.set_xlabel("off-normal angle from the door (deg)")
    ax.set_ylabel("range from the door (m)")
    ax.set_title(f"Chapel {args.id}: interior floor visible from outside "
                 f"({args.mesh_dir.name}, wall {thickness:.2f} m)")
    ax.legend(loc="upper right", fontsize=8)
    fig.colorbar(im, ax=ax, label="% of interior floor visible")
    fig.tight_layout()
    png = args.out_dir / f"envelope_{tag}.png"
    fig.savefig(png, dpi=150)
    plt.close(fig)
    print(f"\n  wrote {csv_path.name}, {png.name}")

    if failures:
        print(f"\n{len(failures)} check(s) failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
