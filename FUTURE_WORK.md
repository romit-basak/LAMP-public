# Future work — El Bagawat viewshed

> What is worth doing next on the visibility half of LAMP, in priority
> order. Written as a handover: each item says what the work is, why it
> matters, and what it unblocks.

The GSoC deliverable — a corrected, aperture-aware visibility graph —
is built and reported in [`GSOC_WORK_PRODUCT.md`](GSOC_WORK_PRODUCT.md).
Everything below is beyond that, and none of it blocks reading or
reusing what exists. Work continues with the mentors past the program.

---

## 1. The full observer point set — blocks the most

`Marks_Brief2.shp` holds **three** points, and every site-wide number
in the project is therefore a 3-observer sample rather than the
finished graph. This has been outstanding since the data-transfer
phase and is mentor-dependent, so it has its own lead time.

Once it lands: regenerate the viewsheds, the visibility graph and the
comparison at full scale, and replicate the shapefile to the remote
datastore per the note in the CLAUDE.md data table.

**Also unresolved:** the 2026-08-08 run reported 1,052 building-observer
pairs (263 × 4) where the current one produces 789 (263 × 3). A fourth
observer existed and its provenance is not recoverable from anything on
disk — no viewgraph artefact postdates June 13. Settle this before
quoting either run's absolute numbers.

## 2. Opening heights, read off the report plates

The single largest evidence gap. `source_dims` in the registry contains
**no plate-derived rows at all** — every sill and head in the model is a
class default, including all 197 door heads at 2.10 m. The plates pair
a plan with an elevation and dimension lines, and are the only possible
height source in the whole dataset; reading them is a human pass that
has not happened.

Until it does, any height-sensitive result rests on assumption. The
fresco-visibility bound is deliberately built to survive this (it uses
the most favourable head height and still finds the dome out of sight),
but that is a bound, not a licence.

`extract_plate_figures.py` already cuts the per-chapel tiles; what is
missing is the reading, and a `source_dims=plate` value to record it.

## 3. Chapel 80 — the Chapel of Peace

One of the two painted chapels, the most-studied structure on the site,
and **absent from the model**: no registry row, no mesh, no observer
station. Its description sits in Chapter V while
`read_report_directions.py` parses Chapter VII. The pages are already
OCR'd, so this is a parse-range fix rather than new extraction.

## 4. Per-chapel floor datum

Every sill and target height comes from the bare DEM sampled at that
feature's own position, so within one chapel a niche on the downhill
wall sits metres below one on the uphill wall. Measured across 263
footprints: median spread 0.79 m under the wall midpoints, 33% over
1 m, 9% over 2 m, worst 4.85 m — comparable to a niche's entire height.

Fixing it means a per-chapel datum, which changes the meshes, so it
must land **behind a flag** with `per-point` kept as the default so the
frozen regression baseline still reproduces.

This is the same class of error as the one caught in the intentionality
nulls, where permuted chapels kept the elevation of the plot they came
from. Worth doing for that reason alone: a z-reference taken per-point
where it should be per-object has already produced one wrong result.

## 5. Domes render as full spheres from inside

Cosmetic, and the first thing anyone notices walking the Blender scene.
The dome centre is deliberately placed **0.35 × radius below the
roofline** so the cap reads as a dome rather than a ball from outside —
which necessarily leaves the lower hemisphere hanging inside the
chamber. From a standing position indoors you are looking up at a
sphere intersecting the walls.

Fix: clip the sphere to a hemisphere at the roof plane in
`blender/build_walkable_scene.py`, which corrects the interior view
without changing the exterior silhouette the offset was chosen for.
Presentation tier only — no analysis result depends on it.

## 6. Site access and the northern taboo

**No chapel opens north** (N/NE/NW all zero, hand-verified), and the
published tests rule out the obvious explanations: solar orientation,
uniform choice, and local ground slope are all rejected.

Two mentor hypotheses remain untested:

- **Approach geometry.** The site opens south, toward the oasis, so a
  north-facing chapel would require entering and turning back on
  yourself. Testable: does entrance direction correlate with a
  chapel's position relative to the southern approach, independent of
  its neighbours?
- **Wind-blown sand.** A north door faces the prevailing northerly
  wind regime of the Western Desert. This fits the data better than
  slope does, and for a structural reason worth stating: a wind
  explanation predicts a *uniform* taboo independent of local terrain,
  which is what is observed, whereas slope predicts variation with
  local terrain, which is not. Needs a wind-climatology source before
  it is more than a good fit.

