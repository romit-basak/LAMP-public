"""OCR the excavation report's per-chapel descriptions for entrance
directions.

Chapter III ("A brief description of all the chapels") walks the
chapels in numeric order, each under a bold `(NNN)` heading, and
states which way it opens in plain words:

    (212)
    A chapel of Type 1 which opens south ; its entrance is in the
    cavetto style and there is one niche for incense at the facade.

This is the authoritative aperture-direction source. Mining the site
plan's linework for door *positions* was tried first and abandoned:
its apparent gaps are dominated by plan-vs-footprint registration
artifacts at corners, and the one ground-truthed chapel (180) has
unbroken linework across its real entrance. Direction is also the
property that actually drives what an observer can see through an
opening, so a stated direction beats a guessed position.

Needs the `tesseract` binary (a one-off extraction tool, deliberately
not a pipeline dependency — the run is cached to text files so the
result is reproducible without it). Page images come from
scripts/extract_report_plates.py.

Writes `report_directions.csv` (ID, direction, source, page, notes)
for `scripts/apertures_from_report.py` to turn into registry rows.
Never overwrites a hand-curated `entrance_directions.csv`; it prints
the diff instead.
"""

import argparse
import csv
import re
import shutil
import subprocess
import sys
from pathlib import Path

from sanity_checks import check, warn, failures
from aperture_registry import APERTURES_DIR

PLATES = APERTURES_DIR / "report_plates"
# Page-number offset drifts through the book (unnumbered plates are
# interleaved), so the book page is read off each scan rather than
# computed; this is only the search default for Chapter III.
DEFAULT_RANGE = (96, 176)

# OCR drops a bracket often enough to matter (chapel 9's heading comes
# out as "9)"), so accept a half-bracketed number too — but require at
# least one bracket, or bare page numbers would read as headings.
HEADING = re.compile(r"^[^\w(]*(?:\(\s*(\d{1,3})\s*\)?|(\d{1,3})\s*\))"
                     r"[^\w)]*$")
# The individually-famous chapels get a named chapter heading instead
# of a bare number — "THE CHAPEL OF EXODUS (No. 30)" — and were
# invisible to the bare-number form above. Deliberately case-SENSITIVE
# and anchored to a whole line: a case-insensitive version matched
# passing cross-references in running text ("...in the Chapel of Peace
# (No. 80) and...") and then attributed every following sentence to
# that chapel — which silently gave chapel 80 an entrance direction
# read off a basilica in a different oasis.
NAMED_HEADING = re.compile(r"^\W*(?:THE\s+)?CHAPEL\s+OF\s+[A-Z]"
                           r"[A-Z\s'’-]{2,30}\(\s*No\.?\s*(\d{1,3})"
                           r"\s*\)\W*$")
# Tolerant of the OCR damage this scan actually produces: "soutb" for
# south, "cpens" for opens, and lost spaces ("openssouth"). Each of
# these cost a real chapel in the first pass, so they are handled here
# rather than hand-patched into the CSV, which would not survive a
# re-run. Dotted abbreviations ("opens S.W.") appear too.
_S = r"sou?t[hb]"
_N = r"n[o0]r[thb]{2}"
_E = r"e[a@]st"
_W = r"w[e@]st"
COMPASS_WORD = (rf"({_N}[-\s]?{_E}|{_N}[-\s]?{_W}|{_S}[-\s]?{_E}|"
                rf"{_S}[-\s]?{_W}|[NS]\.\s?[EW]\.?|{_N}|{_S}|{_E}|{_W})")
OPENS = r"(?:opens?|opened|opening|cpens?|opeus?)"
# Anchored on the words that bind a direction to the opening, so a
# passing mention ("niches in the east, south and north walls") can't
# be mistaken for the entrance.
DIRECTION_PATTERNS = [
    # \s* not \s+ : the scan loses the space in "openssouth".
    re.compile(OPENS + r"\s*(?:to\s+the\s+|towards?\s+the\s+|"
               r"on\s+the\s+)?" + COMPASS_WORD, re.I),
    re.compile(r"entrance\s+(?:is\s+)?(?:at|in|on|to)\s+the\s+"
               + COMPASS_WORD, re.I),
    re.compile(r"entrance\s+(?:opens|faces)\s+(?:to\s+the\s+)?"
               + COMPASS_WORD, re.I),
    re.compile(r"fa(?:c|ç)(?:es|ing)\s+(?:to\s+the\s+)?" + COMPASS_WORD,
               re.I),
    re.compile(r"fa(?:c|ç)ade\s+(?:is\s+)?(?:at|on|to)\s+the\s+"
               + COMPASS_WORD, re.I),
]
TYPE_RE = re.compile(r"\bType\s+(\d{1,2})\b", re.I)
PAGENO = re.compile(r"^[^\w]*(\d{2,3})\s*[—\-–]*\s*$")


def normalize(word):
    w = re.sub(r"[-\s.]+", "", word.strip().lower())
    # Fold the OCR variants back onto the canonical spellings before
    # the lookup (soutb -> south, nortb -> north, ...).
    w = re.sub(r"sou?t[hb]", "south", w)
    w = re.sub(r"n[o0]r[thb]{2}", "north", w)
    w = re.sub(r"e[a@]st", "east", w)
    w = re.sub(r"w[e@]st", "west", w)
    return {"north": "N", "south": "S", "east": "E", "west": "W",
            "northeast": "NE", "northwest": "NW",
            "southeast": "SE", "southwest": "SW",
            "ne": "NE", "nw": "NW", "se": "SE", "sw": "SW"}.get(w)


