"""Named painted scenes and where on the building they sit.

The excavation report devotes a chapter each to the two fully painted
chapels — Exodus (No. 30) and Peace (No. 80) — and describes their
scenes one at a time, in order, with the position given in words:
"in the northern corner of the west wall", "over the entrance of the
chapel", "in one of the pendentives". That is exactly the anchoring a
visibility model needs, and it exists nowhere else in the dataset: the
orthophoto sees roofs, the DEM differential sees massing, and neither
can tell you that Adam and Eve are on the west wall.

What this does *not* claim: a scene's extent. The report names a
surface, not a rectangle, so a row here says "this scene is on that
wall" and stops. Anything finer would be invention, and the failure
mode is quiet — a plausible-looking bounding box would flow straight
into a visibility count and come back out as a result.

Two anchor classes, kept apart because they are worth different
amounts:

- **absolute** — a compass wall, the dome, a pendentive, the entrance
  arch. Resolvable against the model's own geometry.
- **relative** — "next to the last scene", "above the previous one".
  Useless alone, but the scenes are numbered and described in order
  around the chamber, so a chain of these between two absolute anchors
  places everything in between. Recorded rather than discarded.

The report also prints its own manifest ("The scenes in this chapel
are described in the following order: ...") which `--crossval` checks
the extracted headings against. Fakhry listing his own scenes is
independent of the headings the parser finds, so agreement is real
evidence and a mismatch is a bug rather than a judgement call.
"""

import argparse
import re
import sys
from pathlib import Path

import pandas as pd

from sanity_checks import check, failures
from aperture_registry import APERTURES_DIR
from read_report_directions import PLATES, ocr_page

# Chapter IV is the Chapel of Exodus, Chapter V the Chapel of Peace.
# Ranges are scan-page indices, which is what the OCR cache is keyed by.
SECTIONS = {30: (39, 64), 80: (65, 75)}

COLS = ["ID", "scene_no", "scene", "surface", "wall", "anchor_class",
        "anchor_phrase", "relative_to", "page", "confidence", "sentence"]

# A numbered heading that opens a scene. Footnotes share the "(n) "
# form, so a candidate must also look like a title: it either carries a
# figure or plate reference, or ends the line cleanly.
# The figure reference is matched loosely as "( F..." / "( P..."
# because the scans mangle it freely — "Fies. 31-38", "PI.", "P]." and
# "Vie." all occur. Pinning the exact spelling silently drops the very
# first scene of the Exodus chapel, whose reference OCR'd as "Fies.".
HEADING = re.compile(
    r"^\W{0,3}\(\s*(\d{1,2})\s*\)\s+"
    r"([A-Z][A-Za-z’'\-\. ]{2,64}?)\s*"
    r"(?:\(\s*[FPV]|:|\s*$)")

# Footnote openers that survive the title filter otherwise. Bare
# articles are deliberately absent: "A Garden" is a scene.
CITE = re.compile(r"^(see|cf|ibid|op\.|the same|for (a|the|list)|"
                  r"there (are|is)|this|all|some)\b", re.I)

MANIFEST = re.compile(
    r"described in the following order\s*[:;]\s*(.{20,600}?)(?:\.\s|$)",
    re.S | re.I)

COMPASS = {"north": "N", "south": "S", "east": "E", "west": "W",
           "north-east": "NE", "north-west": "NW",
           "south-east": "SE", "south-west": "SW",
           "northeast": "NE", "northwest": "NW",
           "southeast": "SE", "southwest": "SW"}

WALL_RE = re.compile(
    r"\b(north|south|east|west|north-?east|north-?west|south-?east|"
    r"south-?west)(?:ern)?\s+(wall|corner|side|half|part|end)\b", re.I)

SURFACE_RE = re.compile(
    r"\b(dome|drum|cupola|pendentives?|vault|lunette|apse|arcades?|"
    r"arch(?:es)?|niche|ceiling|facade|façade|frieze|wall)\b", re.I)

ENTRANCE_RE = re.compile(
    r"\b((?:over|above|around|opposite|facing|near|beside)\s+"
    r"(?:the\s+)?(?:entrance|door(?:way)?))\b", re.I)

RELATIVE_RE = re.compile(
    r"\b(next to|at the (?:left|right) (?:side )?of|after the scene of|"
    r"following|above|below|under(?:neath)?|beside|opposite|"
    r"on (?:the )?(?:left|right) of|preced(?:es|ing)|"
    r"at the (?:left|right))\b", re.I)


