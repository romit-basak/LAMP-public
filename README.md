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
        ▼
    viewshed.py              cast rays, write viewsheds / visibility graph / 3D volume
        │                    (--domes bakes dome_inventory.csv into the ray-cast
        │                     surface in-memory, opt-in, experimental)
        ├── observer_view.py     first-person snapshots of what each observer sees
        │                        (same ray-march kernel; pano + perspective PNGs)
        ├── volume_convert.py    (optional) volume CSV → PLY / NPY / GeoTIFF / LAS / LAZ
        └── compare_baseline.py  validate against the GRASS r.viewshed baseline

export_scene_bundle.py      (optional) DEM window + ortho + observers + domes →
        │                   scene_bundle/ in a local float32-safe frame
        └── blender/build_bagawat_scene.py   Cycles renders (docs/BLENDER.md);
                                             Unity Terrain import (docs/UNITY.md)
```

Run `sanity_checks.py` before anything else, and after pulling new data.

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

```bash
# all observers, full 360° pano, all three shadings
.venv/bin/python scripts/observer_view.py
# one observer, eastward perspective only
.venv/bin/python scripts/observer_view.py --ids 1 --persp 90 --no-pano
```

Self-checks include a cross-validation: sampled first-hit points must be
visible to `visible_mask` (the r.viewshed-validated kernel) — runs at
99.6–100% in practice.

### `scripts/volume_convert.py`

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

   ```bat
   mklink /J LAMP_DataStore D:\path\to\datastore-root
   ```

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
