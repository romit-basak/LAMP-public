"""Turn wall-anchored light-aperture candidates into registry rows.

`read_report_features.py` reads the excavation report's prose and says
things like "there is a light aperture in every one of the three walls"
— a chapel, a kind, a compass wall and a count. This resolves that into
what `build_aperture_walls.py` can actually build: a canonical wall
index, a position along it, and an opening rectangle. Only rows with a
named wall are eligible; a stated count with no wall stays a count,
because a guessed position here cuts a real hole in a real mesh.

Three things this has to get right:

**The door is already there.** A chapel's door row occupies its wall
from sill to head, and a light aperture at 1.44-1.79 m sits inside that
height range. A window placed at the wall midpoint of the *entrance*
wall would therefore overlap the door, and the builder's all-pairs
(s x z) check would reject the whole chapel. Windows on the door wall
are offset clear of it, and dropped if the wall is too short to hold
both.

**Counts become positions, evenly and visibly.** "Two light apertures
in the north wall" becomes two rows at 1/3 and 2/3 along it. That
spacing is a convention, not evidence, so those rows carry
`source_pos=derived` and `confidence=low` while a single centred
aperture — which the report often states outright ("in the centre of
the wall") — is `med`.

**Dimensions are defaults and say so.** `WINDOW_SILL` 1.44 m and
`WINDOW_HEIGHT` 0.35 m come from the report (chapel 8's section, and
Chapter II's 0.25-0.45 m range); `WINDOW_WIDTH` 0.60 m is the weakest
number in the pipeline — Chapter II calls them "longitudinal slits" but
states no width. Every row is `source_dims=default` and the width is
exposed as a flag so the sensitivity can be shown rather than argued.

Writes `window_candidates.csv` by default and leaves the registry
alone. `--merge` appends into `aperture_inventory.csv` after backing it
up; door rows are never touched, so a `--openings doors` build stays
byte-identical.
"""

import argparse
import csv
import shutil
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from sanity_checks import FOOTPRINTS, check, warn, failures
from aperture_registry import (APERTURES_DIR, INVENTORY, REGISTRY_COLS,
                               WINDOW_SILL, WINDOW_HEIGHT, WINDOW_WIDTH,
                               canonical_walls, wall_for_azimuth,
                               wall_fields)
from apertures_from_report import parse_direction

# Clearance kept between a window and the door on the same wall, on top
# of their half-widths — enough that the overlap check has margin and
# the reveal geometry is not a sliver.
DOOR_CLEAR_M = 0.30


def door_span(reg_rows, bid, walls):
    """(wall index, s, width) of this chapel's door, or None."""
    for r in reg_rows:
        if int(r["ID"]) == bid and r["kind"] == "door":
            try:
                return (int(r["wall"]), float(r["s_m"]),
                        float(r["width_m"]))
            except (TypeError, ValueError):
                return None
    return None


def positions(length, n, door, width, clear=DOOR_CLEAR_M):
    """Evenly spaced positions along a wall, clear of the door.

    Returns fewer than `n` positions when the wall cannot hold them —
    the caller warns. Placing them anyway would produce overlapping
    openings that the mesh builder rejects for the whole chapel."""
    half = width / 2 + 0.05
    slots = [length * (i + 1) / (n + 1) for i in range(n)]
    if door is None:
        return [s for s in slots if half <= s <= length - half]
    d_s, d_w = door
    lo, hi = d_s - d_w / 2 - clear - half, d_s + d_w / 2 + clear + half
    out = []
    for s in slots:
        if lo <= s <= hi:
            # Push to whichever side of the door has room.
            left, right = lo, hi
            cand = [c for c in (left, right)
                    if half <= c <= length - half]
            if not cand:
                continue
            s = min(cand, key=lambda c: abs(c - s))
        if half <= s <= length - half and all(
                abs(s - o) > width + 0.05 for o in out):
            out.append(s)
    return out


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--candidates", type=Path,
                   default=APERTURES_DIR / "report_features_candidates.csv")
    p.add_argument("--footprints", type=Path, default=FOOTPRINTS)
    p.add_argument("--registry", type=Path, default=INVENTORY)
    p.add_argument("--width", type=float, default=WINDOW_WIDTH,
                   help="light-aperture width (m). The report states no "
                        "width; this is the pipeline's weakest number "
                        "and is flagged source_dims=default")
    p.add_argument("--sill", type=float, default=WINDOW_SILL)
    p.add_argument("--height", type=float, default=WINDOW_HEIGHT)
    p.add_argument("--max-per-wall", type=int, default=3,
                   help="refuse to derive more than this many positions "
                        "on one wall from a bare count")
    p.add_argument("--out", type=Path,
                   default=APERTURES_DIR / "window_candidates.csv")
    p.add_argument("--merge", action="store_true",
                   help="append into the registry (backed up first); "
                        "door rows are never modified")
    return p


