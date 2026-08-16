# El Bagawat Viewshed Engine — Usage

Practical "how to run this" reference for the viewshed/visibility half of LAMP
(GSoC 2026, HumanAI). For project scope, thesis, and data-asset details see
[CLAUDE.md](CLAUDE.md); for *why* the engine is built the way it is, see
[docs/CODE_WALKTHROUGH.md](docs/CODE_WALKTHROUGH.md). This document is the
flag reference neither of those cover.

## Setup

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv -r requirements.txt
```

Local environment: Apple Silicon Mac (MPS). Heavy ray-casting and the SAR
imagery subset live on a remote CUDA machine. All compute code is
device-agnostic (CUDA → MPS → CPU). The local data subset lives in `LAMP_DataStore/ElBagawat/`.
For a from-scratch clone-to-running-pipeline walkthrough on the remote
machine, see [docs/REMOTE_SETUP.md](docs/REMOTE_SETUP.md).

## Pipeline at a glance

```
sanity_checks.py            validate every raster/vector is coherent (run first)
        │
build_dem_with_buildings.py rasterize footprint heights onto the base DEM
        │                   → DEMWithBuildings-0.4m-<date>.tif (ray-cast surface)
        │
build_dome_layer.py         (optional) typology + orthophoto → dome_inventory.csv
        │                   + domes.gpkg (QGIS 3D visualization)
        │
make_test_building.py       (optional) synthetic cube+dome+door assets for the
        │                   aperture experiment (scene3d.py runs its self-checks)
        │
extract_report_plates.py    → report scans → read_report_directions.py (OCR)
        │                     → entrance_directions.csv → apertures_from_report.py
        │                     → aperture_inventory.csv (the registry)
extract_site_plan.py        (secondary) plan georeferencing + QC tiles
extract_dxf_plans.py        (secondary) CAD plots for measured door widths
        ▼
build_aperture_walls.py     registry → per-building OBJ walls with real
        │                   openings + roof caps + dome caps (--mesh input)
        ▼
    viewshed.py              cast rays, write viewsheds / visibility graph / 3D volume
        │                    (--domes bakes dome_inventory.csv into the ray-cast
        │                     surface in-memory, opt-in, experimental;
        │                     --mesh adds OBJ buildings with real openings —
        │                     the aperture-capable hybrid scene, scene3d.py)
        ├── observer_view.py     first-person snapshots of what each observer sees
        │                        (same ray-march kernel; pano + perspective PNGs;
        │                         also --mesh-aware)
        ├── volume_convert.py    (optional) volume CSV → PLY / NPY / GeoTIFF / LAS / LAZ
        └── compare_baseline.py  validate against the GRASS r.viewshed baseline

export_scene_bundle.py      (optional) DEM window + ortho + observers + domes →
        │                   scene_bundle/ in a local float32-safe frame
        └── blender/build_bagawat_scene.py   Cycles renders (docs/BLENDER.md);
                                             Unity Terrain import (docs/UNITY.md)
