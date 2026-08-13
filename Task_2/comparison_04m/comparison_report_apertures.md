# Aperture-aware 3D vs r.viewshed baseline — full comparison

## Method

Task_2 ROI (74 buildings) on the 0.4 m grid, 3 observers, eye heights [1.5, 1.75] m. Baseline generated fresh for this resolution (pattern `viewshed_mark{obs}_04m_h{h}.tif`), swept at the same eye heights as the engine — each engine height is compared to its own matching baseline height, not one fixed unknown-height baseline reused throughout. 54 of the 74 ROI buildings carry an aperture registry row and are modeled as meshes (`bare` = same as `compare_baseline.py`'s validated solid-block surface; `doorless` = aperture mesh with every opening omitted; `apertured` = the real model). The other 20 ROI buildings have no aperture data and stay plain heightfield blocks in every variant. Domes swept independently: on = dome caps baked into meshes and the remaining heightfield buildings; off = neither. **None of the 3 CAD-measured chapels fall in this ROI** — every meshed door here uses the calibrated 0.86 m default, so this validates the engine, not the aperture data's precision.

## Why the metric is ground cells only

Every number below counts **ground** cells: the 10,026 cells inside a building footprint are excluded. This is a correction, not a convenience. `target_grid` pins each target point to the **heightfield** surface, so a cell inside a footprint gets its target at the extruded block's roof height — and the mesh variants are then asked whether that same fixed point is visible in a scene whose roof sits somewhere slightly different. A target that lands inside the mesh reads "blocked"; one that floats just above it reads "visible" only if nothing else intervenes. That asymmetry biases every mesh variant against the heightfield **regardless of apertures**. Measured on this ROI: 77-92% of all bare-vs-doorless flips fall inside footprints, and excluding them removes ~84% of the apparent wall/roof effect. Ground visibility is also the well-posed question for this project — what an observer sees *between* and *through* buildings, not whether a rooftop pixel counts.

## Agreement vs r.viewshed baseline (ground cells)

| domes | eye_height_m | observer | apertured | bare | doorless |
| --- | --- | --- | --- | --- | --- |
| False | 1.5 | 1 | 99.77 | 99.83 | 99.77 |
| False | 1.5 | 2 | 99.49 | 99.61 | 99.49 |
| False | 1.5 | 3 | 99.67 | 99.7 | 99.67 |
| False | 1.75 | 1 | 99.65 | 99.71 | 99.65 |
| False | 1.75 | 2 | 99.39 | 99.54 | 99.39 |
| False | 1.75 | 3 | 99.66 | 99.68 | 99.66 |
| True | 1.5 | 1 | 99.77 | 99.83 | 99.77 |
| True | 1.5 | 2 | 99.49 | 99.61 | 99.49 |
| True | 1.5 | 3 | 99.67 | 99.7 | 99.67 |
| True | 1.75 | 1 | 99.65 | 99.71 | 99.65 |
| True | 1.75 | 2 | 99.39 | 99.54 | 99.39 |
| True | 1.75 | 3 | 99.66 | 99.68 | 99.66 |

## Decomposition: visible GROUND cells, summed over 3 observers

| domes | eye_height_m | bare | doorless | apertured | wall_roof_effect | door_effect |
| --- | --- | --- | --- | --- | --- | --- |
| False | 1.5 | 12655.0 | 12592.0 | 12592.0 | -63.0 | 0.0 |
| False | 1.75 | 13797.0 | 13706.0 | 13706.0 | -91.0 | 0.0 |
| True | 1.5 | 12655.0 | 12592.0 | 12592.0 | -63.0 | 0.0 |
| True | 1.75 | 13797.0 | 13706.0 | 13706.0 | -91.0 | 0.0 |

**Door effect on ground visibility is zero here, and that is a property of this raster sweep, not evidence doors have little effect.** `target_grid` fixes every target cell's height once, from the ORIGINAL with-buildings DEM, before any scene variant is built — so a cell inside a footprint is always tested at the block's **roof** height, in `bare`, `doorless`, and `apertured` alike. A door sits near ground level, well below that test height, so it can essentially never change whether that point is visible. Confirmed directly: the handful of doorless-vs-apertured flips that do occur on the (confounded) all-cells metric land 100% inside footprints — i.e. at the fixed roof-height target, not at a newly-revealed floor. This is a structural limitation of testing at a fixed roof-height target, not a sample-size problem — more observers or buildings would not fix it. The visibility **graph**'s centroid test (`build_viewgraph`) does not have this problem: for a meshed building it evaluates centroid visibility through `HybridScene.surface_z`, which forwards to the flattened base scene — the true interior floor height, not the roof. That is the tool that already found a real, positive effect in the site-wide 0.4 m run (`PROGRESS.md`, 2026-08-08): **+115 ground cells / +0.28%** over 197 buildings and 4 observers, and **+23% centroid-visible building pairs**. That graph result, not this raster delta, is the correct aperture evidence.

**wall_roof_effect is -77 ground cells on average** — 0.6% of visible ground (it reads -370 on the confounded all-cells metric). What remains after excluding rooftops is a genuine raster-vs-vector difference: the heightfield's building edges are bilinearly interpolated ramps between cell centres, while the mesh stands a true vertical wall on the exact footprint boundary. Neither is 'wrong' — they are two representations of the same polygon — but they do not produce identical silhouettes. That this residual is a *discretization* artifact rather than a modelling disagreement shows in how it scales with cell size: it is **0.6% of visible ground on this 0.4 m grid**, against ~14% on the coarser 1.5 m grid — over an order of magnitude smaller once the cell is small enough to resolve a building edge. That is what a silhouette-quantization term does and what a real geometric disagreement would not.

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
| False | 1.5 | 14533.0 | 14141.0 | 14149.0 | -392.0 | 8.0 |
| False | 1.75 | 15767.0 | 15369.0 | 15375.0 | -398.0 | 6.0 |
| True | 1.5 | 14430.0 | 14086.0 | 14094.0 | -344.0 | 8.0 |
| True | 1.75 | 15648.0 | 15301.0 | 15307.0 | -347.0 | 6.0 |

## Figures

- `compare_apertures_obs{1,2,3}.png` — r.viewshed | aperture-aware 3D (eye 1.5 m, no domes) | agreement, ground cells only

- `decomposition_apertures.png` — bare/doorless/apertured ground-cell totals, domes on vs off, both eye heights
