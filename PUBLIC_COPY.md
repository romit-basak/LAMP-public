# About this public copy

This is the **code-and-analysis** copy of the El Bagawat viewshed
project, prepared for public release (GSoC 2026, HumanAI Foundation).
Everything needed to read, audit, re-run and extend the work is here.
What is **not** here is the site data itself.

## What was removed, and why

The El Bagawat dataset includes satellite imagery obtained under a
DigitalGlobe Foundation grant, scans of Fakhry's excavation report, and
precise survey coordinates for an archaeological site. Redistribution
of those is not mine to grant, so this copy carries none of them —
**not in the working tree and not in the git history**, which was
rewritten with `git filter-repo` rather than merely deleted at the tip.
Deleting at the tip would leave the files fetchable from any earlier
commit.

Removed everywhere:

| Kind | What it was |
|---|---|
| Rasters — `.tif`, `.tfw`, `.aux.xml` | DEM subsets, orthophoto crops, viewshed outputs |
| Vector geodata — `.shp` family, `.geojson`, `.gpkg` | Building footprints, observer marks, visibility-graph geometry |
| 3D geometry — `.obj`, `.ply`, `.laz` | Chapel meshes, visibility volumes, point clouds |
| Rendered figures — `.png` under `viewshed_runs/`, orthophoto overlays in `Task_2/` | Anything drawn over site imagery |
| `Task_2/Site_Plan.pdf`, `Task_2/Task2.qgz` | Architectural site plan; QGIS project with layer paths and extents |
| Coordinate-bearing CSVs | See below |

The bulk dataset was never in the repository at all — `LAMP_DataStore/`
has always been gitignored.

### Geodata hiding in text files

Filtering by file extension catches `.gpkg` and misses the same data
written as `.csv`. Two files needed removing on content rather than
format:

- `viewshed_volume_*.csv` — a visibility volume as ~18k raw `x,y,z`
  points. The same data as the `.ply` beside it, in a format the
  extension filter did not catch.
- `aperture_inventory.csv` — the opening registry, which carried both
  the UTM midpoint of every chapel wall and verbatim OCR'd passages of
  the excavation report in its `notes` column.

The registry is the artifact that makes the aperture results auditable,
so rather than drop it entirely a redacted variant is published as
**`aperture_inventory.redacted.csv`**:

- **dropped** — `wall_mx`, `wall_my` (survey positions), and the
  verbatim report quotations
- **kept** — `kind`, `wall`, `s_m`, `width_m`, `sill_m`, `head_m`,
  `wall_az`, and the full provenance columns `source_pos`,
  `source_dims`, `confidence`, plus each row's report page reference

`wall_az` is kept deliberately. An azimuth is a *direction*, not a
position: it locates nothing, and it is the quantity the
entrance-direction and intentionality results are computed from.
Removing it would make the headline findings uncheckable while
protecting nothing. Page references are kept for the same reason —
anyone with their own copy of the report can verify any row.

Run logs under `viewshed_runs/fabric_sweep_20260811/` had the three
observer positions echoed in their headers; those coordinates are
redacted in place and the logs are otherwise intact.

## What is here

- **All 41 Python modules** (`scripts/`, `blender/`) — the complete
  engine, aperture pipeline, statistical tests and report generators
- **All documentation** — `CLAUDE.md`, `README.md`,
  `docs/CODE_WALKTHROUGH.md`, `docs/DATA_PROVENANCE.md`, `PROGRESS.md`,
  `FUTURE_WORK.md`, `GSOC_WORK_PRODUCT.md`
- **All derived results in text form** — comparison reports, metrics
  CSVs, viewgraph edge tables, the intentionality results, regression
  hash manifests, run logs
- One figure, `decomposition_apertures.png`, which is a plot rather
  than a picture of the site

The viewgraph tables carry distances and elevations but no eastings or
northings, so they describe *what is visible from what* without
locating anything.

## Consequences you will notice

**Figure links in the comparison reports do not resolve.** The reports
in `Task_2/comparison*/` end with a "Figures" section listing
`compare_obs{1,2,3}.png` and similar. Those are orthophoto overlays and
were removed. The reports' numbers, tables and interpretation are
untouched; the images regenerate when the scripts are run against the
dataset.

**Scripts run but will not find their inputs** until the dataset is in
place. The expected layout under `LAMP_DataStore/ElBagawat/` is
documented in [`CLAUDE.md`](CLAUDE.md), and
[`docs/REMOTE_SETUP.md`](docs/REMOTE_SETUP.md) is a
clone-to-running-pipeline walkthrough. `scripts/sanity_checks.py` will
report exactly which inputs are missing.

**The core self-checks need no site data** and are the quickest way to
confirm the engine works:

```bash
.venv/bin/python scripts/visible_fraction.py --self-test
.venv/bin/python scripts/make_test_building.py --crs EPSG:32636
.venv/bin/python scripts/scene3d.py --assets viewshed_runs/synthetic_building/assets
```

The second builds a synthetic cube-with-dome-and-door from scratch; the
third casts rays through it and asserts analytically known answers —
through-door passes, above-head blocked, blank-wall blocked, exact
first-hit distances, reciprocity, and agreement between the two ray
kernels over 720 rays. All pass in this copy.

`--crs EPSG:32636` is needed only here. Normally the generator reads
the CRS from `Task_2/DEM_Subset-Original.tif`, which is one of the
removed rasters; passing the code explicitly replaces it. Nothing else
about the synthetic scene depends on site data.

**A note on running things here.** Every pipeline — including the
synthetic self-test — writes rasters, geopackages and meshes into the
tree. `.gitignore` therefore ignores all of those formats, so that
running something and then `git add -A` cannot reintroduce the kinds of
file this copy exists to exclude. Already-tracked files are unaffected;
`git add -f` covers a genuinely publishable chart.

## Getting the data

Contact the Late Antiquity Modeling Project (mentors: Camille Leon
Angelo, University of Alabama; Joshua Silver, KIT). Access is theirs to
grant, not mine.

## Provenance of this copy

Filtered from the full working repository with:

```bash
git filter-repo --invert-paths --paths-from-file <list>
```

Commit history, authorship and dates are preserved — 30 commits, all
mine. Commit hashes differ from the private repository, because
rewriting history necessarily rewrites them.
