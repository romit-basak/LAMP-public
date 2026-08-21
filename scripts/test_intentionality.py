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
    same verdict with a smaller p. **Superseded:** batching the ray
    casts brought a draw to ~0.65 s, and the reported run uses the
    pre-registered 999 after all. The reduction was never applied to a
    result.
  - **Correction, recorded: N2/N3 re-datum a moved chapel.** The first
    run of these two translated a permuted chapel in plan only, so it
    kept the elevation of the plot it came from. The necropolis spans
    38 m of relief and a random permutation misplaces the median
    chapel by 9.1 m against 3.6 m walls, leaving 78% of the null's
    buildings hanging above their new ground or buried under it — a
    floating chapel occludes nothing, so null V was inflated (medians
    456 and 481 against an observed 377) and both nulls read as
    "arrangement does not matter" for a purely mechanical reason.
    They now translate in z as well, setting each chapel on its new
    plot at the height above local ground it stands at on its own.
    This does not touch N1, the headline, which never moves anything.
  - **Deviation, recorded: sequential stopping.** A null now halts once
    `--sequential-h` (default 10) of its draws have reached V_obs, and
    reports p = h / L for the L drawn. That is Besag & Clifford's
    curtailed Monte Carlo test, and it is exact rather than a peek —
    validity comes from fixing h in advance and switching formulas
    with the stopping reason, not from stopping when the answer looks
    good. A null that never accumulates h exceedances runs the full
    n_draws and keeps the pre-registered (1 + l) / (1 + n). The gain
    is entirely in the uninteresting direction: a null that is going
    to be non-significant ends in tens of draws instead of 199.
    `--sequential-h 0` restores the original behaviour exactly.
  - **Deviation, recorded: N2 and N3 share their position draw.** Draw
    k of each uses the same layout permutation (common random
    numbers), so N3 - N2 — "does orientation add anything beyond
    position?" — is not carrying the variance of two independent
    rearrangements. Both marginals stay uniform, so each null is still
    sampled correctly, and Holm-Bonferroni holds under arbitrary
    dependence between them.
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
  - **Limitation, recorded 2026-08-20: the nulls establish
    non-randomness, not intent.** N1 permutes observed directions
    across chapels, which holds the compass distribution fixed but also
    destroys, at one stroke, every relationship a door has to its local
    surroundings. So *any* systematic relation between doors and local
    geometry lifts V_obs above the null, and mutual arrangement is only
    one such relation. Isolating one would need a null that holds the
    others fixed; none is written. Until one is, a rejection here means
    the arrangement is non-random with respect to something local, and
    V is the measurement rather than the explanation. Nothing about the
    statistic, the nulls or the p-values changes — only what may be
    concluded from them.

**Observers are chapel doorways, not the three survey marks.** The plan
called for points sampled along the other contributor's path ensemble;
that output is not in the local datastore. Three marks would give a few
dozen pairs and no power. Doorways ask the question more directly
anyway — "standing at chapel A's entrance, can you see into chapel B?"
— and give ~n^2 ordered pairs from data already here. Stated as a
deviation, not passed off as the original design.

Cost control, measured rather than guessed — and the measurement
overturned the obvious plan, so both are recorded here.

Profiling one draw: mesh building 0.11 s cold and free thereafter,
scene assembly under 1 ms, ray-casting 82 s. Since N1 only ever moves a
door to another wall of the same footprint, the same observer-target
segment recurs across draws, so `PairCache` memoises on
(observer chapel, its wall, target chapel, its wall). `--cross-check K`
runs the cached and exhaustive paths from the same seed and asserts
the two V sequences match draw for draw — not merely in distribution,
since equal means over unequal draws would be a cache wrong in
compensating directions. It passes: 6/6 draws identical, 0 mismatches
over 149 audited re-casts.

