"""Measure per-building wall thickness, and seed `building_fabric.csv`.

The mesh builder's 0.4 m wall thickness is not an archaeological
figure. Its only justification in this project is `make_test_building`'s
"one DEM pixel, chapel-like" — chosen for the synthetic test cube, where
it merely had to be one pixel wide, then inherited as the default for
every real chapel. Thickness is not cosmetic: it sets the depth of a
door's reveal, which is what clips oblique sightlines, since a ray
clears the opening only where w*cos(t) > thickness*sin(t).

The excavation report contradicts it for the commonest type. Fakhry
fixes his own unit at chapel 2 — walls needing "a pillar in the middle
for strengthening it because its breadth is only a brick, i.e. about 17
cms." — and defines Type No. 1, 109 of 263 chapels, as walls "of half a
brick or one brick only", i.e. 0.085-0.17 m. At 0.40 m the model has
been building those walls 2.4-4.7x too thick, and thickness runs the
wrong way for this project's question: an over-thick wall narrows the
reveal and *understates* the aperture effect.

A Type 1 wall has two thicknesses, which is the trap in measuring one
off a plan. The report says its lower parts are "much broader than the
upper parts" and that most such chapels carry pilasters; of a Type I
chapel it writes "between the pilasters there are thin walls of one
brick only". Chapel 51's plan shows precisely that — a thin band with
pilasters projecting inward every metre or so. An ink-band scan
crossing a pilaster measures the pilaster, which is why the plates read
Type 1 chapels at 0.48-0.58 m against Fakhry's own 0.17. The number
that governs a doorway's reveal is the panel, so a plate reading that
contradicts its type's stated definition is rejected rather than
believed.

The types the CAD does cover are genuinely thicker: 0.28-0.42 m across
five clean readings, close to the 0.40 m default. So the default was
about right for the substantial chapels and badly wrong for the
commonest ones — which is why this has to be per-building rather than
one better constant.

Two independent measurements, best evidence first:

  cad    the LW1 layer of the seven per-building DXFs draws each wall as
         a closed band, so thickness is the band's local width: rasterise
         the band, distance-transform it, and take twice the median ridge
         value. Validated against the report — chapel 210 measures 0.280 m
         here, and (3.83 - 3.25)/2 = 0.29 from the DXF's own 3.83 m extent
         and the report's stated "measures 3'25 at each side".
  plate  the excavation report's own scaled plans, where a hatched wall
         band is measured against the figure's metre bar (px_per_m from
         plate_figures.csv, itself read off the bar's ticks).

Two shapes on that layer are *not* walls and must not be measured as
such: square piers (chapel 23 has four, ~0.58 m across) and the
courtyard outlines enclosing a whole plot. Both are rejected by
requiring a band to be elongated - centreline length at least 3x its
width - and no longer than any real chapel wall.

The CAD set is a biased sample, and the fallback ladder has to respect
that: all seven drawn chapels are Type 4, 8 or 10 - the substantial
ones - and **none is Type 1**, the largest class at 109 of 263. The
report defines Type 1 as walls "of half a brick or one brick only", so
propagating a CAD median onto them would repeat the very error this
script exists to correct. Type 1 and Type 9 therefore take the report's
own definition ahead of any measured median.

Everything else falls back to a typology default and is labelled as
such, so a reader can always separate measured from propagated. Writes
`building_fabric_candidates.csv`; the curated `building_fabric.csv` is
never overwritten, matching the rest of the aperture pipeline.
"""

import argparse
import csv
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from PIL import Image
from rasterio.features import rasterize
from rasterio.transform import from_origin
from scipy.ndimage import distance_transform_edt, maximum_filter
from shapely.geometry import Polygon

from sanity_checks import FOOTPRINTS, ROOT, check, warn, failures
from aperture_registry import APERTURES_DIR
from extract_dxf_plans import read_dxf_entities

Image.MAX_IMAGE_PIXELS = None

CAD_DIR = ROOT / "100_Data/120_SiteReport/BaseSiteCAD"
PLATES = APERTURES_DIR / "report_plates"
PLATE_INDEX = APERTURES_DIR / "plate_figures/plate_figures.csv"
XLSX = (ROOT / "100_Data/120_SiteReport"
        / "Bagawat Data From Excavation Report.xlsx")

FABRIC_COLS = ["ID", "type", "wall_thickness_m", "thickness_source",
               "thickness_conf", "n_probes", "spread_m", "notes"]

# The report's own unit, glossed at chapel 2: one brick in breadth is
# "about 17 cms."
ONE_BRICK = 0.17
HALF_BRICK = 0.085

