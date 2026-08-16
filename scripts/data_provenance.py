"""Where every modelled fact came from, and where nothing came from.

The registries carry per-row provenance columns, but they are spread
across six files and nobody reads a CSV to find out what a number
rests on. This walks all of them and writes one document: which source
document supplied each chapel's entrance, opening, wall thickness,
dome and painting, and — the half that matters more — which values are
defaults standing in for evidence that does not exist.

**Why this is generated rather than written.** A hand-kept provenance
note is wrong the first time anyone edits the registry, and it is
wrong silently, which is worse than absent: a reader cannot tell a
measured 0.86 m door from a class default of the same number. Deriving
it from the registries means the document cannot drift from them.

The distinction the report is built around is that `source_pos`
answers "which wall does this opening sit in", NOT "where along that
wall". Those have different evidence, and conflating them would let a
wall attribution stated plainly in the excavation report lend its
authority to a position that is a spacing rule.
"""

import argparse
import collections
import csv
from pathlib import Path

from sanity_checks import check, warn, failures

ROOT = Path(__file__).resolve().parents[1] / "LAMP_DataStore/ElBagawat"
APERTURES = ROOT / "200_Projects/250_Apertures"
DEM_DIR = ROOT / "200_Projects/220_BuildingsToDEM"

INVENTORY = APERTURES / "aperture_inventory.csv"
FABRIC = APERTURES / "building_fabric.csv"
DIRECTIONS = APERTURES / "entrance_directions.csv"
PAINTINGS = APERTURES / "painting_inventory.csv"
TARGETS = APERTURES / "target_inventory.csv"
DOMES = DEM_DIR / "dome_inventory.csv"
FOOTPRINTS = (ROOT / "100_Data/130_BuildingFootprintsVectorData"
              / "BuildingTracesCurrent/Buildings_Mask.shp")

# What each provenance token in the registries actually rests on. Kept
# here rather than in the document template so an unrecognised token
# shows up as a gap instead of being silently prosified.
TOKENS = {
    "report": ("Excavation report, read by OCR then confirmed against "
               "the page scan"),
    "xlsx": ("`Bagawat Data From Excavation Report.xlsx`, the "
             "pre-existing spreadsheet digest of the same report"),
    "cad": ("`BaseSiteCAD` DXF plots — threshold marks on the LW2 "
            "layer, measured"),
    "derived": ("Inferred by rule from other registry fields; no "
                "document states it"),
    "default": ("A class constant applied to every opening of this "
                "kind; no measurement"),
    "report_type": ("Chapel typology from the report, mapped to a "
                    "thickness for that type"),
    "type_cad": ("Typology mapped through thicknesses measured on the "
                 "CAD-covered chapels"),
    "site_default": ("One site-wide fallback; neither measured nor "
                     "typology-informed"),
    "ortho": "Bright-blob measurement on the orthophoto",
    "fallback": ("Typology says domed, but no blob was measurable — "
                 "radius is a class default"),
    "footprint": "Geometry of the footprint polygon itself",
    "registry": "Height taken from the aperture registry row",
    "ground+fixed": ("Bare-earth DEM sample plus a fixed offset "
                     "(eye/target height)"),
    "": "Not recorded",
}

# Confidence is a grade, not a source, so it gets its own vocabulary —
# rendering it through the source table would file "med" under "what it
# rests on" and read as though a grade were a document.
GRADES = {
    "high": "A document states this value directly; measured",
    "med": ("A document states the fact but not the number — the "
            "number comes from a rule"),
    "medium": ("A document states the fact but not the number — the "
               "number comes from a rule"),
    "low": "No document states it; inferred or defaulted",
    "none": "No position stated at all, even in prose",
    "ref": ("Reference geometry (footprint centroid), not an "
            "evidential claim"),
    "": "Not graded",
}


def load(path):
    if not path.exists():
        return []
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def tally(rows, col):
    """Counts for one column, most common first."""
    return collections.Counter(r.get(col, "") for r in rows).most_common()


