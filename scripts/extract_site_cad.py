"""Extract measured door positions from the site CAD drawing.

The full-site drawing (`BaseSiteCAD/SITE CAD WORKING.dwg`) is binary
DWG; `dwg2dxf` (LibreDWG, a one-off dev tool) converts it, after which
its layers survive — and the layers are the point. The PDF print of
this same drawing flattens everything to one grey pen, which is why
mining the print for doors failed; here `LW2` (detail) is separable
from `BUILDINGS` (outlines).

What the CAD does and does not give, measured rather than assumed:

* Georeferencing is excellent — 274 `NUMBERING` labels carry chapel
  numbers, ~10x more control points than the print's 131, fitting the
  footprints to ~0.9 m.
* Real door threshold marks exist only on the handful of chapels drawn
  in detail (the `LW1`/`LW2`/`ABOVE` layers cover ~7 buildings, with
  the marks concentrated on 23/24/25). For those, this gives a
  *measured* wall, position and width.
* The other ~250 buildings are plain outlines. Their polylines are
  almost all open, but the end-to-end gap is **not** a doorway: tested
  against the report's stated directions it agrees only 36% of the
  time against a ~25% chance baseline, so it is where the draughtsman
  stopped drawing, not an opening. Those chapels get their direction
  from the report instead (scripts/read_report_directions.py).

Writes `cad_candidates.csv` in registry format. Never touches
`aperture_inventory.csv`.
"""

import argparse
import csv
import math
import shutil
import subprocess
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely.geometry import Point

from sanity_checks import ROOT, FOOTPRINTS, check, warn, failures
from aperture_registry import (APERTURES_DIR, REGISTRY_COLS, DOOR_HEAD,
                               DOOR_SILL, canonical_walls, wall_fields)
from extract_dxf_plans import read_dxf_entities

SITE_DWG = (ROOT / "100_Data/120_SiteReport/BaseSiteCAD/"
            "SITE CAD WORKING.dwg")


def to_dxf(dwg, out_dir):
    """Binary DWG -> DXF via LibreDWG's dwg2dxf, cached."""
    out = out_dir / (dwg.stem.replace(" ", "_") + ".dxf")
    if out.exists():
        return out
    if not check(shutil.which("dwg2dxf") is not None, "dwg2dxf available",
                 "brew install libredwg — needed only for this step"):
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["dwg2dxf", "-o", str(out), str(dwg)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out


def entity_segments(e, tf):
    pts = list(zip(e["xs"], e["ys"]))
    if e["type"] == "LINE" and "x2" in e:
        pts = [(e["xs"][0], e["ys"][0]), (e["x2"], e["y2"])]
    if e.get("closed") and len(pts) > 2:
        pts = pts + pts[:1]
    pts = [tf(x, y) for x, y in pts]
    return list(zip(pts, pts[1:]))


def fit_affine(src, dst, floor=5.0):
    src, dst = np.asarray(src, float), np.asarray(dst, float)
    keep = np.ones(len(src), bool)
    for _ in range(12):
        A = np.column_stack([src[keep], np.ones(keep.sum())])
        mat, *_ = np.linalg.lstsq(A, dst[keep], rcond=None)
        res = np.linalg.norm(
            np.column_stack([src, np.ones(len(src))]) @ mat - dst, axis=1)
        med = np.median(res[keep])
        mad = np.median(np.abs(res[keep] - med)) or 1e-9
        new = res <= max(med + 3 * 1.4826 * mad, floor)
        if (new == keep).all():
            break
        keep = new
    return mat.T, res, keep


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dwg", type=Path, default=SITE_DWG,
                   help="full-site CAD drawing (binary DWG)")
    p.add_argument("--footprints", type=Path, default=FOOTPRINTS)
    p.add_argument("--out-dir", type=Path, default=APERTURES_DIR)
    p.add_argument("--min-width", type=float, default=0.45,
                   help="narrowest LW2 mark accepted as a doorway (m)")
    p.add_argument("--max-width", type=float, default=1.30,
                   help="widest LW2 mark accepted as a doorway (m)")
    p.add_argument("--on-wall", type=float, default=0.45,
                   help="max distance from the footprint boundary for a "
                        "mark to count as sitting in a wall (m)")
    return p


