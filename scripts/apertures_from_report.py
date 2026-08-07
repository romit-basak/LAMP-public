"""Turn stated entrance directions into aperture-registry rows.

The excavation report's Chapter III describes every chapel and states
which way it opens ("(228) No. 228 is a good example of Type 4; it
opens west..."). That is the authoritative aperture source: the site
plan's linework turned out to encode door *positions* far too weakly
to mine (its apparent gaps are mostly plan-vs-footprint registration
artifacts at corners), whereas the direction a chapel faces is stated
in plain words — and direction, not position-along-the-wall, is what
dominates what an observer can see through the opening.

Input is `entrance_directions.csv` — a deliberately tiny, hand-filled
table:

    ID,direction,source,page,notes
    210,W,report,89,"opens west; Type 4"

`direction` takes compass points (N/NE/E/.../NW) or degrees. `source`
records where the reading came from (report / xlsx / mentor). Rows are
matched to the wall whose outward normal best fits, placed at that
wall's midpoint, and given the documented default opening dimensions —
a "direction-only" registry row, flagged `source_dims=default` so the
comparison report can always separate measured from assumed.

Chapels whose stated direction fits two walls almost equally (within
`--ambiguous-deg`) are written with `confidence=low` and called out,
since a square chapel rotated 45 degrees to the compass genuinely has
two candidate facades.

Seeds itself from the excavation spreadsheet's own `Entrance
Direction` column when the CSV doesn't exist yet. Never writes
`aperture_inventory.csv`; emits `report_candidates.csv` for merging,
exactly like the site-plan extractor.
"""

import argparse
import csv
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd

from sanity_checks import FOOTPRINTS, ROOT, check, warn, failures
from aperture_registry import (APERTURES_DIR, INVENTORY, REGISTRY_COLS,
                               COMPASS, DOOR_WIDTH, DOOR_HEAD, DOOR_SILL,
                               canonical_walls, wall_fields,
                               wall_for_azimuth)

XLSX = ROOT / "100_Data/120_SiteReport/Bagawat Data From Excavation Report.xlsx"
DIRECTIONS = APERTURES_DIR / "entrance_directions.csv"


def parse_direction(text):
    """'W' / 'nw' / '270' -> azimuth degrees, or None."""
    t = str(text).strip().upper()
    if not t or t in ("NAN", "?"):
        return None
    if t in COMPASS:
        return COMPASS[t]
    try:
        return float(t) % 360.0
    except ValueError:
        return None


def seed_from_xlsx(path, out):
    """Start the table from the spreadsheet's own (sparse) column."""
    df = pd.read_excel(path, sheet_name="Sheet1")
    rows = []
    for _, r in df.iterrows():
        d = parse_direction(r.get("Entrance Direction", ""))
        if d is not None:
            rows.append({"ID": int(r["Chapel #"]),
                         "direction": str(r["Entrance Direction"]).strip(),
                         "source": "xlsx", "page": "",
                         "notes": "from the excavation spreadsheet"})
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ID", "direction", "source",
                                          "page", "notes"])
        w.writeheader()
        w.writerows(rows)
    return len(rows)


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--directions", type=Path, default=DIRECTIONS,
                   help="hand-filled ID/direction table (seeded from "
                        "the spreadsheet on first run)")
    p.add_argument("--footprints", type=Path, default=FOOTPRINTS)
    p.add_argument("--xlsx", type=Path, default=XLSX,
                   help="excavation spreadsheet, for the initial seed")
    p.add_argument("--out-dir", type=Path, default=APERTURES_DIR)
    p.add_argument("--ambiguous-deg", type=float, default=25.0,
                   help="flag as low-confidence when the runner-up "
                        "wall fits the stated direction this closely")
    p.add_argument("--width", type=float, default=DOOR_WIDTH,
                   help="default opening width (m)")
    p.add_argument("--head", type=float, default=DOOR_HEAD,
                   help="default head height (m)")
    p.add_argument("--sill", type=float, default=DOOR_SILL,
                   help="default sill height (m)")
    return p


def main():
    args = build_parser().parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    if not args.directions.exists():
        check(args.xlsx.exists(), "excavation spreadsheet exists",
              str(args.xlsx))
        if failures:
            sys.exit(1)
        n = seed_from_xlsx(args.xlsx, args.directions)
        print(f"  seeded {args.directions.name} from the spreadsheet "
              f"({n} chapels with a stated direction) — add rows as you "
              f"read the report's Chapter III")

    fp = gpd.read_file(args.footprints)
    by_id = {int(r["ID"]): r.geometry for _, r in fp.iterrows()}
    rows, n_amb, n_bad = [], 0, 0
    with open(args.directions, newline="") as f:
        for rec in csv.DictReader(f):
            bid = int(rec["ID"])
            az = parse_direction(rec["direction"])
            if az is None:
                warn(f"ID {bid}: unparseable direction",
                     repr(rec["direction"]))
                n_bad += 1
                continue
            if bid not in by_id:
                warn(f"ID {bid}: no footprint", "skipped")
                n_bad += 1
                continue
            walls = canonical_walls(by_id[bid])
            wi, err, runner = wall_for_azimuth(walls, az)
            p0, p1 = walls[wi]
            L = float(np.hypot(p1[0] - p0[0], p1[1] - p0[1]))
            wall_az, mx, my = wall_fields(walls, wi)
            ambiguous = runner - err < args.ambiguous_deg
            n_amb += ambiguous
            note = (f"stated entrance {rec['direction']}"
                    f" -> wall {wi} (outward {wall_az:.0f} deg, "
                    f"{err:.0f} deg off)")
            if rec.get("page"):
                note += f"; report p.{rec['page']}"
            if ambiguous:
                note += (f"; AMBIGUOUS — next wall only "
                         f"{runner:.0f} deg off, confirm on tile")
            if rec.get("notes"):
                note += f"; {rec['notes']}"
            rows.append({
                "ID": bid, "ap_id": 1, "kind": "door", "wall": wi,
                "s_m": round(L / 2, 2), "width_m": args.width,
                "sill_m": args.sill, "head_m": args.head,
                "wall_az": wall_az, "wall_mx": mx, "wall_my": my,
                "source_pos": rec.get("source", "report"),
                "source_dims": "default",
                "confidence": "low" if ambiguous else "med",
                "notes": note})

    out = args.out_dir / "report_candidates.csv"
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=REGISTRY_COLS)
        w.writeheader()
        w.writerows(rows)
    print(f"  {len(rows)} direction-only rows written to {out.name} "
          f"({n_amb} ambiguous, {n_bad} skipped)")
    check(not rows or n_bad < len(rows), "some directions resolved")

    if INVENTORY.exists():
        have = {(int(r["ID"]), int(r["ap_id"]))
                for r in csv.DictReader(open(INVENTORY, newline=""))}
        new = [r for r in rows if (r["ID"], r["ap_id"]) not in have]
        print(f"  {INVENTORY.name} left untouched ({len(new)} of these "
              f"rows are not in it yet — merge by hand)")
    if failures:
        print(f"\n{len(failures)} check(s) failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