**It is nonetheless worth about 1.1x, not the order of magnitude the
redundancy suggests, and the reason is worth knowing before optimising
anything else here.** Cost is per *station*, not per segment: casting
only a quarter of the targets takes 78.2 s against 80.3 s for all of
them. Splitting one draw — heightfield march 65.5 s (79%), mesh
occlusion 12.9 s (16%), mesh gathering 0.08 s (0.1%) — puts it in the
terrain ray-march, at 0.33 s per station almost regardless of how many
targets that station carries. At roughly 150 steps over a 60 m reach
that is ~2.2 ms per step for ~40 rays, which is launch and
synchronisation overhead rather than arithmetic. A per-segment memo
cannot reach it: a station is skipped only when *every* one of its ~40
targets is cached, which at a realistic hit rate almost never happens.

The fix is therefore to batch the march across observers rather than
to prune segments — one stepped pass over all stations instead of one
per station. That is an engine change to `HeightfieldScene`, not a
change here, and it would benefit every multi-observer caller. Until
it lands, the cache stays on because it is verified and free, not
because it is doing much.

The cache applies to N1 only, and the restriction is not a
conservatism: N2 and N3 permute chapel *positions*, so a chapel moved
into a sightline blocks it for real, and a memo keyed on walls alone
would be answering about a scene that no longer exists. Those nulls run
exhaustively; `main` enforces it rather than trusting the caller.

