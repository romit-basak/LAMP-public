"""Interior features from the excavation report's Chapter VII prose.

`read_report_directions.py` mined the same pages for one fact per chapel
("it opens west"). This mines them for the things *inside*: light
apertures, niches, apses and pillars. It imports that script's OCR,
paging and entry-splitting rather than copying them, so the 194-row
direction table stays the single definition of how an entry is found.

Two problems this has to solve that the direction pass did not:

**Which wall.** Fakhry rarely says "a niche in the north wall" and stops
there. He writes "in every one of the three walls, east, south and
north", or "in each one of the N. and S. walls", or "in the wall facing
the entrance" — one sentence carrying several features on several
walls. Matching a lone compass word would take the first and drop the
rest, so the sentence grammar here is ordered and conjunction-aware,
and "the three walls" is resolved against `entrance_directions.csv`
(the three that are not the entrance wall). "Facing the entrance"
likewise needs that table, which is why it is an input here.

**Which chapel.** Entry headings are bare numbers in parentheses, and
OCR invents them — a stray "(8)" inside chapel 154's prose would file
its niches under chapel 8. Chapter VII runs in ascending chapel order,
so the fix is a **longest increasing subsequence** over the detected
headings: keep the longest ascending run and drop what cannot belong.
Applied per page range, since each chapter restarts the ascent.

Counts without positions stay counts. A sentence saying "there are
three niches" with no wall named produces rows with a blank wall,
`source_pos=derived`, `confidence=low`, and the sentence in `notes` —
never a guessed position, because `build_aperture_walls.py` would cut
real holes from it. Nothing here writes the curated registry; output is
`report_features_candidates.csv` plus `report_features_raw.csv` as the
audit trail.

`--crossval` checks the yield against Chapter II's own published
tallies (38 oval-topped niches, 19 decorated, 81 facade-triangular, 35
interior). Those are Fakhry counting his own site, so agreement is real
evidence and a wild miss is a bug.
"""

import argparse
import re
import shutil
import sys
from pathlib import Path

import pandas as pd

from sanity_checks import check, failures
from aperture_registry import APERTURES_DIR
from read_report_directions import (PLATES, ocr_page, book_page,
                                    parse_entries)

RAW_COLS = ["ID", "kind", "wall", "n_stated", "source_pos", "confidence",
            "page", "phrase", "sentence"]

COMPASS = {"north": "N", "south": "S", "east": "E", "west": "W",
           "northeast": "NE", "northwest": "NW",
           "southeast": "SE", "southwest": "SW"}
OPPOSITE = {"N": "S", "S": "N", "E": "W", "W": "E",
            "NE": "SW", "SW": "NE", "NW": "SE", "SE": "NW"}

# What we are looking for, and what it becomes in the registry. Light
# apertures perforate; niches and apses are recesses. Pillars are
# recorded but are not wall-addressable, so they never gain a wall.
FEATURES = {
    "window": r"(?:light\s+aperture|aperture\s+for\s+light|"
              r"oval\s+aperture|window)",
    "niche":  r"niche",
    "apse":   r"apse",
    "pillar": r"(?:pillar|column|pilaster)",
}
NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
           "six": 6, "seven": 7, "eight": 8, "a": 1, "an": 1}


def lis_filter(ids):
    """Indices of the longest strictly-increasing subsequence.

    Chapter VII is printed in chapel order, so a heading that breaks
    the ascent is OCR noise rather than a new entry. Patience sorting,
    O(n log n)."""
    import bisect
    tails, prev, idx = [], [-1] * len(ids), []
    for i, v in enumerate(ids):
        j = bisect.bisect_left([ids[k] for k in idx], v)
        if j == len(idx):
            idx.append(i)
        else:
            idx[j] = i
        prev[i] = idx[j - 1] if j > 0 else -1
        tails = idx
    out, k = [], (tails[-1] if tails else -1)
    while k >= 0:
        out.append(k)
        k = prev[k]
    return sorted(out)


def sentences(body):
    flat = re.sub(r"\s+", " ", body)
    return [s.strip() for s in re.split(r"(?<=[.;])\s+", flat)
            if s.strip()]


