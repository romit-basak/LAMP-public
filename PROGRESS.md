# Progress Log

> Running record of major work on the viewshed half of LAMP. Newest entries at the top.
> Keep entries brief: what was done, key findings/decisions, open questions raised.

## 2026-06-25 — Code walkthrough doc + fixed compare_baseline.py

- New [`docs/CODE_WALKTHROUGH.md`](docs/CODE_WALKTHROUGH.md): narrated tour of all five scripts — what each does, the design rationale, and what would break if done otherwise (Scene seam, eye-relative float32 coords, running-max LOS, the two MPS gotchas, view-cone post-mask, additive `visible_mask` signature, self-checks-vs-ground-truth). Closes with the `load_observers` break below as a coupling case study. Linked from the project guide's References section.
- `compare_baseline.py` had been crashing since the 06-13 id-selection refactor: it still called `load_observers` expecting a list of `(x, y)` pairs, but the function now returns `(obs_list, has_id)` with `(oid, x, y)` triples → `ValueError` at the "3 observers" check. The mid-term-gating comparison was silently unrunnable.
- Fix: unpack the 2-tuple and reduce to `(x, y)` pairs (`obs_list, _ = load_observers(...)`). Re-ran clean — reproduces the recorded 97.2–98.7% agreement / IoU 0.86–0.92, confirming the fix restores behavior rather than masking it.

## 2026-06-29 — LAS/LAZ volume output (point clouds for QGIS)

- Added `las`/`laz` to `--volume-format` in `scripts/viewshed.py` and `--to` in `scripts/volume_convert.py`, writing georeferenced point clouds (CRS in the LAS header) so QGIS point-cloud layers can color by Z with a height ramp and CloudCompare opens them directly. Combined-volume observer count is stored as LAS intensity. `all` stays `csv,ply,npy` — LAS/LAZ are explicit opt-ins; laspy is lazy-imported so the engine still runs without it.
- New deps **laspy 2.7.0 + lazrs 0.8.1** (cross-platform CPU wheels) pinned in `requirements.txt`; remote datastore freeze left untouched. Validated: LAZ round-trips coords/intensity and parses back to EPSG:32636.
- Prompted by QGIS work: the 3D *vector* material color can't be data-defined per point (no continuous height ramp), so a native point-cloud format is the way to get height-colored 3D in QGIS. (Separately: render the DEM/buildings in 3D by setting the 3D scene **Terrain** to the DEM-with-buildings, so absolute-Z ground/buildings align with the floating cloud.)

## 2026-06-24 — Vertical view cone + 3D volume + configurable eye height (engine extension)

- Prompted by mentor review: the engine was read as "2D only." It is a true 2.5D heightfield ray-caster (the LOS test already does an elevation-angle reduction), so this round makes the vertical dimension explicit and demonstrable rather than changing the physics. **Window apertures deferred** to step 2 per mentors.
- `scripts/viewshed.py` now has a vertical view cone — `--pitch` (elevation center, 0=horizontal, + up) and `--vfov` (full vertical width, default 180 = unconstrained) — mirroring `--azimuth`/`--fov`. `apply_view_constraints` gained the vertical sector; applies to rasters and the volume, not the graph (same as horizontal). Banner/QC titles show the vertical sector.
- New optional 3D **visibility volume** (`--volume`, off by default): samples a voxel grid (`--voxel`/`--zmin`/`--zmax`/`--zstep`, `--volume-fullres` for native res) over a `--radius` disc or, if no radius, the whole DEM. Reuses `visible_mask` (it already accepts per-target z), so no change to `compute_viewshed`/`visible_mask` signatures (keeps `compare_baseline.py` intact). Writes CSV (canonical), PLY point cloud, and/or NPY+JSON via `--volume-format {csv,ply,npy,all}`, plus a top-down QC PNG and a combined count in shared mode.
- `scripts/volume_convert.py` (new): promotes a saved volume CSV to PLY / NPY+sidecar / per-z GeoTIFF slices without recomputing — so CSV stays the lightweight default.
- `--eye-height` added (default stays 1.5 m); threaded through eye construction and the reverse-LOS self-check.
- Hygiene pass: removed unused `Point` import, neutralized guide/assistant references in code comments and this log, AST-checked for unused locals. Mentors' note on **distance-based visual obscurity** (acuity falls off with range; unbounded rays overstate far visibility) recorded as a planned refinement in the project guide — not implemented.