def main():
    args = build_parser().parse_args()
    check(args.dwg.exists(), "site CAD exists", str(args.dwg))
    if failures:
        sys.exit(1)
    cad_dir = args.out_dir / "site_cad"
    dxf = to_dxf(args.dwg, cad_dir)
    if dxf is None or failures:
        sys.exit(1)
    ents = read_dxf_entities(dxf)

    fp = gpd.read_file(args.footprints)
    cent = {int(r["ID"]): (r.geometry.centroid.x, r.geometry.centroid.y)
            for _, r in fp.iterrows()}
    src, dst = [], []
    for e in ents:
        if e["layer"] != "NUMBERING" or not e.get("text", "").strip().isdigit():
            continue
        n = int(e["text"])
        if n in cent and e["xs"]:
            src.append((e["xs"][0], e["ys"][0]))
            dst.append(cent[n])
    check(len(src) >= 150, "enough CAD chapel labels to georeference",
          f"{len(src)}")
    if failures:
        sys.exit(1)
    mat, res, keep = fit_affine(src, dst)
    med = float(np.median(res[keep]))
    print(f"  georeference: {int(keep.sum())}/{len(src)} labels kept, "
          f"median residual {med:.2f} m")
    check(med < 3.0, "CAD georeference median residual < 3 m", f"{med:.2f}")

    def tf(x, y):
        return tuple(mat @ np.array([x, y, 1.0]))

    marks = []
    for e in ents:
        if e["layer"] != "LW2":
            continue
        for a, b in entity_segments(e, tf):
            w = math.dist(a, b)
            if args.min_width <= w <= args.max_width:
                marks.append((w, (a[0] + b[0]) / 2, (a[1] + b[1]) / 2))
    print(f"  LW2 door-width marks: {len(marks)}")

    rows, per_chapel = [], {}
    for w, mx, my in marks:
        pt = Point(mx, my)
        d = fp.distance(pt)
        i = d.idxmin()
        if d[i] > args.on_wall:
            continue                      # not sitting in a wall line
        bid = int(fp.loc[i, "ID"])
        walls = canonical_walls(fp.loc[i, "geometry"])
        # Attach to the wall the mark lies along, and record how far
        # down that wall it sits — the position the registry wants.
        best = None
        for wi, (p0, p1) in enumerate(walls):
            p0 = np.asarray(p0)
            u = np.asarray(p1) - p0
            L = float(np.linalg.norm(u))
            u = u / L
            rel = np.array([mx, my]) - p0
            s = float(rel @ u)
            off = abs(float(rel @ np.array([u[1], -u[0]])))
            if -0.2 <= s <= L + 0.2 and (best is None or off < best[0]):
                best = (off, wi, min(max(s, 0.0), L))
        if best is None or best[0] > args.on_wall:
            continue
        off, wi, s = best
        key = (bid, wi)
        if key not in per_chapel or off < per_chapel[key][0]:
            per_chapel[key] = (off, s, w)

    for (bid, wi), (off, s, w) in sorted(per_chapel.items()):
        walls = canonical_walls(fp[fp["ID"] == bid].iloc[0].geometry)
        az, mx, my = wall_fields(walls, wi)
        rows.append({
            "ID": bid, "ap_id": 1, "kind": "door", "wall": wi,
            "s_m": round(s, 2), "width_m": round(w, 2),
            "sill_m": DOOR_SILL, "head_m": DOOR_HEAD,
            "wall_az": az, "wall_mx": mx, "wall_my": my,
            "source_pos": "cad", "source_dims": "cad",
            "confidence": "high",
            "notes": f"threshold mark measured off the site CAD LW2 "
                     f"layer, {off:.2f} m from the wall line; height "
                     f"still default"})
    # One row per chapel: keep the mark that sits most exactly in the wall.
    seen, out_rows = set(), []
    for r in sorted(rows, key=lambda r: (r["ID"], r["ap_id"])):
        if r["ID"] in seen:
            continue
        seen.add(r["ID"])
        out_rows.append(r)

    out = args.out_dir / "cad_candidates.csv"
    with open(out, "w", newline="") as f:
        w_ = csv.DictWriter(f, fieldnames=REGISTRY_COLS)
        w_.writeheader()
        w_.writerows(out_rows)
    widths = [r["width_m"] for r in out_rows]
    print(f"  wrote {out.name}: {len(out_rows)} chapels with a MEASURED "
          f"door" + (f", widths {min(widths):.2f}-{max(widths):.2f} m "
                     f"(median {np.median(widths):.2f})" if widths else ""))
    if len(out_rows) < 20:
        warn("CAD door coverage is small by nature",
             f"{len(out_rows)} chapels — only the buildings drawn in "
             "detail carry LW1/LW2; the rest are outlines only")
    if failures:
        print(f"\n{len(failures)} check(s) failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