# Typology defaults from the report's wording rather than from any
# measurement. Type 1 is "half a brick or one brick only" — take the
# one-brick end, since that is the thicker reading and so the one that
# argues *against* a large aperture effect. Type 9 is the circular type
# whose pillars are "screened by half a brick wall", and those screens
# are where its light apertures sit.
REPORT_TYPE_THICKNESS = {1: ONE_BRICK, 9: HALF_BRICK}
SITE_DEFAULT = 0.30

# A plate reading that lands this far from its type's stated definition
# is measuring something else — nearly always a pilaster rather than
# the panel between pilasters.
TYPE_CONTRADICTION = 2.0

# A wall band has to be longer than it is wide, but no longer than a
# chapel: the LW1 layer also carries square piers (rejected by the first
# bound) and plot outlines and hatch runs (rejected by the second).
MIN_THICK_M, MAX_THICK_M = 0.06, 1.20
MIN_ELONGATION, MAX_LENGTH_M = 3.0, 80.0
MAX_REL_SPREAD = 0.35

# A plate reading is only as good as the figure's scale bar, and those
# were read semi-automatically. Cross-check each against a source that
# shares nothing with it: the drawn plan should span about what the
# traced footprint spans. Ratios out to 4.6x show up, so this gate does
# real work rather than rubber-stamping.
PLATE_EXTENT_TOL = 0.25

# Chapel 210 is the one drawing the report independently pins down: its
# DXF spans 3.83 m and the text says the chamber "measures 3'25 at each
# side", so each wall is (3.83 - 3.25) / 2.
VALIDATION_ID = 210
VALIDATION_M = 0.294
VALIDATION_TOL_M = 0.03


def _band_ridge(poly, px=0.005):
    """Local widths (m) sampled along a polygon's medial axis.

    Rasterises the band and distance-transforms it; ridge pixels sit
    equidistant from both faces, so twice their distance is the local
    width. This reads a wall band correctly whether it is straight,
    L-shaped or a full closed ring, which inter-segment probing does
    not — a probe cast across a chamber lands on the far wall and
    reports the room, not the wall."""
    x0, y0, x1, y1 = poly.bounds
    pad = 4 * px
    w = int((x1 - x0 + 2 * pad) / px) + 1
    h = int((y1 - y0 + 2 * pad) / px) + 1
    if w * h > 4e7:
        return None
    tr = from_origin(x0 - pad, y1 + pad, px, px)
    m = rasterize([(poly, 1)], out_shape=(h, w), transform=tr, fill=0,
                  dtype="uint8").astype(bool)
    if m.sum() < 50:
        return None
    d = distance_transform_edt(m) * px
    ridge = (d >= maximum_filter(d, size=3) - 1e-12) & (d > px)
    return 2 * d[ridge] if ridge.sum() >= 10 else None


def cad_thickness(path):
    """Wall thickness (m) from a per-building DXF, or None.

    Pools the ridge samples of every LW1 band that looks like a wall and
    takes the median. The drawings are in sheet millimetres (the
    project's DXF convention), so coordinates scale by 1e-3. Returns
    None rather than a number when the surviving bands disagree, which
    is how the composite plans decline to be reduced to one figure."""
    pool = []
    for e in read_dxf_entities(path):
        if e["layer"] != "LW1":
            continue
        pts = list(zip(e.get("xs", []), e.get("ys", [])))
        if len(pts) < 3:
            continue
        poly = Polygon([(x * 1e-3, y * 1e-3) for x, y in pts]).buffer(0)
        if poly.is_empty or poly.area < 0.02:
            continue
        v = _band_ridge(poly)
        if v is None:
            continue
        t = float(np.median(v))
        length = poly.area / t
        if not MIN_THICK_M <= t <= MAX_THICK_M:
            continue
        if not MIN_ELONGATION * t <= length <= MAX_LENGTH_M:
            continue
        pool.append(v)
    if not pool:
        return None
    v = np.concatenate(pool)
    med = float(np.median(v))
    iqr = float(np.percentile(v, 75) - np.percentile(v, 25))
    if iqr / med > MAX_REL_SPREAD:
        return None
    return med, len(v), iqr


def _band_width(profile, gap):
    """First ink run before a long white gap: (start, end) or None."""
    idx = np.flatnonzero(profile)
    if len(idx) == 0:
        return None
    white = 0
    for i in range(idx[0], len(profile)):
        if profile[i]:
            white = 0
        else:
            white += 1
            if white >= gap:
                return idx[0], i - gap + 1
    return None