```

Run `sanity_checks.py` before anything else, and after pulling new data.

## GUI runner (optional)

For repeated runs where hand-typing a long multi-flag command gets
error-prone, `scripts/run_gui.py` renders every script's own argparse
flags as a browser form (defaults pre-filled, dropdowns for `choices`,
checkboxes for on/off flags) and runs the resulting command as a
subprocess, streaming output back to the page. It reads each script's
flags straight from its `build_parser()` — nothing is hand-copied, so
the form can't drift out of sync with `--help`. The CLI below remains
the reference interface; this is a convenience wrapper around the
exact same argv.

```bash
.venv/bin/python scripts/run_gui.py
```

Opens `http://127.0.0.1:8765/` (`--port` to change, `--no-browser` to
skip auto-open). Covers every script below plus
`blender/build_bagawat_scene.py` (has its own "Blender executable"
field, since that one runs under Blender's bundled Python, not this
venv's).

## Script reference

### `scripts/sanity_checks.py`

Validates CRS consistency, DEM grid alignment, building-height plausibility,
and that observers fall inside the DEM. No flags.

```bash
.venv/bin/python scripts/sanity_checks.py
```

### `scripts/build_dem_with_buildings.py`

Regenerates the canonical ray-casting surface: footprint heights rasterized
onto the base DEM.

| Flag | Default | Meaning |
|---|---|---|
| `--base-dem` | `DEM_BASE_04` (0.4 m current DEM) | Bare-earth DEM to extrude onto |
| `--footprints` | `Buildings_Mask.shp` | Footprint polygons with the height field |
| `--height-field` | `Elevation` | Footprint field holding building height (m) |
| `--out` | `DEMWithBuildings-0.4m-<today>.tif` | Output raster path |
| `--all-touched` | off | Burn every pixel touched by a footprint, not just center-covered ones (dilates thin walls by up to 1 px) |

```bash
.venv/bin/python scripts/build_dem_with_buildings.py
```

### `scripts/build_dome_layer.py`

Builds the dome **visualization** layer: joins the excavation-report chapel
typology onto the footprints (domed types 4/5/6/7/9; legend in
`SiteReport_missing9-12.pdf` pp. 20–23), measures each dome's center/radius
from the orthophoto, and writes an editable inventory + QGIS-ready point
layers.

| Flag | Default | Meaning |
|---|---|---|
| `--footprints` | `Buildings_Mask.shp` | Footprint polygons |
| `--xlsx` | excavation report xlsx | Source of the `Type` column |
| `--ortho` | 0.4 m orthophoto | Grayscale imagery used to measure domes |
| `--dem` | `DEM_BASE_04` | Bare-earth DEM (roof-height sampling) |
| `--out-dir` | `200_Projects/220_BuildingsToDEM/` | Where the CSV/gpkg/QC PNG land |
| `--from-inventory` | none | Skip detection; rebuild gpkg + QC PNG from a hand-edited `dome_inventory.csv` |

```bash
.venv/bin/python scripts/build_dome_layer.py
# after hand-editing dome_inventory.csv:
.venv/bin/python scripts/build_dome_layer.py --from-inventory <out-dir>/dome_inventory.csv
```

Outputs: `dome_inventory.csv` (editable registry), `domes.gpkg` (one PointZ
layer per 0.5 m radius class — QGIS 3D sphere symbols can't be data-defined,
hence one layer per size; sphere centers sit 0.35 × radius below the roofline
so they read as domes rather than full balls, including on slope-sheared
roofs), `dome_qc.png` (visual audit against the orthophoto). `roof_z` is the
**rendered** roofline height — a least-squares plane through the footprint's
vertex ground + building height, matching QGIS's vertex-bound extrusion — not
ground-at-center + height, which rides high on local terrain bumps.

### `scripts/viewshed.py` — the engine

Casts 3D line-of-sight rays from observers against the DEM-with-buildings
heightfield. Flags are grouped by purpose below.

**Input / output**

| Flag | Default | Meaning |
|---|---|---|
| `--dem` | `DEM_REGEN` (newest `DEMWithBuildings-0.4m-*.tif`) | Ray-casting surface |
| `--footprints` | `Buildings_Mask.shp` | Building polygons (graph edges, QC overlays) |
| `--observers` | `Marks_Brief2.shp` | Observer point file |
| `--out-dir` | `200_Projects/220_BuildingsToDEM/` | Output directory |
| `--margin` | `60.0` | Core-window margin (m); ignored when `--radius` is set |
| `--chunk` | `200000` | Ray-march chunk size (memory guard) |

**Observer selection**

| Flag | Default | Meaning |
|---|---|---|
| `--point X Y` | — | Inline observer in the DEM CRS; repeatable; overrides `--observers` |
| `--ids N [N ...]` | all | Select observers from `--observers` by their `id` field |
| `--eye-height` | `1.5` | Observer eye height (m) above the surface |

**View cone** (applies to rasters and the volume, not the graph — graph LOS is
always omnidirectional)

| Flag | Default | Meaning |
|---|---|---|
| `--radius` | none (full core window) | Per-observer sight radius (m); window follows the point |
| `--azimuth` | none | Horizontal view-cone center, compass degrees (0=N, 90=E, clockwise) |
| `--fov` | `360` | Horizontal view-cone full width (degrees) |
| `--pitch` | `0` | Vertical view-cone center, elevation degrees (0=horizontal, + up) |
| `--vfov` | `180` | Vertical view-cone full width (degrees; 180 = unconstrained) |
| `--no-graph` | off | Skip the visibility graph (useful for single/point runs) |

**3D visibility volume** (off by default)

| Flag | Default | Meaning |
|---|---|---|
| `--volume` | off | Also compute a 3D visibility volume |
| `--voxel` | `2.0` | Volume horizontal spacing (m) |
| `--zmin` | `0.0` | Lowest sample height above ground (m) |
| `--zmax` | `30.0` | Highest sample height above ground (m) |
| `--zstep` | `2.0` | Volume vertical spacing (m) |
| `--volume-fullres` | off | Sample at native DEM resolution (voxel = zstep = pixel size); can be very large |
| `--volume-format` | `csv` | Any of `csv ply npy las laz mesh` or `all` (= csv,ply,npy). `las`/`laz` need `laspy` (+`lazrs` for `.laz`); `mesh` writes the volume's **boundary surface** as a PLY triangle mesh (faces + edges, terrain-following) instead of voxel points, plus a 3D QC PNG |
| `--mesh-style` | `blocky` | With `mesh`: `blocky` = exact voxel boundary (auditable — the mesh encloses identically n_voxels × voxel volume, self-checked); `smooth` = marching-cubes isosurface (presentation). Combined volume is meshed as the union (any-observer) shape; per-voxel counts stay in csv/las |

**Domes** (experimental — see [CLAUDE.md](CLAUDE.md) Conventions)

| Flag | Default | Meaning |
|---|---|---|
| `--domes` | off | Bake dome caps into the ray-casting surface in-memory before analysis. Needs `dome_inventory.csv` from `build_dome_layer.py`. Off by default; the validated buildings-only comparison is unaffected |
| `--dome-inventory` | `dome_inventory.csv` | Override the inventory CSV location |

**Apertures / mesh buildings** (step 2 — `scripts/scene3d.py` hybrid scene)

| Flag | Default | Meaning |
|---|---|---|
| `--mesh` | off | OBJ mesh(es) added as explicit 3D occluders. Unlike the heightfield, a mesh wall can have a real opening (door/window) rays pass through. Runs without `--mesh` are byte-identical to before |
| `--mesh-clear-ids` | — | With `--mesh`: footprint IDs whose extruded blocks are flattened back to the bare-earth DEM, so a building the mesh now represents doesn't occlude twice |
| `--bare-dem` | Current_DEM 0.4 m | Bare-earth DEM sampled by `--mesh-clear-ids` |

**Examples**

```bash
# 360° from the 3 sample observers, core window
.venv/bin/python scripts/viewshed.py

# Directional cone from an arbitrary point, horizontal + vertical
.venv/bin/python scripts/viewshed.py --point 254210 2820958 \
    --radius 200 --azimuth 90 --fov 60 --pitch 10 --vfov 40 --no-graph

# 3D visibility volume, LAZ point cloud for QGIS/CloudCompare
.venv/bin/python scripts/viewshed.py --point 254210 2820958 \
    --radius 150 --volume --volume-format laz --no-graph

# Include dome roofs in the ray-cast surface
.venv/bin/python scripts/viewshed.py --point 254210 2820958 \
    --radius 150 --domes --no-graph

# Any point file, 360°, select specific observers by id
.venv/bin/python scripts/viewshed.py --observers my_points.shp --ids 80 180 181
```

**Aperture demo** (synthetic cube+dome+door — the step-2 experiment):

```bash
.venv/bin/python scripts/make_test_building.py       # writes the assets
.venv/bin/python scripts/scene3d.py                   # aperture self-checks
A=viewshed_runs/synthetic_building/assets
# observer inside the building: the visibility fan spills through the door
.venv/bin/python scripts/viewshed.py --dem $A/flat_dem.tif \
    --footprints $A/footprint.gpkg --observers $A/observers.gpkg --ids 2 \
    --mesh $A/building.obj --radius 40 --no-graph \
    --out-dir viewshed_runs/synthetic_building/inside_door
# control: same run against building_solid.obj -> only the interior remains
# first-person view of the doorway from outside (depth shading)
.venv/bin/python scripts/observer_view.py --dem $A/flat_dem.tif \
    --footprints $A/footprint.gpkg --observers $A/observers.gpkg --ids 1 \
    --mesh $A/building.obj --persp 0 --modes depth natural --no-ortho \
    --no-markers --max-range 60 \
    --out-dir viewshed_runs/synthetic_building/fp_outside
```

### `scripts/observer_view.py` — first-person snapshots

Renders what each observer actually sees: equirectangular panoramas and
pinhole perspective views, ray-marched by the **same kernel** the viewshed
products use (`HeightfieldScene.first_hit`), so the images are audit-grade —
every pixel that shows surface is a point the viewshed calls visible. Other
observers appear as markers (filled green = visible per the viewgraph LOS
test, hollow red = occluded).

| Flag | Default | Meaning |
|---|---|---|
| `--dem` / `--footprints` / `--observers` / `--ids` / `--point` / `--eye-height` / `--chunk` | as `viewshed.py` | Same semantics |
| `--out-dir` | `220_BuildingsToDEM/observer_views/` | Where the PNGs land |
| `--azimuth` | none (full 360°) | Pano center, compass degrees |
| `--fov` | `360` | Pano horizontal span (with `--azimuth`) |
| `--pitch` | `0` | View-center elevation angle |
| `--vfov` | `40` | Pano vertical span — the image's angular height (**not** viewshed.py's cone filter; 180 there = unconstrained) |
| `--pano-width` | `1440` | Pano width in px (0.25°/px at 360°) |
| `--no-pano` | off | Skip the panorama |
| `--persp AZ …` | none | Pinhole perspective view(s) at these azimuths |
| `--persp-fov` / `--persp-size` | `60` / `720 480` | Perspective fov / image size |
| `--modes` | `natural depth ids` | Shadings: sand-lit + ortho drape + fog / log-scaled slant range / footprint-ID colors |
| `--ortho` / `--no-ortho` | 0.4 m orthophoto | Drape texture for natural mode |
| `--fog` | `300` | Fog length (m) in natural mode; `0` = off |
| `--max-range` | none (DEM edge) | Stop rays at this distance |
| `--step-scale` | `1.0` | March-step multiplier; >1 fast but can leak through thin walls |
| `--no-markers` | off | Skip other-observer markers |
| `--domes` / `--dome-inventory` | off | Bake dome caps into the surface first |
| `--mesh` | off | OBJ mesh(es) as explicit 3D occluders — as `viewshed.py` |

