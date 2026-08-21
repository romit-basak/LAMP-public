# CLAUDE.md

> Context for AI assistants working in this repository. Read this first.

## Project

**Embodied Reconstructions of El Bagawat** — true 3D ray-casting viewshed analysis for the Necropolis of El Bagawat (Kharga Oasis, Egypt), a late-antique site. Part of the Late Antiquity Modeling Project (LAMP), GSoC 2026, HumanAI organization.

- Contributor: Romit Basak (basak.r@northeastern.edu, github.com/romit-basak)
- Mentors: Camille Leon Angelo (U. Alabama), Joshua Silver (KIT)

## Progress log — keep it current

`PROGRESS.md` (project root) is the running record of major work: every significant action (new script, generated artifact, data transfer, key finding/decision, mentor question raised) gets a brief entry there, newest first, before the session ends. Read it at the start of a session to know where things stand; update it whenever you do something that the next session — or a mentor — would need to know about. Don't log minor edits or dead ends.

## Scope — read carefully

This repo is the **viewshed / visibility** half of the LAMP proposal only. The assigned deliverable is **true 3D ray-cast visibility analysis with building apertures**. A second contributor owns **path / movement analysis** (multispectral cost surfaces, entrance-weighted costs, Monte Carlo path ensembles).

The original proposal describes both halves; ignore the path-analysis sections when reasoning about *this* codebase. Do not add cost-surface or path-tracing features here unless explicitly asked. The deliverable is a corrected, aperture-aware **visibility graph** for the site.

## Why this exists (the thesis)

Standard visibility tools (GRASS `r.viewshed`, 2D space-syntax VGA) are planimetric and **treat every building as a solid, opaque block**. They miss what makes the site legible: window apertures and doorway orientations let sight and light pass *through* and *between* structures.

The corrective is a **physics simulation**: cast rays through a real 3D scene in which buildings have height and explicitly-modeled openings. The result is a visibility graph that existing tools cannot produce. Keep this as the north star — features that don't improve the fidelity or auditability of that graph are out of scope.

## Terminology — "aperture" is ambiguous, say which you mean

**This caused a real misunderstanding with mentors, so it is worth being pedantic about.** The excavation report — the primary source — uses *aperture* narrowly, to mean a **light opening**, i.e. what this project calls a window:

> "The chamber was lit by means of **apertures** in the three walls"
> "the two **apertures for light** under which there is a triangular niche"

This project adopted *aperture* as an umbrella for every modelled opening, which is the broader and **non-standard** usage. Anyone reading the report, or an archaeologist using its vocabulary, will hear "window". Use the precise terms below in writing and in conversation with mentors; keep bare "aperture" for the legacy path and script names only.

| Term | Means | Registry `kind` | `perforates` |
|---|---|---|---|
| **opening** | umbrella for all four modelled kinds; one registry row | any | — |
| **perforating opening** | passes clean through the wall — sight and light cross it | `door`, `window` | `True` |
| **recess** | cut into a wall face, does **not** pass through | `niche`, `apse` | `False` |
| **aperture** *(report sense)* | a light opening — this project's **window** | `window` | `True` |

The distinction is not cosmetic: it is the difference the results turn on. Doors are the only opening that puts a building's interior in view (+3 centroid-visible pairs); windows add ~9x more visible *ground* and no interiors, because a sill above standing eye height sends the sightline high onto the far wall; recesses change **nothing** on any visibility metric, exactly, because they never perforate.

Legacy names keep the umbrella sense and are **not** being renamed — paths appear in the frozen regression baseline, the datastore layout and every generated report: `250_Apertures/`, `aperture_inventory.csv`, `aperture_registry.py`, `build_aperture_walls.py`, `--openings`. The builder's own flag values already carry the precise vocabulary: `--openings perforating` means doors + windows, `all` adds the recesses.

## Validation philosophy

Ground truth is ~1,700 years old; there is **no measured visibility data to fit against**. So:

- Outputs are **visually audited** against the site plan (`bagawat print.pdf` / `scan557`, and QGIS overlays). Correctness means "a human can confirm the rays respect the geometry," not "it matches a label set."
- Prefer **deterministic, inspectable** ray-casting over learned models for the core viewshed. A GNN-over-VGA approach is a **stretch goal / follow-on**, not a substitute for this phase.
- Every result should be reproducible from the DEMs + footprints + aperture definitions.

## Data assets

3D geometry is derived from the **height differential between two DEMs** (DEM-with-buildings − base DEM), **not** from SAR. A local subset of the dataset lives in `LAMP_DataStore/ElBagawat/` (mirrors the remote layout); paths below are relative to that root.

