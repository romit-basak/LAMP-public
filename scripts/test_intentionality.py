"""Were the chapels' entrances arranged so their interiors are visible?

The visibility graph can say which interiors are inter-visible. It
cannot, on its own, say whether that is a *decision* — a dense
necropolis on a slope would produce some inter-visibility by accident.
This tests the arrangement against nulls that hold everything fixed
except the choice being questioned.

**PRE-REGISTERED, before the first run.** Recorded here so the analysis
cannot be tuned to its own answer:

  - Statistic V: ordered chapel pairs (A, B), A != B, within --radius,
    where B's interior entrance-axis point is visible from a standing
    position just outside A's doorway. Reported with V / n_pairs and
    p_chapel, the fraction of chapels seen into by at least one other.
  - alpha = 0.01, one-sided (excess visibility only).
  - Holm-Bonferroni across the family of nulls x targets actually run.
  - Empirical p = (1 + #{V_null >= V_obs}) / (1 + n_draws). The +1 is
    required for a valid Monte-Carlo p; without it p can be 0, which is
    not a probability any finite sample can support.
  - **Deviation, recorded rather than quietly applied:** 999 draws was
    pre-registered, then measured at about 90 s per draw — 75 hours for
    three nulls, which is not runnable here. Reduced to 199, whose
    smallest attainable p is 1/200 = 0.005 and so still supports the
    pre-registered alpha of 0.01. The change was made on timing alone;
    a 3-draw pilot had already run and its p was granularity-limited at
    0.25, so no p-value informed this choice, though the pilot's effect
    size was visible. Anyone re-running with more draws should get the
    same verdict with a smaller p.
  - Effect size beside every p: (V_obs - median(V_null)) / IQR(V_null).
    A significant p on a 1% effect is not an argument about intent.
  - Nulls: N1 permutes the observed entrance directions across chapels,
    holding footprint, position, typology, topography *and the compass
    distribution* fixed. This is the headline. Drawing a wall uniformly
    instead would silently also randomise the direction distribution,
    which `test_entrance_azimuth.py` already showed is strongly
    non-uniform (zero chapels open north) — so a uniform null would
    reject for that reason and tell us nothing about arrangement. N2
    permutes chapel positions, N3 both.

**Observers are chapel doorways, not the three survey marks.** The plan
called for points sampled along the other contributor's path ensemble;
that output is not in the local datastore. Three marks would give a few
dozen pairs and no power. Doorways ask the question more directly
anyway — "standing at chapel A's entrance, can you see into chapel B?"
— and give ~n^2 ordered pairs from data already here. Stated as a
deviation, not passed off as the original design.

Cost control: N1 only ever moves a chapel's door to a different wall of
the same footprint, so meshes are cached by (id, wall) and built once —
a few hundred distinct meshes rather than one rebuild per chapel per
draw.
"""

import argparse
import csv
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

from sanity_checks import (FOOTPRINTS, DEM_BASE_04, DEM_REGEN, ROOT,
                           check, failures)
from aperture_registry import (INVENTORY, BUILDING_FABRIC,
                               DOOR_WIDTH, DOOR_HEAD, DOOR_SILL,
                               canonical_walls, largest_poly)
from build_aperture_walls import build_building_mesh, plane_fit
from viewshed import select_device, load_dem, HeightfieldScene
from scene3d import HybridScene, flatten_footprints

ALPHA = 0.01
AXIS_INSET_M = 0.5        # target this far inside the doorway
STAND_OFF_M = 1.5         # observer this far outside the doorway
EYE_HEIGHT = 1.5


def wall_frame(walls, wi):
    """(centre, outward normal) at the midpoint of one wall."""
    (x0, y0), (x1, y1) = walls[wi]
    L = math.hypot(x1 - x0, y1 - y0)
    ux, uy = (x1 - x0) / L, (y1 - y0) / L
    cx, cy = x0 + ux * L / 2, y0 + uy * L / 2
    return (cx, cy), (-uy, ux), L


def orient(nrm, centre, inside_pt):
    """Flip a wall normal so it points away from the interior."""
    nx, ny = nrm
    if (inside_pt[0] - centre[0]) * nx + (inside_pt[1] - centre[1]) * ny > 0:
        return (-nx, -ny)
    return (nx, ny)