```bash
# all observers, full 360° pano, all three shadings
.venv/bin/python scripts/observer_view.py
# one observer, eastward perspective only
.venv/bin/python scripts/observer_view.py --ids 1 --persp 90 --no-pano
```

Self-checks include a cross-validation: sampled first-hit points must be
visible to `visible_mask` (the r.viewshed-validated kernel) — runs at
99.6–100% in practice.

### `scripts/scene3d.py` — aperture-capable hybrid scene

The step-2 seam in action: `HybridScene` wraps the validated
`HeightfieldScene` and adds OBJ triangle meshes as explicit occluders
(batched Möller–Trumbore, same cuda→mps→cpu stack). A mesh wall can
carry a real opening, so rays pass *through* doors and windows — the
thing a heightfield structurally cannot represent. Composition:
`visible_mask` = heightfield AND clear-of-triangles; `first_hit` = min
of the two distances; `surface_z` stays heightfield (eye placement
unchanged). Not run directly in the pipeline — `viewshed.py --mesh` and
`observer_view.py --mesh` construct it — but running the module itself
executes the aperture self-checks against the synthetic assets
(analytic door/wall/dome sightlines, reciprocity, exact first-hit
distances, kernel-consistency fan).

### `scripts/make_test_building.py` — synthetic aperture test assets

