"""Georeference Site_Plan.pdf and seed door candidates for the registry.

`Task_2/Site_Plan.pdf` is a vector print of the full-site CAD drawing —
per its README, "showing ... the direction of doorway and openings"
(1:5000). Its linework carries no layer/style separation (one gray, one
width), but it has two machine-readable hooks:

1. 131 chapel-number text labels (decoded via the font's ToUnicode
   map) whose values equal footprint IDs — enough correspondences to
   fit an affine page->UTM transform by least squares, with iterative
   outlier rejection.
2. 2,338 line segments which, once transformed, land on the footprint
   walls. Doorways in plan drawings are *gaps* in the wall linework,
   so per canonical wall this script accumulates the covered
   intervals and reports interior uncovered spans of door-like width
   as candidates.

The candidates are seeds, not truth: every labeled chapel gets a QC
tile (footprint + fitted linework + wall indices + candidates) for a
fast human confirm/correct pass directly in the registry CSV. If
`aperture_inventory.csv` does not exist yet it is seeded from the
candidates; if it exists it is NEVER touched — candidates go to
`siteplan_candidates.csv` and a printed diff summary instead (the
dome-inventory hand-edit rule).
"""

import argparse
import csv
import json
import re
import sys
import zlib
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from sanity_checks import FOOTPRINTS, check, warn, failures
from aperture_registry import (APERTURES_DIR, INVENTORY, REGISTRY_COLS,
                               DOOR_HEAD, DOOR_SILL, canonical_walls,
                               wall_fields)

SITE_PLAN = Path(__file__).resolve().parent.parent / "Task_2/Site_Plan.pdf"


def pdf_streams(raw):
    """Byte ranges of every stream in the file, decompressed when
    FlateDecode. Minimal on purpose — this one generated PDF, not the
    format in general (same stance as the report-plate extractor)."""
    out = []
    for m in re.finditer(rb"stream\r?\n", raw):
        s, e = m.end(), raw.find(b"endstream", m.end())
        data = raw[s:e]
        try:
            data = zlib.decompress(data)
        except zlib.error:
            pass
        out.append(data)
    return out


def parse_cmap(streams):
    """code -> character from the font's ToUnicode bfchar table."""
    for data in streams:
        if b"beginbfchar" not in data:
            continue
        pairs = re.findall(rb"<([0-9A-Fa-f]{4})>\s*<([0-9A-Fa-f]{4})>",
                           data.split(b"beginbfchar")[1])
        return {int(a, 16): chr(int(b, 16)) for a, b in pairs}
    return {}


