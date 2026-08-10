"""Cut measurable per-chapel tiles from the excavation report's figures.

Chapter III pairs about forty chapels with a drawn plan, and half of
those with a section or facade elevation as well. Those drawings are
the only place in the whole dataset where a doorway is drawn *to size*
in both axes: the site CAD measures three doors and states no height
anywhere, and the report's prose gives a direction but never a
dimension. So the figures are the sole source for

    * where along a wall the entrance actually sits (every
      report-sourced registry row currently assumes the wall midpoint),
    * door width beyond the three CAD-measured chapels,
    * sill and head heights, which are presently 100% assumed,
    * light apertures (windows) — drawn as small rectangles high in
      the wall on the sections, and named in the prose for dozens of
      chapels that have no figure at all.

Every figure carries a metric scale bar, which is what makes them
measurable rather than merely suggestive. The bar is drawn as an
outlined strip divided by full-height ticks at each metre (the first
metre subdivided into decimetres), so `find_scale_bar` recovers the
pixels-per-metre from the *tick spacing* rather than from the printed
"3 M." label — the geometry is far more reliable to read than small
italic type on a 1951 scan, and it validates itself: the ticks must be
evenly spaced and must divide the bar into a whole number of metres.

Outputs (in --out-dir, inside the gitignored datastore):
    plate_figures.csv     one row per detected figure — page, crop box,
                          rotation, measured px_per_m, and the chapel it
                          belongs to. Hand-editable; this is the record
                          that makes a measurement auditable back to a
                          page.
    chapel_NNN.png        the figure at native scan resolution with a
                          1 m grid and decimetre ticks burned on, so a
                          reader measures by counting squares instead of
                          trusting a caption.

Chapel attribution is seeded by OCR-ing the caption strip under each
detected figure ("FIG. 121.—Chapel No. 213"). That is small italic type
and OCR misses a fair share of it, so unattributed rows are written
with a blank ID and `confidence=none` for a human to fill rather than
guessed at. Rerunning with `--from-index` honours every hand edit and
only re-renders the tiles, the same contract the dome inventory uses.

Needs the `tesseract` binary for the caption pass only (a one-off
extraction tool, deliberately not a pipeline dependency); without it
the geometry still runs and every row simply arrives unattributed.
Page images come from scripts/extract_report_plates.py.
"""

import argparse
import csv
import re
import shutil
import subprocess
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy import ndimage

from sanity_checks import FOOTPRINTS, check, warn, failures
from aperture_registry import APERTURES_DIR

PLATES = APERTURES_DIR / "report_plates"
OUT_DIR = APERTURES_DIR / "plate_figures"

INDEX_COLS = ["ID", "fig_no", "page", "rotate", "x0", "y0", "x1", "y1",
              "px_per_m", "bar_m", "drawn", "confidence", "notes"]

# Chapter III and the typology chapter carry the architectural figures;
# everything outside this span is painted decoration or photo plates.
DEFAULT_PAGES = (88, 170)

# Detection is done on a downscaled copy — the scans are ~7000x9000 and
# nothing here needs that resolution. Only the final tile crop uses the
# full-size image.
WORK_PX = 2000

FIG_LINE = re.compile(r"^\W*F[il1]G\W{0,3}(\d{1,3})\b", re.I)
# Both caption idioms the plates use: "...Chapel No. 213" / "...Chapel
# 23" (the "No." is dropped as often as not) and "...(No. 180)" for the
# handful of figures captioned "Plan of the Church" rather than by
# chapel number.
CHAPEL_NO = re.compile(r"Chapel\W{0,4}(?:N[o0]\W{0,3})?(\d{1,3})\b", re.I)
PAREN_NO = re.compile(r"\(\s*N[o0]\W{0,3}(\d{1,3})\s*\)", re.I)


def page_path(page):
    return PLATES / f"page_{page:03d}.jpg"


def binarize(im, work_px=WORK_PX):
    """Downscaled ink mask plus the scale factor back to full size.

    A fixed threshold is enough: these are high-contrast line drawings
    on paper, and the scans carry dark gutter bars at the page edge
    that an adaptive threshold would chase."""
    small = im.convert("L")
    scale = max(small.size) / work_px
    if scale > 1:
        small = small.resize((int(small.width / scale),
                              int(small.height / scale)),
                             Image.BILINEAR)
    else:
        scale = 1.0
    return np.asarray(small) < 150, scale


