"""Plot the per-building CAD plans for manual door measurement.

Seven chapels (1, 23, 24, 25, 26, 175, 210) have individual ASCII DXF
plans in `120_SiteReport/BaseSiteCAD/`. They are drawing-sheet local
(millimetres, laid out side by side — NOT georeferenced) and carry no
door/window layer, but their LW2 detail linework includes what look
like door conventions (parallel segment pairs ~0.7-0.95 m apart). This
tool renders each plan with its layers colour-separated next to the
chapel's canonical footprint walls, so a human can read the door's
wall index, position and measured width off the plot and enter/update
the registry row (`source_pos=dxf`, `source_dims=dxf` for the width —
heights still come from the report plates).

Deliberately assist-only: with 7 buildings, an automated sheet-to-UTM
fit plus mark classification costs more than it saves and would still
need eyeballing. The DXF parser is hand-rolled (the files are flat
LWPOLYLINE/ARC/CIRCLE/LINE soups; ezdxf isn't in the environment and
isn't needed).
"""

import argparse
import math
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from sanity_checks import ROOT, FOOTPRINTS, check, warn, failures
from aperture_registry import APERTURES_DIR, canonical_walls

CAD_DIR = ROOT / "100_Data/120_SiteReport/BaseSiteCAD"
DXF_IDS = (1, 23, 24, 25, 26, 175, 210)
LAYER_STYLE = {                      # colour, linewidth, linestyle
    "LW1": ("k", 1.2, "-"),
    "LW2": ("tab:orange", 0.8, "-"),
    "ABOVE": ("0.6", 0.8, "--"),
}


def read_dxf_entities(path):
    """(code, value) tag stream -> entity dicts from ENTITIES.

    Handles the four entity types these files actually contain
    (LWPOLYLINE, ARC, CIRCLE, LINE) plus TEXT for the chapel-number
    check; SPLINE/ELLIPSE (a handful in two files) are counted and
    skipped with a warn — decorative detail, not wall geometry."""
    lines = path.read_text(errors="replace").splitlines()
    tags = [(int(lines[i].strip()), lines[i + 1].strip())
            for i in range(0, len(lines) - 1, 2)]
    ents, cur = [], None
    in_entities = False
    skipped = {}
    for code, val in tags:
        if code == 0 and val == "SECTION":
            cur = None
        elif code == 2 and not in_entities and val == "ENTITIES":
            in_entities = True
        elif code == 0 and in_entities:
            if val == "ENDSEC":
                break
            if val in ("LWPOLYLINE", "ARC", "CIRCLE", "LINE", "TEXT"):
                cur = {"type": val, "xs": [], "ys": [], "layer": "0"}
                ents.append(cur)
            else:
                skipped[val] = skipped.get(val, 0) + 1
                cur = None
        elif cur is None or not in_entities:
            continue
        elif code == 8:
            cur["layer"] = val
        elif code == 10:
            cur["xs"].append(float(val))
        elif code == 20:
            cur["ys"].append(float(val))
        elif code == 11:
            cur["x2"] = float(val)
        elif code == 21:
            cur["y2"] = float(val)
        elif code == 40:
            cur["r"] = float(val)
        elif code == 50:
            cur["a0"] = float(val)
        elif code == 51:
            cur["a1"] = float(val)
        elif code == 70 and cur["type"] == "LWPOLYLINE":
            cur["closed"] = bool(int(val) & 1)
        elif code == 1 and cur["type"] == "TEXT":
            cur["text"] = val
    for kind, n in skipped.items():
        warn(f"{path.name}: {kind} entities skipped", str(n))
    return ents


