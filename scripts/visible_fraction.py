"""How much of a surface is visible, not just whether any of it is.

Every visibility answer in this project is currently a boolean: the
centroid is visible or it is not, the niche is seen or it is not. That
throws away most of the question. Standing at one chapel you may see a
sliver of a neighbour's west wall or the whole of it, and those are
different facts about the site — the first is a glimpse through a gap,
the second is a building you are looking at.

The primitive here is one-dimensional: given an eye and a segment,
what fraction of the segment is visible, and which parts. Surfaces are
built from it — a wall is a stack of horizontal segments, a footprint
is its four walls — so there is one piece of logic to get right and one
place where the sampling error is defined.

**Why not a plain binary search.** Cast to both ends of a wall; if one
is visible and the other is not, bisect for the boundary. That is
correct when there is exactly one transition, and it is wrong here.
Visibility along a wall is not monotone: a chapel standing in front can
shadow the *middle* of a wall while both ends stay visible, and several
occluders give several bands. A bisection between two disagreeing
endpoints finds one boundary, silently assumes it is the only one, and
returns a confident wrong number.

So the search is seeded with a coarse uniform sample first, and only
the intervals whose endpoints disagree get bisected. With one boundary
that degenerates to exactly the bisection above. With k boundaries it
finds all of them, provided the seed is fine enough that no shadow band
falls entirely between two seed points — which is a real assumption,
so `n_seed` is a documented argument and the residual is reported
rather than hidden: every call returns how many intervals were still
unresolved at the depth limit, and each contributes at most `tol / 2`
of length to the error.

Each refinement level batches all of its candidate points into a single
`visible_mask` call, so the cost is a few calls of a few points, not
one call per point.
"""

import argparse
import math

import numpy as np


def _probe(scene, eye, p0, p1, ts):
    """Visibility at parameters `ts` along p0->p1, in one batched call."""
    p0 = np.asarray(p0, float)
    p1 = np.asarray(p1, float)
    pts = p0[None, :] + (p1 - p0)[None, :] * np.asarray(ts, float)[:, None]
    return np.asarray(scene.visible_mask(eye, pts)) == 1


def visible_fraction(scene, eye, p0, p1, tol=0.05, n_seed=9,
                     max_depth=16):
    """Fraction of segment p0->p1 visible from `eye`.

    Returns `(fraction, intervals, n_unresolved)` where `intervals` is
    a list of visible `(t_lo, t_hi)` in [0, 1] and `n_unresolved` counts
    boundaries the depth limit stopped short of. Worst-case error in
    the fraction is `n_unresolved * tol / (2 * length)`.

    `tol` is in metres along the segment; `n_seed` sets how narrow a
    shadow band can be before it is missed entirely (a band must be
    wider than the seed spacing to be seen at all)."""
    p0 = np.asarray(p0, float)
    p1 = np.asarray(p1, float)
    L = float(np.linalg.norm(p1 - p0))
    if L < 1e-9:
        v = _probe(scene, eye, p0, p1, [0.0])[0]
        return (1.0 if v else 0.0), ([(0.0, 1.0)] if v else []), 0

    n_seed = max(2, int(n_seed))
    ts = list(np.linspace(0.0, 1.0, n_seed))
    vs = list(_probe(scene, eye, p0, p1, ts))

    # Intervals still straddling a boundary, refined breadth-first so
    # every level costs one batched call regardless of how many
    # boundaries are in play.
    pend = [(ts[i], ts[i + 1], vs[i], vs[i + 1])
            for i in range(len(ts) - 1) if vs[i] != vs[i + 1]]
    settled = [(ts[i], ts[i + 1], vs[i])
               for i in range(len(ts) - 1) if vs[i] == vs[i + 1]]

    depth = 0
    while pend and depth < max_depth:
        wide = [iv for iv in pend if (iv[1] - iv[0]) * L > tol]
        done = [iv for iv in pend if (iv[1] - iv[0]) * L <= tol]
        if not wide:
            pend = done
            break
        mids = [(a + b) / 2.0 for a, b, _, _ in wide]
        mv = _probe(scene, eye, p0, p1, mids)
        nxt = list(done)
        for (a, b, va, vb), m, vm in zip(wide, mids, mv):
            # The half whose ends agree is settled; the other still
            # holds a boundary. Both halves can hold one when the seed
            # straddled two, which is why this appends rather than
            # replaces.
            for lo, hi, vlo, vhi in ((a, m, va, vm), (m, b, vm, vb)):
                (nxt if vlo != vhi else settled).append(
                    (lo, hi, vlo, vhi) if vlo != vhi else (lo, hi, vlo))
        pend = nxt
        depth += 1

    # An unresolved interval is narrower than tol; split it at its
    # midpoint so the attributed length is wrong by at most tol/2.
    for a, b, va, vb in pend:
        m = (a + b) / 2.0
        settled.append((a, m, va))
        settled.append((m, b, vb))

    settled.sort()
    vis = [(a, b) for a, b, v in settled if v]
    merged = []
    for a, b in vis:
        if merged and a - merged[-1][1] < 1e-12:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    frac = float(sum(b - a for a, b in merged))
    return frac, merged, len(pend)