def ids_in(rows, key="ID"):
    out = set()
    for r in rows:
        try:
            out.add(int(float(r[key])))
        except (KeyError, TypeError, ValueError):
            pass
    return out


def pct(n, d):
    return f"{100.0 * n / d:.1f}%" if d else "—"


def _table(rows, col, total, title, vocab, header):
    out = [f"\n**{title}**\n",
           f"| value | rows | share | {header} |",
           "| --- | --- | --- | --- |"]
    for val, n in tally(rows, col):
        out.append(f"| `{val or '(blank)'}` | {n} | {pct(n, total)} | "
                   f"{vocab.get(val, '**UNDOCUMENTED**')} |")
    return out


def token_table(rows, col, total, title):
    return _table(rows, col, total, title, TOKENS, "what it rests on")


def grade_table(rows, col, total, title):
    return _table(rows, col, total, title, GRADES, "what it means")


def count_table(rows, col, total, title, label="value"):
    """Plain distribution, for columns that are data rather than
    provenance — a compass direction has no source to name."""
    out = [f"\n**{title}**\n", f"| {label} | rows | share |",
           "| --- | --- | --- |"]
    for val, n in tally(rows, col):
        out.append(f"| `{val or '(blank)'}` | {n} | {pct(n, total)} |")
    return out


def footprint_ids():
    """Every chapel that exists, from the footprint layer, or empty.

    The denominator has to come from the footprints rather than from
    the registries, because the chapels missing from every registry are
    exactly what this report is for. Counting only chapels that appear
    somewhere would divide the gaps by themselves and report full
    coverage.

    Returns an empty set when the layer or geopandas is unavailable, so
    the report can still be generated; the caller falls back to the
    registry union and warns, since that fallback quietly changes what
    the percentages are a share of."""
    try:
        import geopandas as gpd
    except ImportError:
        return set()
    if not FOOTPRINTS.exists():
        return set()
    fp = gpd.read_file(FOOTPRINTS)
    return {int(v) for v in fp["ID"]}


def per_chapel(ap, fab, dirs, dome, paint, all_ids):
    """One row per chapel: what is known about it, and from where."""
    by_kind = collections.defaultdict(lambda: collections.Counter())
    for r in ap:
        by_kind[int(float(r["ID"]))][r["kind"]] += 1
    dirsrc = {int(float(r["ID"])): (r["direction"], r["source"],
                                    r.get("page", ""))
              for r in dirs}
    fabsrc = {int(float(r["ID"])): (r["wall_thickness_m"],
                                    r["thickness_source"],
                                    r["thickness_conf"]) for r in fab}
    domsrc = {int(float(r["ID"])): (r["has_dome"], r["source"])
              for r in dome}
    # One painting row is one *anchor mention*, and a scene is usually
    # described more than once, so scenes have to be counted distinctly
    # or a chapel looks like it holds half again as many as it does.
    scenes = collections.defaultdict(set)
    for r in paint:
        scenes[int(float(r["ID"]))].add(r["scene_no"])
    anchors = collections.Counter(int(float(r["ID"])) for r in paint)

    rows = []
    for bid in sorted(all_ids):
        k = by_kind.get(bid, collections.Counter())
        d = dirsrc.get(bid, ("", "", ""))
        f = fabsrc.get(bid, ("", "", ""))
        m = domsrc.get(bid, ("", ""))
        rows.append({
            "ID": bid,
            "entrance_direction": d[0], "direction_source": d[1],
            "direction_page": d[2],
            "doors": k.get("door", 0), "windows": k.get("window", 0),
            "niches": k.get("niche", 0), "apses": k.get("apse", 0),
            "wall_thickness_m": f[0], "thickness_source": f[1],
            "thickness_conf": f[2],
            "has_dome": m[0], "dome_source": m[1],
            "painted_scenes": len(scenes.get(bid, ())),
            "painting_anchor_rows": anchors.get(bid, 0),
            "modelled": "yes" if bid in by_kind else "no",
        })
    return rows