def ocr_page(img, cache, force=False):
    """OCR one page image, caching the text beside the plates."""
    out = cache / (img.stem + ".txt")
    if out.exists() and not force:
        return out.read_text(errors="replace")
    # psm 3 (full auto) keeps the centred `(NNN)` headings on their own
    # lines; psm 6 merges them into the body and loses the number.
    subprocess.run(["tesseract", str(img), str(out.with_suffix("")),
                    "--psm", "3"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out.read_text(errors="replace")


def book_page(text):
    for line in text.splitlines()[:6]:
        m = PAGENO.match(line.strip())
        if m:
            return int(m.group(1))
    return None


def parse_entries(text):
    """[(chapel_id, body_text)] from one page's OCR, plus any entry
    continuing from the previous page (id None)."""
    entries, cur, buf = [], None, []
    for line in text.splitlines():
        m = HEADING.match(line) or NAMED_HEADING.search(line)
        if m:
            entries.append((cur, "\n".join(buf)))
            cur = int(m.group(1) if m.re is NAMED_HEADING
                      else (m.group(1) or m.group(2)))
            buf = []
        else:
            buf.append(line)
    entries.append((cur, "\n".join(buf)))
    return entries


def find_direction(body):
    flat = re.sub(r"\s+", " ", body)
    for pat in DIRECTION_PATTERNS:
        m = pat.search(flat)
        if m:
            d = normalize(m.group(1))
            if d:
                s = max(0, m.start() - 40)
                return d, flat[s:m.end() + 40].strip()
    return None, ""


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--plates", type=Path, default=PLATES,
                   help="folder of page_NNN.jpg scans")
    p.add_argument("--pages", type=int, nargs=2, default=DEFAULT_RANGE,
                   metavar=("FIRST", "LAST"),
                   help="PDF page range to OCR (Chapter III)")
    p.add_argument("--out", type=Path,
                   default=APERTURES_DIR / "report_directions.csv",
                   help="extracted directions table")
    p.add_argument("--force", action="store_true",
                   help="re-run OCR even where cached text exists")
    return p


def main():
    args = build_parser().parse_args()
    check(shutil.which("tesseract") is not None, "tesseract available",
          "brew install tesseract — needed only for this extraction")
    check(args.plates.exists(), "page scans exist",
          f"{args.plates} — run scripts/extract_report_plates.py first")
    if failures:
        sys.exit(1)
    cache = args.plates / "ocr"
    cache.mkdir(exist_ok=True)

    first, last = args.pages
    rows, seen, carry = [], {}, None
    n_pages = 0
    for pno in range(first, last + 1):
        img = args.plates / f"page_{pno:03d}.jpg"
        if not img.exists():
            continue
        text = ocr_page(img, cache, force=args.force)
        n_pages += 1
        bp = book_page(text)
        for cid, body in parse_entries(text):
            if cid is None:
                # Text before the first heading continues the previous
                # page's entry.
                if carry is not None:
                    cid, body = carry, body
                else:
                    continue
            d, quote = find_direction(body)
            tm = TYPE_RE.search(re.sub(r"\s+", " ", body))
            if cid in seen and not d:
                continue
            if d:
                rows.append({
                    "ID": cid, "direction": d, "source": "report",
                    "page": bp if bp else pno,
                    "notes": (f"type {tm.group(1)}; " if tm else "")
                             + f'"{quote}"'})
                seen[cid] = d
            elif cid not in seen:
                seen[cid] = None
        ents = parse_entries(text)
        carry = ents[-1][0] if ents and ents[-1][0] is not None else carry

    # One row per chapel, first statement wins.
    dedup, out_rows = set(), []
    for r in sorted(rows, key=lambda r: (r["ID"], r["page"])):
        if r["ID"] in dedup:
            continue
        dedup.add(r["ID"])
        out_rows.append(r)

    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ID", "direction", "source",
                                          "page", "notes"])
        w.writeheader()
        w.writerows(out_rows)
    found = {r["ID"] for r in out_rows}
    silent = sorted(c for c, d in seen.items() if d is None
                    and c not in found)
    print(f"  OCR'd {n_pages} pages; {len(seen)} chapel entries seen, "
          f"{len(out_rows)} with a stated direction")
    print(f"  wrote {args.out.name}")
    if silent:
        print(f"  {len(silent)} entries state no direction: "
              f"{silent[:20]}{' ...' if len(silent) > 20 else ''}")
    check(len(out_rows) >= 50, "a useful number of directions read",
          f"{len(out_rows)}")

    curated = APERTURES_DIR / "entrance_directions.csv"
    if curated.exists():
        have = {}
        for r in csv.DictReader(open(curated, newline="")):
            have[int(r["ID"])] = r["direction"].strip().upper()
        clash = [(r["ID"], have[r["ID"]], r["direction"])
                 for r in out_rows
                 if r["ID"] in have and have[r["ID"]] != r["direction"]]
        print(f"  {curated.name} left untouched — "
              f"{len([r for r in out_rows if r['ID'] not in have])} new, "
              f"{len(clash)} conflicting")
        for cid, old, new in clash[:10]:
            warn(f"ID {cid}: direction conflict",
                 f"existing {old} vs report {new}")
    if failures:
        print(f"\n{len(failures)} check(s) failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