def quad_visible_fraction(scene, eye, a, b, z_lo, z_hi, rows=5,
                          tol=0.05, n_seed=9):
    """Visible *area* fraction of the vertical quad over segment a->b.

    The quad is sampled as `rows` horizontal segments at evenly spaced
    heights and their fractions averaged, which is a midpoint rule in
    the vertical: exact when the shadow boundary is vertical, and
    first-order in `rows` when it is not. Vertical resolution is left
    coarse on purpose — the horizontal direction is where a wall's
    occlusion structure actually lives, and that direction is adaptive.
    """
    a = np.asarray(a, float)[:2]
    b = np.asarray(b, float)[:2]
    zs = ((np.arange(rows) + 0.5) / rows) * (z_hi - z_lo) + z_lo
    fracs, unres = [], 0
    for z in zs:
        f, _iv, u = visible_fraction(scene, eye, (a[0], a[1], z),
                                     (b[0], b[1], z), tol, n_seed)
        fracs.append(f)
        unres += u
    return float(np.mean(fracs)), unres


class _MockScene:
    """Scene whose visibility is a known function of position.

    Lets the subdivision be tested against an exact answer without a
    DEM or a mesh in the way: any disagreement is this module's bug,
    not the ray-caster's."""

    def __init__(self, shadows):
        self.shadows = shadows          # [(t_lo, t_hi)] hidden bands
        self.calls = 0

    def visible_mask(self, eye, pts):
        self.calls += 1
        pts = np.asarray(pts, float)
        t = (pts[:, 0] - eye[0]) / 100.0          # x runs 0..100 -> t
        out = np.ones(len(pts), bool)
        for lo, hi in self.shadows:
            out &= ~((t >= lo) & (t < hi))
        return out