def clauses(sentence):
    """Split a sentence where a new feature is introduced.

    One sentence routinely carries two features on different walls —
    "three light apertures, one in every wall and there is an
    oval-topped niche in the wall facing the entrance". Attributing
    walls per sentence gives the apertures the niche's wall. Splitting
    only on ";" is not enough and splitting on every "and" is too much:
    it would cut "east, south and north" apart. So the cut is made at
    "and" only where a new existential clause starts."""
    parts = re.split(r";|\bwhile\b|\bbut\b|"
                     r"\band\s+(?=there\s+(?:is|are|exists?)\b|"
                     r"(?:a|an|two|three|four)\s+\w+\s+(?:exists?|is|are)\b)",
                     sentence, flags=re.I)
    return [p.strip() for p in parts if p and p.strip()]


# A feature has to be *introduced*, not merely referred to. "there is an
# oval-topped niche" introduces one; "plastered around the incense
# niches" points back at niches already counted, and matching it again
# double-counts. Requiring an existential verb or an indefinite/numeral
# determiner near the mention is what separates the two.
INTRODUCES = re.compile(
    r"(?:there\s+(?:is|are|was|were|exists?)|we\s+find|"
    r"contains?|has|have|\bexists?\b|"
    r"\b(?:a|an|one|two|three|four|five|six|seven|eight|\d+)\b)",
    re.I)


def introduced(clause, kind_pat):
    """Is the feature introduced in this clause, or just referred to?"""
    m = re.search(kind_pat, clause, re.I)
    if not m:
        return False
    before = clause[:m.start()]
    # Look only at the run of words immediately before the mention, so
    # an unrelated numeral earlier in the clause cannot license it.
    tail = " ".join(before.split()[-6:])
    if re.search(r"\bthe\s+\w*\s*$", tail) and not INTRODUCES.search(tail):
        return False
    return bool(INTRODUCES.search(tail)) or bool(
        re.match(r"\s*(?:there|we)\b", clause, re.I))


def walls_in(sentence, entrance):
    """(walls, phrase) named by one sentence, entrance-aware.

    Ordered most specific first: a sentence matching "every one of the
    three walls" must not also be read as a single-wall mention."""
    s = sentence.lower()
    ent = entrance
    facing = OPPOSITE.get(ent) if ent else None

    named = [COMPASS[w] for w in re.findall(
        r"\b(north|south|east|west|northeast|northwest|southeast|"
        r"southwest)\b", s)]
    # Compass words attached to "wall" or in a list next to one.
    if re.search(r"\b(?:every|each)\s+one\s+of\s+the\s+three\s+walls?", s):
        if ent:
            return [d for d in ("N", "S", "E", "W") if d != ent], \
                   "every one of the three walls"
        return [], "three walls (entrance unknown)"
    m = re.search(r"\b(?:each|every)\s+one\s+of\s+the\s+([^.]*?)walls?", s)
    if m and named:
        return sorted(set(named)), f"each of the {m.group(1)}walls"
    if re.search(r"\bfacing\s+the\s+entrance|opposite\s+the\s+entrance", s):
        return ([facing], "wall facing the entrance") if facing else \
               ([], "facing the entrance (entrance unknown)")
    if re.search(r"\bat\s+the\s+(?:facade|fagade|façade)", s):
        return ([ent], "at the facade") if ent else ([], "facade")
    if re.search(r"\b(?:either|each)\s+side\s+of\s+the\s+door", s):
        return ([ent], "either side of the door") if ent else \
               ([], "beside the door")
    if re.search(r"\b(?:one\s+in\s+)?(?:every|each)\s+wall\b", s):
        return ["N", "S", "E", "W"], "one in every wall"
    if named:
        # "in the north and south walls" / "in the east wall", and also
        # "an apse at the east side" — Fakhry uses side/end for the
        # larger features and wall for the ones cut into masonry.
        if re.search(r"\b(?:walls?|sides?|ends?)\b", s):
            return sorted(set(named)), "named wall(s)"
    return [], ""