## 2026-06-13 — id-based observer selection (engine CLI extension)

- `scripts/viewshed.py` now reads the observer file's `id` field: `load_observers` returns `(id, x, y)` (id field if present, else 1-based row index). New `--ids N [N …]` selects specific observers by id from `--observers` (fails cleanly listing any missing id); omitting `--ids` runs the whole table. Built for the mentors' forthcoming exhaustive, id-referenced point set — so observers are chosen by id, not coordinates.
- Ids flow through everything: output filenames (`viewshed_id80.tif/.png`), QC titles, visibility-graph `src_id`/`dst_id`, and the obs-obs matrix labels. Inline `--point` still works (synthetic `obs1..` labels; `--ids` then ignored with a warn).
- Used it to run the user's analysis on `my_observers.gpkg` (ids 80/180/181) → `viewshed_runs/`: 360° from all three; id80 NE cone (az 45°, fov 120°); id180 (az 10°, fov 45°); id181 due N (az 0°, fov 45°). Radius 200 m, `--no-graph`.
- Fixed self-check #1: a building-adjacent observer legitimately has some immediate neighbours occluded by its own wall (id181 sits 0.6 m from building 181), which false-FAILED the old "all 4 neighbours visible" assertion. Now requires ≥1 of 8 neighbours visible (catches total blindness / sign bugs; tolerates walls) — important since the mentors' full point set will be building-adjacent.

## 2026-06-13 — Directional / point-based viewshed (engine CLI extension)

- `scripts/viewshed.py` gained: arbitrary observer count (relaxed the hardcoded 3), inline `--point X Y` (repeatable, overrides `--observers`), per-observer `--radius` (window follows the point), and a `--azimuth`/`--fov` view cone (compass degrees, 0=N/90=E/CW; `--fov` = full width; default 360 = omni). `--no-graph` for single/point runs. Sector + radius applied as a post-mask on the LOS result; QC PNGs draw the view wedge.
- Notes: 360° was already the default behavior. In radius mode each observer has its own window so no combined/count layer. The visibility graph stays omnidirectional (sector applies to rasters only). Small-window tiling fixed in `write_viewshed_tif` (untiled <256 px). Smoke-tested: single-point 60° east cone @200 m (wedge audited) and multi-point radius runs.

## 2026-06-13 — Baseline (r.viewshed) vs 3D comparison (mid-term-gating deliverable)

- User moved their QGIS work into `Task_2/` (GRASS r.viewshed baseline on a tiny 54×68 @1.5 m ROI around the 3 observers, + the original-task DEM subsets + the real `Site_Plan.pdf` = aperture source for step 2).
- New `scripts/compare_baseline.py`: re-runs the 3D engine on Task_2's own `DEM_Subset-WithBuildings.tif` (same grid/surface/datum as the baseline → isolates *algorithm + height*), binarizes the r.viewshed output (finite cell = visible), computes per-observer agreement, writes side-by-side + agreement figures, a `building_effect.png`, `comparison_metrics.csv`, and `comparison_report.md`. Outputs in `Task_2/comparison/`.
- **Result = engine validated**: 3D vs r.viewshed agree **97.2–98.7%** (IoU 0.86–0.92). This is the *expected* outcome — both treat buildings as solid right now, so close agreement confirms the engine reproduces the established tool. The headline 3D-beats-2D divergence is step 2 (apertures), not now.
- Clue: agreement/visible-counts match the baseline best at **1.75 m** eye height (baseline param was unrecorded) → baseline likely used GRASS's 1.75 m default. Mentor question.
- Two substantive findings for the report: (1) at 1.5 m the buildings block ~0 *new ground* and only add rooftops — the coarse baseline barely registers building occlusion; (2) resolution matters as much as algorithm — the 0.4 m production run resolves buildings as real blocks (0.5–1.8% visible) where 1.5 m cannot.
- Refactor: comparison reuses `viewshed.py` (`HeightfieldScene`, `compute_viewshed`, etc.); dependency-free markdown tables (no `tabulate`); tiny-raster writer (untiled).

## 2026-06-13 — True-3D ray-casting viewshed engine (build-order step 1)

