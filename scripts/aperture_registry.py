"""Shared vocabulary for the aperture pipeline (registry + walls).

Library module (torch-free, like volume_mesh.py) imported by
extract_site_plan.py, extract_dxf_plans.py and build_aperture_walls.py.
Holds the two things that MUST agree across all of them:

1. The registry schema for `aperture_inventory.csv` — one row per
   opening, editable by hand, provenance split between where the
   *position* came from and where the *dimensions* came from.
2. `canonical_walls()` — the deterministic footprint-ring
   canonicalization that gives "wall index N" a stable meaning. The
   seeder and the builder must compute identical wall lists, so the
   function lives here exactly once. Each registry row also carries
   the wall's outward azimuth + midpoint as drift detectors: if a
   future footprint edit or tolerance tweak renumbers the walls, the
   builder notices the mismatch instead of cutting a hole in the
   wrong wall.
"""

import math
from pathlib import Path

import numpy as np

from sanity_checks import ROOT

APERTURES_DIR = ROOT / "200_Projects/250_Apertures"
INVENTORY = APERTURES_DIR / "aperture_inventory.csv"

REGISTRY_COLS = ["ID", "ap_id", "kind", "wall", "s_m", "width_m",
                 "sill_m", "head_m", "wall_az", "wall_mx", "wall_my",
                 "source_pos", "source_dims", "confidence", "notes"]

# Opening defaults (m) for rows lacking measured dimensions. Every
# defaulted row is tagged source_dims=default so the comparison report
# can always split measured from assumed.
#
# WIDTH is calibrated, not guessed: the only doors anywhere in this
# dataset that are actually *drawn to size* are the threshold marks on
# the site CAD's LW2 layer, which measure 0.79 / 0.86 / 1.09 m on
# chapels 23 / 24 / 25 (scripts/extract_site_cad.py). n=3 is thin, so
# treat this as a calibrated placeholder rather than a site statistic
# — but it beats the round 1.0 m it replaces, and it is narrower,
# which makes the aperture effect smaller rather than flattering it.
#
# SILL and HEAD remain wholly assumed for doors. Neither the report
# text nor the CAD states a door height anywhere; only the plate
# elevation drawings could supply one. Those drawings were read
# (scripts/extract_plate_figures.py), pixel-exact against their own
# scale bars, and the finding is negative rather than confirmatory:
# the plain rectangle in the middle of a decorated facade (present on
# chapels 8/45/71/166 and read carefully on two of them) measures only
# 0.3-0.45 m wide — narrower than a passable doorway and narrower than
# every CAD-measured door (0.79-1.09 m). It is very likely a recessed
# decorative panel drawn *within* the true opening, not the opening
# itself, but there is no second view (e.g. a plan cut at the same
# spot) to confirm which. Rather than merge a number that may be
# measuring the wrong thing, the defaults below are left as the CAD
# calibration alone. Most plates can't help further regardless:
# plan-only figures (~20 of 29 with a scaled drawing) show no wall
# break for the door at all.
DOOR_WIDTH = 0.86
DOOR_HEAD = 2.1
DOOR_SILL = 0.0


def largest_poly(geom):
    """Largest polygon of a possibly-multi-part footprint (the same
    idiom as build_dome_layer's helper, repeated here so this module
    doesn't import that script's matplotlib stack)."""
    if geom.geom_type == "MultiPolygon":
        return max(geom.geoms, key=lambda g: g.area)
    return geom


def canonical_walls(geom, simplify_tol=0.15, min_edge=0.5,
                    az_merge_deg=5.0):
    """Footprint -> deterministic wall list [(p0, p1), ...].

    Ring cleanup order matters: simplify first (kills densified-circle
    vertex noise), then merge digitizing slivers (< min_edge edges are
    collapsed to their midpoint — they are artifacts, not walls), then
    drop near-collinear vertices (a wall digitized as two segments is
    one physical wall). The ring is oriented CCW and rotated so wall 0
    starts at the lexicographically-lowest vertex, making indices
    independent of where the digitizer happened to start."""
    poly = largest_poly(geom).simplify(simplify_tol,
                                       preserve_topology=True)
    coords = [tuple(c) for c in poly.exterior.coords[:-1]]
    if not poly.exterior.is_ccw:
        coords.reverse()

    def edge_len(a, b):
        return math.hypot(b[0] - a[0], b[1] - a[1])

    changed = True
    while changed and len(coords) > 3:
        changed = False
        # Collapse the shortest sub-threshold edge to its midpoint.
        n = len(coords)
        lens = [edge_len(coords[i], coords[(i + 1) % n])
                for i in range(n)]
        i = int(np.argmin(lens))
        if lens[i] < min_edge:
            a, b = coords[i], coords[(i + 1) % n]
            mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
            coords = [c for j, c in enumerate(coords)
                      if j not in (i, (i + 1) % n)]
            coords.insert(i if i < len(coords) else len(coords), mid)
            changed = True
            continue
        # Drop a vertex whose turn is below the merge angle.
        for i in range(len(coords)):
            a = coords[i - 1]
            b = coords[i]
            c = coords[(i + 1) % len(coords)]
            az1 = math.atan2(b[0] - a[0], b[1] - a[1])
            az2 = math.atan2(c[0] - b[0], c[1] - b[1])
            turn = abs((math.degrees(az2 - az1) + 180) % 360 - 180)
            if turn < az_merge_deg:
                coords.pop(i)
                changed = True
                break

    start = min(range(len(coords)), key=lambda i: coords[i])
    coords = coords[start:] + coords[:start]
    return [(coords[i], coords[(i + 1) % len(coords)])
            for i in range(len(coords))]