def main():
    args = build_parser().parse_args()
    cand = pd.read_csv(args.candidates)
    cand = cand[cand["kind"] == "window"].copy()
    # Test for the missing wall before stringifying. `astype(str)` does
    # not turn NA into "nan" under pandas' native string dtype, so a
    # "!= 'nan'" filter silently passes the unplaced rows through — and
    # `groupby` then drops them anyway, which hides the discrepancy
    # behind a correct-looking result.
    has_wall = cand["wall"].notna() & (
        cand["wall"].astype("object").astype(str).str.strip() != "")
    placed = cand[has_wall].copy()
    placed["wall"] = placed["wall"].astype("object").astype(str).str.strip()
    check(len(placed) > 0, "wall-anchored window candidates",
          f"{len(placed)} of {len(cand)} window rows carry a wall")
    print(f"  {len(cand) - len(placed)} counted-but-unplaced rows are "
          f"left in the candidates file, not built")

    fp = gpd.read_file(args.footprints)
    geoms = {int(r["ID"]): r.geometry for _, r in fp.iterrows()}
    reg = list(csv.DictReader(open(args.registry, newline="")))
    next_ap = {}
    for r in reg:
        bid = int(r["ID"])
        next_ap[bid] = max(next_ap.get(bid, 0), int(r["ap_id"]))

    rows, n_short, n_nowall, n_ambig = [], 0, 0, 0
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
        door = door_span(reg, bid, walls)
        door_here = (door[1], door[2]) if door and door[0] == wi else None
        got = positions(L, n, door_here, args.width)
        if len(got) < n:
            warn(f"ID {bid} wall {wi} ({wall_dir}): wall too short for "
                 f"{n} aperture(s)", f"placed {len(got)} on {L:.2f} m"
                 + (" beside the door" if door_here else ""))
            n_short += n - len(got)
        phrase = str(grp["phrase"].iloc[0])
        page = grp["page"].iloc[0]
        for k, s in enumerate(got):
            next_ap[bid] = next_ap.get(bid, 0) + 1
            derived = n > 1
            note = (f"light aperture, stated {wall_dir} wall "
                    f"[{phrase}] -> wall {wi} ({wall_az:.0f} deg, "
                    f"{err:.0f} deg off); report p.{page}")
            if derived:
                note += f"; position {k + 1} of {n}, evenly spaced"
            if door_here:
                note += "; offset clear of the door"
            if ambiguous:
                note += (f"; AMBIGUOUS — next wall only {runner:.0f} deg "
                         f"off, confirm on tile")
            rows.append({
                "ID": bid, "ap_id": next_ap[bid], "kind": "window",
                "wall": wi, "s_m": round(s, 2),
                "width_m": args.width, "sill_m": args.sill,
                "head_m": round(args.sill + args.height, 2),
                "wall_az": wall_az, "wall_mx": mx, "wall_my": my,
                "source_pos": "derived" if derived else "report",
                "source_dims": "default",
                "confidence": "low" if (derived or ambiguous) else "med",
                "notes": note,
                "perforates": "", "depth_m": "", "face": "", "form": ""})

    rows.sort(key=lambda r: (r["ID"], r["ap_id"]))
    check(bool(rows), "window rows built", f"{len(rows)} rows")
    if failures:
        sys.exit(1)

    n_ch = len({r["ID"] for r in rows})
    print(f"\n{len(rows)} light-aperture rows over {n_ch} chapels")
    print(f"  positions from a stated single aperture : "
          f"{sum(1 for r in rows if r['source_pos'] == 'report')}")
    print(f"  positions derived by even spacing       : "
          f"{sum(1 for r in rows if r['source_pos'] == 'derived')}")
    print(f"  flagged ambiguous wall                  : {n_ambig}")
    print(f"  dropped, wall too short                 : {n_short}")
    print(f"  dropped, wall unresolvable              : {n_nowall}")

    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REGISTRY_COLS)
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {args.out.name}")

    if args.merge:
        have = {(int(r["ID"]), int(r["ap_id"])) for r in reg}
        new = [r for r in rows if (r["ID"], r["ap_id"]) not in have]
        check(len(new) == len(rows), "no ap_id collides with the registry",
              f"{len(rows) - len(new)} collisions")
        if failures:
            sys.exit(1)
        bak = args.registry.with_suffix(".pre-windows.bak")
        shutil.copy2(args.registry, bak)
        merged = reg + new
        merged.sort(key=lambda r: (int(r["ID"]), int(r["ap_id"])))
        with open(args.registry, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=REGISTRY_COLS)
            w.writeheader()
            for r in merged:
                w.writerow({c: r.get(c, "") for c in REGISTRY_COLS})
        kinds = {}
        for r in merged:
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        print(f"\nmerged into {args.registry.name} "
              f"(backup: {bak.name})")
        print(f"  {len(merged)} rows: " +
              ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))
        print("  door rows unchanged — a --openings doors build must "
              "still reproduce the frozen meshes")

    if failures:
        print(f"\n{len(failures)} check(s) failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
