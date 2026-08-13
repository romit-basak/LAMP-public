"""Curate extracted niches into the registry as wall recesses.

The sibling of `curate_windows.py`, for the other half of what the
report describes inside a chapel. The difference that matters is that a
niche does not perforate: it is a recess cut into the inner face, so it
changes how deep the first hit is and what a wall looks like from
inside, without ever letting a ray through. `build_aperture_walls.py`
already models that — this is the first time real data has used the
path, which until now existed only under `--self-test`.

Three things this has to get right:

**Depth against thickness.** The default recess is 0.15 m and
`row_depth` clamps it to 0.6x the wall, so a Type 1 wall at one brick
(0.17 m) admits only 0.10 m. That clamp is the whole reason the
perforation gate was built first: an unclamped 0.15 m niche in a 0.17 m
wall leaves 0.02 m of fabric, and any rounding turns it into a hole
through the chapel, which would inflate the aperture effect this
project exists to measure.

**Stacking, not collision.** The report repeatedly puts a light
aperture directly above a niche — "an aperture for light is over every
one of these niches". Those share a position along the wall and differ
only in height, which is exactly the arrangement the column-sweep
rewrite of `wall_panel()` was built to represent. So a niche on a wall
that already carries an aperture is placed at the *same* distance along
it and its head clipped to clear the sill, rather than being shoved
sideways into a position the report does not describe.

**Doors are different.** A door spans the full wall height, so there is
no height at which a niche can share its position. Those are offset
along the wall instead, the same way apertures are.

Writes `niche_candidates.csv`; `--merge` appends to the registry after
backing it up, and never touches an existing row.
"""

import argparse
import csv
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from sanity_checks import FOOTPRINTS, check, warn, failures
from aperture_registry import (APERTURES_DIR, INVENTORY, REGISTRY_COLS,
                               NICHE_SILL, NICHE_HEIGHT, NICHE_WIDTH,
                               APSE_SILL, APSE_HEIGHT, APSE_WIDTH,
                               KINDS, canonical_walls, wall_for_azimuth,
                               wall_fields)
from apertures_from_report import parse_direction
from curate_windows import positions

# Vertical clearance kept between a niche head and the sill of an
# aperture stacked above it, so the all-pairs overlap check has margin
# rather than sitting exactly on its tolerance.
STACK_GAP_M = 0.06

# Per-kind opening defaults; see the constants' own notes on how thin
# the evidence is. `traced` lists chapels whose feature is already real
# geometry in the footprint and so must not also become a registry row,
# or it would be modelled twice.
KIND_DEFAULTS = {
    "niche": dict(sill=NICHE_SILL, height=NICHE_HEIGHT,
                  width=NICHE_WIDTH, max_per_wall=2, traced=()),
    "apse":  dict(sill=APSE_SILL, height=APSE_HEIGHT,
                  width=APSE_WIDTH, max_per_wall=1, traced=(205,)),
}


def wall_openings(reg_rows, bid, wi):
    """Existing (kind, s, width, sill, head) on one wall of a chapel."""
    out = []
    for r in reg_rows:
        if int(r["ID"]) != bid or str(r["wall"]).strip() == "":
            continue
        if int(r["wall"]) != wi:
            continue
        try:
            out.append((r["kind"], float(r["s_m"]), float(r["width_m"]),
                        float(r["sill_m"]), float(r["head_m"])))
        except (TypeError, ValueError):
            continue
    return out