def wall_azimuth(p0, p1):
    """Compass azimuth (deg) of the wall's outward normal. The ring is
    CCW, so the interior lies left of p0->p1 and outward is the right-
    hand normal (dy, -dx)."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    return math.degrees(math.atan2(dy, -dx)) % 360.0


COMPASS = {"N": 0.0, "NE": 45.0, "E": 90.0, "SE": 135.0, "S": 180.0,
           "SW": 225.0, "W": 270.0, "NW": 315.0}


def wall_for_azimuth(walls, azimuth):
    """Wall whose outward normal best matches a compass azimuth, with
    the angular error. This is how the excavation report's per-chapel
    statements ("it opens west", "its entrance opens south" — Chapter
    III) become registry rows: the report gives the *direction* a
    chapel faces, which is the fact that actually drives visibility,
    while the exact position along that wall is a detail the report
    rarely fixes. Ambiguity is real and worth surfacing — a chapel
    whose two candidate walls sit within a few degrees of the stated
    direction should be flagged, not silently resolved."""
    errs = [abs((wall_azimuth(*w) - azimuth + 180) % 360 - 180)
            for w in walls]
    best = int(np.argmin(errs))
    runner = sorted(errs)[1] if len(errs) > 1 else 180.0
    return best, errs[best], runner


def wall_fields(walls, wi):
    """The registry's redundant anchor fields for wall index `wi`:
    (wall_az, wall_mx, wall_my)."""
    p0, p1 = walls[wi]
    return (round(wall_azimuth(p0, p1), 1),
            round((p0[0] + p1[0]) / 2, 2),
            round((p0[1] + p1[1]) / 2, 2))


def resolve_wall(walls, row, check, warn):
    """Map a registry row back to a wall index, drift-safely.

    Trusts the stored index when its azimuth/midpoint still match
    (5 deg / 0.5 m); otherwise re-matches by nearest wall midpoint
    with a warn, and returns None (caller checks) when nothing lies
    within 2 m — a silently-renumbered wall must never get a hole cut
    on faith.

    Hand-edit convention: **blank the three anchor cells whenever you
    change `wall` by hand.** Empty anchors mean "trust the index" and
    the builder re-derives them; leaving stale anchors in place would
    otherwise re-match the row to the wall you just moved it away
    from, silently undoing the edit."""
    wi = int(row["wall"])
    blank = [row.get(k, "") in ("", None) for k in
             ("wall_az", "wall_mx", "wall_my")]
    if any(blank):
        if not check(0 <= wi < len(walls),
                     f"ID {row['ID']} ap {row['ap_id']}: wall index in "
                     "range", f"{wi} of {len(walls)} walls"):
            return None
        az, mx, my = wall_fields(walls, wi)
        print(f"    ID {row['ID']} ap {row['ap_id']}: anchors blank, "
              f"trusting wall {wi} — derived az {az}, mid ({mx}, {my})")
        return wi
    az, mx, my = (float(row["wall_az"]), float(row["wall_mx"]),
                  float(row["wall_my"]))
    if 0 <= wi < len(walls):
        waz, wmx, wmy = wall_fields(walls, wi)
        d_az = abs((waz - az + 180) % 360 - 180)
        d_m = math.hypot(wmx - mx, wmy - my)
        if d_az <= 5.0 and d_m <= 0.5:
            return wi
    dists = [math.hypot((p0[0] + p1[0]) / 2 - mx,
                        (p0[1] + p1[1]) / 2 - my)
             for p0, p1 in walls]
    best = int(np.argmin(dists))
    if dists[best] <= 2.0:
        warn(f"ID {row['ID']} ap {row['ap_id']}: wall re-matched",
             f"stored index {wi} disagrees with its anchor; using "
             f"wall {best} ({dists[best]:.2f} m from the stored "
             f"midpoint). If you moved this opening by hand, blank "
             f"the wall_az/wall_mx/wall_my cells instead")
        return best
    check(False, f"ID {row['ID']} ap {row['ap_id']}: wall resolves",
          f"stored wall {wi} az {az} mid ({mx}, {my}) matches nothing")
    return None