def drawing_blocks(ink, min_span=0.03, dilate=0.025, min_frac=0.012):
    """Bounding boxes of the drawn figures on a page.

    Text and line drawings separate cleanly by component size: a word
    of body type spans well under 3% of the page, while a hatched wall,
    an arch or a scale bar spans far more. Surviving components are
    dilated so the parts of one figure (plan, section, scale bar, north
    arrow) merge into a single block, then boxed."""
    h, w = ink.shape
    lab, n = ndimage.label(ink)
    if not n:
        return []
    objs = ndimage.find_objects(lab)
    span = min_span * max(h, w)
    big = np.zeros(ink.shape, bool)
    for i, sl in enumerate(objs, start=1):
        bh, bw = sl[0].stop - sl[0].start, sl[1].stop - sl[1].start
        if max(bh, bw) >= span:
            big[sl] |= lab[sl] == i
    if not big.any():
        return []
    r = max(2, int(dilate * max(h, w)))
    grown = ndimage.binary_dilation(big, np.ones((r, r), bool))
    lab2, n2 = ndimage.label(grown)
    out = []
    for i, sl in enumerate(ndimage.find_objects(lab2), start=1):
        # Box the *undilated* ink inside each cluster: subtracting the
        # dilation radius back off the grown box is a guess that can
        # clip a scale bar off the edge of its own figure.
        cluster = big[sl] & (lab2[sl] == i)
        ys, xs = np.nonzero(cluster)
        if not len(xs):
            continue
        x0, x1 = sl[1].start + xs.min(), sl[1].start + xs.max() + 1
        y0, y1 = sl[0].start + ys.min(), sl[0].start + ys.max() + 1
        if (y1 - y0) * (x1 - x0) < min_frac * h * w:
            continue
        out.append((int(x0), int(y0), int(x1), int(y1)))
    return sorted(out, key=lambda b: (b[1], b[0]))


def find_scale_bars(ink):
    """Every scale bar on a page as (px_per_m, metres, bar_box).

    The bar is a long, thin, hollow component. Its metre divisions run
    the full height of the strip while the decimetre subdivisions only
    run part of it, so taking the columns whose ink fills the strip
    picks out the metre marks — including the two end rules, which are
    0 m and N m. Even spacing across at least three such marks is
    required, which is what rules out a stray dimension line or a
    hatched wall edge.

    Deliberately run over the whole page rather than inside a figure
    box: a box that clips the bar by even a few pixels destroys the
    end rule and with it the tick spacing."""
    lab, n = ndimage.label(ink)
    found = []
    for i, sl in enumerate(ndimage.find_objects(lab), start=1):
        bh = sl[0].stop - sl[0].start
        bw = sl[1].stop - sl[1].start
        if bh < 4 or bw < 40 or bw / bh < 5 or bw / bh > 60:
            continue
        comp = (lab[sl] == i)
        cols = comp.sum(axis=0)
        full = cols >= 0.8 * bh
        if full.sum() < 3:
            continue
        # Collapse runs of adjacent full columns to one mark each.
        marks = []
        run = None
        for c, f in enumerate(full):
            if f and run is None:
                run = c
            elif not f and run is not None:
                marks.append((run + c - 1) / 2)
                run = None
        if run is not None:
            marks.append((run + len(full) - 1) / 2)
        if len(marks) < 3:
            continue
        gaps = np.diff(marks)
        if gaps.min() <= 0:
            continue
        if gaps.std() > 0.10 * gaps.mean():
            continue                       # not an evenly divided bar
        metres = len(marks) - 1
        if not 2 <= metres <= 6:
            continue
        found.append((float(gaps.mean()), metres,
                      (sl[1].start, sl[0].start,
                       sl[1].stop, sl[0].stop)))
    return found


def ocr_strip(strip, min_width=1400):
    """OCR one small crop at psm 6 (uniform block of text).

    Captions are small italic type, so resolution is what actually
    limits legibility here — cropping tight to the strip and upscaling
    it (rather than downscaling the whole page for one OCR pass) is
    what makes these readable at all."""
    if not shutil.which("tesseract"):
        return ""
    if strip.width < 10 or strip.height < 10:
        return ""
    g = strip.convert("L")
    if g.width < min_width:
        f = min_width / g.width
        g = g.resize((int(g.width * f), int(g.height * f)), Image.LANCZOS)
    try:
        return subprocess.run(
            ["tesseract", "stdin", "stdout", "--psm", "6"],
            input=_png_bytes(g), capture_output=True,
            timeout=60).stdout.decode("utf8", "ignore")
    except (subprocess.SubprocessError, OSError):
        return ""