def build_report(ap, fab, dirs, dome, paint, tgt, all_ids, chapel_rows):
    """The whole document, as a list of markdown lines.

    Nine sections: the source documents and what each can and cannot
    give, then one section per registry rendering its provenance
    columns, then the gaps. Built as one function because the sections
    share derived counts — the same three chapel sets feed section 3
    and gap G3, and splitting them would either recompute or pass a
    growing bag of totals between helpers.

    The gap numbers are all computed here rather than written into the
    prose, so a registry edit moves the text; the only hand-written
    figures are the ones sourced from outside the registries (the
    floor-datum spread, the 46 undescribed chapels), which are marked
    as such where they appear."""
    n_ap, n_ch = len(ap), len(all_ids)
    ap_ids = ids_in(ap)
    dir_ids = ids_in(dirs)
    kinds = collections.Counter(r["kind"] for r in ap)

    # Only a row whose along-wall position was actually measured may
    # claim one; every other s_m is a spacing rule regardless of what
    # source_pos says about the wall.
    pos_sourced = [r for r in ap if r["source_pos"] == "cad"]
    dims_sourced = [r for r in ap if r["source_dims"] != "default"]

    R = ["# Data provenance — what each modelled fact rests on",
         "",
         "> Generated by `scripts/data_provenance.py`; do not edit by "
         "hand. Regenerate after any registry change.",
         "",
         f"Covers **{n_ch} chapels** and **{n_ap} openings**. Every "
         "number in the visibility results is downstream of a row in "
         "one of the registries below.",
         "",
         "## 1. The source documents",
         "",
         "| source | what it can give | what it cannot |",
         "| --- | --- | --- |",
         "| Excavation report (Fakhry), 200 page scans | entrance "
         "direction in words; presence of niches/apses/windows and "
         "which wall; chapel typology; painted scene names | any "
         "position along a wall; almost all dimensions |",
         "| Report plates (plan + elevation pairs) | opening **heights** "
         "(sill/head), read by eye | nothing yet — see gap G2 |",
         "| `BaseSiteCAD` DXF (7 plots) | door width and position, from "
         "LW2 threshold marks | only the chapels those plots cover |",
         "| `Bagawat Data From Excavation Report.xlsx` | typology; a "
         "few entrance directions | dimensions |",
         "| Orthophoto | dome presence/radius by bright-blob | anything "
         "on a vertical surface (near-nadir) |",
         "| DEM pair (with-buildings − bare) | building height, wall "
         "top, floor datum | anything about openings |",
         "| Site plan / `Site_Plan.pdf` | **nothing usable** — see gap "
         "G6 | door positions (proven misleading) |",
         "",
         "## 2. Openings registry (`aperture_inventory.csv`)",
         "",
         f"{n_ap} openings over {len(ap_ids)} chapels: "
         + ", ".join(f"{v} {k}" for k, v in kinds.most_common()) + ".",
         "",
         "**`source_pos` answers *which wall*, not *where along it*.* "
         "This is the single most important thing to understand about "
         "the registry, and the column name invites the opposite "
         "reading. A row marked `report` means the excavation report "
         "named the wall — the report never gives an along-wall "
         "position for any opening in any chapel.",
         ]
    R += token_table(ap, "source_pos", n_ap,
                     "source_pos — provenance of the WALL attribution")
    R += token_table(ap, "source_dims", n_ap,
                     "source_dims — provenance of width/sill/head")
    R += grade_table(ap, "confidence", n_ap, "confidence")

    R += ["",
          "### What is actually measured",
          "",
          "| quantity | measured | derived / default |",
          "| --- | --- | --- |",
          f"| position along wall (`s_m`) | **{len(pos_sourced)}** of "
          f"{n_ap} ({pct(len(pos_sourced), n_ap)}) | "
          f"{n_ap - len(pos_sourced)} from a spacing rule |",
          f"| dimensions (`width_m`/`sill_m`/`head_m`) | "
          f"**{len(dims_sourced)}** of {n_ap} "
          f"({pct(len(dims_sourced), n_ap)}) | "
          f"{n_ap - len(dims_sourced)} class defaults |",
          "",
          "The measured rows are chapels "
          + ", ".join(sorted({r["ID"] for r in pos_sourced}, key=int))
          + " — all three from CAD threshold marks, and even those "
          "carry default *heights*.",
          "",
          "### The class defaults, and what each stands on",
          "",
          "| kind | width | sill | head | calibration |",
          "| --- | --- | --- | --- | --- |",
          "| door | 0.86 m | 0.00 m | 2.10 m | measured on the 3 CAD "
          "chapels |",
          "| window | 0.60 m | 1.44 m | 1.79 m | not measured anywhere "
          "|",
          "| niche | 0.54 m | 0.90 m | 1.45 m | **n = 1** — the report "
          "dimensions exactly one niche in 263 chapels |",
          "| apse | 3.30 m | 0.00 m | 1.38 m | not measured anywhere |",
          "",
          "## 3. Entrance directions (`entrance_directions.csv`)",
          "",
          f"{len(dirs)} of {n_ch} chapels "
          f"({pct(len(dirs), n_ch)}) have a stated entrance direction.",
          ]
    R += token_table(dirs, "source", len(dirs), "source")
    R += count_table(dirs, "direction", len(dirs),
                     "direction as stated", "direction")

    R += ["", "## 4. Wall fabric (`building_fabric.csv`)", ""]
    R += token_table(fab, "thickness_source", len(fab),
                     "thickness_source")
    R += grade_table(fab, "thickness_conf", len(fab), "confidence")

    R += ["", "## 5. Domes (`dome_inventory.csv`)", ""]
    R += token_table(dome, "source", len(dome), "source")

    scene_keys = {(r["ID"], r["scene_no"]) for r in paint}
    R += ["", "## 6. Painted scenes (`painting_inventory.csv`)", "",
          f"**{len(scene_keys)} distinct scenes** over "
          f"{len(ids_in(paint))} chapels, recorded across "
          f"{len(paint)} anchor rows — one row is one *mention* of a "
          "scene's position, and the report describes most scenes more "
          "than once, so rows overcount scenes by about half again."]
    R += count_table(paint, "anchor_class", len(paint),
                     "anchor_class — how the position is stated")

    R += ["", "## 7. Ray-cast targets (`target_inventory.csv`)", "",
          f"{len(tgt)} targets. `z_source` records what set each "
          "target's height, which is where the floor-datum caveat "
          "(gap G4) enters the visibility results."]
    R += token_table(tgt, "z_source", len(tgt), "z_source")

    # ---- gaps -------------------------------------------------------
    # Three different sets that are easy to conflate and differ by
    # enough to matter: a chapel can have a stated direction but no
    # modelled door, or registry rows (a niche, say) but no door.
    no_dir = sorted(all_ids - dir_ids)
    no_model = sorted(all_ids - ap_ids)
    door_ids = {int(float(r["ID"])) for r in ap if r["kind"] == "door"}
    no_door = sorted(all_ids - door_ids)
    dome_rows = [r for r in dome if r.get("has_dome") == "True"]
    dome_fb = [r for r in dome_rows if r.get("source") == "fallback"]
    fab_default = [r for r in fab
                   if r["thickness_source"] == "site_default"]
    # A scene counts as placed if *any* of its mentions is absolute;
    # judging row by row would condemn a scene that the report anchors
    # once and then refers to loosely afterwards.
    anchored = {(r["ID"], r["scene_no"]) for r in paint
                if r.get("anchor_class") == "absolute"}
    paint_weak = {(r["ID"], r["scene_no"]) for r in paint} - anchored

    R += ["", "## 8. Gaps — what is not evidenced", "",
          "Ordered by how much they constrain the conclusions.", "",
          "### G1 — No opening has a sourced position along its wall "
          "(except 3)",
          "",
          f"{n_ap - len(pos_sourced)} of {n_ap} openings "
          f"({pct(n_ap - len(pos_sourced), n_ap)}) are placed by a "
          "spacing rule: a lone opening goes at the wall's midpoint, "
          "several are spread evenly and nudged apart to stop them "
          "overlapping. No document states otherwise for any of them.",
          "",
          "**Consequence.** Only *wall attribution* is claimable. Any "
          "result that depends on where along a wall an opening sits "
          "— two openings facing each other, a window aligning with a "
          "niche opposite — is an artefact of the rule, because "
          "opposite-wall features placed near wall centres align by "
          "construction. This already invalidated one apparent finding "
          "(a window framing a niche in 8 chapels) and is why the "
          "wall-level version of that question is reported instead.",
          "",
          "### G2 — Opening heights were never read off the plates",
          "",
          "The report plates pair a plan with an elevation and are the "
          "only source of sill/head heights in the whole dataset. That "
          "human pass has not happened: `source_dims` contains no "
          "plate-derived rows at all. Every height in the model is a "
          "class default, including all "
          f"{kinds.get('door', 0)} door heads at 2.10 m.",
          "",
          "**Consequence.** Height-sensitive results rest on "
          "assumption. The fresco-visibility bound is deliberately "
          "built to survive this — it uses the registry's *most "
          "favourable* head height and still finds the dome out of "
          "sight — but a result that needed heights to be right would "
          "not be defensible today.",
          "",
          f"### G3 — {len(no_door)} chapels have no modelled entrance",
          "",
          "Three counts that are easy to conflate, and are not the "
          "same set:",
          "",
          "| set | chapels | share |",
          "| --- | --- | --- |",
          f"| no entrance **direction** stated | {len(no_dir)} | "
          f"{pct(len(no_dir), n_ch)} |",
          f"| no **door** in the registry (so no observer station) | "
          f"{len(no_door)} | {pct(len(no_door), n_ch)} |",
          f"| no registry row of **any** kind | {len(no_model)} | "
          f"{pct(len(no_model), n_ch)} |",
          "",
          f"The {len(no_door)} without a door are the set that matters "
          "for the visibility graph, since an observer stands outside "
          "a doorway. **46 of them are never described in the report "
          "at all**, in contiguous ID runs — the signature of a "
          "reporting gap rather than an architectural class. Tested "
          "directly: they are **not** sealed mausolea (bolts, "
          "thresholds and brick jambs are described as general "
          "features, and no chapel anywhere is called entrance-less).",
          "",
          "**Chapel 80, the Chapel of Peace, is among the missing** — "
          "one of the two painted chapels and the most-studied "
          "structure on the site. Its description sits in Chapter V "
          "while the entrance parser reads Chapter VII, so it has "
          "painted scenes recorded but no door, no mesh and no "
          "observer station.",
          "",
          "### G4 — Floor datum is per-point, not per-chapel",
          "",
          "Every sill and target height is derived from the bare DEM "
          "sampled at that feature's own position, so within one "
          "chapel a niche on the downhill wall sits metres below one "
          "on the uphill wall. Measured across 263 footprints: median "
          "spread under the wall midpoints 0.79 m, 33% over 1 m, 9% "
          "over 2 m, worst 4.85 m. That is comparable to a niche's "
          "entire height.",
          "",
          f"### G5 — {len(dome_fb)} domes are typology, not measurement",
          "",
          f"Of {len(dome_rows)} chapels recorded as domed, "
          f"{len(dome_fb)} ({pct(len(dome_fb), max(len(dome_rows), 1))})"
          " have a radius from a class default because no bright blob "
          "was measurable on the orthophoto. `--domes` is off by "
          "default and flagged experimental pending mentor review.",
          "",
          "### G6 — The site plan is not a door source (resolved "
          "negative)",
          "",
          "It looks like the obvious one: a clean 1:5000 line drawing "
          "with visible wall gaps. Those gaps are plan-vs-footprint "
          "registration artefacts at corners, and chapel 180's real "
          "entrance has unbroken linework. Recorded here so nobody "
          "re-derives doors from it; its tiles stay useful for "
          "sanity-checking a wall attribution.",
          "",
          f"### G7 — {len(fab_default)} chapels use a site-default wall "
          "thickness",
          "",
          f"{len(fab_default)} of {len(fab)} chapels have neither a "
          "measured nor a typology-derived thickness, and only "
          f"{len([r for r in fab if r['thickness_source'] == 'cad'])} "
          "are measured from CAD. Thickness sets how deep an opening's "
          "reveal is, which is what clips oblique sightlines through "
          "it.",
          "",
          f"### G8 — {len(paint_weak)} painted scenes have no absolute "
          "position",
          "",
          f"{len(paint_weak)} of {len(scene_keys)} scenes are never "
          "anchored to a compass wall, dome sector or other fixed "
          "surface in any of their mentions — they are positioned only "
          "relative to the previous scene, or not at all. They stay "
          "usable because the report numbers the scenes and walks them "
          "in order around the chamber, but they cannot be placed on a "
          "specific wall independently.",
          "",
          "### G9 — Derived schema columns are intentionally empty",
          "",
          "`perforates`, `depth_m`, `face` and `form` are blank in "
          "every row. They are not missing data: they are resolved at "
          "build time from the opening's `kind`, so that changing how "
          "a niche recesses is one edit rather than 172.",
          "",
          "## 9. Per-chapel table",
          "",
          "`data_provenance_by_chapel.csv` beside this file carries one "
          f"row per chapel ({len(chapel_rows)} rows) with its entrance "
          "direction and source page, opening counts by kind, wall "
          "thickness and its source, dome source, and painted-scene "
          "count.",
          ]
    return "\n".join(R) + "\n"


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path,
                   default=Path(__file__).resolve().parents[1]
                   / "docs/DATA_PROVENANCE.md")
    return p