class Chapels:
    """Per-chapel geometry, plus a cache of (id, wall) meshes."""

    def __init__(self, fp, fabric, ground_z, bare_src):
        self.ids, self.geom, self.walls = [], {}, {}
        self.plane, self.thick, self.inside = {}, {}, {}
        self.floor = {}
        for _, r in fp.iterrows():
            bid = int(r["ID"])
            g = largest_poly(r.geometry)
            w = canonical_walls(g)
            if len(w) < 3:
                continue
            elev = (float(r["Elevation"])
                    if np.isfinite(r.get("Elevation", np.nan)) else 0.0)
            self.ids.append(bid)
            self.geom[bid] = g
            self.walls[bid] = w
            self.plane[bid] = plane_fit(g, elev if elev > 0 else 3.5,
                                        ground_z)
            self.thick[bid] = fabric.get(bid, 0.4)
            p = g.representative_point()
            self.inside[bid] = (p.x, p.y)
            self.floor[bid] = float(next(
                bare_src.sample([(p.x, p.y)]))[0])
        self._cache = {}
        self._gz = ground_z

    def n_walls(self, bid):
        return len(self.walls[bid])

    def mesh(self, bid, wi):
        """Triangles for this chapel with its door on wall `wi`."""
        key = (bid, wi)
        if key in self._cache:
            return self._cache[key]
        walls = self.walls[bid]
        (cx, cy), _, L = wall_frame(walls, wi)
        gz = float(self._gz([(cx, cy)])[0])
        z_top = self.plane[bid](cx, cy)
        z_hi = min(gz + DOOR_HEAD, z_top - 0.1)
        s = L / 2
        holes = {wi: [(s - DOOR_WIDTH / 2, s + DOOR_WIDTH / 2,
                       gz + DOOR_SILL, z_hi)]} if z_hi > gz else {}
        n_orig = len(self.geom[bid].exterior.coords)
        th = 0.0 if (n_orig > 30 or self.geom[bid].buffer(
            -3 * self.thick[bid]).is_empty) else self.thick[bid]
        v, f, _ = build_building_mesh(walls, holes, self._gz,
                                      self.plane[bid], th)
        tris = np.asarray(v, float)[np.asarray(f, int)] if len(f) else \
            np.zeros((0, 3, 3))
        lo = tris.reshape(-1, 3).min(0) if len(tris) else np.zeros(3)
        hi = tris.reshape(-1, 3).max(0) if len(tris) else np.zeros(3)
        self._cache[key] = (tris, (lo, hi))
        return self._cache[key]

    def station(self, bid, wi):
        """(observer outside the door, target inside it)."""
        walls = self.walls[bid]
        (cx, cy), nrm, _ = wall_frame(walls, wi)
        nx, ny = orient(nrm, (cx, cy), self.inside[bid])
        gz = float(self._gz([(cx, cy)])[0])
        h = (DOOR_SILL + DOOR_HEAD) / 2
        tgt = (cx - nx * AXIS_INSET_M, cy - ny * AXIS_INSET_M, gz + h)
        eye = (cx + nx * STAND_OFF_M, cy + ny * STAND_OFF_M,
               gz + EYE_HEIGHT)
        return eye, tgt


def visible_pairs(ch, assign, base, device, radius, offsets=None):
    """V and the per-chapel seen flags for one wall assignment."""
    ids = [b for b in ch.ids if b in assign]
    off = offsets or {}
    meshes, eyes, tgts = [], {}, {}
    for b in ids:
        tris, aabb = ch.mesh(b, assign[b])
        dx, dy = off.get(b, (0.0, 0.0))
        if dx or dy:
            tris = tris + np.array([dx, dy, 0.0])
            aabb = (aabb[0] + np.array([dx, dy, 0.0]),
                    aabb[1] + np.array([dx, dy, 0.0]))
        meshes.append((tris, aabb))
        e, t = ch.station(b, assign[b])
        eyes[b] = (e[0] + dx, e[1] + dy, e[2])
        tgts[b] = (t[0] + dx, t[1] + dy, t[2])
    scene = HybridScene(base, meshes)

    V, seen = 0, {b: False for b in ids}
    xy = np.array([[tgts[b][0], tgts[b][1]] for b in ids])
    for i, a in enumerate(ids):
        e = eyes[a]
        d = np.hypot(xy[:, 0] - e[0], xy[:, 1] - e[1])
        sel = [j for j in range(len(ids))
               if j != i and d[j] <= radius]
        if not sel:
            continue
        arr = np.array([tgts[ids[j]] for j in sel], float)
        vis = np.asarray(scene.visible_mask(e, arr)) == 1
        V += int(vis.sum())
        for j, ok in zip(sel, vis):
            if ok:
                seen[ids[j]] = True
    return V, seen, sum(1 for b in ids for _ in [0])


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--footprints", type=Path, default=FOOTPRINTS)
    p.add_argument("--registry", type=Path, default=INVENTORY)
    p.add_argument("--fabric", type=Path, default=BUILDING_FABRIC)
    p.add_argument("--dem", type=Path, default=DEM_REGEN)
    p.add_argument("--bare-dem", type=Path, default=DEM_BASE_04)
    p.add_argument("--radius", type=float, default=60.0,
                   help="max observer-target distance tested (m)")
    p.add_argument("--n-draws", type=int, default=999)
    p.add_argument("--nulls", nargs="+", default=["N1", "N2", "N3"],
                   choices=["N1", "N2", "N3"])
    p.add_argument("--seed", type=int, default=20260813)
    p.add_argument("--out-dir", type=Path,
                   default=ROOT / "200_Projects/250_Apertures/intentionality")
    return p