def _caption_match(text):
    """(fig_no, chapel_id) found anywhere in an OCR'd caption blob, or
    (None, None). Unanchored search: captions run across OCR line
    breaks unpredictably, so the two numbers are hunted independently
    rather than required to share one line."""
    fig = chapel = None
    m = FIG_LINE.search(text)
    if m:
        fig = m.group(1)
    mc = CHAPEL_NO.search(text) or PAREN_NO.search(text)
    if mc:
        chapel = mc.group(1)
    return fig, chapel


def ocr_page_lines(im):
    """[(x0, y0, x1, y1, text), ...] for every text line, at native
    resolution — expensive (whole page, no downscaling), so this is
    only the fallback for whatever the tight crop search below misses,
    not the first pass."""
    if not shutil.which("tesseract"):
        return []
    try:
        tsv = subprocess.run(
            ["tesseract", "stdin", "stdout", "--psm", "11", "tsv"],
            input=_png_bytes(im.convert("L")), capture_output=True,
            timeout=180).stdout.decode("utf8", "ignore")
    except (subprocess.SubprocessError, OSError):
        return []
    words = {}                     # (block,par,line) -> [x0,y0,x1,y1,[text]]
    for row in tsv.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5" or not cols[11].strip():
            continue
        key = (cols[2], cols[3], cols[4])
        x, y = int(cols[6]), int(cols[7])
        x1, y1 = x + int(cols[8]), y + int(cols[9])
        e = words.setdefault(key, [x, y, x1, y1, []])
        e[0], e[1] = min(e[0], x), min(e[1], y)
        e[2], e[3] = max(e[2], x1), max(e[3], y1)
        e[4].append(cols[11])
    return [(e[0], e[1], e[2], e[3], " ".join(e[4])) for e in words.values()]


_PAGE_OCR_CACHE = {}


def _cached_page_lines(im):
    """(upright_lines, rotated_lines_in_upright_frame) for a page,
    computed once no matter how many figures on it need the fallback —
    a page with two undetected captions would otherwise pay for the
    same full-resolution OCR pass twice."""
    key = id(im)
    if key not in _PAGE_OCR_CACHE:
        w, h = im.size
        upright = ocr_page_lines(im)
        rot = ocr_page_lines(im.transpose(Image.ROTATE_180))
        flipped = [(w - a[2], h - a[3], w - a[0], h - a[1], a[4])
                  for a in rot]
        _PAGE_OCR_CACHE[key] = (upright, flipped)
    return _PAGE_OCR_CACHE[key]


def _nearest_caption(lines, cx, cy, max_dist):
    """Nearest line that looks like an actual figure caption — i.e.
    starts "Fig. N" — within max_dist of a figure's center.

    Requiring the FIG_LINE match (rather than accepting any line
    mentioning a chapel number) is what keeps this fallback from
    latching onto body prose: the chapter's running text is full of
    sentences like "Chapel 150 is one of the most beautiful..." which
    would otherwise out-compete the real, more distant caption on
    every page that has both a figure and its own descriptive
    paragraph."""
    best = None
    for x0, y0, x1, y1, text in lines:
        if not FIG_LINE.search(text):
            continue
        fig, chapel = _caption_match(text)
        d = (((x0 + x1) / 2 - cx) ** 2 + ((y0 + y1) / 2 - cy) ** 2) ** 0.5
        if d > max_dist:
            continue
        cand = (d, fig or "", chapel or "")
        if best is None or (bool(cand[2]), -cand[0]) > (bool(best[2]), -best[0]):
            best = cand
    return best