def mat_mult(m1, m2):
    """PDF matrix concatenation (a b c d e f), row-vector convention."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
            c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2)


def mat_apply(m, x, y):
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


def parse_content(content, cmap):
    """Interpret the (line-oriented, printer-generated) content stream.

    Tracks the q/Q graphics-state stack and cm concatenations so every
    path coordinate and text anchor comes out in page space. Only the
    operators this file actually uses are handled — anything else is
    ignored, which is safe because unknown operators cannot move the
    pen. Returns (segments [((x0,y0),(x1,y1))], labels [(text,x,y)])."""
    ident = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    ctm, stack = ident, []
    cur = start = None
    tm = ident
    segments, labels, text_parts = [], [], []
    in_text = False
    for line in content.splitlines():
        toks = line.split()
        if not toks:
            continue
        op = toks[-1]
        if op == "q":
            stack.append(ctm)
        elif op == "Q":
            ctm = stack.pop() if stack else ident
        elif op == "cm" and len(toks) >= 7:
            ctm = mat_mult(tuple(float(t) for t in toks[:6]), ctm)
        elif op == "m" and len(toks) >= 3:
            cur = start = mat_apply(ctm, float(toks[0]), float(toks[1]))
        elif op == "l" and len(toks) >= 3 and cur is not None:
            p = mat_apply(ctm, float(toks[0]), float(toks[1]))
            segments.append((cur, p))
            cur = p
        elif op == "h" and cur is not None and start is not None:
            segments.append((cur, start))
            cur = start
        elif op == "BT":
            in_text, tm, text_parts = True, ident, []
        elif op == "Tm" and len(toks) >= 7:
            tm = tuple(float(t) for t in toks[:6])
        elif op == "TJ" and in_text:
            for hexs in re.findall(r"<([0-9A-Fa-f]+)>", line):
                for i in range(0, len(hexs), 4):
                    text_parts.append(cmap.get(int(hexs[i:i + 4], 16),
                                               "?"))
        elif op == "ET":
            in_text = False
            if text_parts:
                x, y = mat_apply(mat_mult(tm, ctm), 0.0, 0.0)
                labels.append(("".join(text_parts), x, y))
    return segments, labels


def fit_affine(src_xy, dst_xy):
    """Least-squares 6-parameter affine src -> dst with iterative
    MAD outlier rejection. Returns (matrix (2,3), residuals, keep).

    The cutoff is floored at 5 m: labels legitimately sit a few metres
    from their footprint centroid (drawn beside small chapels), so a
    tight MAD band on an otherwise excellent fit would reject normal
    placement offsets instead of actual mismatches."""
    src = np.asarray(src_xy, float)
    dst = np.asarray(dst_xy, float)
    keep = np.ones(len(src), bool)
    mat = None
    for _ in range(10):
        A = np.column_stack([src[keep], np.ones(keep.sum())])
        mat, *_ = np.linalg.lstsq(A, dst[keep], rcond=None)
        pred = np.column_stack([src, np.ones(len(src))]) @ mat
        res = np.linalg.norm(pred - dst, axis=1)
        med = np.median(res[keep])
        mad = np.median(np.abs(res[keep] - med)) or 1e-9
        new_keep = res <= max(med + 3 * 1.4826 * mad, 5.0)
        if (new_keep == keep).all():
            break
        keep = new_keep
    return mat.T, res, keep


def best_local_shift(walls, segs, radius=3.0, step=0.25, corridor=0.6):
    """Small per-building translation that best registers the plan
    linework onto the footprint walls.

    The global affine is fit from label positions, so individual
    buildings can sit a metre or three off their footprint (the plan
    and the photogrammetric footprints are independent drawings).
    Searching a translation grid for maximum wall coverage fixes that
    without letting any single building distort the global fit.
    Vectorized per wall: in the wall's (s, n) frame a shift d is just
    a constant (u . d, n . d) offset to every segment."""
    if not segs:
        return (0.0, 0.0)
    per_wall = []
    for p0, p1 in walls:
        p0 = np.asarray(p0)
        u = np.asarray(p1) - p0
        L = float(np.linalg.norm(u))
        u = u / L
        n = np.array([u[1], -u[0]])
        a = np.array([s[0] for s in segs]) - p0
        b = np.array([s[1] for s in segs]) - p0
        per_wall.append((u, n, L, a @ u, b @ u, a @ n, b @ n))
    shifts = np.arange(-radius, radius + step / 2, step)
    best, best_cov = (0.0, 0.0), -1.0
    zero_cov = 0.0
    for dx in shifts:
        for dy in shifts:
            d = np.array([dx, dy])
            cov = 0.0
            for u, n, L, sa, sb, na, nb in per_wall:
                ds, dn = float(u @ d), float(n @ d)
                inside = np.maximum(np.abs(na + dn),
                                    np.abs(nb + dn)) <= corridor
                lo = np.clip(np.minimum(sa, sb)[inside] + ds, 0, L)
                hi = np.clip(np.maximum(sa, sb)[inside] + ds, 0, L)
                cov += float((hi - lo).sum())
            if dx == 0.0 and dy == 0.0:
                zero_cov = cov
            if cov > best_cov:
                best_cov, best = cov, (float(dx), float(dy))
    # Prefer no shift unless moving genuinely helps — a near-tie jump
    # would just add noise to the gap positions.
    if zero_cov >= 0.95 * best_cov:
        return (0.0, 0.0)
    return best


def wall_coverage(walls, segs, corridor=0.8):
    """Per wall: merged covered intervals along its axis, from plan
    segments lying within `corridor` metres of the wall line."""
    out = []
    for p0, p1 in walls:
        p0 = np.asarray(p0)
        u = np.asarray(p1) - p0
        L = float(np.linalg.norm(u))
        u = u / L
        n = np.array([u[1], -u[0]])
        ivals = []
        for a, b in segs:
            sa = np.asarray(a) - p0
            sb = np.asarray(b) - p0
            if max(abs(sa @ n), abs(sb @ n)) > corridor:
                continue
            s0, s1 = sorted((float(sa @ u), float(sb @ u)))
            s0, s1 = max(s0, 0.0), min(s1, L)
            if s1 > s0:
                ivals.append((s0, s1))
        merged = []
        for s0, s1 in sorted(ivals):
            if merged and s0 <= merged[-1][1] + 0.05:
                merged[-1][1] = max(merged[-1][1], s1)
            else:
                merged.append([s0, s1])
        out.append((L, merged))
    return out


def find_gaps(coverage, min_w=0.4, max_w=2.0, min_cover=0.5):
    """Uncovered spans of door-like width, measured around the whole
    footprint ring rather than per wall.

    Working in ring-arclength (walls concatenated, wrapping at the
    last vertex) is what lets an opening that straddles a corner
    register as one gap: chapel 181's doorway sits exactly on a vertex
    of the octagon its circular footprint canonicalizes to, and a
    per-wall scan either splits it in two or discards it as a
    wall-end artifact. Coverage is judged ring-wide for the same
    reason — the plan often draws some walls of a chapel and not
    others, and an undrawn wall is absence of evidence, not a door."""
    lengths = [L for L, _ in coverage]
    total = sum(lengths)
    starts = np.concatenate([[0.0], np.cumsum(lengths)[:-1]])
    spans = [(starts[wi] + s0, starts[wi] + s1)
             for wi, (_, merged) in enumerate(coverage)
             for s0, s1 in merged]
    if not spans or sum(b - a for a, b in spans) < min_cover * total:
        return []
    merged = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1] + 0.05:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    gaps = []
    for (_, a), (b, _) in zip(merged, merged[1:]):
        gaps.append((a, b - a))
    # The wrap-around gap between the last covered span and the first.
    wrap = (total - merged[-1][1]) + merged[0][0]
    if wrap > 0.01:
        gaps.append((merged[-1][1], wrap))
    out = []
    for start, w in gaps:
        if not min_w <= w <= max_w:
            continue
        mid = (start + w / 2) % total
        wi = int(np.searchsorted(starts, mid, side="right") - 1)
        out.append((wi, mid - starts[wi], w))
    return out


def wall_stubs(walls, segs, max_perp=0.35, min_len=0.15, max_len=4.0,
               near_tol=0.6, min_depth=0.3, max_depth=3.0):
    """Per wall: plan segments running roughly *perpendicular* into the
    building from that wall — [(s, depth, length)].

    This is the load-bearing signature. These drawings mark a doorway
    not as a hole in the wall line but as the passage through the wall
    thickness: two short lines running inward, one per jamb (confirmed
    on chapel 180, whose real door is a 0.72 m x 1.4 m passage while
    its wall line runs unbroken across the opening)."""
    per_wall = {}
    for wi, (p0, p1) in enumerate(walls):
        p0 = np.asarray(p0)
        u = np.asarray(p1) - p0
        L = float(np.linalg.norm(u))
        u = u / L
        n = np.array([u[1], -u[0]])            # outward normal
        found = []
        for a, b in segs:
            a, b = np.asarray(a), np.asarray(b)
            d = b - a
            ln = float(np.linalg.norm(d))
            if not min_len <= ln <= max_len:
                continue
            if abs(float(d @ u)) / ln > max_perp:
                continue                       # not perpendicular enough
            sa, na = float((a - p0) @ u), float((a - p0) @ n)
            sb, nb = float((b - p0) @ u), float((b - p0) @ n)
            if min(abs(na), abs(nb)) > near_tol:
                continue                       # doesn't touch the wall
            depth = -min(na, nb)               # + = reaches inside
            if not min_depth <= depth <= max_depth:
                continue
            s = (sa + sb) / 2
            if -0.1 <= s <= L + 0.1:
                found.append((s, depth, ln))
        per_wall[wi] = sorted(found)
    return per_wall


def find_passages(walls, segs, min_w=0.5, max_w=1.6):
    """Jamb pairs -> [(wall, s, width, depth)], best pair per wall.

    Two perpendicular stubs the right distance apart bracket a
    doorway. Preference goes to the deepest pair (a real passage runs
    the wall thickness) and, among equals, the most door-like width."""
    out = []
    for wi, stubs in wall_stubs(walls, segs).items():
        best = None
        for i in range(len(stubs)):
            for j in range(i + 1, len(stubs)):
                w = stubs[j][0] - stubs[i][0]
                if not min_w <= w <= max_w:
                    continue
                depth = min(stubs[i][1], stubs[j][1])
                score = (depth, -abs(w - 0.9))
                if best is None or score > best[0]:
                    best = (score, (stubs[i][0] + stubs[j][0]) / 2, w,
                            depth)
        if best is not None:
            out.append((wi, best[1], best[2], best[3]))
    return out


def detect_openings(walls, segs, corridor=0.8):
    """Combine both signatures into ranked door candidates.

    Returns [(wall, s, width, confidence, evidence)]. A passage and a
    gap agreeing on the same spot corroborate each other and promote
    the candidate to high confidence; a lone signal stays low unless
    it is the building's only candidate and geometrically clean."""
    passages = find_passages(walls, segs)
    gaps = find_gaps(wall_coverage(walls, segs, corridor=corridor))
    gaps = [g for g in gaps if 0 <= g[0] < len(walls)]
    cands = []
    used_gaps = set()
    for wi, s, w, depth in passages:
        ev, conf = f"passage (jambs {w:.2f} m apart, {depth:.2f} m deep)", "low"
        for gi, (gwi, gs, gw) in enumerate(gaps):
            if gwi == wi and abs(gs - s) < max(0.6, w):
                used_gaps.add(gi)
                ev += f" + wall-line gap {gw:.2f} m"
                conf = "high"
                break
        if conf != "high" and depth >= 0.5 and 0.6 <= w <= 1.5:
            conf = "med"
        cands.append((wi, s, w, conf, ev))
    for gi, (wi, s, w) in enumerate(gaps):
        if gi in used_gaps:
            continue
        cands.append((wi, s, w, "low", f"wall-line gap {w:.2f} m only"))
    # A single candidate on a building is more trustworthy than one of
    # several competing ones; demote everything when the plan offers a
    # crowd of equally-plausible openings.
    if len(cands) > 3:
        cands = [(a, b, c, "low", e + "; 4+ competing candidates")
                 for a, b, c, _, e in cands]
    order = {"high": 0, "med": 1, "low": 2}
    return sorted(cands, key=lambda c: (order[c[3]], -c[2]))


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pdf", type=Path, default=SITE_PLAN,
                   help="vector site plan (print of the site CAD)")
    p.add_argument("--footprints", type=Path, default=FOOTPRINTS,
                   help="footprint polygons (IDs join the plan labels)")
    p.add_argument("--out-dir", type=Path, default=APERTURES_DIR,
                   help="where georef/candidates/tiles/overlay land")
    p.add_argument("--corridor", type=float, default=0.8,
                   help="max distance (m) from a wall for plan "
                        "linework to count as covering it")
    p.add_argument("--no-tiles", action="store_true",
                   help="skip the per-chapel QC tiles (fast rerun)")
    return p