Long runs are interruptible. SIGINT or SIGTERM finishes the draw in
flight, writes a checkpoint and exits 0; `--resume` restores the
finished draws, the generator state and the cache, so a paused run
continues as the same experiment rather than a new one. The checkpoint
carries a fingerprint of the registry, DEM, radius, seed and null list
and refuses to load into a different configuration — silently mixing
draws from two experiments into one null distribution would produce a
wrong histogram that nothing downstream could detect.
"""

import argparse
import csv
import json
import math
import signal
import sys
import time
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio

from sanity_checks import (FOOTPRINTS, DEM_BASE_04, DEM_REGEN, ROOT,
                           check, warn, failures)
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

# Bump whenever a change alters what a draw *means* rather than which
# draws are asked for. The checkpoint fingerprint covers configuration,
# so without this a run resumed across such a change would splice two
# different experiments into one null distribution and look fine doing
# it. 2: N2/N3 re-datum a permuted chapel onto its new ground.
DRAW_VERSION = 2

# Nulls whose draws leave every chapel where it stands. Only these may
# use the pair cache: it is keyed on (chapel, wall) pairs and says
# nothing about a scene in which the chapels have been rearranged.
STATIC_NULLS = ("N1",)

# Set by SIGINT/SIGTERM so a long run stops between draws with its
# checkpoint written, rather than in the middle of one with nothing.
_STOP = {"now": False}


def _on_signal(signum, _frame):
    _STOP["now"] = True
    print(f"\n[signal {signum}] finishing the current draw, then "
          "checkpointing — press again to abort without saving",
          flush=True)
    signal.signal(signum, signal.SIG_DFL)


class PairCache:
    """Memoised `(observer chapel, its wall, target chapel, its wall)
    -> visible`.

    N1 moves a door from one wall of a footprint to another. The
    observer station and the interior target both move with it, but
    every chapel stays where it stands, so the same four-tuple recurs
    across draws — about 173k distinct tuples against 4.7M evaluations
    over a full 199-draw run.

    **The key is approximate, so this is off by default.** It asserts
    that no third chapel's door matters. The argument for that was
    that a sightline crossing some other chapel C must enter and leave
    it, needing two openings, while C has only one — but chapels are
    modelled as wall panels, not watertight solids, so a ray can take
    a single opening and pass out over a wall top or through a corner
    gap. Cast exhaustively over 12 draws, 21,468 four-tuples recurred
    and 13 of them were answered differently in different draws: a
    rate of 6.1e-4, worth a few pairs of V per draw.

    An earlier check compared 474 repeated pairs and found none, which
    is why the assumption stood; at 6.1e-4 that test expected 0.29
    counterexamples, so seeing zero told us almost nothing. The
    `--cross-check` run, casting 25 draws both ways, is what caught it.

    Kept behind a flag rather than deleted: it still reproduces the
    finding, and it costs nothing to leave off now that batching has
    brought a draw under a second, where the memo was worth only 1.1x.

    **Also invalid when chapels move.** N2 and N3 permute positions,
    and a chapel shifted into a sightline blocks it for real. The
    caller must not pass a cache for those; `main` enforces it."""

    def __init__(self, audit_rate=0.0, seed=0):
        self.d = {}
        self.hits = self.misses = 0
        self.audited = self.mismatches = 0
        self.audit_rate = float(audit_rate)
        self.rng = np.random.default_rng(seed)

    def __len__(self):
        return len(self.d)

    def wants_audit(self, n):
        """Boolean mask picking which of `n` cache hits to re-cast."""
        if self.audit_rate <= 0:
            return np.zeros(n, bool)
        return self.rng.random(n) < self.audit_rate

    def note(self, key, cast_value):
        """Record a freshly cast value, auditing it against the cache."""
        prev = self.d.get(key)
        if prev is None:
            self.d[key] = cast_value
        else:
            self.audited += 1
            if prev != cast_value:
                self.mismatches += 1
        return cast_value

    def report(self):
        tot = self.hits + self.misses
        rate = (100.0 * self.hits / tot) if tot else 0.0
        line = (f"pair cache: {len(self.d):,} entries, {self.hits:,} hits "
                f"/ {tot:,} lookups ({rate:.1f}%)")
        if self.audited:
            line += (f"; audited {self.audited:,} re-casts, "
                     f"{self.mismatches} mismatch(es)")
        return line


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


def visible_pairs(ch, assign, base, device, radius, offsets=None,
                  cache=None):
    """V and the per-chapel seen flags for one wall assignment.

    With `cache`, only the (observer wall, target wall) combinations
    not seen before are ray-cast; the rest are looked up. The scene is
    still assembled in full either way, because a cache miss has to be
    cast against the same geometry every other draw saw."""
    ids = [b for b in ch.ids if b in assign]
    off = offsets or {}
    meshes, eyes, tgts = [], {}, {}
    for b in ids:
        tris, aabb = ch.mesh(b, assign[b])
        # Rigid in all three axes: a chapel moved across this site has
        # to be re-datumed onto the ground it lands on. Translating in
        # plan alone leaves it at the elevation it came from, and the
        # necropolis spans 38 m of relief — a random permutation
        # misplaces the median chapel by 9 m, well over its own 3.6 m
        # walls, so most of the null's buildings would hang in the air
        # occluding nothing (or sit buried). dz keeps the building's
        # height above its own floor exactly as measured.
        dx, dy, dz = off.get(b, (0.0, 0.0, 0.0))
        if dx or dy or dz:
            shift = np.array([dx, dy, dz])
            tris = tris + shift
            aabb = (aabb[0] + shift, aabb[1] + shift)
        meshes.append((tris, aabb))
        e, t = ch.station(b, assign[b])
        eyes[b] = (e[0] + dx, e[1] + dy, e[2] + dz)
        tgts[b] = (t[0] + dx, t[1] + dy, t[2] + dz)
    scene = HybridScene(base, meshes)

    # Plan every station first, then cast the whole site in one batch.
    # The march is launch-bound, so 197 separate calls of ~40 rays cost
    # far more than one call of ~7,800; planning first is what makes
    # that single call possible without changing which rays are cast.
    xy = np.array([[tgts[b][0], tgts[b][1]] for b in ids])
    plans, eyes_b, tgts_b, eidx = [], [], [], []
    for i, a in enumerate(ids):
        e = eyes[a]
        d = np.hypot(xy[:, 0] - e[0], xy[:, 1] - e[1])
        sel = [j for j in range(len(ids))
               if j != i and d[j] <= radius]
        if not sel:
            continue
        if cache is None:
            keys = None
            vis = np.zeros(len(sel), bool)
            todo = list(range(len(sel)))
        else:
            keys = [(a, assign[a], ids[j], assign[ids[j]]) for j in sel]
            known = np.array([k in cache.d for k in keys])
            audit = cache.wants_audit(len(keys)) & known
            cache.hits += int(known.sum())
            cache.misses += int((~known).sum())
            vis = np.array([cache.d.get(k, False) for k in keys], bool)
            todo = [int(t) for t in np.flatnonzero(~known | audit)]
        if todo:
            m = len(eyes_b)
            eyes_b.append(e)
            for t in todo:
                tgts_b.append(tgts[ids[sel[t]]])
                eidx.append(m)
        plans.append((sel, keys, vis, todo))

    cast = (scene.visible_mask_multi(np.array(eyes_b, float),
                                     np.array(tgts_b, float),
                                     np.array(eidx, np.int64))
            if tgts_b else np.zeros(0, bool))

    V, seen, pos = 0, {b: False for b in ids}, 0
    for sel, keys, vis, todo in plans:
        for t in todo:
            ok = bool(cast[pos])
            pos += 1
            vis[t] = cache.note(keys[t], ok) if cache is not None else ok
        V += int(vis.sum())
        for j, ok in zip(sel, vis):
            if ok:
                seen[ids[j]] = True
    return V, seen, len(ids)


def fingerprint(args, n_chapels):
    """Config identity a checkpoint must match before it is resumed.

    Resuming into a different scene would silently mix draws from two
    different experiments into one null distribution, which no
    downstream check would catch — the histogram would simply be
    wrong. Everything that changes what a draw means goes in here."""
    return {
        "n_draws": args.n_draws, "radius": args.radius,
        "seed": args.seed, "nulls": list(args.nulls),
        "chapels": n_chapels, "registry": str(args.registry),
        "fabric": str(args.fabric), "dem": str(args.dem),
        "registry_mtime": Path(args.registry).stat().st_mtime,
        "draw_version": DRAW_VERSION,
    }


def save_checkpoint(path, fp, done, cache, elapsed):
    """Atomically persist progress: finished draws and the pair cache.

    No generator state is stored. Every draw is seeded from
    (seed, stream, draw index), so resuming means re-deriving draw k
    rather than replaying a stream — which is both simpler and immune
    to a checkpoint written mid-stream."""
    keys = np.array([list(k) for k in cache.d], dtype=np.int32) \
        if cache and cache.d else np.zeros((0, 4), np.int32)
    vals = np.array(list(cache.d.values()), bool) if cache and cache.d \
        else np.zeros(0, bool)
    tmp = path.with_suffix(".tmp.npz")
    np.savez_compressed(
        tmp, meta=json.dumps({
            "fingerprint": fp, "done": {k: list(v) for k, v in done.items()},
            "elapsed_s": elapsed,
            "cache_audited": getattr(cache, "audited", 0),
            "cache_mismatches": getattr(cache, "mismatches", 0),
        }, default=str), cache_keys=keys, cache_vals=vals)
    tmp.replace(path)


def load_checkpoint(path, fp, cache):
    """Restore a checkpoint, or return None if it does not apply."""
    if not path.exists():
        return None
    with np.load(path, allow_pickle=False) as z:
        meta = json.loads(str(z["meta"]))
        if meta.get("fingerprint") != json.loads(json.dumps(fp, default=str)):
            warn("checkpoint ignored", "it was written for a different "
                 "configuration; delete it or change --checkpoint")
            return None
        if cache is not None:
            for k, v in zip(z["cache_keys"], z["cache_vals"]):
                cache.d[tuple(int(x) for x in k)] = bool(v)
    return meta


def run_cross_check(args, ch, base, device, draw_for):
    """Assert the cached path reproduces the exhaustive one exactly.

    Both runs are driven from the same seed, so draw k is the same
    permutation in each and the two V sequences must agree element by
    element — not merely in distribution. An equal mean with different
    draws would mean the cache is wrong in compensating directions,
    which is the failure this is here to catch.

    It does fail, and that is the finding rather than a defect here:
    at 25 draws it shows the memo drifting V by a few pairs on over
    half of them, which is why `--pair-cache` now defaults to off. Run
    it to reproduce that; expect a non-zero exit."""
    k = args.cross_check
    print(f"\nCROSS-CHECK: {k} N1 draws, cached vs exhaustive")

    t0 = time.time()
    slow = []
    for i in range(k):
        a, off = draw_for("N1", i)
        v, _, _ = visible_pairs(ch, a, base, device, args.radius, off)
        slow.append(v)
    t_slow = time.time() - t0

    t0 = time.time()
    cache = PairCache(args.cache_audit_rate, args.seed)
    fast = []
    for i in range(k):
        a, off = draw_for("N1", i)
        v, _, _ = visible_pairs(ch, a, base, device, args.radius, off,
                                cache)
        fast.append(v)
    t_fast = time.time() - t0

    bad = [(i, s, f) for i, (s, f) in enumerate(zip(slow, fast)) if s != f]
    check(not bad, "cached V matches exhaustive V on every draw",
          f"{len(bad)} of {k} differ: {bad[:5]}")
    check(cache.mismatches == 0,
          "in-run cache audit found no stale entries",
          f"{cache.mismatches} mismatch(es) over {cache.audited} re-casts")
    print(f"  exhaustive: {t_slow:6.1f}s  ({t_slow / k:.1f}s/draw)")
    print(f"  cached:     {t_fast:6.1f}s  ({t_fast / k:.1f}s/draw), "
          f"speed-up {t_slow / max(t_fast, 1e-9):.1f}x")
    print(f"  {cache.report()}")
    print(f"  V sequence: {slow[:8]}{' ...' if k > 8 else ''}")
    if failures:
        sys.exit(1)
    print("\ncross-check passed")


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
    p.add_argument("--pair-cache", dest="pair_cache",
                   action="store_true", default=False,
                   help="memoise (chapel, wall) pair visibility across "
                        "draws — N1 only. Approximate: a third "
                        "chapel's door flips ~6e-4 of repeated pairs, "
                        "moving V by a few (default: off)")
    p.add_argument("--no-pair-cache", dest="pair_cache",
                   action="store_false",
                   help="cast every pair every draw (default)")
    p.add_argument("--cache-audit-rate", type=float, default=0.02,
                   help="fraction of cache hits re-cast and compared, "
                        "so the memo keeps proving itself mid-run")
    p.add_argument("--checkpoint", type=Path, default=None,
                   help="progress file (default: <out-dir>/"
                        "intentionality_checkpoint.npz). Written after "
                        "every --checkpoint-every draws and on SIGINT/"
                        "SIGTERM, so a long run survives being stopped")
    p.add_argument("--checkpoint-every", type=int, default=10,
                   help="draws between checkpoint writes")
    p.add_argument("--resume", action="store_true",
                   help="continue from the checkpoint if its "
                        "configuration matches")
    p.add_argument("--sequential-h", type=int, default=10, metavar="H",
                   help="stop a null once H draws have reached V_obs "
                        "and report p = H/L (Besag-Clifford); 0 runs "
                        "the full --n-draws every time")
    p.add_argument("--cross-check", type=int, default=0, metavar="K",
                   help="run K draws of N1 both with and without the "
                        "cache and assert the V sequences match, then "
                        "exit without writing results")
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

    ids = sorted(assign)
    cent = {b: ch.geom[b].centroid for b in ids}

    def walls_from(perm):
        return {b: assign[o] % ch.n_walls(b) for b, o in zip(ids, perm)}

    def offsets_from(perm):
        """Move each chapel onto another's plot, ground included.

        `ch.floor` is the bare-earth elevation under the chapel, so
        the z term sets it down on the new plot at the same height
        above local ground it stands at on its own."""
        return {b: (cent[o].x - cent[b].x, cent[o].y - cent[b].y,
                    ch.floor[o] - ch.floor[b])
                for b, o in zip(ids, perm)}

    def draw_for(null, k):
        """Draw `k` of one null, as (wall assignment, position offsets).

        Each stream is seeded from (seed, stream id, draw index) rather
        than drawn from one running generator. Two consequences, both
        wanted: a resumed run reproduces draw k exactly by re-deriving
        it, with no generator state to carry; and **N2 and N3 share
        stream 2, so draw k of each uses the same position
        permutation**. That coupling is deliberate — common random
        numbers across the two nulls. Each null is still sampled
        correctly on its own, since both permutations remain uniform,
        and Holm-Bonferroni is valid under arbitrary dependence. What
        it buys is the contrast: N3 minus N2 is the question "does
        orientation add anything beyond position?", and sharing the
        layout cancels the position noise common to both instead of
        adding the variance of two independent draws."""
        if null == "N1":
            r = np.random.default_rng([args.seed, 1, k])
            return walls_from(r.permutation(ids)), None
        pos = np.random.default_rng([args.seed, 2, k]).permutation(ids)
        if null == "N2":
            return dict(assign), offsets_from(pos)
        r3 = np.random.default_rng([args.seed, 3, k])
        return walls_from(r3.permutation(ids)), offsets_from(pos)

    if args.cross_check:
        run_cross_check(args, ch, base, device, draw_for)
        return

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    ckpt = args.checkpoint or (args.out_dir /
                               "intentionality_checkpoint.npz")
    fprint = fingerprint(args, n_ch)
    cache = (PairCache(args.cache_audit_rate, args.seed)
             if args.pair_cache else None)
    done, elapsed0 = {}, 0.0
    if args.resume:
        meta = load_checkpoint(ckpt, fprint, cache)
        if meta:
            done = {k: list(v) for k, v in meta["done"].items()}
            elapsed0 = float(meta.get("elapsed_s", 0.0))
            print(f"resumed from {ckpt.name}: "
                  + ", ".join(f"{k} {len(v)}/{args.n_draws}"
                              for k, v in done.items())
                  + (f", cache {len(cache):,} entries" if cache else ""))

    t0 = time.time()
    rows, dists = [], {}
    for null in args.nulls:
        Vs = list(done.get(null, []))
        use_cache = cache if null in STATIC_NULLS else None
        if cache and use_cache is None and len(Vs) == 0:
            print(f"  {null}: pair cache off — this null moves chapels, "
                  "so a cached pair no longer describes the same scene")
        stopped_early = False
        while len(Vs) < args.n_draws:
            a, off = draw_for(null, len(Vs))
            v, _, _ = visible_pairs(ch, a, base, device, args.radius,
                                    off, use_cache)
            Vs.append(v)
            n = len(Vs)
            n_ge = int(np.sum(np.array(Vs) >= V_obs))
            if args.sequential_h and n_ge >= args.sequential_h:
                stopped_early = True
                print(f"  {null}: stopped at draw {n} — {n_ge} null "
                      f"draws reached V_obs, p = {n_ge / n:.4f}",
                      flush=True)
            if (n % args.checkpoint_every == 0 or _STOP["now"]
                    or stopped_early):
                done[null] = Vs
                save_checkpoint(ckpt, fprint, done, cache,
                                elapsed0 + time.time() - t0)
            if n % 25 == 0 or _STOP["now"]:
                print(f"  {null}: {n}/{args.n_draws} draws, "
                      f"{time.time() - t0:.0f}s this session"
                      + (f", {cache.report()}" if use_cache else ""),
                      flush=True)
            if _STOP["now"]:
                done[null] = Vs
                save_checkpoint(ckpt, fprint, done, cache,
                                elapsed0 + time.time() - t0)
                print(f"\npaused at {null} {len(Vs)}/{args.n_draws}. "
                      f"Resume with:\n  --resume --checkpoint {ckpt}")
                sys.exit(0)
            if stopped_early:
                break
        done[null] = Vs
        Vs = np.array(Vs, float)
        dists[null] = Vs
        n_ge = int(np.sum(Vs >= V_obs))
        # Besag-Clifford: a run curtailed on the h-th exceedance reads
        # p = h / L, and one that goes the distance keeps the usual
        # (1 + l) / (1 + n). Both are exact; mixing the two formulas is
        # what makes early stopping valid rather than a peek at the data.
        pval = (n_ge / len(Vs)) if stopped_early else \
            (1 + n_ge) / (1 + len(Vs))
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