def find_caption(im, box, scale, strip_frac=0.13, min_strip=350):
    """(fig_no, chapel_id, rotate, confidence) for the figure at `box`.

    Tried in order, cheapest and most reliable first: the tight, high-
    res strip directly below the figure (where every caption in this
    book sits when the plate is printed right-side up); then the strip
    directly above it rotated 180 degrees (where the caption sits,
    itself upside down, on the handful of plates printed rotated
    relative to the page — the scale-bar detector does not care about
    that rotation, only this caption reader does); and only if both of
    those come up empty, a full native-resolution page OCR restricted
    to the nearest matching line, which catches the captions sitting
    further from the figure than the tight strips reach but costs far
    more time, hence being the last resort rather than the default."""
    w, h = im.size
    x0, y0, x1, y1 = [int(v * scale) for v in box]
    pad = max(int(strip_frac * (y1 - y0)), min_strip)
    below = im.crop((max(0, x0 - 40), y1,
                     min(w, x1 + 40), min(h, y1 + pad)))
    fig, chapel = _caption_match(ocr_strip(below))
    if fig or chapel:
        return fig or "", chapel or "", 0, "high" if (fig and chapel) else "low"
    above = im.crop((max(0, x0 - 40), max(0, y0 - pad),
                     min(w, x1 + 40), y0)).transpose(Image.ROTATE_180)
    fig, chapel = _caption_match(ocr_strip(above))
    if fig or chapel:
        return fig or "", chapel or "", 180, "high" if (fig and chapel) else "low"

    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    max_dist = 3 * pad + 0.15 * h              # near the box, not the page
    upright, flipped = _cached_page_lines(im)
    cand = _nearest_caption(upright, cx, cy, max_dist)
    if cand is not None:
        _, fig, chapel = cand
        return fig, chapel, 0, "high" if (fig and chapel) else "low"
    # The fallback above uses the same full-res OCR pass either way —
    # this second candidate reads the *rotated* page (a wrong-position
    # reflection of upright OCR would still be garbage glyphs, not just
    # the wrong coordinates, on the plates printed upside down).
    cand = _nearest_caption(flipped, cx, cy, max_dist)
    if cand is not None:
        _, fig, chapel = cand
        return fig, chapel, 180, "high" if (fig and chapel) else "low"
    return "", "", 0, "none"


def _png_bytes(im):
    import io
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                      # Pillow < 10
        return ImageFont.load_default()


def render_tile(row, out_path, grid_m=1.0, max_px=3000):
    """Figure crop with a metre grid burned on.

    The grid is the whole point of the tile: a reader measures a
    doorway by counting squares against the drawing, which keeps the
    measurement independent of any later guess about the crop or the
    display size. Decimetre ticks along the top edge carry the
    precision the scan actually supports."""
    im = Image.open(page_path(int(row["page"]))).convert("RGB")
    box = tuple(int(row[k]) for k in ("x0", "y0", "x1", "y1"))
    tile = im.crop(box)
    rot = int(row.get("rotate") or 0)
    if rot:
        tile = tile.rotate(rot, expand=True)
    ppm = float(row["px_per_m"])
    f = min(1.0, max_px / max(tile.size))
    if f < 1.0:
        tile = tile.resize((int(tile.width * f), int(tile.height * f)),
                           Image.LANCZOS)
        ppm *= f
    d = ImageDraw.Draw(tile)
    w, h = tile.size
    for i in range(int(w / ppm) + 1):
        x = i * ppm
        d.line([(x, 0), (x, h)], fill=(255, 120, 120), width=1)
        d.line([(x, 0), (x, 14)], fill=(200, 0, 0), width=3)
        for j in range(1, 10):             # decimetres along the top
            d.line([(x + j * ppm / 10, 0), (x + j * ppm / 10, 7)],
                   fill=(200, 0, 0), width=1)
    for i in range(int(h / ppm) + 1):
        y = i * ppm
        d.line([(0, y), (w, y)], fill=(255, 120, 120), width=1)
        d.line([(0, y), (14, y)], fill=(200, 0, 0), width=3)
    label = (f"chapel {row['ID'] or '?'}  fig {row['fig_no'] or '?'}  "
             f"page {row['page']}  grid = {grid_m:g} m "
             f"({ppm:.1f} px/m)")
    d.rectangle([(0, h - 30), (w, h)], fill=(255, 255, 255))
    d.text((6, h - 24), label, fill=(160, 0, 0), font=_font(18))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tile.save(out_path)
    return tile.size