def dedupe(df):
    """One row per (chapel, kind, wall); drop unplaced re-mentions.

    The prose returns to a feature after introducing it — "there are in
    this apse two large niches", "a part of the apse have fallen down".
    Those are the same apse. Keeping a wall-anchored row over a blank
    one, and only one blank row per chapel and kind, turns the raw scan
    into something a curator can work down rather than dedupe by hand."""
    df = df.copy()
    df["_placed"] = df["wall"].astype(str).str.strip().ne("") & \
        df["wall"].notna()
    placed = df[df["_placed"]].drop_duplicates(["ID", "kind", "wall"])
    has_placed = set(zip(placed["ID"], placed["kind"]))
    loose = df[~df["_placed"]].drop_duplicates(["ID", "kind"])
    loose = loose[~loose.apply(
        lambda r: (r["ID"], r["kind"]) in has_placed, axis=1)]
    out = pd.concat([placed, loose], ignore_index=True)
    return out.drop(columns="_placed").sort_values(
        ["ID", "kind", "wall"]).reset_index(drop=True)


def count_in(sentence, kind_pat):
    """Stated count for a feature in one sentence, or None."""
    m = re.search(r"\b(one|two|three|four|five|six|seven|eight|\d+)\s+"
                  r"(?:\w+\s+){0,3}?" + kind_pat, sentence, re.I)
    if m:
        tok = m.group(1).lower()
        return NUMBERS.get(tok, int(tok) if tok.isdigit() else None)
    if re.search(r"\b(?:a|an|there\s+is\s+an?)\s+(?:\w+\s+){0,3}?"
                 + kind_pat, sentence, re.I):
        return 1
    return None


def scan_entry(cid, body, page, entrance):
    """Raw feature rows from one chapel's prose."""
    rows = []
    for sent in sentences(body):
        for c in clauses(sent):
            for kind, pat in FEATURES.items():
                if not introduced(c, pat):
                    continue
                n = count_in(c, pat)
                walls, phrase = ([], "") if kind == "pillar" else \
                    walls_in(c, entrance)
                # "three light apertures, one in every wall" on a
                # four-walled chapel means the three that are not the
                # doorway — the entrance wall already has the door. The
                # stated count is the arbiter, so trust it over the
                # loose "every".
                if (walls and n and n == len(walls) - 1
                        and entrance in walls):
                    walls = [w for w in walls if w != entrance]
                    phrase += " (entrance wall excluded by the count)"
                per = 1 if (n and n == len(walls)) else (n or 1)
                if walls:
                    for w in walls:
                        rows.append(dict(
                            ID=cid, kind=kind, wall=w, n_stated=per,
                            source_pos="report", confidence="med",
                            page=page, phrase=phrase, sentence=c[:300]))
                else:
                    rows.append(dict(
                        ID=cid, kind=kind, wall="",
                        n_stated=n if n else 1, source_pos="derived",
                        confidence="low", page=page, phrase=phrase,
                        sentence=c[:300]))
    return rows


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--plates", type=Path, default=PLATES)
    p.add_argument("--pages", type=int, nargs=2, action="append",
                   metavar=("FIRST", "LAST"),
                   help="scan page range to read; repeatable. The "
                        "default reads the per-chapel chapter (92-165) "
                        "plus the three monographic ones, because a "
                        "chapel written up at length there carries only "
                        "a cross-reference in the per-chapel chapter "
                        "('has been described in Chapter VI') and none "
                        "of its interior detail: 76-91 covers the "
                        "painted chapels 25/172/173/175/210, 39-64 the "
                        "Chapel of Exodus (30) and 65-75 the Chapel of "
                        "Peace (80). The heading-order filter is applied "
                        "per range, since each chapter restarts the "
                        "ascent")
    p.add_argument("--directions", type=Path,
                   default=APERTURES_DIR / "entrance_directions.csv",
                   help="entrance wall per chapel; required to resolve "
                        "'the three walls' and 'facing the entrance'")
    p.add_argument("--out-dir", type=Path, default=APERTURES_DIR)
    p.add_argument("--force", action="store_true",
                   help="re-OCR instead of using the cache")
    p.add_argument("--no-lis", action="store_true",
                   help="skip the increasing-order heading filter "
                        "(diagnostic: shows what it removes)")
    p.add_argument("--crossval", action="store_true",
                   help="compare yields against Chapter II's tallies")
    return p