def run_self_test():
    from sanity_checks import check, failures

    eye = (0.0, -10.0, 1.5)
    p0, p1 = (0.0, 0.0, 1.0), (100.0, 0.0, 1.0)

    # Shadow bands are given an upper edge past t=1 throughout. The
    # mock hides a half-open [lo, hi), so a band ending exactly at 1.0
    # leaves the final endpoint visible and plants a second boundary
    # there — an artefact of the test fixture that would otherwise read
    # as an error in the search.
    L_M = 100.0

    # 1. Fully visible and fully hidden.
    f, iv, u = visible_fraction(_MockScene([]), eye, p0, p1, tol=0.05)
    check(abs(f - 1.0) < 1e-9 and u == 0, "self-test: clear segment",
          f"fraction {f:.4f}")
    f, iv, u = visible_fraction(_MockScene([(-0.1, 1.1)]), eye, p0, p1,
                                tol=0.05)
    check(f == 0.0 and not iv, "self-test: fully shadowed segment",
          f"fraction {f:.4f}")

    # 2. One boundary — the case a plain bisection also gets right.
    sc = _MockScene([(0.4, 1.1)])
    f, iv, u = visible_fraction(sc, eye, p0, p1, tol=0.05)
    check(abs(f - 0.4) * L_M <= 0.05 / 2 + 1e-9,
          "self-test: single boundary lands within tol/2",
          f"fraction {f:.5f} vs 0.4, {sc.calls} batched calls")

    # 3. A shadow band in the MIDDLE, both ends visible. This is the
    #    case that defeats a bisection seeded only on the endpoints:
    #    both agree "visible", so it would report 1.0 and never look.
    sc = _MockScene([(0.45, 0.55)])
    f, iv, u = visible_fraction(sc, eye, p0, p1, tol=0.05)
    check(abs(f - 0.9) <= 2 * 0.05 / 100.0 / 2 + 1e-9,
          "self-test: interior shadow band is found, not skipped",
          f"fraction {f:.5f} vs 0.9, {len(iv)} visible interval(s)")
    check(len(iv) == 2, "self-test: interior band splits the segment "
          "in two", f"{iv}")

    # 4. Three bands — several boundaries at once.
    sc = _MockScene([(0.1, 0.2), (0.4, 0.45), (0.8, 0.95)])
    f, iv, u = visible_fraction(sc, eye, p0, p1, tol=0.05, n_seed=17)
    expect = 1.0 - (0.1 + 0.05 + 0.15)
    check(abs(f - expect) <= 6 * 0.05 / 100.0 / 2 + 1e-9,
          "self-test: three shadow bands all resolved",
          f"fraction {f:.5f} vs {expect}, {len(iv)} interval(s)")

    # 5. A band narrower than the seed spacing and lying between two
    #    seeds is missed. Assert the documented failure mode rather
    #    than pretending the sampling is unconditional. (Placed off a
    #    seed on purpose: a band that happens to straddle one IS found,
    #    which is luck, not a guarantee.)
    sc = _MockScene([(0.51, 0.514)])
    f, _iv, _u = visible_fraction(sc, eye, p0, p1, tol=0.05, n_seed=5)
    check(f == 1.0, "self-test: sub-seed-spacing band between seeds is "
          "missed, as documented",
          f"fraction {f:.4f} (band is 0.4% of the span, seed spacing 25%)")

    # 6. Tightening tol tightens the answer.
    sc = _MockScene([(0.333, 1.1)])
    f_coarse, _i, _u = visible_fraction(sc, eye, p0, p1, tol=1.0)
    f_fine, _i, _u = visible_fraction(sc, eye, p0, p1, tol=0.01)
    check(abs(f_fine - 0.333) <= abs(f_coarse - 0.333) + 1e-12,
          "self-test: smaller tol is at least as accurate",
          f"coarse {f_coarse:.5f}, fine {f_fine:.5f}, true 0.333")

    # 6b. The tol/2 bound is the module's actual promise, so assert it
    #     over many boundary positions rather than one lucky case.
    rng = np.random.default_rng(0)
    worst = 0.0
    for tol in (0.5, 0.05, 0.005):
        for bnd in rng.uniform(0.05, 0.95, 120):
            f, _i, _u = visible_fraction(_MockScene([(bnd, 1.1)]), eye,
                                         p0, p1, tol=tol)
            worst = max(worst, abs(f - bnd) * L_M / tol)
    check(worst <= 0.5 + 1e-9,
          "self-test: error never exceeds tol/2 over 360 boundaries",
          f"worst observed {worst:.3f} x tol")

    # 7. Cost stays logarithmic in tol, not linear.
    sc = _MockScene([(0.4, 0.6)])
    visible_fraction(sc, eye, p0, p1, tol=0.5)
    coarse_calls = sc.calls
    sc2 = _MockScene([(0.4, 0.6)])
    visible_fraction(sc2, eye, p0, p1, tol=0.005)
    check(sc2.calls <= coarse_calls + 8,
          "self-test: 100x finer tol costs a few more batched calls",
          f"{coarse_calls} -> {sc2.calls} calls")

    print(f"\n{len(failures)} check(s) failed" if failures
          else "\nall self-tests passed")
    return 1 if failures else 0


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true",
                   help="run the analytic checks and exit")
    return p


def main():
    args = build_parser().parse_args()
    if args.self_test:
        raise SystemExit(run_self_test())
    build_parser().print_help()


if __name__ == "__main__":
    main()