def plot_entity(ax, ent, scale=1e-3):
    """Draw one entity in metres (sheet mm / 1000)."""
    style = LAYER_STYLE.get(ent["layer"], ("g", 0.8, "-"))
    color, lw, ls = style
    if ent["type"] == "LWPOLYLINE" and ent["xs"]:
        xs = [x * scale for x in ent["xs"]]
        ys = [y * scale for y in ent["ys"]]
        if ent.get("closed"):
            xs, ys = xs + xs[:1], ys + ys[:1]
        ax.plot(xs, ys, color=color, lw=lw, ls=ls)
    elif ent["type"] == "LINE" and ent["xs"]:
        ax.plot([ent["xs"][0] * scale, ent.get("x2", 0) * scale],
                [ent["ys"][0] * scale, ent.get("y2", 0) * scale],
                color=color, lw=lw, ls=ls)
    elif ent["type"] in ("ARC", "CIRCLE") and ent["xs"]:
        a0 = math.radians(ent.get("a0", 0.0))
        a1 = math.radians(ent.get("a1", 360.0))
        if a1 <= a0:
            a1 += 2 * math.pi
        t = np.linspace(a0, a1, 64)
        r = ent.get("r", 0.0) * scale
        ax.plot(ent["xs"][0] * scale + r * np.cos(t),
                ent["ys"][0] * scale + r * np.sin(t),
                color=color, lw=lw, ls=ls)


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--cad-dir", type=Path, default=CAD_DIR,
                   help="folder holding the Building*.dxf plans")
    p.add_argument("--footprints", type=Path, default=FOOTPRINTS,
                   help="footprint polygons (canonical wall indices)")
    p.add_argument("--out-dir", type=Path,
                   default=APERTURES_DIR / "dxf_plans",
                   help="where the per-building plots land")
    p.add_argument("--ids", type=int, nargs="+", default=list(DXF_IDS),
                   help="building ids to plot (default: all 7 with DXFs)")
    return p


def main():
    args = build_parser().parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    fp = gpd.read_file(args.footprints)

    for bid in args.ids:
        path = args.cad_dir / f"Building{bid}.dxf"
        if not check(path.exists(), f"DXF for building {bid} exists",
                     str(path)):
            continue
        ents = read_dxf_entities(path)
        texts = [e.get("text", "") for e in ents if e["type"] == "TEXT"]
        if texts and texts[0] != str(bid):
            warn(f"Building{bid}.dxf NUMBERING text differs",
                 f"{texts[0]!r} (Chapel of Peace = 26, etc.)")

        fig, (ax1, ax2) = plt.subplots(
            1, 2, figsize=(14, 8),
            gridspec_kw={"width_ratios": [2, 1]})
        for ent in ents:
            if ent["type"] != "TEXT":
                plot_entity(ax1, ent)
        # 1 m scale bar, bottom-left of the drawing extents.
        xs = [x * 1e-3 for e in ents for x in e["xs"]]
        ys = [y * 1e-3 for e in ents for y in e["ys"]]
        if xs:
            x0, y0 = min(xs), min(ys) - 0.8
            ax1.plot([x0, x0 + 1.0], [y0, y0], "k-", lw=3)
            ax1.annotate("1 m", (x0 + 0.5, y0 - 0.35), ha="center",
                         fontsize=9)
        ax1.set_aspect("equal")
        ax1.set_title(f"Building{bid}.dxf — LW1 black / LW2 orange / "
                      "ABOVE dashed (sheet coords, m)")

        row = fp[fp["ID"] == bid]
        if len(row):
            walls = canonical_walls(row.iloc[0].geometry)
            ring = [w[0] for w in walls] + [walls[0][0]]
            ax2.plot(*zip(*ring), color="c", lw=1.5)
            for wi, (p0, p1) in enumerate(walls):
                ax2.annotate(str(wi), ((p0[0] + p1[0]) / 2,
                                       (p0[1] + p1[1]) / 2),
                             color="b", fontsize=12, ha="center")
            ax2.set_aspect("equal")
            ax2.margins(0.2)
            ax2.set_title(f"footprint ID {bid} — canonical wall "
                          "indices (UTM, north up)")
        fig.tight_layout()
        out = args.out_dir / f"dxf_building_{bid}.png"
        fig.savefig(out, dpi=150, bbox_inches="tight")
        plt.close(fig)
        n_lw2 = sum(1 for e in ents if e["layer"] == "LW2")
        print(f"  wrote {out.name} ({len(ents)} entities, "
              f"{n_lw2} on LW2 — look there for door marks)")

    if failures:
        print(f"\n{len(failures)} check(s) failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