| File | Role |
|---|---|
| `200_Projects/220_BuildingsToDEM/DEMWithBuildings-0.4m-*.tif` | **Canonical ray-casting surface.** Regenerated locally by `scripts/build_dem_with_buildings.py` (Current_DEM + `Buildings_Mask` `Elevation` field, exact grid match to the 0.4 m base; QC hillshade/diff PNGs alongside). Newest date wins. |
| `200_Projects/220_BuildingsToDEM/BuildingsToDEM.tif` | Legacy DEM with buildings (see that folder's README for the recipe). **Do not use**: 1.5 m res and its vertical reference sits ~78 m below Current_DEM (measured by `scripts/sanity_checks.py`). Superseded by the regenerated raster above. |
| `100_Data/150_DigitalElevationModel/Generated_DEMs/Current_DEM/Bagawat-DEM-NewImageryOnly-0.4m-DEM.tif` | Current base DEM, 0.4 m (a 0.5 m variant + orthophotos sit alongside, for QGIS audit) |
| `100_Data/130_BuildingFootprintsVectorData/BuildingTracesCurrent/Buildings_Mask.shp` | Planimetric building polygons (carries the height field used for extrusion) |
| `200_Projects/220_BuildingsToDEM/Temp_Buildings_Explode/IndividualBuildings/ID_*.gpkg` | Per-building geometry, IDs 1–263 |
| `100_Data/160_ViewpointMarks/Marks_Brief2.shp` | Observer / viewpoint locations. **Local-only folder for now**, holding the 3-point sample from the proposal task; full site set TBD with mentors. Replicate to the remote datastore once finalized. |
| `200_Projects/220_BuildingsToDEM/viewshed_*.tif`, `viewgraph_*.{geojson,csv}` | Viewshed-engine outputs (`scripts/viewshed.py`): per-observer + combined/count visibility rasters and the visibility graph. Regenerate from the DEM-with-buildings + observers; QC PNGs alongside. Volume runs add `viewshed_volume_*` (csv/ply/npy/las/laz voxel outputs and, via `--volume-format mesh`, the volume's boundary-surface PLY mesh — `scripts/volume_mesh.py`, blocky=exact/smooth=marching-cubes). |
| `200_Projects/220_BuildingsToDEM/observer_views/` | First-person observer snapshots (`scripts/observer_view.py`): pano/perspective PNGs in natural/depth/ids shadings, rendered by the same kernel as the viewsheds (`HeightfieldScene.first_hit`) — the audit-grade "what the observer sees". Other-observer markers mirror the viewgraph edges. |
| `200_Projects/220_BuildingsToDEM/scene_bundle/`, `blender_renders/` | External-renderer export (`scripts/export_scene_bundle.py`): local-origin heightmap/ortho/observers/domes (+ optional Unity 16-bit RAW) consumed by `blender/build_bagawat_scene.py` (Cycles stills) and Unity Terrain. Presentation tier, not evidence — see `docs/BLENDER.md` / `docs/UNITY.md`. |
| `Task_2/Site_Plan.pdf` (+ `100_Data/120_SiteReport/bagawat print.pdf`, `BaseSiteCAD/`) | Architectural plan. `Task_2/Site_Plan.pdf` is a clean 1:5000 line drawing that *looks* like the obvious door source and **is not one** — see the `250_Apertures/` row; its tiles are for sanity-checking a wall attribution, not for deriving positions. `BaseSiteCAD/` DXF **is** a real source, for door width/position on the 7 chapels it covers (LW2 threshold marks). |
| `Task_2/` (r.viewshed baseline) + `Task_2/comparison/` | User's GRASS r.viewshed baseline (54×68 @1.5 m ROI) and the engine-vs-baseline comparison (`scripts/compare_baseline.py`): metrics, figures, report. Engine validated at 97–99% agreement. |
| `100_Data/120_SiteReport/Bagawat Data From Excavation Report.xlsx` | Excavation reference data. `Sheet1.Type` = chapel typology 1–10 (legend: `SiteReport_missing9-12.pdf` PDF pp. 20–23, report Ch. III). **Domed types: {4,5,6,7,9}**; 1/2/3 flat, 8 composite, 10 barrel-vaulted. Join `Chapel #` = footprint `ID`. |
| `200_Projects/220_BuildingsToDEM/dome_inventory.csv`, `domes.gpkg`, `dome_qc.png` | Dome layer (`scripts/build_dome_layer.py`): per-chapel dome centers/radii from typology + orthophoto bright-blob measurement; gpkg has one PointZ layer per 0.5 m radius class (QGIS 3D spheres). CSV is the editable registry — rerun with `--from-inventory` to honor hand edits. `viewshed.py --domes` can also bake these caps into the ray-casting surface in-memory (opt-in, off by default; the validated buildings-only baseline is unaffected) — treat `--domes` results as experimental pending mentor visual review. |
| `200_Projects/250_Apertures/` | Aperture pipeline outputs (step 2, real doors). **Primary source = the excavation report's Chapter VII**, which states each chapel's entrance direction in words ("it opens west"); `read_report_directions.py` OCRs it (tesseract, cached to `report_plates/ocr/`) → `entrance_directions.csv` → `apertures_from_report.py` → registry. 194/263 chapels covered, validated 6/6 by eye; **no chapel opens north** (verified, not an artifact). Registry now **469 openings over 202 chapels** (197 door, 93 window, 172 niche, 7 apse). The **site plan is NOT a usable door-position source** — its apparent wall gaps are plan-vs-footprint registration artifacts at corners, and chapel 180's real entrance has unbroken linework; its tiles remain useful for sanity-checking. Also: `aperture_inventory.csv` — the **hand-editable registry** (one row per opening, canonical-wall anchored, provenance-split `source_pos`/`source_dims`; extraction scripts NEVER overwrite it — they write `siteplan_candidates.csv` instead), `report_plates/` (200 excavation-report page scans + contact sheets — the only *possible* height source, and **not yet read**: no row in the registry has plate-derived dimensions, so every sill/head is a class default), `siteplan_tiles/` + `siteplan_georef.json` (per-chapel confirm/correct tiles; plan georeferenced at ~1 m), `dxf_plans/` (7 CAD plots, LW2 threshold marks = doors), `meshes/building_*.obj` + `mesh_args.txt` (`build_aperture_walls.py` → `viewshed.py --mesh` input). |
| `100_Data/140_SAR_Imagery/` | Multispectral/stereo imagery; surface **classification/texture only** — NOT a height source. Stays on the remote machine (bulk of the ~200 GB dataset; mainly the other contributor's input). |

CRS is **projected and metric** (large eastings/northings, ~UTM for Egypt). Always read the CRS from the raster with rasterio — never assume one. Distances and observer heights are in **meters**.

## Environment

Stack: Python — PyTorch, **Rasterio**, **GeoPandas**, **Shapely**, SciPy, CuPy. GIS: GRASS, QGIS, depthmapX (baseline only).

The full ~200 GB dataset lives on a **remote Windows machine** (access via RealVNC; CuPy/CUDA available there). Work locally where possible (Apple Silicon Mac — MPS, no CUDA); reserve the remote box for compute-heavy ray-casting and for `140_SAR_Imagery/`.

**Setup** — do *not* copy the remote `.venv`. The local env is managed with **uv** (Python 3.13, matching the remote):
```bash
uv venv --python 3.13 .venv
uv pip install --python .venv -r requirements.txt
```
The project-root `requirements.txt` is the device-agnostic one (platform markers for CUDA-only packages); the raw remote freeze is preserved at `LAMP_DataStore/ElBagawat/requirements.txt` — don't install from that one. The needed data subset is already pulled into `LAMP_DataStore/ElBagawat/`; leave `140_SAR_Imagery/` remote.

### Device selection (required convention)

All compute code must run unchanged on the remote box (CUDA), this Mac (MPS), and CPU-only machines. Never hard-code a device, call `.cuda()`, or pin `+cuXXX` wheel variants in requirements. Use:

```python
# PyTorch
device = torch.device("cuda" if torch.cuda.is_available()
                      else "mps" if torch.backends.mps.is_available()
                      else "cpu")

# Array ops (CuPy on the remote GPU, NumPy everywhere else)
try:
    import cupy as xp
except ImportError:
    import numpy as xp
```

Caveat: if a needed op is unimplemented on MPS, fall back per-op (e.g. `PYTORCH_ENABLE_MPS_FALLBACK=1` or move that tensor to CPU) rather than abandoning device-agnosticism. With the CuPy/NumPy pattern, keep data in `xp` arrays end-to-end and convert at the boundaries (e.g. `xp.asarray(...)` in, `cp.asnumpy(...)`/`np.asarray(...)` out).

## Conventions & gotchas

- **Observer height = 1.5 m** is the default, not the 1.75 m GIS default — reflects skeletal data for late-antique Egyptian populations. Now adjustable via `--eye-height`; keep 1.5 m as the default (don't silently switch to 1.75).
- Building **height comes from the DEM differential**, not from SAR or assumed constants.
- Shapefiles travel with their sidecars; moving a `.shp` alone silently breaks it.
- Use **clean filenames** (no spaces/odd characters) for any institutional uploads/portals — they get silently stripped otherwise.
- **Say "opening", "perforating opening" or "recess", not bare "aperture"** — the report means *window* by that word and mentors read it that way (see Terminology above). Openings come from the **excavation report** (`120_SiteReport/`), not the site plan. The "translucent partition" / partial-transmission case is a refinement, not the first cut.
- **Memoising a sightline across scene variants is unsound here.** A pair's visibility depends on *all* the geometry, not just the two chapels named in a cache key: chapels are modelled as wall panels rather than watertight solids, so a ray can enter one opening and leave over a wall top. Measured at 6.1e-4 of repeated keys — small, but it drifts the statistic being tested. `test_intentionality.py --pair-cache` is kept only to reproduce the finding and defaults **off**.
- **When a change alters what an experiment's draw *means*, bump `DRAW_VERSION`** (`test_intentionality.py`) so a resumed run is refused instead of splicing two experiments into one null distribution. Config lives in the checkpoint fingerprint; semantics do not, unless put there.
- **Code hygiene**: keep scripts free of unused variables/imports and inconsistent styling (kebab-case flags, ~79-col wrapping, the `check`/`warn`/`failures` self-check pattern). Do **not** name the AI assistant or this guide in code comments, docstrings, or help text — describe the rationale directly, or say "the project guide" if a pointer is unavoidable.
- **Git is user-managed** — never run git commands (stage/commit/push/checkout/stash, anything). Surface what changed and let the user handle version control.

## depthmapX & the 2D baseline

depthmapX is used **only** as a one-shot 2D VGA baseline to compare against the 3D result — **time-box to ~1–2 days**. Heavier depthmapX usage belongs to the path-analysis contributor. If it's fiddly, the fallback is computing **2D isovists directly in Python with Shapely** — sufficient for the baseline.

The **baseline-vs-3D comparison report** is the most judgment-weighted deliverable; invest there. A first iteration exists: `scripts/compare_baseline.py` compares the engine against the user's GRASS **r.viewshed** baseline (in `Task_2/`) on a shared grid → `Task_2/comparison/comparison_report.md`. The r.viewshed baseline supersedes the need for depthmapX as the *primary* baseline; depthmapX/2D-VGA remains a possible secondary baseline. That comparison validates the engine on solid buildings (97–99% agreement). Apertures have since landed, and the divergence they produce shows on the **visibility graph**, not on the ROI raster sweep — see build-order step 3 for why the raster metric structurally cannot register a door.

Open question: Silver mentioned depthmapX doing "2.5D" — not confirmed as a native feature. **Ask Silver directly** before relying on it.

## Build order (viewshed half)

1. **3D scene + ray-casting engine** — DEM-WithBuildings as the surface; cast rays from `Marks_Brief2` viewpoints. *(early phase)* Now also exposes the vertical view cone (`--pitch`/`--vfov`) alongside the horizontal one, an optional 3D visibility **volume** (`--volume`, csv/ply/npy/las/laz/mesh + `scripts/volume_convert.py`; `mesh` = boundary-surface PLY via `scripts/volume_mesh.py`), adjustable `--eye-height`, an experimental **`--domes`** flag to bake dome caps into the ray-casting surface (opt-in; off by default), and **first-person observer snapshots** (`scripts/observer_view.py` via `HeightfieldScene.first_hit`, cross-validated ≥98% against `visible_mask`). External renderers hang off `scripts/export_scene_bundle.py` → Blender/Unity — presentation tier only, never evidence (`docs/BLENDER.md`, `docs/UNITY.md`). Also exposes a **many-observer batched cast** (`visible_mask_multi` on both scenes, `segments_blocked_multi` in `scene3d.py`) for experiments that fire thousands of sightlines from hundreds of stations — the march is launch-bound, so batching is ~179x on terrain and ~35x on mesh, and it was gated bit-identical against the per-eye path before use. `scripts/visible_fraction.py` turns a boolean sightline into *what fraction* of a wall or feature is visible, by adaptive subdivision (not bisection — visibility along a wall is not monotone).
2. **Add window/door apertures** — primary source the **excavation report**, not the site plan (which was tried and proven unusable). *Flagged as the likely slip point — budget extra time.* **Landed**: the hybrid mesh scene (`scripts/scene3d.py`, `--mesh` on both drivers) is validated (synthetic cube+dome+door, analytic self-checks, no-mesh runs byte-identical), and the pipeline (`read_report_directions` / `extract_report_plates` / `extract_dxf_plans` → `aperture_inventory.csv` → `build_aperture_walls.py`) now carries **469 openings over 202 chapels**. Heights and confirmations are human-in-the-loop by design (see `250_Apertures/` data row). **What is evidenced is the *wall*, not the position along it** — only 3 of 469 openings have a sourced along-wall position and only 3 have measured dimensions; the rest are spacing rules at class defaults. `docs/DATA_PROVENANCE.md` (generated by `scripts/data_provenance.py`) is the row-by-row audit and the list of gaps — read it before quoting any aperture-derived number.
3. **Comparison report**: `r.viewshed` / 2D-VGA baseline vs. 3D aperture-aware result. *(highest-value deliverable; gates mid-term eval)* **Landed, in three layers**: engine-vs-`r.viewshed` on solid buildings validates at 97–99% (`Task_2/comparison/`); the ROI raster sweep (`scripts/compare_apertures.py` → `Task_2/comparison_all_fabric/`) reports a **structurally zero** door effect and says so — `target_grid` pins every target at roof height, so a ground-level door cannot flip it, and no number of observers fixes that; the **site-wide graded viewgraph** is the metric that carries the result, because it tests centroid visibility at true interior floor height. Graded run over 202 meshed chapels, matched building set at every step: solid 36,520 ground cells / 8 centroid-visible pairs → doors **+137 cells, 8→11 pairs** → windows **+1,201 cells, +0 pairs** → niches/apses **+0 on everything**. Doors are the only opening that puts an interior in view; a window sill sits above standing eye height.
4. **Statistical tests of arrangement** — beyond the original build order, and where the site-level findings live. `test_entrance_azimuth.py` rejects solar, uniform and downhill-slope nulls for the compass distribution; `test_intentionality.py` is a **pre-registered** Monte Carlo (α = 0.01, Holm, 999 draws) on whether entrances are arranged so interiors are inter-visible — V = 377 against null medians 270/252/264, all three p = 0.001. **The nulls establish non-randomness, not intent** — permuting directions across chapels destroys every relationship a door has to its local surroundings at once, so any systematic relation to local geometry would beat the null just as mutual arrangement would, and the test cannot say which. Do not quote this as evidence of intent. `test_feature_visibility.py` / `test_painting_visibility.py` cover niches, apses and the painted programmes. Pre-registration lives in `test_intentionality.py`'s module docstring; **every deviation is recorded there as a dated entry rather than quietly applied**.
5. Stretch: observer-height sensitivity (1.5 vs 1.75 m, now a flag), GNN-over-VGA.

### Planned refinements (not yet implemented)

- **Distance-based visual obscurity**: human visual acuity falls off with range, so an unbounded straight ray overstates what is meaningfully *seen* far away. A ray traveling kilometres and counting a distant cell as "visible" is geometrically true but perceptually wrong. Future work should model distance attenuation — e.g. an acuity/contrast threshold (target subtends too small an angle), atmospheric extinction, or a weighting that decays with distance — rather than a hard binary visible/not. Raised by mentors; deliberately deferred. When added, the baseline comparison should report both raw and acuity-weighted visibility.

If scene complexity grows, add a **BVH** acceleration structure (see *Ray Tracing: The Next Week*).

## References

- [`docs/CODE_WALKTHROUGH.md`](docs/CODE_WALKTHROUGH.md) — narrated tour of the scripts: what each does, the design rationale, and what breaks if done otherwise (internal doc; read alongside this guide)
- [`docs/DATA_PROVENANCE.md`](docs/DATA_PROVENANCE.md) — generated: which document supplied each modelled fact, and the nine gaps where nothing did. Read before quoting any aperture-derived number
- [`GSOC_WORK_PRODUCT.md`](GSOC_WORK_PRODUCT.md) — the checkpoint write-up: what was built, what it found, how it is validated, what is known-limited
- [`docs/REMOTE_SETUP.md`](docs/REMOTE_SETUP.md) — clone-to-running-pipeline walkthrough for the remote CUDA/Windows workstation
- Turner et al. (2001) — visibility graph analysis (start here)
- Benedikt (1979) — isovist fields
- *Ray Tracing in a Weekend* + *Ray Tracing: The Next Week* — ray-casting engine + BVH
- Rasterio docs — georeferenced raster handling
- UCL Space Syntax Lab lectures