def main():
    args = build_parser().parse_args()
    check(args.pdf.exists(), "site plan PDF exists", str(args.pdf))
    if failures:
        sys.exit(1)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    streams = pdf_streams(args.pdf.read_bytes())
    cmap = parse_cmap(streams)
    check(len(cmap) == 10, "digit ToUnicode map decoded",
          f"{len(cmap)} codes")
    # The page content stream is the one with drawing + text operators
    # (the other streams are the font program and PDF dictionaries).
    content = max((s for s in streams if b"] TJ" in s and b" cm" in s),
                  key=len).decode("latin-1")
    segments, labels = parse_content(content, cmap)
    print(f"  parsed: {len(segments):,} segments, {len(labels)} labels")
    check(len(labels) >= 120, "chapel labels decoded",
          f"{len(labels)} (expect ~131)")

    fp = gpd.read_file(args.footprints)
    cent = {int(r["ID"]): (r.geometry.centroid.x, r.geometry.centroid.y)
            for _, r in fp.iterrows()}
    src, dst, ids = [], [], []
    for text, x, y in labels:
        try:
            bid = int(text)
        except ValueError:
            warn("unparseable label", repr(text))
            continue
        if bid in cent:
            src.append((x, y))
            dst.append(cent[bid])
            ids.append(bid)
        else:
            warn("label matches no footprint", text)
    mat, res, keep = fit_affine(src, dst)
    scale = float(np.sqrt(abs(np.linalg.det(mat[:, :2]))))
    med_res = float(np.median(res[keep]))
    print(f"  affine fit: {int(keep.sum())}/{len(src)} labels kept, "
          f"median residual {med_res:.2f} m, scale {scale:.3f} m/pt")
    check(med_res < 3.0, "georeference median residual < 3 m",
          f"{med_res:.2f} m")
    check(keep.mean() >= 0.9, "few affine outliers",
          f"{int((~keep).sum())} rejected")
    (args.out_dir / "siteplan_georef.json").write_text(json.dumps({
        "matrix": mat.tolist(), "n_labels": len(src),
        "n_kept": int(keep.sum()), "median_residual_m": med_res,
        "scale_m_per_pt": scale,
        "residuals": {str(b): round(float(r), 2)
                      for b, r in zip(ids, res)}}, indent=2) + "\n")

    def to_utm(p):
        return tuple(mat @ np.array([p[0], p[1], 1.0]))

    segs_utm = [(to_utm(a), to_utm(b)) for a, b in segments]

    # Gap detection per labeled chapel; every labeled chapel gets a QC
    # tile regardless, so a detection miss degrades to manual entry.
    tiles = args.out_dir / "siteplan_tiles"
    if not args.no_tiles:
        tiles.mkdir(exist_ok=True)
    seg_arr = np.array([[a[0], a[1], b[0], b[1]] for a, b in segs_utm])
    cand_rows = []
    n_with = 0
    conf_counts = {"high": 0, "med": 0, "low": 0}
    for bid in sorted(set(ids)):
        geom = fp[fp["ID"] == bid].iloc[0].geometry
        walls = canonical_walls(geom)
        minx, miny, maxx, maxy = geom.bounds
        near = ((seg_arr[:, [0, 2]].max(1) > minx - 3)
                & (seg_arr[:, [0, 2]].min(1) < maxx + 3)
                & (seg_arr[:, [1, 3]].max(1) > miny - 3)
                & (seg_arr[:, [1, 3]].min(1) < maxy + 3))
        local = [((r[0], r[1]), (r[2], r[3])) for r in seg_arr[near]]
        dx, dy = best_local_shift(walls, local)
        if (dx, dy) != (0.0, 0.0):
            local = [((a[0] + dx, a[1] + dy), (b[0] + dx, b[1] + dy))
                     for a, b in local]
        gaps = detect_openings(walls, local, corridor=args.corridor)
        if gaps:
            n_with += 1
            conf_counts[gaps[0][3]] += 1
        shift_note = (f"; local shift ({dx:+.2f}, {dy:+.2f}) m"
                      if (dx, dy) != (0.0, 0.0) else "")
        for k, (wi, s_m, w, conf, ev) in enumerate(gaps, 1):
            az, mx, my = wall_fields(walls, wi)
            cand_rows.append({
                "ID": bid, "ap_id": k, "kind": "door", "wall": wi,
                "s_m": round(s_m, 2), "width_m": round(w, 2),
                "sill_m": DOOR_SILL, "head_m": DOOR_HEAD,
                "wall_az": az, "wall_mx": mx, "wall_my": my,
                "source_pos": "siteplan", "source_dims": "default",
                "confidence": conf,
                "notes": f"{ev}; confirm on tile" + shift_note})
        if not args.no_tiles:
            fig, ax = plt.subplots(figsize=(6, 6))
            for a, b in local:
                ax.plot([a[0], b[0]], [a[1], b[1]], color="0.55",
                        lw=1.0)
            xs, ys = geom.exterior.xy
            ax.plot(xs, ys, color="c", lw=1.2)
            for wi, (p0, p1) in enumerate(walls):
                ax.annotate(str(wi), ((p0[0] + p1[0]) / 2,
                                      (p0[1] + p1[1]) / 2),
                            color="b", fontsize=11, ha="center")
            conf_color = {"high": "lime", "med": "orange", "low": "r"}
            for wi, s_m, w, conf, _ in gaps:
                p0, p1 = walls[wi]
                u = np.subtract(p1, p0)
                u = u / np.linalg.norm(u)
                c = np.asarray(p0) + u * s_m
                ax.plot(*zip(np.asarray(p0) + u * (s_m - w / 2),
                             np.asarray(p0) + u * (s_m + w / 2)),
                        color=conf_color[conf], lw=4, alpha=0.8)
                ax.annotate(f"w{wi} {w:.1f}m {conf}", c,
                            color=conf_color[conf], fontsize=9)
            ax.set_title(f"chapel {bid} — site-plan linework vs walls"
                         f"{' — ' + str(len(gaps)) + ' candidate(s)' if gaps else ' — no gap found'}")
            # Frame on the footprint, not the linework: a single stray
            # far-away segment inside the search box would otherwise
            # autoscale the axes to hundreds of metres and squash the
            # chapel to an unreadable smear.
            cx, cy = geom.centroid.x, geom.centroid.y
            r = max(maxx - minx, maxy - miny) / 2 + 6.0
            ax.set_xlim(cx - r, cx + r)
            ax.set_ylim(cy - r, cy + r)
            ax.set_aspect("equal")
            fig.savefig(tiles / f"chapel_{bid:03d}.png", dpi=110,
                        bbox_inches="tight")
            plt.close(fig)
    print(f"  door detection: {len(cand_rows)} candidates on "
          f"{n_with}/{len(set(ids))} labeled chapels "
          f"(best-per-chapel confidence: {conf_counts['high']} high, "
          f"{conf_counts['med']} med, {conf_counts['low']} low)")

    # Whole-site overlay.
    fig, ax = plt.subplots(figsize=(10, 14))
    for a, b in segs_utm:
        ax.plot([a[0], b[0]], [a[1], b[1]], color="0.6", lw=0.4)
    for _, r in fp.iterrows():
        xs, ys = r.geometry.exterior.xy
        ax.plot(xs, ys, color="c", lw=0.4)
    for row in cand_rows:
        ax.plot(row["wall_mx"], row["wall_my"], "r.", ms=3)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.set_title("Site_Plan.pdf georeferenced over footprints "
                 "(red = door candidates)")
    fig.savefig(args.out_dir / "siteplan_overlay.png", dpi=200,
                bbox_inches="tight")
    plt.close(fig)

    # Candidates always; the registry only if absent (hand-edit rule).
    cand_csv = args.out_dir / "siteplan_candidates.csv"
    with open(cand_csv, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=REGISTRY_COLS)
        wr.writeheader()
        wr.writerows(cand_rows)
    print(f"  wrote {cand_csv.name} ({len(cand_rows)} rows)")
    if not INVENTORY.exists():
        with open(INVENTORY, "w", newline="") as f:
            wr = csv.DictWriter(f, fieldnames=REGISTRY_COLS)
            wr.writeheader()
            wr.writerows(cand_rows)
        print(f"  seeded {INVENTORY.name} — confirm rows against the "
              "tiles, then edit freely (it is never overwritten)")
    else:
        have = set()
        with open(INVENTORY) as f:
            for row in csv.DictReader(f):
                have.add((int(row["ID"]), int(row["ap_id"])))
        new = [r for r in cand_rows if (r["ID"], r["ap_id"]) not in have]
        print(f"  {INVENTORY.name} exists — left untouched "
              f"({len(new)} candidate rows not present in it; merge "
              f"by hand from {cand_csv.name})")

    if failures:
        print(f"\n{len(failures)} check(s) failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