def place(length, n, existing, width, sill, height):
    """[(s, sill, head, stacked)] for n niches on one wall.

    Prefers to sit under an aperture where one exists, since that is
    the arrangement the report describes; otherwise spaces evenly clear
    of everything already on the wall."""
    doors = [(s, w) for k, s, w, _, _ in existing if k == "door"]
    slits = [(s, w, sl, hd) for k, s, w, sl, hd in existing
             if k == "window"]
    out = []

    # Stack under existing apertures first, tallest-priority order.
    for s, w, sl, _hd in sorted(slits, key=lambda t: t[0]):
        if len(out) >= n:
            break
        head = min(sill + height, sl - STACK_GAP_M)
        if head - sill < 0.15:
            continue                      # no room under the slit
        out.append((s, sill, round(head, 2), True))

    if len(out) >= n:
        return out[:n]

    # Remaining niches go on free wall, clear of the door and of the
    # positions already taken.
    door = doors[0] if doors else None
    taken = [s for s, _, _, _ in out]
    free = positions(length, n - len(out), door, width)
    for s in free:
        if len(out) >= n:
            break
        if any(abs(s - t) < width + 0.05 for t in taken):
            continue
        out.append((s, sill, round(sill + height, 2), False))
        taken.append(s)
    return out


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kind", choices=sorted(KIND_DEFAULTS),
                   default="niche",
                   help="which recess kind to curate; both build the "
                        "same non-perforating geometry and differ only "
                        "in default size and how many fit on a wall")
    p.add_argument("--candidates", type=Path,
                   default=APERTURES_DIR / "report_features_candidates.csv")
    p.add_argument("--footprints", type=Path, default=FOOTPRINTS)
    p.add_argument("--registry", type=Path, default=INVENTORY)
    p.add_argument("--width", type=float)
    p.add_argument("--sill", type=float)
    p.add_argument("--height", type=float)
    p.add_argument("--depth", type=float, default=None,
                   help="recess depth (m) before clamping; blank leaves "
                        "the KINDS default so the builder derives and "
                        "clamps it per wall")
    p.add_argument("--max-per-wall", type=int,
                   help="refuse to derive more than this many positions "
                        "on one wall from a bare count")
    p.add_argument("--out", type=Path,
                   help="candidates CSV; defaults to <kind>_candidates.csv")
    p.add_argument("--merge", action="store_true",
                   help="append into the registry (backed up first); "
                        "existing rows are never modified")
    return p