Generates the mentor-specified minimal aperture case: a hollow cube
chapel (8 m, 0.4 m walls) with a hemispherical dome and a 1.2 × 2.2 m
door in the south wall, on a flat ground raster at site-like UTM
coordinates — self-contained, no datastore needed, every expected
sightline analytic. Writes `building.obj`, `building_solid.obj` (no
door — the control), `flat_dem.tif`, `footprint.gpkg`,
`observers.gpkg` (1 = outside the door, 2 = inside, 3 = blank-wall
control), `building_qc.png`, `params.json`. All dimensions are flags
(`--size`, `--door-width`, `--door-head`, `--dome-radius`, …). See the
aperture demo block above for the standard runs.

### The aperture pipeline — real doors from real sources

Four scripts populate and consume `aperture_inventory.csv` (in
`200_Projects/250_Apertures/`, datastore) — one row per opening,
anchored to a **canonical wall index** of its footprint
(`scripts/aperture_registry.py` holds the shared canonicalization and
schema; rows carry the wall's azimuth + midpoint as drift detectors).
Provenance is split per row: `source_pos` (where the door's location
came from) and `source_dims` (where its width/heights came from) —
`siteplan` / `dxf` / `plate` / `default` — so the comparison report can
always separate measured from assumed. Extraction scripts never
overwrite an existing registry (the dome-inventory hand-edit rule):
reruns write `siteplan_candidates.csv` for manual merging.

**`extract_report_plates.py`** dumps the 200 excavation-report page
scans (the only height source; 617 MB PDF, one JPEG per page,
memory-mapped hand extraction) plus browsable contact sheets and a
`plate_index.csv` template. *Manual workflow*: browse the contact
sheets → open the full-res `page_NNN.jpg` → read door sill/head/width
off the plate's dimension lines → edit the registry row
(`source_dims=plate`, note the page number). Chapter VII's per-chapel
descriptions start around p.88.

**`extract_site_cad.py`** is the *measured* aperture source, and takes
precedence where it has data. It converts the binary
`SITE CAD WORKING.dwg` with `dwg2dxf` (LibreDWG — a one-off dev tool,
`brew install libredwg`), which preserves the layers the PDF print
destroys. Georeferences on 274 `NUMBERING` labels (0.93 m median
residual) and reads door threshold marks off the `LW2` layer, giving a
real wall, position and width. **Coverage is inherently ~3 chapels**
(23/24/25): only buildings drawn in detail carry `LW1`/`LW2`. The rest
are plain outlines, and their open-polyline end gaps are *not* doors —
tested against the report's stated directions they agree 36% against a
~25% chance baseline.

**`read_report_directions.py`** is the site-wide aperture source. It
OCRs Chapter VII (`tesseract`, ~2.3 s/page, cached to
`report_plates/ocr/`) and pulls each chapel's stated entrance
direction from sentences like *"A chapel of Type 1 which opens
south"*, keeping the quote and book page for audit. **194 of 263
chapels (71%)** yield a direction this way; validated 6/6 against
pages read by eye. Chapels whose entry states no direction are listed
so they can be chased by hand.

**`extract_site_plan.py`** georeferences `Task_2/Site_Plan.pdf` (median
residual ~1 m) and writes per-chapel QC tiles. **Its door detection is
secondary and unreliable** — the plan's apparent wall-line gaps are
dominated by plan-vs-footprint registration artifacts at corners, and
the one ground-truthed chapel (180) has unbroken linework across its
real entrance. Use the tiles to sanity-check a direction, not to
source one.

**`extract_dxf_plans.py`** plots the 7 detailed CAD plans (buildings
1/23–26/175/210; LW1 walls black, LW2 detail orange — the orange
threshold marks bridging wall gaps are the doors) beside the chapel's
canonical wall indices, for measured door widths (`source_dims=dxf`).

**`build_aperture_walls.py`** turns registry rows into
`meshes/building_<ID>.obj`: outer + inner wall faces (0.4 m thickness
with real door reveals, so oblique sightlines are clipped by wall
depth; zero-thickness fallback for circular/tiny footprints), roof cap,
and the chapel's dome cap from `dome_inventory.csv` (`--no-domes` to
skip). Wall bases follow the bare DEM; wall tops follow the same
fitted roof plane QGIS extrusion uses. Also writes per-building QC
renders, a site coverage figure, and `mesh_args.txt` — the
ready-to-paste `--mesh … --mesh-clear-ids … --bare-dem …` fragment for
`viewshed.py`. `--self-test` proves registry→mesh→engine on synthetic
data with analytic sightlines; `--calibrate` reports measured-opening
statistics for updating the documented defaults (provisional: width
1.0 m, sill 0, head 2.1 m).