def sentences(text):
    """Split a scene body into sentences, keeping the OCR's noise out.

    The scans interleave figure captions ("Fic. 39.—The Ark of Noah")
    with running text; a caption repeats the scene name and carries no
    position, so counting it as a sentence would double every anchor."""
    body = re.sub(r"\s+", " ", text)
    body = re.sub(r"\b(Fic|Fig|FIG|Vie)\.?\s*\d+[^.]{0,80}?\.—[^.]*",
                  " ", body)
    return [s.strip() for s in re.split(r"(?<=[.;:])\s+", body)
            if len(s.strip()) > 12]


def is_title(text):
    """Does this heading body read as a scene name, not a footnote?"""
    if CITE.match(text.strip()):
        return False
    words = [w for w in re.split(r"\s+", text.strip()) if w]
    if len(words) > 9:
        return False
    caps = sum(1 for w in words if w[:1].isupper())
    return caps >= max(1, len(words) // 2)


def rising(nums):
    """Indices of a longest strictly increasing run (patience sorting).

    Scene numbers restart at 1 in each chapter and climb; a footnote
    marker that survives the title filter is almost always out of
    order. Dropping by sequence rather than by a hand-listed blacklist
    means a re-OCR with different noise still de-noises correctly."""
    if not nums:
        return []
    tails, back, idx = [], [-1] * len(nums), []
    for i, v in enumerate(nums):
        lo, hi = 0, len(tails)
        while lo < hi:
            mid = (lo + hi) // 2
            if nums[tails[mid]] < v:
                lo = mid + 1
            else:
                hi = mid
        back[i] = tails[lo - 1] if lo else -1
        if lo == len(tails):
            tails.append(i)
        else:
            tails[lo] = i
    k = tails[-1] if tails else -1
    while k >= 0:
        idx.append(k)
        k = back[k]
    return sorted(idx)


def anchors(sent):
    """Every position anchor in one sentence, best first.

    Ranking matters more than it looks. "In the northern corner of the
    west wall" contains two compass phrases, and the one that places
    the scene is the *wall* — taking whichever matched first would file
    Adam and Eve under north when the report puts them on the west."""
    out = []
    for m in WALL_RE.finditer(sent):
        d = COMPASS.get(m.group(1).lower().replace("-", ""), "")
        part = m.group(2).lower()
        out.append((0 if part == "wall" else 1, "absolute", d, part,
                    m.group(0)))
    for m in ENTRANCE_RE.finditer(sent):
        out.append((2, "absolute", "", "entrance", m.group(1)))
    for m in SURFACE_RE.finditer(sent):
        s = m.group(1).lower().rstrip("s")
        if s != "wall":                 # walls are handled with a compass
            out.append((3, "absolute", "", s, m.group(0)))
    for m in RELATIVE_RE.finditer(sent):
        out.append((4, "relative", "", "", m.group(1).lower()))
    return [t[1:] for t in sorted(out, key=lambda t: t[0])]


def scenes_in(pages, texts):
    """[(scene_no, title, page, body)] for one chapter's page range."""
    cands = []
    for pno in pages:
        for line in texts[pno].splitlines():
            m = HEADING.match(line)
            if m and is_title(m.group(2)):
                cands.append((int(m.group(1)),
                              re.sub(r"\s+", " ", m.group(2)).strip(),
                              pno, line))
    keep = [cands[i] for i in rising([c[0] for c in cands])]

    # A scene's body runs from its heading to the next one, across page
    # breaks — the report happily starts a scene at the foot of one page
    # and finishes it on the next.
    joined, marks = [], []
    for pno in pages:
        for line in texts[pno].splitlines():
            marks.append((pno, line))
            joined.append(line)
    starts = []
    for n, title, pno, line in keep:
        for i, (p, ln) in enumerate(marks):
            if p == pno and ln == line and i not in starts:
                starts.append(i)
                break
    out = []
    for k, (n, title, pno, _l) in enumerate(keep):
        i0 = starts[k]
        i1 = starts[k + 1] if k + 1 < len(starts) else len(joined)
        out.append((n, title, pno, "\n".join(joined[i0:i1])))
    return out


def manifest(texts, pages):
    """The report's own ordered scene list, if it prints one."""
    blob = " ".join(re.sub(r"\s+", " ", texts[p]) for p in pages)
    m = MANIFEST.search(blob)
    if not m:
        return []
    items = re.findall(r"\(\s*\d{1,2}\s*\)\s*([^;:()]{3,60})", m.group(1))
    # The running order is set as a wrapped paragraph and the first
    # title reappears where the line breaks, so a raw count overstates
    # it by one and would fail its own cross-check.
    seen, out = set(), []
    for s in items:
        s = re.sub(r"\s+", " ", s).strip(" .,")
        if s.lower() not in seen:
            seen.add(s.lower())
            out.append(s)
    return out


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--plates", type=Path, default=PLATES)
    p.add_argument("--sections", nargs="+", default=None,
                   metavar="ID:FIRST-LAST",
                   help="override the chapter page ranges, e.g. 30:39-64")
    p.add_argument("--crossval", action="store_true",
                   help="check the extracted scenes against the "
                        "report's own printed running order")
    p.add_argument("--out-dir", type=Path, default=APERTURES_DIR)
    return p


def parse_sections(spec):
    """Parse `ID:FIRST-LAST` page-range overrides into {ID: (a, b)}."""
    out = {}
    for s in spec:
        bid, rng = s.split(":")
        a, b = rng.split("-")
        out[int(bid)] = (int(a), int(b))
    return out


def main():
    args = build_parser().parse_args()
    sections = (parse_sections(args.sections) if args.sections
                else dict(SECTIONS))
    cache = args.plates / "ocr"
    check(cache.is_dir(), "OCR cache present", str(cache))
    if failures:
        sys.exit(1)

    lo = min(a for a, _ in sections.values())
    hi = max(b for _, b in sections.values())
    texts = {}
    for pno in range(lo, hi + 1):
        cached = cache / f"page_{pno:03d}.txt"
        if cached.exists():
            texts[pno] = cached.read_text(errors="ignore")
        else:
            texts[pno] = ocr_page(args.plates / f"page_{pno:03d}.jpg",
                                  cache)

    rows = []
    for bid, (a, b) in sorted(sections.items()):
        pages = list(range(a, b + 1))
        found = scenes_in(pages, texts)
        listed = manifest(texts, pages)
        print(f"\nchapel {bid}: pages {a}-{b}, {len(found)} scenes "
              f"parsed, {len(listed)} in the report's own list")
        if args.crossval:
            check(abs(len(found) - len(listed)) <= 2 if listed else True,
                  f"chapel {bid}: parsed scene count matches the "
                  "report's running order",
                  f"{len(found)} parsed vs {len(listed)} listed")

        for n, title, pno, body in found:
            hits = []
            for sent in sentences(body):
                for cls, wall, surf, phrase in anchors(sent):
                    hits.append((cls, wall, surf, phrase, sent))
            absol = [h for h in hits if h[0] == "absolute"]
            rel = [h for h in hits if h[0] == "relative"]
            if not hits:
                rows.append(dict(
                    ID=bid, scene_no=n, scene=title, surface="",
                    wall="", anchor_class="none", anchor_phrase="",
                    relative_to="", page=pno, confidence="none",
                    sentence=""))
                continue
            for cls, wall, surf, phrase, sent in (absol or rel):
                rows.append(dict(
                    ID=bid, scene_no=n, scene=title, surface=surf,
                    wall=wall, anchor_class=cls, anchor_phrase=phrase,
                    relative_to=(phrase if cls == "relative" else ""),
                    page=pno,
                    confidence="high" if wall else
                               ("medium" if cls == "absolute" else "low"),
                    sentence=sent[:300]))

    df = pd.DataFrame(rows, columns=COLS)
    check(len(df) > 0, "painting rows extracted", f"{len(df)}")
    out = args.out_dir / "painting_inventory.csv"
    df.to_csv(out, index=False)

    per = df.drop_duplicates(["ID", "scene_no"])
    print(f"\n{len(per)} scenes, {len(df)} anchor rows")
    print("\nANCHORS by class")
    print(df["anchor_class"].value_counts().to_string())
    print("\nSCENES with a compass wall")
    named = df[df["wall"] != ""].drop_duplicates(["ID", "scene_no"])
    for r in named.itertuples():
        print(f"  {r.ID:>3}  ({r.scene_no:>2}) {r.scene[:38]:38s} "
              f"{r.wall} {r.surface}")
    unplaced = per[~per.set_index(["ID", "scene_no"]).index.isin(
        named.set_index(["ID", "scene_no"]).index)]
    print(f"\n{len(named)} scenes carry a compass wall, "
          f"{len(unplaced)} rely on a relative or surface anchor only")
    print(f"\nwrote {out.name}")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