def main():
    args = build_parser().parse_args()
    dflt = KIND_DEFAULTS[args.kind]
    for name in ("width", "sill", "height", "max_per_wall"):
        if getattr(args, name) is None:
            setattr(args, name, dflt[name.replace("max_per_wall",
                                                  "max_per_wall")])
    if args.out is None:
        args.out = APERTURES_DIR / f"{args.kind}_candidates.csv"
    cand = pd.read_csv(args.candidates)
    cand = cand[cand["kind"] == args.kind].copy()
    has_wall = cand["wall"].notna() & (
        cand["wall"].astype("object").astype(str).str.strip() != "")
    placed = cand[has_wall].copy()
    placed["wall"] = placed["wall"].astype("object").astype(str).str.strip()
    traced = set(dflt["traced"])
    if traced:
        n_tr = int(placed["ID"].isin(traced).sum())
        placed = placed[~placed["ID"].isin(traced)]
        print(f"  {n_tr} row(s) skipped on chapels whose {args.kind} is "
              f"already traced in the footprint: {sorted(traced)}")
    check(len(placed) > 0, f"wall-anchored {args.kind} candidates",
          f"{len(placed)} of {len(cand)} {args.kind} rows carry a wall")
    print(f"  {len(cand) - len(placed)} counted-but-unplaced rows are "
          f"left in the candidates file, not built")

    fp = gpd.read_file(args.footprints)
    geoms = {int(r["ID"]): r.geometry for _, r in fp.iterrows()}
    reg = list(csv.DictReader(open(args.registry, newline="")))
    next_ap = {}
    for r in reg:
        bid = int(r["ID"])
        next_ap[bid] = max(next_ap.get(bid, 0), int(r["ap_id"]))

    rows = []
    n_short = n_nowall = n_ambig = n_stacked = 0
    for (bid, wall_dir), grp in placed.groupby(["ID", "wall"]):
        bid = int(bid)
        if bid not in geoms:
            warn(f"ID {bid}: no footprint", "skipped")
            n_nowall += 1
            continue
        az = parse_direction(wall_dir)
        if az is None:
            warn(f"ID {bid}: unparseable wall {wall_dir!r}", "skipped")
            n_nowall += 1
            continue
        walls = canonical_walls(geoms[bid])
        wi, err, runner = wall_for_azimuth(walls, az)
        if err > 45.0:
            warn(f"ID {bid}: no wall faces {wall_dir}",
                 f"best is {err:.0f} deg off — skipped")
            n_nowall += 1
            continue
        ambiguous = runner - err < 15.0
        n_ambig += ambiguous
        p0, p1 = walls[wi]
        L = float(np.hypot(p1[0] - p0[0], p1[1] - p0[1]))
        wall_az, mx, my = wall_fields(walls, wi)

        n = int(max(1, min(args.max_per_wall,
                           grp["n_stated"].max() if
                           grp["n_stated"].notna().any() else 1)))
        existing = wall_openings(reg, bid, wi)
        got = place(L, n, existing, args.width, args.sill, args.height)
        if len(got) < n:
            warn(f"ID {bid} wall {wi} ({wall_dir}): no room for {n} "
                 f"niche(s)", f"placed {len(got)} on {L:.2f} m")
            n_short += n - len(got)
        phrase = str(grp["phrase"].iloc[0])
        page = grp["page"].iloc[0]
        for k, (s, sill, head, stacked) in enumerate(got):
            n_stacked += stacked
            next_ap[bid] = next_ap.get(bid, 0) + 1
            derived = n > 1
            note = (f"{args.kind}, stated {wall_dir} wall [{phrase}] -> wall "
                    f"{wi} ({wall_az:.0f} deg, {err:.0f} deg off); "
                    f"report p.{page}")
            if stacked:
                note += ("; placed under the light aperture on this "
                         "wall, head clipped to clear its sill")
            if derived:
                note += f"; position {k + 1} of {n}"
            if ambiguous:
                note += (f"; AMBIGUOUS — next wall only {runner:.0f} deg "
                         f"off, confirm on tile")
            rows.append({
                "ID": bid, "ap_id": next_ap[bid], "kind": args.kind,
                "wall": wi, "s_m": round(s, 2),
                "width_m": args.width, "sill_m": sill, "head_m": head,
                "wall_az": wall_az, "wall_mx": mx, "wall_my": my,
                "source_pos": "derived" if derived else "report",
                "source_dims": "default",
                "confidence": "low" if (derived or ambiguous) else "med",
                "notes": note,
                "perforates": "", "face": "",
                "depth_m": "" if args.depth is None else args.depth,
                "form": ""})

    rows.sort(key=lambda r: (r["ID"], r["ap_id"]))
    check(bool(rows), f"{args.kind} rows built", f"{len(rows)} rows")
    check(all(r["head_m"] > r["sill_m"] for r in rows),
          f"every {args.kind} has positive height")
    check(KINDS[args.kind]["perforates"] is False,
          f"{args.kind} kind does not perforate")
    if failures:
        sys.exit(1)

    n_ch = len({r["ID"] for r in rows})
    print(f"\n{len(rows)} {args.kind} rows over {n_ch} chapels")
    print(f"  stacked under a light aperture          : {n_stacked}")
    print(f"  positions from a stated single {args.kind:<9}: "
          f"{sum(1 for r in rows if r['source_pos'] == 'report')}")
    print(f"  positions derived by even spacing       : "
          f"{sum(1 for r in rows if r['source_pos'] == 'derived')}")
    print(f"  flagged ambiguous wall                  : {n_ambig}")
    print(f"  dropped, no room on the wall            : {n_short}")
    print(f"  dropped, wall unresolvable              : {n_nowall}")

    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REGISTRY_COLS)
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {args.out.name}")

    if args.merge:
        have = {(int(r["ID"]), int(r["ap_id"])) for r in reg}
        new = [r for r in rows if (r["ID"], r["ap_id"]) not in have]
        bak = args.registry.with_suffix(f".pre-{args.kind}s.bak")
        if not bak.exists():
            bak.write_bytes(args.registry.read_bytes())
        with open(args.registry, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=REGISTRY_COLS)
            w.writeheader()
            for r in reg:
                w.writerow({c: r.get(c, "") for c in REGISTRY_COLS})
            w.writerows(new)
        print(f"merged {len(new)} rows into {args.registry.name} "
              f"(backup {bak.name}); {len(reg)} existing rows untouched")

    if failures:
        print(f"\n{len(failures)} check(s) failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