```bash
.venv/bin/python scripts/extract_report_plates.py     # once, ~1 min
.venv/bin/python scripts/read_report_directions.py    # OCR, ~4 min
.venv/bin/python scripts/apertures_from_report.py     # -> registry rows
.venv/bin/python scripts/build_aperture_walls.py      # -> 188 meshes
.venv/bin/python scripts/viewshed.py \
    --observers LAMP_DataStore/.../160_ViewpointMarks/my_observers.gpkg \
    --ids 18001 18002 18101 $(cat .../meshes/mesh_args.txt) --no-graph
```

### The aperture pipeline, in order

Sources → candidates → curated registry → meshes. **Extraction scripts never
write `aperture_inventory.csv`** — they emit `*_candidates.csv` and a human
merges. That rule is what lets you re-run any extractor without losing hand
edits.

| # | Script | Does |
|---|---|---|
| 1 | `extract_report_plates.py` | Cuts the 200 report page scans + contact sheets |
| 2 | `extract_plate_figures.py` | Cuts measurable per-chapel tiles out of those figures |
| 3 | `read_report_directions.py` | OCRs Ch. VII for stated entrance directions → `entrance_directions.csv` |
| 4 | `read_report_features.py` | Interior features (niches, apses, windows) from Ch. VII prose |
| 5 | `read_report_paintings.py` | Named painted scenes and where on the building they sit |
| 6 | `extract_site_cad.py` / `extract_dxf_plans.py` | Measured door positions/widths off the CAD (LW2 threshold marks) |
| 7 | `apertures_from_report.py` | Turns stated directions into registry door rows |
| 8 | `curate_windows.py` / `curate_niches.py` | Promote wall-anchored candidates into registry rows |
| 9 | `build_aperture_walls.py` | Registry → per-building OBJ meshes + `mesh_args.txt` |

```bash
.venv/bin/python scripts/read_report_directions.py      # → entrance_directions.csv
.venv/bin/python scripts/apertures_from_report.py       # → registry door rows
.venv/bin/python scripts/curate_windows.py              # then curate_niches.py
.venv/bin/python scripts/build_aperture_walls.py --openings all \
    --thickness-mode fabric --thin-rule legacy
```

`read_report_paintings.py --crossval` re-checks the parsed scene order against
the report's own printed running order (17/17) — run it after any parser change.

### `scripts/measure_wall_fabric.py` — per-building wall thickness

Measures wall thickness from the CAD plans and the report plate plans, falling
back to typology. Thickness sets how deep an opening's reveal is, which is what
clips oblique sightlines through it.

| Flag | Default | Meaning |
|---|---|---|
| `--cad-dir` / `--plate-index` | datastore | Measurement sources |
| `--no-plates` | off | Skip plate measurement (slow: each plate is rasterised) |
| `--out` | `building_fabric_candidates.csv` | **Candidates** — the curated `building_fabric.csv` is never overwritten |

```bash
.venv/bin/python scripts/measure_wall_fabric.py
```

### `scripts/check_regression.py` — the frozen baseline gate

Asserts the frozen mesh baseline still reproduces **byte for byte**. Run after
any change to the registry, `aperture_registry.py` or `build_aperture_walls.py`.

| Flag | Default | Meaning |
|---|---|---|
| `--frozen` | `viewshed_runs/frozen_baseline_20260811` | Baseline holding the `.sha256` manifests |
| `--only` | all | Restrict to named artefact groups, e.g. `meshes` |

```bash
.venv/bin/python scripts/check_regression.py --only meshes
```