def main():
    args = build_parser().parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    device = select_device()

    fp = gpd.read_file(args.footprints)
    fp["ID"] = fp["ID"].astype(int)
    fabric = {int(r["ID"]): float(r["wall_thickness_m"])
              for r in csv.DictReader(open(args.fabric))}
    reg = list(csv.DictReader(open(args.registry)))
    doors = {int(r["ID"]): int(r["wall"]) for r in reg
             if r["kind"] == "door" and str(r["wall"]).strip() != ""}
    check(len(doors) > 150, "door rows loaded", f"{len(doors)} chapels")

    bare = rasterio.open(args.bare_dem)

    def ground_z(pts):
        return np.array([v[0] for v in bare.sample(
            [(float(x), float(y)) for x, y in pts])], float)

    ch = Chapels(fp, fabric, ground_z, bare)
    assign = {b: w for b, w in doors.items()
              if b in ch.walls and w < ch.n_walls(b)}
    check(len(assign) > 150, "chapels with a usable door wall",
          f"{len(assign)}")
    if failures:
        sys.exit(1)

    dem, tr, _c, nod, _p = load_dem(args.dem)
    geoms = [ch.geom[b] for b in assign]
    dem, n_cleared = flatten_footprints(dem, tr, nod, geoms,
                                        bare_dem_path=args.bare_dem)
    base = HeightfieldScene(dem, tr, nod, device)
    print(f"\nscene: {len(assign)} chapels, {n_cleared} cells flattened, "
          f"radius {args.radius:.0f} m")

    V_obs, seen_obs, _ = visible_pairs(ch, assign, base, device,
                                       args.radius)
    n_ch = len(assign)
    p_chapel_obs = sum(seen_obs.values()) / n_ch
    print(f"observed: V = {V_obs} visible pairs, "
          f"p_chapel = {p_chapel_obs:.3f} "
          f"({sum(seen_obs.values())}/{n_ch} chapels seen into)")

    rng = np.random.default_rng(args.seed)
    ids = sorted(assign)
    cent = {b: ch.geom[b].centroid for b in ids}
    rows, dists = [], {}
    for null in args.nulls:
        Vs = []
        for _ in range(args.n_draws):
            if null == "N1":
                perm = rng.permutation(ids)
                a = {b: assign[o] % ch.n_walls(b)
                     for b, o in zip(ids, perm)}
                off = None
            elif null == "N2":
                perm = rng.permutation(ids)
                a = dict(assign)
                off = {b: (cent[o].x - cent[b].x, cent[o].y - cent[b].y)
                       for b, o in zip(ids, perm)}
            else:
                p1, p2 = rng.permutation(ids), rng.permutation(ids)
                a = {b: assign[o] % ch.n_walls(b)
                     for b, o in zip(ids, p1)}
                off = {b: (cent[o].x - cent[b].x, cent[o].y - cent[b].y)
                       for b, o in zip(ids, p2)}
            v, _, _ = visible_pairs(ch, a, base, device, args.radius, off)
            Vs.append(v)
        Vs = np.array(Vs, float)
        dists[null] = Vs
        pval = (1 + int(np.sum(Vs >= V_obs))) / (1 + len(Vs))
        iqr = float(np.percentile(Vs, 75) - np.percentile(Vs, 25))
        eff = ((V_obs - float(np.median(Vs))) / iqr) if iqr > 0 else \
            float("nan")
        rows.append(dict(null=null, V_obs=V_obs,
                         V_null_median=float(np.median(Vs)),
                         V_null_iqr=iqr, effect=eff, p=pval,
                         n_draws=len(Vs)))
        print(f"  {null}: null median {np.median(Vs):.0f} "
              f"(IQR {iqr:.0f})  effect {eff:+.2f}  p = {pval:.4f}")

    order = np.argsort([r["p"] for r in rows])
    m, run = len(rows), 0.0
    for rank, i in enumerate(order):
        run = max(run, min(1.0, (m - rank) * rows[i]["p"]))
        rows[i]["p_holm"] = run
        rows[i]["reject"] = run < ALPHA

    df = pd.DataFrame(rows)
    df.to_csv(args.out_dir / "intentionality_results.csv", index=False)

    fig, axes = plt.subplots(1, len(dists), figsize=(4.6 * len(dists), 3.8),
                             squeeze=False)
    for ax, (null, Vs) in zip(axes[0], dists.items()):
        ax.hist(Vs, bins=30, color="0.6", edgecolor="k", linewidth=0.4)
        ax.axvline(V_obs, color="crimson", lw=2,
                   label=f"observed {V_obs}")
        r = next(x for x in rows if x["null"] == null)
        ax.set_title(f"{null}: p={r['p_holm']:.3g}, "
                     f"effect {r['effect']:+.2f}", fontsize=10)
        ax.set_xlabel("visible pairs V")
        ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(args.out_dir / "intentionality_null.png", dpi=150)
    plt.close(fig)

    meta = dict(alpha=ALPHA, n_draws=args.n_draws, radius_m=args.radius,
                chapels=n_ch, V_obs=V_obs, p_chapel=p_chapel_obs,
                observers="just outside each chapel's doorway",
                targets="entrance-axis point inside each doorway",
                results=rows)
    (args.out_dir / "intentionality_meta.json").write_text(
        json.dumps(meta, indent=2, default=float))
    print(f"\nwrote intentionality_results.csv, intentionality_null.png")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