A scratch analysis already establishes the terrain half: north is
uphill at 40.3% of chapels and the ground rises northward at all at
76.8%, but of the 45 chapels where north is *downhill*, **zero** open
north and 20 open south — straight uphill. So slope cannot be the
mechanism.

## 7. Distance-based visual obscurity

Human acuity falls off with range, so an unbounded straight ray
overstates what is meaningfully *seen* far away: a ray travelling
kilometres and counting a distant cell as visible is geometrically true
and perceptually wrong.

Model it as an acuity/contrast threshold (target subtends too small an
angle), atmospheric extinction, or a distance-decaying weight, layered
onto `visible_mask` — and report **both** raw and acuity-weighted
visibility in the comparison, so the change is auditable rather than a
silent redefinition. Raised by mentors and deliberately deferred.

## 8. GNN over the visibility graph

An explicit stretch goal, and a follow-on rather than a substitute for
the deterministic engine — the whole argument of the project is that
the corrected graph is something existing tools cannot produce, so a
learned model is only interesting once it consumes *that* graph rather
than a solid-building one. Scope it after items 1 and 2, which are what
make the graph trustworthy.

The remote workstation (RTX 5000 Ada 32 GB, Threadripper Pro 7985WX,
512 GB RAM) is already picked up by the existing cuda-first device
selection.

## 9. Newer satellite imagery — confirm the sensor, then decide

`100_Data/140_SAR_Imagery/LandInfo_2026/` (remote machine only; the
local folder is an empty stub) holds a delivery received May 2026 that
mentors describe as PNEO stereo from LAND INFO Worldwide Mapping. The
folder name records the **purchase** year, not the acquisition year,
and its three products do not all match that description:

| Subfolder | Product prefix | Acquired |
|---|---|---|
| `LG04 HD15` | `LG4_26JAN01124636-S3DS-` | 2026-01-01 |
| `Stereo WV1 Source Data` | `23APR03...-P2AS-` | 2023-04-03 |
| `WV1 50cm Pan Ortho Scene` | `WV1_23APR03...` | 2023-04-03 |

Two of the three are WorldView-1 — a Maxar sensor at the same
resolution class as the GeoEye/WV2 pairs the current DEMs already came
from, so not an upgrade. Only `LG4` is recent enough to be the new
delivery, and `LG4` is **not** a sensor code identifiable from a
directory listing. Each product ships its own `README.TXT`/`.XML`/
`.IMD` alongside; read those first — they state sensor and product type
outright, and settle this in minutes.

**If `LG4` is Pléiades Neo** (~30 cm pan), a DEM regeneration could
sharpen the ray-casting surface. `DEM_Process.md` has the full recipe
(Ames Stereo Pipeline: `parallel_stereo` -> `point2dem` ->
`dem_mosaic`), so the path is known rather than exploratory. It is
still a multi-day run on the remote box, and it **replaces the surface
every published result stands on** — so it needs the regression
baseline re-frozen and the `r.viewshed` comparison re-run, not just a
file swap.

**It does not help with openings.** Both products are near-nadir, and
a wall face is edge-on from overhead: a door is close to invisible from
directly above at any resolution. That is geometry, not sharpness. The
finding that imagery cannot supply apertures stands regardless of what
this delivery turns out to be — worth stating plainly to mentors, since
the expectation seems to be otherwise.

---

## Not on this list, deliberately

- **BVH acceleration**, previously carried as a contingency, is **not
  needed**. The site totals 7,376 triangles and the bottleneck was
  never the intersection arithmetic — batching the ray cast took a draw
  from 104 s to 0.65 s. Revisit only if real chapel models arrive at a
  much higher polygon count.
- **depthmapX / 2D-VGA as a second baseline.** Mentors had a poor
  experience with the tool, its "2.5D" capability is unpublished, and
  `r.viewshed` is already a 2.5D tool that the engine reproduces at
  97–99% on solid buildings. It would add little.
- **Path and movement analysis** (cost surfaces, entrance-weighted
  costs, Monte Carlo path ensembles) belongs to the other contributor's
  half of the proposal. Not to be added here unless explicitly asked.
- **More visualization.** The Blender/Unity/volume-mesh stack is
  finished, working, and was never proposal-required. It is
  communication and audit tooling built around the deliverable, not the
  deliverable itself, and should not absorb time that items 1–4 need.