Expect `every mesh byte-identical — 0 changed`. The "5 unexpected meshes"
failure is **pre-existing and expected**: chapels 66, 126, 136, 162 and 206
have apertures but no stated entrance, so they exist in the 202-chapel set and
not in the frozen 197.

### `scripts/build_visual_targets.py` — the things worth seeing

Turns registry openings and interior features into named ray-cast targets
(`target_inventory.csv`): entrance-axis points, footprint centroids, and
recess targets that sit *inside* the pocket at mid-depth.

| Flag | Default | Meaning |
|---|---|---|
| `--kinds` | all four | `entrance_axis`, `centroid`, `apse`, `niche` |
| `--fabric` | `building_fabric.csv` | Per-building thickness; sets how deep a recess target sits |
| `--min-headroom` | — | Drop targets with less clearance than this |

```bash
.venv/bin/python scripts/build_visual_targets.py
```

### `scripts/aperture_envelope.py` — from where outside can you see in?

Sweeps standing positions around one chapel and reports the envelope from
which its interior is visible through its own openings.

| Flag | Default | Meaning |
|---|---|---|
| `--id` | — | Chapel to stand outside of |
| `--mesh-dir` | `meshes` | Which wall-fabric mesh set to measure |
| `--neighbour-radius` | — | How far out to include occluding neighbours |

```bash
.venv/bin/python scripts/aperture_envelope.py --id 180
```

### `scripts/volume_convert.py` / `scripts/volume_mesh.py`

`volume_convert.py` re-exports an existing visibility volume into another
format without recomputing it. `volume_mesh.py` is the library behind
`viewshed.py --volume-format mesh`: it extracts the volume's **boundary
surface** as a PLY, either `blocky` (exact voxel faces) or `smooth`
(marching cubes). Not a driver — there is no CLI.

Promotes a saved `--volume` CSV into other formats without recomputing the
ray-casting.