- New `scripts/viewshed.py`: device-agnostic (cuda→mps→cpu) true-3D LOS engine behind a `Scene` interface. `HeightfieldScene` ray-marches each eye→target bearing against the DEM-with-buildings, running-max elevation-angle reduction (step-by-step `torch.maximum`, NOT cummax — unsupported on MPS), manual bilinear gather (grid_sample border-padding also unsupported on MPS), eye-relative coords for float32 precision. `Scene` seam lets an aperture-capable geometry scene drop in at step 2 unchanged.
- Outputs (in `220_BuildingsToDEM/`): per-observer `viewshed_obs{1,2,3}.tif` (uint8, windowed, georeferenced) + `_combined_max`/`_count`, QC PNGs, and the visibility graph (`viewgraph_edges.geojson`/`.csv`, `viewgraph_obs_obs.csv`).
- Result (3 Marks_Brief2 observers, eye 1.5 m, core+60 m window = 1.26M cells): visible fractions 0.5–1.8%; **all 3 observers mutually invisible**; ~8–9 building centroids visible each (~60 by any-vertex). Low numbers are physically correct — a ground-level observer in the dense necropolis is heavily occluded by adjacent tombs (the thesis in miniature). Visual audit (QC PNGs) confirms clean radial splat + shadows behind footprints, no rays through solid blocks.
- All 5 baked-in self-checks PASS (self-neighborhood, obs-obs symmetry, height-monotonicity, forward/reverse LOS). `sanity_checks.py` still all-PASS (viewshed_*.tif don't collide with the DEM glob).
- Refactor: extracted `core_window()` into `build_dem_with_buildings.py`, shared with the engine.
- Perf note: ~9 min for 3 observers on MPS (per-step kernel dispatch dominates, 80% CPU). Fine for the demo; full-site needs CUDA or step/chunk tuning.
- Limitation (by design): solid flat-top buildings, no apertures yet — that's step 2, and the `Scene` seam is where it plugs in.

## 2026-06-12 — Regenerated building-extruded DEM at 0.4 m

- New `scripts/build_dem_with_buildings.py`: rasterizes `Buildings_Mask` `Elevation` heights onto the Current_DEM grid and adds them (recipe from `220_BuildingsToDEM/README.md`), with input validation, per-feature coverage audit, self-verification on the written file, and QC images.
- Output: `220_BuildingsToDEM/DEMWithBuildings-0.4m-20260612.tif` — **canonical ray-casting surface**. Self-checks: off-footprint diff exactly 0; on-footprint = base + height (float32 eps); all 263 footprints burned.
- Visual audit of hillshade + diff PNGs passed (discrete blocks, coherent shadows, heights 0.7–8 m).
- `sanity_checks.py` extended: auto-detects newest `DEMWithBuildings-0.4m-*.tif`, hard-PASS grid identity vs base04.
- Data note: footprint `Type` is numeric codes 0–10, meaning unknown → mentor question #3.

## 2026-06-10/11 — Local environment + data verification

- Device-agnostic `requirements.txt` at project root (CUDA pins stripped or behind `sys_platform == "win32"` markers); raw remote freeze preserved in datastore. Convention codified in the project guide: cuda → mps → cpu, CuPy-or-NumPy `xp` pattern.
- `.venv` built with **uv**, Python 3.13 (matches remote); all 70 packages installed; MPS verified.
- `Marks_Brief2.*` (3-point proposal sample) filed into `100_Data/160_ViewpointMarks/` (local-only folder; full set TBD with mentors). Points are MultiPoint/EPSG:4326 — explode + reproject on load.
- New `scripts/sanity_checks.py`: all assets EPSG:32636; 263 footprints with `Elevation` 0.7–8 m; 263 gpkgs valid; viewpoints inside DEM.
- **Key finding**: legacy `BuildingsToDEM.tif` unusable for heights — 1.5 m res and ~78 m vertical-reference offset vs Current_DEM. Decision: regenerate locally (done 06-12).

## 2026-06-10/11 — Data transfer from UA remote machine

- Identified viewshed-relevant subset (~2.1 GB of the ~200 GB datastore) from `tree.txt`; SAR imagery (`140_SAR_Imagery/`) stays remote.
- PowerShell staging + zip on the remote; transferred via OneDrive after RealVNC file transfer stalled; SHA256-verified.
- Windows `Compress-Archive` backslash paths extracted flat on macOS — restructured 305 files into `LAMP_DataStore/ElBagawat/` mirroring the remote layout.
- Discovered the project guide's asset names were conceptual; real paths recorded in the project guide (gpkg IDs are 1–263, not 1–99; no literal `Site_Plan.pdf` — plan is `bagawat print.pdf` + `BaseSiteCAD/`).