def cluster_around(blocks, seed, gap):
    """Union of every block within `gap` of a growing seed box.

    A report figure is rarely one connected ink blob: a facade
    elevation, one or more sections, the plan, the compass rose and the
    scale bar are usually drawn with real white space between them,
    wider than the small dilation `drawing_blocks` uses to fuse a
    single drawing's own parts. Anchoring on the scale bar (the one
    element every figure has exactly one of) and iteratively absorbing
    whatever else sits within `gap` of the current union is what pulls
    the *whole* figure into one box instead of just whichever single
    piece happened to touch the bar — a plan-only box was silently
    dropping the facade elevation that carries the doorway, which is
    the entire reason these tiles exist. Text paragraphs never enter
    into it: they were already filtered out of `blocks` by size before
    this runs, so growing the gap has nothing but drawing ink to latch
    onto."""
    cur = seed
    remaining = list(blocks)
    changed = True
    while changed:
        changed = False
        kept = []
        for b in remaining:
            gx = max(0, max(cur[0], b[0]) - min(cur[2], b[2]))
            gy = max(0, max(cur[1], b[1]) - min(cur[3], b[3]))
            if gx <= gap and gy <= gap:
                cur = (min(cur[0], b[0]), min(cur[1], b[1]),
                      max(cur[2], b[2]), max(cur[3], b[3]))
                changed = True
            else:
                kept.append(b)
        remaining = kept
    return cur


def scan_pages(pages, verbose=False):
    """Detect every figure block with a readable scale bar."""
    rows = []
    for page in pages:
        p = page_path(page)
        if not p.exists():
            continue
        im = Image.open(p)
        ink, scale = binarize(im)
        blocks = drawing_blocks(ink)
        bars = find_scale_bars(ink)
        if not bars:
            continue
        gap = 0.12 * max(ink.shape)
        for ppm, metres, bar in bars:
            box = cluster_around(blocks, bar, gap)
            fig, chapel, rot, conf = find_caption(im, box, scale)
            rows.append({
                "ID": chapel, "fig_no": fig, "page": page, "rotate": rot,
                "x0": int(box[0] * scale), "y0": int(box[1] * scale),
                "x1": int(box[2] * scale), "y1": int(box[3] * scale),
                "px_per_m": round(ppm * scale, 1), "bar_m": metres,
                "drawn": "", "confidence": conf,
                "notes": "scale bar read from its metre ticks"})
            if verbose:
                print(f"  page {page:3d} box {box} "
                      f"{ppm * scale:7.1f} px/m over {metres} m "
                      f"-> chapel {chapel or '?'} ({conf})"
                      f"{' [180]' if rot else ''}")
    return rows