def plate_extent_ratio(page, bbox, ppm, footprint):
    """Drawn plan span / traced footprint span, or None.

    Only the top 80% of the crop is used: the scale bar and north arrow
    sit below the plan and would otherwise widen the ink bounding box."""
    img = PLATES / f"page_{page:03d}.jpg"
    if not img.exists():
        return None
    a = np.asarray(Image.open(img).convert("L"))
    x0, y0, x1, y1 = bbox
    sub = np.asarray(a[y0:y1, x0:x1]) < 128
    ys, xs = np.nonzero(sub)
    if len(xs) < 100:
        return None
    keep = ys < sub.shape[0] * 0.80
    ys, xs = ys[keep], xs[keep]
    if len(xs) < 100:
        return None
    drawn = max((xs.max() - xs.min()) / ppm, (ys.max() - ys.min()) / ppm)
    bx0, by0, bx1, by1 = footprint.total_bounds
    traced = max(bx1 - bx0, by1 - by0)
    return drawn / traced if traced > 0 else None


def plate_thickness(page, bbox, ppm, min_room_m=1.0):
    """Wall-band thickness (m) from a scaled report plan.

    Scans across the figure and reads the ink band before the first
    long white run — the hatched wall, then the open chamber. `gap` is
    set in metres via the figure's own scale so it means the same thing
    on every plate regardless of how large the chapel was drawn."""
    img = PLATES / f"page_{page:03d}.jpg"
    if not img.exists():
        return None
    a = np.asarray(Image.open(img).convert("L"))
    x0, y0, x1, y1 = bbox
    sub = np.asarray(a[y0:y1, x0:x1]) < 128
    if sub.size == 0:
        return None
    H, W = sub.shape
    gap = max(int(min_room_m * ppm), 20)
    vals = []
    for y in range(int(H * 0.30), int(H * 0.70), max(H // 60, 1)):
        for row in (sub[y], sub[y][::-1]):
            b = _band_width(row, gap)
            if b:
                vals.append(b[1] - b[0])
    for x in range(int(W * 0.30), int(W * 0.70), max(W // 60, 1)):
        for col in (sub[:, x], sub[:, x][::-1]):
            b = _band_width(col, gap)
            if b:
                vals.append(b[1] - b[0])
    if len(vals) < 8:
        return None
    v = np.array(vals) / ppm
    v = v[(v > 0.05) & (v < 1.5)]
    if len(v) < 8:
        return None
    return float(np.median(v)), len(v), float(
        np.percentile(v, 75) - np.percentile(v, 25))


def load_types():
    """{chapel ID: typology 1-10} from the excavation spreadsheet.

    Rows whose `Chapel #` will not parse as an integer are skipped
    rather than guessed at: the sheet carries section headings and
    footnotes in the same column."""
    x = pd.read_excel(XLSX, sheet_name=0)
    out = {}
    for _, r in x.iterrows():
        try:
            bid = int(r["Chapel #"])
        except (TypeError, ValueError):
            continue
        t = r.get("Type")
        out[bid] = int(t) if pd.notna(t) else None
    return out


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--footprints", type=Path, default=FOOTPRINTS)
    p.add_argument("--cad-dir", type=Path, default=CAD_DIR)
    p.add_argument("--plate-index", type=Path, default=PLATE_INDEX)
    p.add_argument("--out", type=Path,
                   default=APERTURES_DIR / "building_fabric_candidates.csv",
                   help="candidates CSV; the curated building_fabric.csv "
                        "is never written here")
    p.add_argument("--no-plates", action="store_true",
                   help="skip the report-plan measurements (slow: each "
                        "plate is a 64 MP scan)")
    return p


def main():
    args = build_parser().parse_args()
    fp = gpd.read_file(args.footprints)
    ids = sorted(int(i) for i in fp["ID"])
    types = load_types()
    check(len(types) > 200, "typology loaded", f"{len(types)} chapels")

    measured = {}

    print("\nCAD (per-building DXF, LW1 wall bands, medial-axis width)")
    for path in sorted(args.cad_dir.glob("Building*.dxf")):
        bid = int("".join(c for c in path.stem if c.isdigit()))
        got = cad_thickness(path)
        if got is None:
            warn(f"{path.name}: no wall band found", "")
            continue
        t, n, iqr = got
        measured[bid] = ("cad", t, n, iqr, path.name)
        print(f"  ID {bid:>3}: {t:.3f} m  (n={n}, IQR {iqr:.3f})")

    if not args.no_plates:
        print("\nPLATES (report plans against their own metre bar)")
        idx = pd.read_csv(args.plate_index)
        agree = []
        for _, r in idx.iterrows():
            bid = int(r["ID"])
            box = (int(r["x0"]), int(r["y0"]), int(r["x1"]), int(r["y1"]))
            ppm = float(r["px_per_m"])
            g = fp[fp["ID"].astype(int) == bid]
            ratio = (None if g.empty else
                     plate_extent_ratio(int(r["page"]), box, ppm, g))
            if ratio is not None and abs(ratio - 1) > PLATE_EXTENT_TOL:
                warn(f"ID {bid}: plate scale disagrees with the footprint",
                     f"drawn span is {ratio:.2f}x the traced span")
                continue
            got = plate_thickness(int(r["page"]), box, ppm)
            if got is None:
                continue
            t, n, iqr = got
            if iqr / t > MAX_REL_SPREAD:
                warn(f"ID {bid}: plate spread too wide to trust",
                     f"median {t:.2f} m, IQR {iqr:.2f} m")
                continue
            stated = REPORT_TYPE_THICKNESS.get(types.get(bid))
            if stated and t > TYPE_CONTRADICTION * stated:
                warn(f"ID {bid}: plate contradicts its stated type",
                     f"read {t:.2f} m against {stated:.3f} m for Type "
                     f"{types[bid]:g} — a pilaster, not the panel")
                continue
            if bid in measured:
                # Both sources drew the same chapel: keep the CAD
                # reading, but say how far apart they landed — the two
                # methods share no code and no input.
                agree.append((bid, measured[bid][1], t))
                print(f"  ID {bid:>3}: {t:.3f} m  (n={n}, IQR {iqr:.3f})"
                      f"  [CAD says {measured[bid][1]:.3f}]")
                continue
            measured[bid] = ("plate", t, n, iqr, f"page {int(r['page'])}")
            print(f"  ID {bid:>3}: {t:.3f} m  (n={n}, IQR {iqr:.3f})")
        if agree:
            d = np.array([abs(c - p) for _, c, p in agree])
            print(f"\n  CAD-vs-plate on {len(agree)} shared chapel(s): "
                  f"median |diff| {np.median(d):.3f} m, max {d.max():.3f} m")

    if VALIDATION_ID in measured:
        got = measured[VALIDATION_ID][1]
        check(abs(got - VALIDATION_M) <= VALIDATION_TOL_M,
              f"chapel {VALIDATION_ID} matches the report's stated chamber",
              f"measured {got:.3f} m vs {VALIDATION_M:.3f} m derived")

    # Per-type median of whatever was measured within that type. The CAD
    # set covers only the substantial types, so this never reaches Type
    # 1 — which is the point: those take the report's definition below.
    by_type = {}
    for bid, v in measured.items():
        t = types.get(bid)
        if t is not None:
            by_type.setdefault(t, []).append(v[1])
    type_median = {t: round(float(np.median(v)), 3)
                   for t, v in by_type.items()}
    all_vals = [v[1] for v in measured.values()]
    site_default = (round(float(np.median(all_vals)), 3) if all_vals
                    else SITE_DEFAULT)
    print(f"\nper-type median from measurements: "
          f"{ {t: type_median[t] for t in sorted(type_median)} }")
    print(f"site default (median of all measured): {site_default:.3f} m "
          f"from n={len(all_vals)}")

    rows = []
    for bid in ids:
        t = types.get(bid)
        if bid in measured:
            src, val, n, iqr, note = measured[bid]
            conf = "high" if src == "cad" else "med"
            rows.append(dict(ID=bid, type=t or "",
                             wall_thickness_m=round(val, 3),
                             thickness_source=src, thickness_conf=conf,
                             n_probes=n, spread_m=round(iqr, 3),
                             notes=note))
        elif t in REPORT_TYPE_THICKNESS:
            rows.append(dict(ID=bid, type=t,
                             wall_thickness_m=REPORT_TYPE_THICKNESS[t],
                             thickness_source="report_type",
                             thickness_conf="med", n_probes=0, spread_m="",
                             notes=f"Type {t} as defined in the report"))
        elif t in type_median:
            rows.append(dict(ID=bid, type=t,
                             wall_thickness_m=type_median[t],
                             thickness_source="type_cad",
                             thickness_conf="low", n_probes=0, spread_m="",
                             notes=f"median of {len(by_type[t])} measured "
                                   f"Type {t} chapel(s)"))
        else:
            rows.append(dict(ID=bid, type=t or "",
                             wall_thickness_m=site_default,
                             thickness_source="site_default",
                             thickness_conf="low", n_probes=0, spread_m="",
                             notes="median of all measured chapels"))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FABRIC_COLS)
        w.writeheader()
        w.writerows(rows)

    df = pd.DataFrame(rows)
    print("\nCOVERAGE by provenance")
    for src, grp in df.groupby("thickness_source"):
        v = grp["wall_thickness_m"].astype(float)
        print(f"  {src:14s} {len(grp):>4} chapels   "
              f"{v.min():.3f}-{v.max():.3f} m (median {v.median():.3f})")
    check(len(df) == len(ids), "one row per footprint",
          f"{len(df)} vs {len(ids)}")
    check((df["wall_thickness_m"].astype(float) > 0.05).all(),
          "no implausibly thin walls")
    print(f"\nwrote {args.out}")
    if failures:
        print(f"\n{len(failures)} check(s) failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
