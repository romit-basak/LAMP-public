# Baseline (r.viewshed) vs 3D engine — comparison

## Method

Both sides use the **same surface** — `Task_2/DEM_Subset-WithBuildings.tif`
(54x68 @ 1.5 m, EPSG:32636, the baseline's exact grid) — so this isolates
**algorithm + observer height**, not DEM/datum/resolution. The 3D engine
(`scripts/viewshed.py`, `HeightfieldScene`) is re-run on that grid and compared
cell-for-cell to the user's GRASS r.viewshed output (`viewshed_mark*_curr.tif`,
binarized as finite-cell = visible). Observer height was not recorded in the
QGIS project, so it is **swept (1.5 m, 1.75 m)** and
reported as a sensitivity.

## Why high agreement is the *expected, validating* result

r.viewshed already computes true 3D line-of-sight on the DEM; its
thesis-relevant limitation is that buildings are baked in as **solid** blocks.
The 3D engine currently also treats buildings as solid (no apertures yet), so
the two should largely **agree** — confirming the engine reproduces the
established tool on the common case. The contribution of this project — sight
passing *through* window/door apertures — is **step 2**, and that is where the
3D result will deliberately diverge from r.viewshed.

## Results (primary eye height 1.5 m)

| observer | eye_height_m | baseline_visible | engine_visible | both_visible | baseline_only | engine_only | both_hidden | agreement_pct | jaccard_iou |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 1.5 | 568 | 536 | 522 | 46 | 14 | 3090 | 98.37 | 0.897 |
| 2 | 1.5 | 709 | 668 | 637 | 72 | 31 | 2932 | 97.19 | 0.861 |
| 3 | 1.5 | 460 | 411 | 402 | 58 | 9 | 3203 | 98.18 | 0.857 |

### Height sensitivity (all runs)

| observer | eye_height_m | baseline_visible | engine_visible | both_visible | baseline_only | engine_only | both_hidden | agreement_pct | jaccard_iou |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 1.5 | 568 | 536 | 522 | 46 | 14 | 3090 | 98.37 | 0.897 |
| 2 | 1.5 | 709 | 668 | 637 | 72 | 31 | 2932 | 97.19 | 0.861 |
| 3 | 1.5 | 460 | 411 | 402 | 58 | 9 | 3203 | 98.18 | 0.857 |
| 1 | 1.75 | 568 | 565 | 543 | 25 | 22 | 3082 | 98.72 | 0.92 |
| 2 | 1.75 | 709 | 692 | 650 | 59 | 42 | 2921 | 97.25 | 0.866 |
| 3 | 1.75 | 460 | 439 | 424 | 36 | 15 | 3197 | 98.61 | 0.893 |

Mean agreement is highest at **1.75 m** (98.2%), a hint at the
baseline's unrecorded observer height.

## Figures

- `compare_obs{1,2,3}.png` — baseline | 3D | agreement (green=both,
  blue=baseline-only, red=3D-only) over the orthophoto.
- `building_effect.png` — bare-earth vs with-buildings r.viewshed. Finding: at
  1.5 m the buildings block **~0 additional ground** (cells behind them were
  already terrain-hidden) and only add their own **rooftops** to the visible
  set. The coarse baseline barely registers building occlusion.

## Interpretation & caveats

- Residual disagreement comes from: ray-march + bilinear sampling vs
  r.viewshed's sweep/interpolation; the (unconfirmed) baseline height;
  edge effects at the tiny ROI; ~1-px registration. Earth curvature is
  negligible over ~100 m.
- **Resolution matters as much as algorithm.** On this 1.5 m subset both
  methods under-represent building occlusion (a ~4 m building spans <3 cells);
  the production 3D run on the 0.4 m surface resolves the same buildings as real
  blocks (visible fractions drop to ~0.5-1.8%). The validation here is "same
  algorithm, same grid → same answer"; the fidelity gain is at 0.4 m + apertures.
- The subset DEM is the **older datum** (~78 m above the current 0.4 m DEM);
  irrelevant here because both sides use it, but the production 3D run uses the
  regenerated 0.4 m surface.
- **No apertures yet** — the headline 3D-vs-2D divergence is step 2, sourced
  from `Task_2/Site_Plan.pdf` (doorway/opening directions).

## For mentors

1. What observer/target height + max distance did the r.viewshed baseline use?
2. Confirm the comparison ROI / whether to extend the baseline to the full site.
3. Aperture digitization from `Site_Plan.pdf` — confirm the source is authoritative.
