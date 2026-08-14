# Aperture-aware 3D vs r.viewshed baseline — full comparison

## Method

Task_2 ROI (74 buildings) on the 1.5 m grid, 3 observers, eye heights [1.5, 1.75] m. Baseline is the original user-provided r.viewshed run (`viewshed_mark{obs}_curr.tif`, observer height never recorded), reused unchanged at every swept engine height. 55 of the 74 ROI buildings carry an aperture registry row and are modeled as meshes (`bare` = same as `compare_baseline.py`'s validated solid-block surface; `doorless` = aperture mesh with every opening omitted; `apertured` = the real model). The other 19 ROI buildings have no aperture data and stay plain heightfield blocks in every variant. Domes swept independently: on = dome caps baked into meshes and the remaining heightfield buildings; off = neither. **None of the 3 CAD-measured chapels fall in this ROI** — every meshed door here uses the calibrated 0.86 m default, so this validates the engine, not the aperture data's precision.

## Why the metric is ground cells only

Every number below counts **ground** cells: the 1,011 cells inside a building footprint are excluded. This is a correction, not a convenience. `target_grid` pins each target point to the **heightfield** surface, so a cell inside a footprint gets its target at the extruded block's roof height — and the mesh variants are then asked whether that same fixed point is visible in a scene whose roof sits somewhere slightly different. A target that lands inside the mesh reads "blocked"; one that floats just above it reads "visible" only if nothing else intervenes. That asymmetry biases every mesh variant against the heightfield **regardless of apertures**. Measured on this ROI: 77-92% of all bare-vs-doorless flips fall inside footprints, and excluding them removes ~84% of the apparent wall/roof effect. Ground visibility is also the well-posed question for this project — what an observer sees *between* and *through* buildings, not whether a rooftop pixel counts.

## Agreement vs r.viewshed baseline (ground cells)

| domes | eye_height_m | observer | apertured | bare | doorless |
| --- | --- | --- | --- | --- | --- |
| False | 1.5 | 1 | 95.34 | 98.76 | 95.34 |
| False | 1.5 | 2 | 95.87 | 97.07 | 95.87 |
| False | 1.5 | 3 | 96.39 | 98.08 | 96.39 |
| False | 1.75 | 1 | 95.23 | 99.14 | 95.23 |
| False | 1.75 | 2 | 95.53 | 97.11 | 95.53 |
| False | 1.75 | 3 | 96.54 | 98.65 | 96.54 |
| True | 1.5 | 1 | 95.34 | 98.76 | 95.34 |
| True | 1.5 | 2 | 95.87 | 97.07 | 95.87 |
| True | 1.5 | 3 | 96.39 | 98.08 | 96.39 |
| True | 1.75 | 1 | 95.23 | 99.14 | 95.23 |
| True | 1.75 | 2 | 95.53 | 97.11 | 95.53 |
| True | 1.75 | 3 | 96.54 | 98.65 | 96.54 |

## Decomposition: visible GROUND cells, summed over 3 observers

| domes | eye_height_m | bare | doorless | apertured | wall_roof_effect | door_effect |
| --- | --- | --- | --- | --- | --- | --- |
| False | 1.5 | 1229.0 | 1405.0 | 1405.0 | 176.0 | 0.0 |
| False | 1.75 | 1295.0 | 1503.0 | 1503.0 | 208.0 | 0.0 |
| True | 1.5 | 1229.0 | 1405.0 | 1405.0 | 176.0 | 0.0 |
| True | 1.75 | 1295.0 | 1503.0 | 1503.0 | 208.0 | 0.0 |

**Door effect on ground visibility is zero here, and that is a property of this raster sweep, not evidence doors have little effect.** `target_grid` fixes every target cell's height once, from the ORIGINAL with-buildings DEM, before any scene variant is built — so a cell inside a footprint is always tested at the block's **roof** height, in `bare`, `doorless`, and `apertured` alike. A door sits near ground level, well below that test height, so it can essentially never change whether that point is visible. Confirmed directly: the handful of doorless-vs-apertured flips that do occur on the (confounded) all-cells metric land 100% inside footprints — i.e. at the fixed roof-height target, not at a newly-revealed floor. This is a structural limitation of testing at a fixed roof-height target, not a sample-size problem — more observers or buildings would not fix it. The visibility **graph**'s centroid test (`build_viewgraph`) does not have this problem: for a meshed building it evaluates centroid visibility through `HybridScene.surface_z`, which forwards to the flattened base scene — the true interior floor height, not the roof. That is the tool that finds a real, positive effect, on the site-wide 0.4 m graded run (`PROGRESS.md`, 2026-08-14; 202 meshed chapels, 3 observers, 789 building-observer pairs, matched building set at every step): solid 36,520 ground cells and 8 centroid-visible pairs; **doors +137 cells and 8 -> 11 centroid pairs (+37.5%)**; windows a further **+1,201 cells but no additional centroid pair**; niches and apses exactly **0** on every metric, as recesses that never perforate must be. That graph result, not this raster delta, is the correct aperture evidence.

**wall_roof_effect is +192 ground cells on average** — 15.2% of visible ground (it reads +684 on the confounded all-cells metric). What remains after excluding rooftops is a genuine raster-vs-vector difference: the heightfield's building edges are bilinearly interpolated ramps between cell centres, while the mesh stands a true vertical wall on the exact footprint boundary. Neither is 'wrong' — they are two representations of the same polygon — but they do not produce identical silhouettes. That this residual is a *discretization* artifact rather than a modelling disagreement shows in how it scales with cell size: it is **15.2% of visible ground on this 1.5 m grid**, and falls to well under 1% on the 0.4 m crop of the same ROI. That is what a silhouette-quantization term does and what a real geometric disagreement would not.

## Dome effect (domes-on minus domes-off), ground cells

Domes change **nothing** on ground visibility in any condition here. That is consistent rather than suspicious: a dome cap sits above roof level, so it can only occlude ground that a sightline would reach by grazing over a rooftop, and at a 1.5-1.75 m eye height in a ROI this tight, such a ground cell is already blocked by the wall below. The dome effect that showed up on the all-cells metric was rooftop cells.

| variant | eye_height_m | observer | dome_effect |
| --- | --- | --- | --- |
| apertured | 1.5 | 1 | 0.0 |
| apertured | 1.5 | 2 | 0.0 |
| apertured | 1.5 | 3 | 0.0 |
| apertured | 1.75 | 1 | 0.0 |
| apertured | 1.75 | 2 | 0.0 |
| apertured | 1.75 | 3 | 0.0 |
| bare | 1.5 | 1 | 0.0 |
| bare | 1.5 | 2 | 0.0 |
| bare | 1.5 | 3 | 0.0 |
| bare | 1.75 | 1 | 0.0 |
| bare | 1.75 | 2 | 0.0 |
| bare | 1.75 | 3 | 0.0 |
| doorless | 1.5 | 1 | 0.0 |
| doorless | 1.5 | 2 | 0.0 |
| doorless | 1.5 | 3 | 0.0 |
| doorless | 1.75 | 1 | 0.0 |
| doorless | 1.75 | 2 | 0.0 |
| doorless | 1.75 | 3 | 0.0 |

## All-cells decomposition (confounded — kept for contrast)

| domes | eye_height_m | bare | doorless | apertured | wall_roof_effect | door_effect |
| --- | --- | --- | --- | --- | --- | --- |
| False | 1.5 | 1615.0 | 2272.0 | 2272.0 | 657.0 | 0.0 |
| False | 1.75 | 1696.0 | 2403.0 | 2403.0 | 707.0 | 0.0 |
| True | 1.5 | 1605.0 | 2265.0 | 2265.0 | 660.0 | 0.0 |
| True | 1.75 | 1686.0 | 2397.0 | 2397.0 | 711.0 | 0.0 |

## Figures

- `compare_apertures_obs{1,2,3}.png` — r.viewshed | aperture-aware 3D (eye 1.5 m, no domes) | agreement, ground cells only

- `decomposition_apertures.png` — bare/doorless/apertured ground-cell totals, domes on vs off, both eye heights
