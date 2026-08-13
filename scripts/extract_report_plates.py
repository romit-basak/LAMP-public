"""Extract the excavation-report page scans for manual plate reading.

The report (Fakhry, *The Necropolis of El-Bagawat in Kharga Oasis*) is
the project's only source for aperture heights: dozens of its plates
pair a chapel plan with a front elevation/section carrying dimension
lines. The 617 MB PDF is a pure scan — one DCTDecode (JPEG) image per
page, no text layer — so "extraction" means dumping the embedded JPEGs
and making them fast to browse; the sill/head/width values themselves
are read by eye and entered into the aperture registry
(`aperture_inventory.csv`, see scripts/build_aperture_walls.py).

Outputs (in --out-dir, inside the gitignored datastore):
    page_001.jpg .. page_200.jpg   the embedded page scans, untouched
    contact_sheet_N.png            downsampled grids with page numbers,
                                   for finding the chapel plates quickly
    plate_index.csv                template the reader fills in while
                                   browsing (page, chapel ids seen,
                                   has_elevation, notes) — the coverage
                                   record for measured-vs-assumed later

Existing page JPEGs are skipped (rerun-safe); `--force` re-extracts.
The PDF is memory-mapped, so the 617 MB never loads at once.
"""

import argparse
import csv
import mmap
import sys
from pathlib import Path

from PIL import Image, ImageDraw

from sanity_checks import ROOT, check, failures

REPORT_PDF = ROOT / "100_Data/120_SiteReport/SiteReport_missing9-12.pdf"
OUT_DIR = ROOT / "200_Projects/250_Apertures/report_plates"


def find_jpeg_streams(mm):
    """Byte ranges [(start, end)] of every DCTDecode stream, in file
    order (which is page order for this linearly-written scan).

    Hand-rolled on purpose: the file is one JPEG per page, so full PDF
    parsing buys nothing — each `/DCTDecode` dict is followed by its
    `stream ... endstream` payload, and the JPEG SOI/EOI markers
    confirm every slice."""
    spans = []
    pos = 0
    while True:
        tag = mm.find(b"/DCTDecode", pos)
        if tag < 0:
            break
        s = mm.find(b"stream", tag)
        if s < 0:
            break
        s += len(b"stream")
        # The stream keyword is followed by CRLF or LF before the data.
        if mm[s:s + 2] == b"\r\n":
            s += 2
        elif mm[s:s + 1] == b"\n":
            s += 1
        e = mm.find(b"endstream", s)
        if e < 0:
            break
        data_end = e
        while mm[data_end - 1:data_end] in (b"\r", b"\n"):
            data_end -= 1
        spans.append((s, data_end))
        pos = e
    return spans


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pdf", type=Path, default=REPORT_PDF,
                   help="scanned excavation-report PDF (one JPEG/page)")
    p.add_argument("--out-dir", type=Path, default=OUT_DIR,
                   help="where page JPEGs / contact sheets / the index "
                        "template are written")
    p.add_argument("--force", action="store_true",
                   help="re-extract pages that already exist on disk")
    p.add_argument("--page", type=int, default=None,
                   help="extract only this 1-based page (quick re-export "
                        "for close reading)")
    p.add_argument("--sheet-cols", type=int, default=5,
                   help="contact-sheet grid columns")
    p.add_argument("--sheet-rows", type=int, default=8,
                   help="contact-sheet grid rows")
    p.add_argument("--thumb-width", type=int, default=260,
                   help="contact-sheet thumbnail width (px)")
    return p


def main():
    args = build_parser().parse_args()
    check(args.pdf.exists(), "report PDF exists", str(args.pdf))
    if failures:
        sys.exit(1)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    with open(args.pdf, "rb") as f, \
            mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        spans = find_jpeg_streams(mm)
        check(len(spans) == 200, "one JPEG stream per report page",
              f"{len(spans)} found (expected 200)")

        pages = ([args.page] if args.page
                 else range(1, len(spans) + 1))
        n_written = n_skipped = 0
        for pno in pages:
            if not 1 <= pno <= len(spans):
                check(False, "--page in range", f"{pno} of {len(spans)}")
                sys.exit(1)
            out = args.out_dir / f"page_{pno:03d}.jpg"
            if out.exists() and not args.force:
                n_skipped += 1
                continue
            s, e = spans[pno - 1]
            data = mm[s:e]
            ok = data[:2] == b"\xff\xd8" and b"\xff\xd9" in data[-4:]
            check(ok, f"page {pno} slice is a JPEG (SOI/EOI markers)",
                  f"{len(data):,} bytes")
            if not ok:
                continue
            out.write_bytes(data)
            n_written += 1
        print(f"  pages: {n_written} written, {n_skipped} already present")

    # Decode audit + contact sheets (skipped in single-page mode).
    if not args.page:
        thumbs = []
        for pno in range(1, len(spans) + 1):
            path = args.out_dir / f"page_{pno:03d}.jpg"
            try:
                with Image.open(path) as im:
                    w, h = im.size
                    if pno == 1:
                        check(w > 1000 and h > 1000,
                              "scan resolution plausible", f"{w}x{h} px")
                    tw = args.thumb_width
                    thumbs.append((pno, im.convert("L").resize(
                        (tw, max(1, round(h * tw / w))))))
            except OSError as exc:
                check(False, f"page {pno} decodes", str(exc))
        per_sheet = args.sheet_cols * args.sheet_rows
        th = max(t.size[1] for _, t in thumbs)
        pad = 22                             # room for the burned number
        for si in range(0, len(thumbs), per_sheet):
            batch = thumbs[si:si + per_sheet]
            sheet = Image.new(
                "L", (args.sheet_cols * args.thumb_width,
                      args.sheet_rows * (th + pad)), 255)
            draw = ImageDraw.Draw(sheet)
            for k, (pno, t) in enumerate(batch):
                cx = (k % args.sheet_cols) * args.thumb_width
                cy = (k // args.sheet_cols) * (th + pad)
                sheet.paste(t, (cx, cy))
                draw.text((cx + 4, cy + th + 3), f"p.{pno}", fill=0)
            out = args.out_dir / f"contact_sheet_{si // per_sheet + 1}.png"
            sheet.save(out)
            print(f"  wrote {out.name} (pages {batch[0][0]}-{batch[-1][0]})")

    # Index template for the manual pass — never overwritten.
    index = args.out_dir / "plate_index.csv"
    if not index.exists():
        with open(index, "w", newline="") as f:
            wr = csv.writer(f)
            wr.writerow(["page", "chapel_ids", "has_elevation", "notes"])
            for pno in range(1, len(spans) + 1):
                wr.writerow([pno, "", "", ""])
        print(f"  wrote {index.name} (fill in while browsing)")
    else:
        print(f"  {index.name} already exists — left untouched")

    if failures:
        print(f"\n{len(failures)} check(s) failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