def main():
    args = build_parser().parse_args()
    ap = load(INVENTORY)
    fab, dirs = load(FABRIC), load(DIRECTIONS)
    dome, paint, tgt = load(DOMES), load(PAINTINGS), load(TARGETS)

    check(len(ap) > 400, "openings registry loaded", f"{len(ap)} rows")
    check(len(fab) > 200, "wall fabric loaded", f"{len(fab)} rows")

    all_ids = footprint_ids()
    if not all_ids:
        all_ids = ids_in(ap) | ids_in(fab) | ids_in(dome)
        warn("chapel id universe fell back to the registry union",
             f"{len(all_ids)} chapels — footprint layer unreadable, so "
             "every coverage share below is a share of chapels that "
             "appear in some registry, not of chapels that exist")
    check(len(all_ids) > 200, "chapel id universe resolved",
          f"{len(all_ids)} chapels")

    unknown = {r.get("source_pos") for r in ap} | \
              {r.get("source_dims") for r in ap}
    unknown |= {r.get("thickness_source") for r in fab}
    unknown |= {r.get("source") for r in dome}
    unknown |= {r.get("source") for r in dirs}
    unknown |= {r.get("z_source") for r in tgt}
    unknown -= set(TOKENS)
    check(not unknown, "every provenance token has a documented "
          "meaning", f"undocumented: {sorted(unknown)}")

    # A grade nobody has defined would render as a blank cell in the
    # table and read as though the row were simply ungraded.
    ungraded = {r.get("confidence") for r in ap} | \
               {r.get("thickness_conf") for r in fab} | \
               {r.get("confidence") for r in paint} | \
               {r.get("confidence") for r in tgt}
    ungraded -= set(GRADES)
    check(not ungraded, "every confidence grade has a documented "
          "meaning", f"undocumented: {sorted(ungraded)}")

    chapel_rows = per_chapel(ap, fab, dirs, dome, paint, all_ids)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build_report(ap, fab, dirs, dome, paint, tgt,
                                     all_ids, chapel_rows))

    csv_path = args.out.with_name("data_provenance_by_chapel.csv")
    with open(csv_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(chapel_rows[0]))
        w.writeheader()
        w.writerows(chapel_rows)

    print(f"\nwrote {args.out.name}, {csv_path.name} "
          f"({len(chapel_rows)} chapels)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