def self_test():
    """Synthetic scale bar -> the detector must recover its metre."""
    ppm, metres, height = 60, 4, 14
    a = np.zeros((200, 400), bool)
    x0, y0 = 40, 100
    a[y0:y0 + height, x0:x0 + ppm * metres + 1] = True   # solid strip
    a[y0 + 2:y0 + height - 2, x0 + 1:x0 + ppm * metres] = False
    for i in range(metres + 1):            # full-height metre marks
        a[y0:y0 + height, x0 + i * ppm] = True
    for j in range(1, 10):                 # half-height decimetres
        a[y0:y0 + height // 2, x0 + j * ppm // 10] = True
    got = find_scale_bars(a)
    check(len(got) == 1, "self-test: exactly one scale bar found",
          f"detector returned {len(got)}")
    if not got:
        return
    check(abs(got[0][0] - ppm) <= 1.0, "self-test: px per metre",
          f"got {got[0][0]:.2f}, expected {ppm}")
    check(got[0][1] == metres, "self-test: bar length in metres",
          f"got {got[0][1]}, expected {metres}")
    check(not find_scale_bars(np.zeros((200, 400), bool)),
          "self-test: blank page yields no bar")


def validate_ids(rows, valid_ids):
    """Drop any chapel attribution that doesn't name a real footprint,
    and flag chapels claimed by more than one row.

    Caught by this exact check during development: page 129's caption
    OCR misread "Fig. 106.—Chapel No. 117" and dropped a digit, filing
    the figure under a nonexistent "chapel 11" — a wrong-but-plausible
    number that a confidence threshold alone would not have caught,
    since the crop OCR'd cleanly and returned high confidence. A row
    naming a chapel that isn't in the footprints is unambiguously
    wrong; blanking it is safer than trusting a lucky-looking read."""
    bad = 0
    for r in rows:
        rid = str(r.get("ID", "")).strip()
        if rid and int(rid) not in valid_ids:
            r["notes"] = (f"caption OCR read chapel {rid}, which is not "
                          f"a footprint ID; discarded — {r.get('notes', '')}")
            r["ID"], r["confidence"] = "", "none"
            bad += 1
    if bad:
        warn("some captions named a nonexistent chapel",
             f"{bad} row(s) blanked — see their notes")
    seen = {}
    for r in rows:
        rid = str(r.get("ID", "")).strip()
        if rid:
            seen.setdefault(rid, []).append(r["page"])
    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    if dupes:
        warn("more than one figure claims the same chapel",
             "; ".join(f"chapel {k}: pages {v}" for k, v in dupes.items()))
    return bad


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", type=Path, default=OUT_DIR)
    p.add_argument("--footprints", type=Path, default=FOOTPRINTS,
                   help="building footprints, for validating chapel IDs")
    p.add_argument("--pages", type=int, nargs=2, default=DEFAULT_PAGES,
                   metavar=("FIRST", "LAST"),
                   help="page range to scan for figures")
    p.add_argument("--from-index", action="store_true",
                   help="skip detection; re-render tiles from the "
                        "existing (hand-edited) plate_figures.csv")
    p.add_argument("--grid", type=float, default=1.0,
                   help="grid spacing burned onto the tiles (m)")
    p.add_argument("--max-px", type=int, default=3000,
                   help="longest tile edge (px)")
    p.add_argument("--ids", type=int, nargs="*",
                   help="only render these chapels")
    p.add_argument("--self-test", action="store_true",
                   help="run the scale-bar detector checks and exit")
    return p


def main():
    args = build_parser().parse_args()
    if args.self_test:
        self_test()
        print(f"\n{len(failures)} check(s) failed" if failures
              else "\nself-test passed")
        sys.exit(1 if failures else 0)

    valid_ids = {int(i) for i in gpd.read_file(args.footprints)["ID"]}

    index = args.out_dir / "plate_figures.csv"
    if args.from_index:
        if not check(index.exists(), "index exists", str(index)):
            sys.exit(1)
        rows = list(csv.DictReader(open(index, newline="")))
        validate_ids(rows, valid_ids)
        print(f"  {len(rows)} rows from {index.name} (hand edits kept)")
    else:
        check(page_path(args.pages[0]).exists(), "page scans present",
              "run extract_report_plates.py first")
        if failures:
            sys.exit(1)
        rows = scan_pages(range(args.pages[0], args.pages[1] + 1),
                          verbose=True)
        validate_ids(rows, valid_ids)
        args.out_dir.mkdir(parents=True, exist_ok=True)
        if index.exists():
            index = args.out_dir / "plate_figures_candidates.csv"
            warn("index already exists",
                 f"writing {index.name} instead so hand edits survive; "
                 "merge it yourself, then rerun with --from-index")
        with open(index, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=INDEX_COLS)
            w.writeheader()
            w.writerows(rows)
        print(f"  wrote {index.name}: {len(rows)} figures")

    named = [r for r in rows if str(r.get("ID", "")).strip()]
    print(f"  chapel attributed by caption OCR: {len(named)}/{len(rows)}")
    if len(rows):
        check(len(named) >= 0.2 * len(rows),
              "caption OCR attributed a usable share",
              f"{len(named)} of {len(rows)}")

    made = 0
    for r in rows:
        rid = str(r.get("ID", "")).strip()
        if not rid or not str(r.get("px_per_m", "")).strip():
            continue
        if args.ids and int(rid) not in args.ids:
            continue
        out = args.out_dir / f"chapel_{int(rid):03d}.png"
        size = render_tile(r, out, grid_m=args.grid, max_px=args.max_px)
        made += 1
        print(f"    chapel_{int(rid):03d}.png {size[0]}x{size[1]} "
              f"({float(r['px_per_m']):.0f} px/m at full scale)")
    print(f"  rendered {made} measurable tiles into {args.out_dir}")
    if made < len(named):
        warn("some attributed figures produced no tile",
             f"{len(named) - made} rows lack a px_per_m or an ID")
    if failures:
        print(f"\n{len(failures)} check(s) failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