def main():
    args = build_parser().parse_args()
    check(shutil.which("tesseract") is not None, "tesseract available",
          "needed only if the OCR cache is cold")
    check(args.plates.exists(), "page scans exist", str(args.plates))
    if failures:
        sys.exit(1)
    cache = args.plates / "ocr"
    cache.mkdir(exist_ok=True)

    dirs = pd.read_csv(args.directions)
    entrance = {int(r["ID"]): r["direction"] for _, r in dirs.iterrows()}
    check(len(entrance) > 150, "entrance directions loaded",
          f"{len(entrance)} chapels")

    ranges = args.pages or [[92, 165], [76, 91], [39, 64],
                            [65, 75]]
    raw, n_found, n_kept, dropped = [], 0, 0, []
    for first, last in ranges:
        found = []
        for pno in range(first, last + 1):
            img = args.plates / f"page_{pno:03d}.jpg"
            if not img.exists():
                continue
            text = ocr_page(img, cache, force=args.force)
            bp = book_page(text) or pno
            for cid, body in parse_entries(text):
                if cid is not None:
                    found.append((cid, body, bp))
        if not found:
            continue
        ids = [c for c, _, _ in found]
        keep = (list(range(len(found))) if args.no_lis
                else lis_filter(ids))
        dropped += sorted(set(ids) - {ids[i] for i in keep})
        n_found += len(found)
        n_kept += len(keep)
        for i in keep:
            cid, body, bp = found[i]
            raw += scan_entry(cid, body, bp, entrance.get(cid))
    check(n_found > 0, "entries parsed", f"{n_found} headings")
    print(f"\nheadings: {n_found} detected -> {n_kept} kept after the "
          f"increasing-order filter, over {len(ranges)} page range(s)")
    if dropped:
        print(f"  dropped out-of-order headings: {sorted(set(dropped))}")
    check(bool(raw), "features found", f"{len(raw)} raw rows")

    df = pd.DataFrame(raw, columns=RAW_COLS)
    df.to_csv(args.out_dir / "report_features_raw.csv", index=False)

    print("\nRAW yield by kind")
    for kind, grp in df.groupby("kind"):
        wal = grp[grp["wall"] != ""]
        print(f"  {kind:8s} {len(grp):>4} rows, {grp['ID'].nunique():>3} "
              f"chapels, {len(wal):>4} wall-anchored "
              f"({wal['ID'].nunique()} chapels)")

    # The candidates file is what a human curates: one row per opening,
    # wall-anchored rows kept over unplaced re-mentions, and never
    # written over the registry.
    cand = dedupe(df)
    cand.to_csv(args.out_dir / "report_features_candidates.csv",
                index=False)
    print("\nCANDIDATES after dedupe (one row per chapel/kind/wall)")
    for kind, grp in cand.groupby("kind"):
        wal = grp[grp["wall"].astype(str).str.strip() != ""]
        print(f"  {kind:8s} {len(grp):>4} rows, {grp['ID'].nunique():>3} "
              f"chapels, {len(wal):>4} wall-anchored "
              f"({wal['ID'].nunique()} chapels)")

    if args.crossval:
        print("\nCROSSVAL against Chapter II's own tallies")
        niche_ch = df[df["kind"] == "niche"]["ID"].nunique()
        win_ch = df[df["kind"] == "window"]["ID"].nunique()
        apse_ch = df[df["kind"] == "apse"]["ID"].nunique()
        # Ch. II states 35 chapels with interior niches and 15 apses.
        for got, lo, hi, what in ((niche_ch, 30, 160, "chapels w/ niches"),
                                  (win_ch, 30, 90, "chapels w/ apertures"),
                                  (apse_ch, 8, 30, "chapels w/ apses")):
            check(lo <= got <= hi, f"{what} within band",
                  f"{got} (expected {lo}-{hi})")

    print(f"\nwrote report_features_raw.csv and "
          f"report_features_candidates.csv to {args.out_dir}")
    if failures:
        print(f"\n{len(failures)} check(s) failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