| Flag | Default | Meaning |
|---|---|---|
| `csv` (positional) | — | Volume CSV from `viewshed.py` |
| `--to` | required | Any of `ply npy tif las laz mesh` |
| `--out-stem` | input filename stem | Output path stem |
| `--voxel` | inferred from data | Horizontal spacing (m), for `npy`/`tif`/`mesh` |
| `--zstep` | `1.0` if omitted | Vertical **bin size** (m), for `npy`/`tif`/`mesh` — unlike `--voxel` this is never inferred (the CSV's z values are absolute elevations, not a clean lattice) |
| `--crs` | none | CRS for the npy sidecar / GeoTIFF / LAS (e.g. `EPSG:32636`) |
| `--mesh-style` | `blocky` | With `--to mesh`: `blocky` exact voxel boundary / `smooth` marching cubes |

Caveat on `--to mesh`: the CSV's z is absolute and gets re-binned at
`--zstep`, so this mesh is a staircase approximation; the faithful
terrain-following shape comes from `viewshed.py --volume-format mesh`
(this path exists for only-kept-the-CSV workflows).

```bash
.venv/bin/python scripts/volume_convert.py viewshed_volume_id7.csv --to ply
.venv/bin/python scripts/volume_convert.py viewshed_volume_id7.csv \
    --to laz --crs EPSG:32636
```

### `scripts/run_grass_viewshed.py` — regenerate the GRASS baseline

Produces a fresh `r.viewshed` baseline at native 0.4 m instead of the
user-supplied 1.5 m ROI run. Needs GRASS on `PATH`.

| Flag | Default | Meaning |
|---|---|---|
| `--dem` | Task_2 0.4 m crop | Surface to run the baseline on |
| `--eye-heights` | `1.5 1.75` | Heights to generate |
| `--out-dir` | `Task_2/` | Where the baseline rasters land |

```bash
.venv/bin/python scripts/run_grass_viewshed.py
```

### `scripts/crop_task2_04m.py` — Task_2 ROI at 0.4 m

Crops the canonical site-wide 0.4 m rasters (DEM, bare earth, orthophoto) to
the Task_2 ROI so the baseline and the engine can be compared at native
resolution rather than at the 1.5 m subset's.

```bash
.venv/bin/python scripts/crop_task2_04m.py
```

### `scripts/compare_baseline.py`

Validates the engine against the user's GRASS `r.viewshed` baseline
(`Task_2/`) on a shared grid, sweeping observer height.

| Flag | Default | Meaning |
|---|---|---|
| `--baseline-dir` | `Task_2/` | Baseline data directory |
| `--dem` | `Task_2/DEM_Subset-WithBuildings.tif` | Shared surface (same grid as the baseline) |
| `--footprints` | `Task_2/BuildingFootprints.shp` | Footprints on the baseline grid |
| `--observers` | `Task_2/Marks_Brief2.shp` | Observer points |
| `--ortho` | `Task_2/OrthoImage_Subset.tif` | Orthophoto for QC overlays |
| `--out-dir` | `Task_2/comparison` | Report + figures output |
| `--eye-heights` | `1.5 1.75` | Eye heights (m) to sweep |

```bash
.venv/bin/python scripts/compare_baseline.py
```

### `scripts/compare_apertures.py` — aperture-aware vs baseline

Sweeps solid / doorless / apertured meshes (and domes on-off) against the
r.viewshed baseline on the Task_2 ROI. **Counts ground cells only**: a cell
inside a footprint gets its target pinned at the block's roof height, which
biases every mesh variant regardless of apertures. Read the generated report's
own caveat before quoting its `door_effect` — it is structurally zero here,
and the visibility graph is the metric that registers doors.

| Flag | Default | Meaning |
|---|---|---|
| `--baseline-dir` | `Task_2/` | Baseline data directory |
| `--out-dir` | `Task_2/comparison` | Report + figures output |
| `--eye-heights` | `1.5 1.75` | Eye heights (m) to sweep |
| `--mesh-suffix` | — | Which `meshes*` variant set to read |

```bash
.venv/bin/python scripts/compare_apertures.py --out-dir Task_2/comparison_all_fabric
```

### `scripts/test_intentionality.py` — were the entrances arranged?

**Pre-registered** Monte Carlo test: counts ordered chapel pairs where one
interior is visible from outside another's doorway, against nulls that
permute door walls (N1), positions (N2) or both (N3). The pre-registration
and every dated deviation live in the module docstring — read it before
changing anything here.

| Flag | Default | Meaning |
|---|---|---|
| `--n-draws` | `999` | Draws per null |
| `--nulls` | `N1 N2 N3` | Which nulls to run |
| `--radius` | `60` | Max observer–target distance (m) |
| `--seed` | `20260813` | Per-draw seeding is `(seed, stream, draw)` — resume re-derives, no RNG state |
| `--sequential-h` | `10` | Stop a null after H draws reach V_obs (Besag–Clifford); `0` disables |
| `--checkpoint` / `--resume` | out-dir | Pause-safe; a resume is refused if config or `DRAW_VERSION` differs |
| `--pair-cache` | **off** | Memoisation kept only to reproduce its own unsoundness (6.1e-4); leave off |
| `--cross-check K` | — | Cached vs exhaustive on K draws. **Expected to fail** — that failure is the finding |

```bash
.venv/bin/python scripts/test_intentionality.py --n-draws 999 --nulls N1 N2 N3 --sequential-h 10
```

### `scripts/test_entrance_azimuth.py` — orientation nulls

Tests the compass distribution of entrances against solar (sunrise, and
sunrise+sunset arcs computed for Kharga's latitude), uniform, and
downhill-slope nulls. Casts no rays and loads no meshes — it constrains the
explanation space cheaply before any expensive run.

| Flag | Default | Meaning |
|---|---|---|
| `--directions` | `250_Apertures/entrance_directions.csv` | Stated entrance directions |
| `--alpha` | `0.01` | Pre-registered one-sided level |

```bash
.venv/bin/python scripts/test_entrance_azimuth.py
```

### `scripts/test_feature_visibility.py` — are niches and apses seen?

Treats interior features as *targets* rather than occluders: can this niche be
seen from its own doorway, and from other chapels? Observers stand 1.5 m
outside an opening on its axis; external observers are other chapels' door
stations within 60 m.

```bash
.venv/bin/python scripts/test_feature_visibility.py
```

### `scripts/test_painting_visibility.py` — can the frescoes be seen from outside?

Computes a **bound**, not a measurement: how high a sightline can rise inside
the chamber given the door head, against the dome's springing line. Every
choice favours visibility (empty chamber, observer anywhere on the axis, the
registry's most generous head height), so a negative result survives the fact
that no opening height in the registry is measured.

```bash
.venv/bin/python scripts/test_painting_visibility.py
```

### `scripts/visible_fraction.py` — how much of a surface is visible

Library, not a driver: `visible_fraction(scene, eye, p0, p1)` returns what
fraction of a segment is visible and which parts, by adaptive subdivision.
Seeds a coarse sample, then refines only intervals whose endpoints disagree —
a plain endpoint bisection is wrong here, because visibility along a wall is
**not monotone** (a chapel in front can shadow a wall's middle while both ends
stay visible). Every call reports how many boundaries the depth limit did not
resolve.

```bash
.venv/bin/python scripts/visible_fraction.py --self-test
```

### `scripts/data_provenance.py` — what each fact rests on

Walks all six registries and writes `docs/DATA_PROVENANCE.md` plus a
per-chapel CSV: which document supplied each entrance, opening, thickness,
dome and painting, and the nine gaps where nothing did. Fails its self-check
if any provenance token or confidence grade lacks a documented meaning, so a
new token in a registry surfaces instead of rendering as a blank cell.

```bash
.venv/bin/python scripts/data_provenance.py
```

### `scripts/export_scene_bundle.py` — Blender/Unity export

Writes `scene_bundle/`: heightmap, ortho drape texture, observer eye
positions and dome geometry, all shifted to a **local whole-meter origin**
(raw UTM coords overflow the float32 both engines use). Domes are exported
as geometry, never baked into the heightmap.

| Flag | Default | Meaning |
|---|---|---|
| `--dem` / `--footprints` / `--observers` / `--ids` / `--eye-height` / `--dome-inventory` | as `viewshed.py` | Same semantics |
| `--margin` | `60` | Window margin (m) around the footprint bounds |
| `--full` | off | Export the whole DEM instead of the core window |
| `--unity` | off | Also write a 16-bit RAW heightmap for Unity Terrain |
| `--unity-res` | `1025` | Unity heightmap resolution, (2^n)+1 |
| `--out-dir` | `220_BuildingsToDEM/scene_bundle/` | Bundle folder |

```bash
.venv/bin/python scripts/export_scene_bundle.py --unity
```

### `scripts/export_walkable_scene.py` + `blender/build_walkable_scene.py`

Exports bare ground plus the **real chapel meshes** (not the extruded
heightfield blocks) as a scene you can walk through in Blender — the
qualitative counterpart to the numeric viewsheds. Presentation tier, never
evidence.

| Flag | Default | Meaning |
|---|---|---|
| `--bare-dem` | 0.4 m bare earth | Ground surface — must be **bare** earth, not the DEM-with-buildings |
| `--mesh-dir` | `meshes` | Which built chapel meshes to place |
| `--step` | `1` | Terrain decimation; 1 = native 0.4 m |
| `--margin` | — | Ground margin (m) beyond the footprints |

```bash
.venv/bin/python scripts/export_walkable_scene.py
blender --python blender/build_walkable_scene.py
```

### `blender/build_bagawat_scene.py` — Cycles renders

Runs inside Blender's own Python (no repo imports); consumes the bundle.
Install Blender 4.x LTS, then:

```bash
blender -b -P blender/build_bagawat_scene.py -- \
    --bundle LAMP_DataStore/ElBagawat/200_Projects/220_BuildingsToDEM/scene_bundle \
    --camera id1 --azimuth 90 --domes
```

Flags: `--camera` (default all) · `--azimuth` (default 90, repeatable) ·
`--pitch` · `--fov 60` · `--size 720 480` · `--samples 64` · `--stride`
(draft decimation) · `--domes` · `--height-ramp` · `--overview` ·
`--save-blend` · `--no-render` · `--render-dir`. Full setup, conventions,
and the engine-vs-Blender cross-check: [docs/BLENDER.md](docs/BLENDER.md).
Unity Terrain import: [docs/UNITY.md](docs/UNITY.md).

## Reproducing the QGIS scene on the remote machine

`Task_2/Task2.qgz` (tracked in git) stores the full 3D scene — terrain
config, vertex-bound building extrusion, dome-sphere styling, lights — with
**relative** layer paths. To rebuild it from a clone on the remote Windows
box:

1. `git clone` the repo.
2. From the repo root, junction the datastore so `../LAMP_DataStore/...`
   layer paths resolve (the junction must contain `ElBagawat/`):

   ```powershell
   New-Item -ItemType Junction -Path LAMP_DataStore -Target D:\path\to\datastore-root
   ```

   (`mklink` is a `cmd.exe` builtin, not a PowerShell command — it fails
   with "not recognized" if run directly at a PowerShell prompt; the
   `New-Item` form above is the native equivalent.)

3. Make sure the generated artifacts the project references exist in the
   datastore: `DEMWithBuildings-0.4m-*.tif` and `domes.gpkg`
   (`build_dem_with_buildings.py` + `build_dome_layer.py`, or copy from the
   local machine). **Gotcha:** the DEM filename is date-stamped — a fresh
   regeneration gets a new date, and QGIS will show one unavailable layer;
   repoint it via the repair dialog.
4. The LAZ volume layer (`viewshed_runs/id18002_NE_laz/…laz`) either travels
   in git or is regenerated with
   `viewshed.py … --volume --volume-format laz` (repair the layer path if
   regenerated).
5. Open `Task_2/Task2.qgz`.

The small Task_2 subset rasters (3–30 KB each) are exempted from the global
`*.tif` ignore so they arrive with the clone; the multi-GB datastore stays
out of git by design.

## Outputs

See CLAUDE.md's [Data assets](CLAUDE.md#data-assets) table for the full list
of where files land and what each one means.

## Key conventions

- Default eye height is **1.5 m**, not the 1.75 m GIS default (skeletal-data
  baseline for late-antique Egyptians) — override with `--eye-height`.
- All compute code must run unchanged on CUDA, MPS, and CPU — never hard-code
  a device.
- **Git is user-managed** — scripts and assistants never run git commands.

See [CLAUDE.md](CLAUDE.md) for the complete conventions list and the project's
validation philosophy.
