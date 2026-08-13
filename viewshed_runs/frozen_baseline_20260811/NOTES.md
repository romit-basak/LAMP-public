# Frozen baseline — 2026-08-11

Reference state captured **before** the interior-feature work, so that
"did I break the published aperture result?" is answerable mechanically.
Verified with `scripts/check_regression.py` (which is itself verified to
detect drift: a one-character edit and a deleted file both trip it).

Published numbers this protects: **+115 ground cells / +0.28%** over 197
buildings and 4 observers, and **+23% centroid-visible** building pairs
on the visibility graph.

## Manifests

- `meshes.sha256` (197), `meshes_solid.sha256` (197),
  `meshes_nodomes.sha256` (54), `meshes_solid_nodomes.sha256` (54)
- `aperture_inventory.csv` — the registry as frozen (197 rows, all
  `kind=door`, 15 columns)
- `comparison_metrics_apertures*.csv` — the 0.4 m and 1.5 m ROI sweeps

## Amendment 1 — `wall_panel()` column-sweep rewrite

`wall_panel()` was rewritten from a left-to-right walk to a column
sweep, because the old form could not represent two openings stacked in
the same span: the lower opening's header band ran from its head to the
roof and filled in the upper opening, which then silently did not
exist. The excavation report repeatedly places a light aperture
directly above a niche, so that case had to work before any interior
row could be merged.

**191 of 197 meshes are byte-identical after the rewrite.** Six are
not, and the difference is a deliberate correction rather than drift:

| building | wall area delta | max AABB shift |
|---|---|---|
| 42 | −0.034 m² | 0.000 m |
| 154 | −0.089 m² | 0.016 m |
| 224 | −0.720 m² | 0.000 m |
| 248 | −0.032 m² | 0.000 m |
| 261 | −0.253 m² | 0.000 m |
| 262 | −0.189 m² | 0.019 m |

These are exactly the six chapels whose door wall is shorter than the
0.86 m default door, so the builder's own nudge bounds cross and the
opening's start lands at a negative distance along the wall (the build
log warns for each: *"opening nudged inside its wall"*). The previous
code then drew the opening's header and sill bands starting **before
the wall's first vertex**, i.e. a sliver of wall face hanging past the
corner. The rewrite clamps the sweep to the panel, so the bands now
start at the wall.

Practically inert — walls meet at corners, so the overhang only
overlapped the neighbouring panel, which an any-hit occlusion test
cannot see. It was still geometry outside its own wall, and
reproducing it deliberately would have meant writing code to preserve
an artifact. Triangle counts are unchanged, all six still pass
`check_soup`, and chapel 181 (also nudged, but to a position that stays
positive) correctly did **not** change — which is the control that
confirms the cause.

`meshes.sha256` was re-frozen after this amendment. `meshes.sha256.pre_colsweep`
keeps the original hashes as the audit trail.
