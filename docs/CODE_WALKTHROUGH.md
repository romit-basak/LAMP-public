# Code Walkthrough — El Bagawat Viewshed Engine

> A narrated tour of the viewshed half of LAMP: what each script does, *why* it
> is built the way it is, and what would break if it were built otherwise.
> Read [CLAUDE.md](../CLAUDE.md) for project scope and [PROGRESS.md](../PROGRESS.md)
> for the running history; this document is the "how and why" of the code itself.
>
> Written so that no prior programming or GIS background is assumed. If a
> paragraph leans on a term like "bilinear interpolation" or "CRS," it's
> defined in [the primer below](#before-you-start-six-ideas-everything-else-builds-on) —
> read that section first if any of this is unfamiliar. Everywhere the code
> does something that looks arbitrary or "hacky" at a glance, this document
> tries to say *why*, and what would silently go wrong if it were done the
> more obvious-looking way instead.

## The one thing to keep in mind

Standard visibility tools (GRASS `r.viewshed`, 2D space-syntax VGA) treat every
building as a **solid, opaque block**. The thesis of this project is that the
necropolis is legible precisely *because* sight and light pass **through** window
apertures and **between** doorway-oriented structures. The deliverable is a
visibility graph that respects that — produced by a real 3D ray-casting physics
simulation rather than a planimetric approximation.

A second consequence shapes the whole codebase: **there is no ground truth.** The
site is ~1,700 years old; no one recorded what was visible from where. So we
cannot "fit to labels." Correctness instead means two things, and both are baked
into the code:

1. **Deterministic, inspectable ray-casting** — a human can confirm the rays
   respect the geometry (QC PNG overlays), and
2. **Self-verifying scripts** — every script asserts physical invariants
   (symmetry, monotonicity, reciprocity) and exits nonzero if any fail.

Keep those two facts in mind and most of the design choices below explain
themselves.

## Before you start: six ideas everything else builds on

**A DEM (Digital Elevation Model)** is just a grid of numbers. Each cell
records the ground's height above sea level at that one spot — think of it as
a black-and-white photograph where "brightness" means "how high up." This
project also calls it a **heightfield**, which is the same idea under a
different name. "0.4 m resolution" means each grid cell covers a 0.4 x 0.4
meter square of real ground.

**A viewshed, and ray-casting.** A viewshed answers "what can be seen from
this one spot?" for every other point on the map. This project answers that
question the physically honest way, called **ray-casting**: draw an
imaginary straight line (a "ray") from the eye to the point being tested, and
walk along that line checking whether the ground rises up and crosses it
anywhere in between — the way a hill or a wall would actually block your
view. If nothing crosses the line, the point is visible; if something does,
it isn't. **Line of sight (LOS)** is just the name for that imaginary line.

**A CRS, and why UTM.** Coordinates need a reference system to mean anything
— a CRS (Coordinate Reference System) is the technical name for "which
system this file's X/Y numbers are measured in." Latitude/longitude works
everywhere on Earth, but a degree of longitude is a *different* number of
real-world meters depending how far you are from the equator — useless for
directly asking "how many meters away is that wall?" **UTM** solves this by
carving the Earth into zones and giving plain X/Y numbers in meters within
each zone, so subtracting two points' coordinates gives a real distance
immediately. This project always reads the CRS from the file itself
(`rasterio`/`geopandas` report it) rather than assuming one, because an
unprojected or wrong CRS makes every downstream height/distance/radius
calculation silently, invisibly wrong.

**GPUs, and why the code checks for three different kinds.** Ray-casting
means testing millions of lines-of-sight, and a graphics card (GPU) can do
that arithmetic thousands-at-once instead of one-at-a-time — much faster than
the plain processor (CPU) alone. Different computers have different GPUs:
NVIDIA's are programmed via **CUDA**, Apple Silicon Macs' via **MPS**. This
project runs on both a Mac (for day-to-day development) and a remote NVIDIA
workstation (for heavy compute), plus it must never simply crash on a machine
with no supported GPU at all — hence the code always checks CUDA, then MPS,
then falls back to plain CPU, and is written so the *same* code path works on
all three (more on why that's harder than it sounds in §4.4 below).

**Bilinear interpolation.** The height grid only records elevation at fixed
points (every 0.4 m), but a ray's path crosses the space *between* those
points constantly. Bilinear interpolation is the standard way to estimate the
height at an in-between point: take the four nearest known grid corners and
blend their heights in proportion to how close the point is to each one. It
shows up constantly in this codebase — anywhere a "world coordinate" needs
converting into "a height."

**Why there's no ordinary test suite.** Normal software is tested by
comparing its output to a known correct answer. Nobody recorded what was
visible from where at this site 1,700 years ago, so there is no correct
answer to compare against. Instead, every script checks that its answers obey
rules that must *always* hold regardless of what the true answer is — e.g.
"standing up higher should never make you see less" or "if I can see you,
you should generally be able to see me back." Each script prints these
checks live to the terminal as `[PASS]`/`[FAIL]`/`[WARN]`, so a human — a
mentor, not just a programmer — can read top-to-bottom and audit the run
themselves, without needing to trust a hidden test file. §1 below covers the
exact mechanism.

One more pair of terms used a lot once 3D **volumes** come up: a **voxel** is
the 3D version of a pixel — a small cube of space that is either "visible" or
"not." A **point cloud** is just a scatter of dots, one per visible voxel's
center. A **mesh** instead describes the *outer skin* wrapped around a solid
shape — a surface built from flat triangular patches ("faces") stitched
together at shared corners ("vertices"), much closer to a real 3D object you
could 3D-print or load into a game engine than an unconnected cloud of dots.

## The pipeline

```
sanity_checks.py             validate every raster/vector is coherent (run first)
        │
build_dem_with_buildings.py  rasterize footprint heights onto the base DEM
        │                    -> DEM-with-buildings (the ray-cast surface)
        │
build_dome_layer.py          (optional) typology + orthophoto -> dome layer
        │                    for QGIS 3D + viewshed.py's --domes
        │
make_test_building.py        (optional) synthetic cube+dome+door assets;
        │                    scene3d.py runs the aperture self-checks (§10)
        │
extract_report_plates.py     aperture sources -> aperture_inventory.csv
extract_site_plan.py         (registry; human-in-the-loop by design, §11)
extract_dxf_plans.py         -> build_aperture_walls.py -> building OBJs
        ▼
    viewshed.py               cast rays -> viewsheds / visibility graph / 3D volume
        │   │                          │   (--mesh -> scene3d.py hybrid scene)
compare_baseline.py ──────────┘        volume_convert.py
(validate vs GRASS)                    (CSV -> PLY / NPY / GeoTIFF / mesh,
        │                                via volume_mesh.py, a shared library)
        ▼
observer_view.py              first-person snapshots (same Scene, first_hit kernel;
                              also --mesh-aware)

export_scene_bundle.py -> scene_bundle/ -> blender/build_bagawat_scene.py + Unity
```

Ten runnable scripts, two shared library modules
([volume_mesh.py](../scripts/volume_mesh.py) — mesh/point-cloud I/O and
checks; [scene3d.py](../scripts/scene3d.py) — the aperture-capable hybrid
scene, also runnable as its own self-check, see §10), and one Blender-side
runner ([blender/build_bagawat_scene.py](../blender/build_bagawat_scene.py))
that lives outside the regular Python environment entirely (Blender ships
its own Python interpreter — see §8).
[sanity_checks.py](../scripts/sanity_checks.py) is the shared foundation —
every other script imports its canonical asset paths and its `check` /
`warn` / `failures` self-check vocabulary. One more script,
[run_gui.py](../scripts/run_gui.py), isn't part of the pipeline itself — it's
a browser-form wrapper around all the others' command lines (see §9).

---

## 1. `sanity_checks.py` — the data contract

**What it does.** Run before anything else, it verifies the rasters and vectors
are mutually coherent: every asset is projected/metric and shares one CRS, the
building DEM aligns with the base DEM grid, the height differential is plausible,
and the viewpoints fall inside the DEM.

**Why it exists.** Geospatial bugs are silent. A shapefile in the wrong CRS, a
raster on a different grid, or a height field in the wrong units will not throw —
it will just produce a *plausible-looking wrong answer*. The cheapest place to
catch that is up front, with assertions, not three scripts downstream when a
viewshed looks subtly off.

**The `check`/`warn` vocabulary — the project's whole testing idiom, in full:**

```python
def check(ok, label, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(f"{label}: {detail}")
    return ok


def warn(label, detail=""):
    print(f"  [WARN] {label}" + (f" — {detail}" if detail else ""))
    warnings.append(f"{label}: {detail}")
```

`check` takes a yes/no condition the caller already computed, prints
PASS/FAIL *immediately* (so someone watching the terminal sees results as
they happen, not buried in a report afterward), records a one-line message in
a shared list if it failed, and — importantly — **returns the yes/no
answer**, so a caller can write `if not check(...): sys.exit(1)` to hard-stop
only when a later step would be nonsense without it. `warn` is the same
shape but never fails the run; it's for "this is odd, take a look" rather
than "this is wrong."

**Why this instead of `pytest`/a real test framework.** A test framework
compares output to a known-correct fixture. There is no known-correct
fixture here (see the primer above). The goal instead is a **readable, linear
audit transcript**: every diagnostic number (medians, percentiles, counts)
prints inline next to its verdict, in the order it was computed, so a
non-programmer reading the console top-to-bottom can follow *why* a run is
trusted. Both `check`/`warn` mutate shared module-level lists (`failures`,
`warnings`) rather than raising an exception, so a script keeps running after
one bad check and surfaces *every* problem in a single pass — it only exits
nonzero at the very end, once everything has had a chance to report in. (One
implicit assumption worth knowing: those two lists are never reset, so if a
script's `main()` were ever called twice in the same Python process —
unlikely in normal command-line use, but possible from a notebook — stale
failures from the first run would carry into the second.)

**The decision this script forced.** It contains the check that killed the legacy
DEM. `BuildingsToDEM.tif` looked usable, but differencing it against the base DEM
revealed the off-footprint median was *not* zero:

```python
offset = np.median(off)
rel = np.median(on) - offset
...
if abs(offset) > 0.5:
    warn("off-footprint differential not ~0",
         f"median {offset:.2f} m — the two rasters use different "
         "vertical references / source DEMs. Do NOT difference them "
         "for building heights; regenerate the extruded DEM from "
         "Current_DEM + footprint heights")
```

The two rasters sit on different vertical datums (~78 m apart) at different
resolutions (1.5 m vs 0.4 m). **If we had skipped this check** and used the legacy
DEM, every building would have been ~78 m too tall relative to the terrain the
observer stands on — the entire viewshed would be physically meaningless, and
nothing downstream would have complained, because nothing about that error
would ever raise an exception. That single WARN is what justified
regenerating the surface locally (the next script). The height-plausibility
band it checks against (`0.5 <= rel <= 15` meters — "buildings here are
between half a meter and 15 m tall") is a domain judgment call baked into the
code as a literal number; there's no citation for the exact bounds beyond
"plausible for this site's chapels," and the same band is repeated verbatim
in a second check later in the file rather than named once — a minor
duplication worth knowing about if the bounds ever need revisiting.

A few of its checks hard-code the currently-known scale of the dataset —
`Marks_Brief2.shp` has exactly 3 points, there are exactly 263 per-building
`.gpkg` files — which read as arbitrary literals out of context but are
deliberate **drift detectors**: if the observer set or the footprint count
ever changes upstream, these checks start failing on purpose, which is the
point (a human should notice and update the assumption, not have it silently
stop matching reality).

---

## 2. `build_dem_with_buildings.py` — making the surface

**What it does.** Produces the canonical ray-casting surface: rasterize each
footprint's `Elevation` height onto the base-DEM grid, add it to the base DEM,
self-verify the written file, emit QC images. ("Rasterize" means "burn a
vector shape — here, a building's footprint outline — onto a pixel grid," the
same way a printer fills in a shape with ink pixel by pixel.)

**The thought process, decision by decision:**

**Rasterize ascending by height.** Footprints can overlap. `rasterio.features.rasterize`
burns features in order and the *last* one wins, so the buildings are sorted
shortest-first:

```python
# ascending height so the taller building wins where footprints overlap
# (rasterize burns in order; last wins)
gdf = gdf.sort_values(args.height_field)
```

**If done otherwise** (unsorted, or descending), a tall tomb overlapped by a short
one could be overwritten and silently shrink — an occluder vanishing from the
scene. Sorting makes the overlap rule explicit and physically correct (the taller
structure is what blocks sight). This is a strict order-of-operations
dependency: removing or reversing that one `sort_values` call would silently
flip which building wins at every overlap, with no error raised anywhere,
because `rasterize` has no concept of "conflict" to report.

**Add only on valid cells; never invent nodata.** ("Nodata" is the sentinel
value a raster uses to mean "no real measurement exists here" — a hole in the
data, not a real elevation of zero or negative infinity.)

```python
out = base.copy()
out[fp_mask & valid] += heights[fp_mask & valid]
```

A footprint sitting over a nodata hole in the terrain is left as nodata rather
than getting a height bolted onto garbage. **Otherwise** the ray-caster would
later read a fabricated elevation and cast against terrain that does not exist,
with no way to tell "this was measured" from "this was invented."

**Per-feature coverage audit.** A thin footprint can fall *between* pixel centers
and rasterize to nothing. Rather than trust the height-burn alone, the script
rasterizes the buildings' ID numbers separately (a building's ID is always a
non-zero integer, so "this pixel burned to 0" unambiguously means "no
footprint touched it," which "burned to height 0" could not tell you) and
checks every footprint survived:

```python
burned_ids = set(np.unique(id_grid)) - {0}
missing = sorted(set(gdf["ID"].astype(int)) - burned_ids)
check(not missing, "every footprint covers >=1 pixel",
      f"missing IDs {missing} — consider --all-touched" if missing else
      f"{len(burned_ids)} features burned")
```

**Otherwise** a building could quietly disappear from the surface and you would
only notice when its viewshed shadow was missing — if ever. This is a
deliberate second rasterize pass purely to get a stronger audit signal, not
an accident of duplicated work.

**Self-verify by re-reading the written file.** Compression, dtype casts, and
georeferencing are all places a write can go wrong. So after writing, it re-opens
the file and asserts the round-trip is exact:

```python
check(float(np.abs(off).max()) == 0.0,
      "off-footprint pixels unchanged", f"max |diff| {np.abs(off).max()}")
check(float(on_err.max()) < 1e-3,
      "on-footprint pixels = base + height", f"max error {on_err.max():.2e} m")
```

(The `1e-3` m tolerance on the second check — versus an exact `0.0` on the
first — is float32 addition rounding error, not a bug; off-footprint pixels
are untouched copies so they must match exactly, while on-footprint pixels
went through an actual floating-point add.)

**Output settings, and why they matter downstream:**

```python
# tiled=True: downstream scripts (viewshed.py's core-window crop,
# build_dome_layer.py's per-footprint ortho window reads) only read
# small windows out of this raster, and GeoTIFF can only do that
# efficiently against internal tiles, not a single strip. lzw is a
# lossless, no-downside compression for a float32 elevation grid.
profile.update(dtype="float32", compress="lzw", tiled=True)
```

**Why a pure-NumPy hillshade.** The QC images use a hand-written hillshade
("hillshade" = a simulated-sunlight shading of terrain, the standard way to
make a flat elevation grid visually readable as 3D-looking hills and
valleys) rather than GDAL's:

```python
def hillshade(z, res, azimuth=315.0, altitude=45.0):
    """Plain NumPy hillshade (avoids a GDAL dependency)."""
```

GDAL-the-binary is a heavyweight, platform-variable dependency; the engine already
needs rasterio (which wraps GDAL for file I/O) but not the separate GDAL command-line
tool. Twelve lines of NumPy keep the QC path dependency-light and identical across
the Mac and the remote box. This function is imported by `viewshed.py` and
`compare_baseline.py` too — written once, reused everywhere a hillshade is needed.

---

## 3. `build_dome_layer.py` — the dome visualization layer

**What it does.** Joins the excavation report's chapel typology onto the
building footprints, works out which chapels have domed roofs (typology
codes 4/5/6/7/9), measures each dome's center point and radius from the
orthophoto, and writes an editable registry plus QGIS-ready 3D sphere
layers.

**Why domes need a separate pass at all.** The building footprints only carry
a flat "Elevation" (height) per building — there's no roof-shape information
anywhere in the source data. Domes are reconstructed from two independent
clues instead: the excavation report's typology (which chapels are
documented as domed) and the orthophoto (a dome catches direct sun and shows
up as a distinctly bright rounded patch on the roof, so its center and size
can be measured from brightness alone).

**Detecting a dome from a photo: the core trick.**

```python
def detect_dome(ortho, transform, geom):
    """Largest bright blob inside the footprint. Returns (cx, cy, radius_m)
    or None. Bright = upper quartile of the footprint's own brightness (domes
    catch the sun; local threshold is robust to scene-wide contrast)."""
```

The key design choice is **local, not global, brightness**: "bright" means
"in the brightest 25% of pixels *for this one building's own roof*," not
"brighter than some fixed number across the whole orthophoto." A single
global brightness cutoff would fail wherever lighting or shadow varies
across the site; measuring each footprint's own pixel population adapts to
that automatically. The bright pixels are grouped into connected blobs, the
largest blob is assumed to be the dome (smaller bright specks — glare,
debris — are assumed smaller than an actual dome), and its pixel area is
converted to a radius by treating it as a circle (`area = pi * r^2`) even
though the real blob shape is irregular — the downstream use (a sphere
symbol) only needs one number, not a precise outline. A detected blob whose
centroid somehow lands outside the building's own footprint, or whose
radius comes out implausibly small (under 0.8 m, `R_MIN`), is rejected
outright rather than trusted — the code falls back to a geometric estimate
instead (below) whenever the photo evidence looks unreliable.

**Fallback when no credible blob exists: the room's "widest point."**
`inradius_point` finds the center of the largest circle that fits entirely
inside a building's footprint (imagine inflating a balloon inside the room
until it touches every wall — its center and radius when it stops growing).
For chapels where the orthophoto gives no reliable dome signal, this
becomes the assumed dome center, and its size is estimated from the
**median ratio of dome-radius-to-room-size actually measured** across every
other chapel where a real photo detection succeeded — a data-driven guess
rather than an arbitrary one, since it's calibrated from this exact site's
own real domes each time the script runs.

**Clamping to the wall — a genuinely two-step, easy-to-get-wrong
correction.** A detected dome's raw radius could be large enough that the
rendered sphere pokes through the nearest wall. The obvious-looking fix —
cap the radius using the room's widest-point clearance (from
`inradius_point`, already computed) — is subtly wrong, and the code says so
directly:

```python
# clamp by the wall clearance at the *detected* center — the
# polylabel inradius describes a different, wider point and
# would let the sphere poke through the nearest wall
r_wall = R_FRAC_MAX * clearance(geom, cx, cy)
```

The room's widest point and the photo-detected dome center are usually
*different physical locations* — a dome near one edge of a room has much
less wall clearance than the room's geometric center does. Clamping by the
wrong point's clearance would let a dome sized "as if centered in the middle
of the room" still visually clip through a wall it's actually much closer
to. The fix recomputes clearance fresh, at the dome's *actual* detected
position, every time.

**Anchoring a dome's height to the roof QGIS actually draws, not the ground
underneath it.** This is the single most important, most carefully-reasoned
calculation in the file:

```python
def roof_plane_z(geom, elevation, dem, x, y):
    """Rendered-roofline height at (x, y): least-squares plane through the
    footprint's vertex roof points (vertex ground + building height). A
    vertex-bound, terrain-clamped extrusion shears the roof with the terrain,
    so ground-at-center + height rides above the rendered roof wherever the
    center sits on a local high — anchoring to the vertex plane keeps dome
    spheres flush with the roof QGIS actually draws."""
```

QGIS's 3D view builds a building's roof by lifting each *corner* of its
footprint outline by the building's height above *that corner's own ground
elevation* — so on sloped terrain, the resulting roof is a tilted plane, not
a flat cap. If a dome's height were computed the obvious way — sample the
ground once at the dome's center, add the building height — it could sit
*above* the real rendered roof whenever the footprint's center happens to sit
over a local terrain bump, making the sphere visibly float above the roof
rather than sit on it. The fix: sample the ground at every corner of the
footprint, add the building height to each, fit a flat plane through those
points (a standard "least-squares fit" — the plane that best matches a
scatter of points overall), and read the dome's height off *that* plane at
its actual (x, y) position. This calculation is only correct because it
deliberately mirrors QGIS's specific extrusion rule — if that rendering rule
ever changed, this formula would need to change with it, and nothing in the
code enforces that link beyond the comment explaining it.

**Why a sphere is placed 35% of its own radius *below* the roofline
(`DOME_SINK`).** QGIS has no built-in "hemisphere" or "dome cap" symbol —
only full spheres. A full sphere centered exactly on a flat roof looks like
a ball resting on the surface, not a dome. Sinking the sphere's center
slightly below the roof plane lets the roof itself hide the sphere's lower
half, so only the visible cap reads as a dome — and this works even on the
tilted, slope-sheared roofs `roof_plane_z` produces, since the sinking is
relative to the roof plane at that exact point, not a fixed world height.

**Domes as one GeoPackage layer per size class.** QGIS's 3D point-sphere
symbol can only take one fixed radius per *layer*, not a different radius
per point within a layer — so every dome is rounded to the nearest 0.5 m
size class and written into its own same-sized-only layer
(`domes_r1_0`, `domes_r1_5`, ...), purely so each layer can be styled with
the one matching sphere size in QGIS.

```python
gpkg.unlink(missing_ok=True)     # drop stale class layers from prior runs
```

A GeoPackage is a single file holding multiple named layers; simply writing
new layers into an *existing* file would leave old layers behind from a
previous run (e.g. a `domes_r2_5` layer from back when a 2.5 m class still
existed) unless the whole file is deleted first — this line is why the tool
deletes and rebuilds the file from scratch every run rather than editing it
in place.

**Three "golden" self-checks by name.** The self-verification section ends
with three specific chapels checked by ID and name:

```python
for bid, label in [(30, "Chapel of Exodus"), (80, "Chapel of Peace"),
                   (181, "circular structure")]:
```

These are chapels independently known (from the site literature) to be
famously domed, used as a spot-check that the whole pipeline still gets the
"obvious" cases right. If the underlying footprint ID numbering were ever
regenerated from scratch, these three checks would need re-confirming
against whichever new IDs correspond to the same real buildings — nothing in
the code protects against silently checking the wrong building if that
numbering ever shifts.

**A hard-won lesson, twice.** Two edits earlier in this project's history are
worth calling out because they show the same class of bug caught two
different ways. First, the wall-clearance clamp above (a general fix). Then,
during actual visual review, a specific dome (chapel #150) was found
rasterizing a spike onto *open ground next to* its own building, because the
clearance clamp bounds the *radius* but doesn't guarantee the resulting disc
stays inside the building's actual (possibly irregular) outline. The fix
lives in `viewshed.py`'s `apply_dome_overlay`, not here: every dome cap is
now clipped to its own footprint polygon before being baked into the
ray-casting surface, regardless of what radius the CSV claims — a
belt-and-suspenders check layered on top of the upstream fit, rather than a
reason to distrust this script's math.

---

## 4. `viewshed.py` — the engine

This is the deliverable. It is worth slowing down here.

### 4.1 The `Scene` seam — the most important design choice

`HeightfieldScene` hides the line-of-sight test behind a small interface:

```python
class HeightfieldScene:
    """DEM-as-heightfield LOS scene. Geometry only — eye height is the
    driver's responsibility. Implements the Scene contract used by the
    viewshed driver and graph builder: surface_z, visible_mask, is_visible."""
```

The driver, the graph builder, and the volume code **never** touch DEM internals —
they only call `surface_z`, `visible_mask`, `is_visible`. Step 2 of the project
(window/door apertures) means replacing the heightfield with explicit 3D geometry
that rays can pass *through*. Because everything goes through the Scene contract,
that aperture-aware scene can drop in **without editing the driver or the graph
builder**.

**If the LOS test were inlined into the driver instead**, adding apertures would
mean surgery across the whole file — the per-observer loop, the combined-layer
logic, the graph builder, the volume sampler would all need rewriting, and each is
a chance to introduce a regression in code that is already validated. The seam
isolates the one part that is *supposed* to change.

**This promise has now been exercised.** Step 2's aperture-capable
scene exists — `HybridScene` in `scripts/scene3d.py` (§10) — and it
dropped in exactly as designed: `--mesh` wraps the constructed
`HeightfieldScene`, and the driver, graph builder, volume sampler, and
first-person renderer all run against it unchanged.

### 4.2 World → pixel, and why eye-relative coordinates

The CRS is UTM for Egypt, so coordinates are huge — eastings like `254210.8`,
northings like `2820958.0`. That is a problem for the GPU, which works in
**float32** (MPS has no good float64 path). A float32 has ~7 significant digits;
`2820958.0` already eats all of them, so adding a 0.2 m ray-march step to it is
below the representable precision — the increment would round away to nothing.

The fix is to do all ray-marching in coordinates **relative to the observer's
pixel**, where the numbers are small:

```python
col_eye, row_eye = self._pix(ex, ey)
ci, ri = int(round(col_eye)), int(round(row_eye))   # integer anchor (host, exact)
cf, rf = col_eye - ci, row_eye - ri                 # small fractional remainder
```

The big integer part `(ci, ri)` stays on the host as an exact `int`; only the
small fractional offsets and step increments ever become float32 tensors. **Done
naively** — feeding absolute UTM coordinates straight into float32 tensors — the
ray-march would stall or jitter, sampling the same cell repeatedly, and the
viewshed would be subtly and untraceably wrong. This is the kind of bug that never
throws an error; it just makes the physics quietly incorrect. This same trick
(exact integer anchor on the host, small remainder on the device) recurs
throughout the file, including in the new `first_hit` method below — see §4.6.

One small, easy-to-miss detail lives in `_pix`:

```python
def _pix(self, x, y):
    """World -> fractional cell-center pixel coords (col_f, row_f)."""
    return (x - self.x0) / self.a - 0.5, (y - self.y0) / self.e - 0.5
```

The `- 0.5` matters: a raster's coordinate grid technically addresses pixel
*corners*, but elevation values are recorded at pixel *centers* — without this
shift, every sampled elevation would be silently off by half a pixel.

### 4.3 The line-of-sight test — running-max elevation angle

This is the physics. For one eye and many targets, `visible_mask` decides
visibility with the classic viewshed insight: **a target is visible iff nothing
between it and the eye rises above the line of sight to it** — equivalently, iff
the target's elevation angle (as seen from the eye) is at least as high as the
maximum terrain elevation angle accumulated along the way. ("Elevation angle" here
just means "how far up or down you'd have to tilt your head to look at that
point" — a slope, mathematically rise-over-run.)

First, each target's own elevation angle:

```python
D = np.hypot(dx, dy)                 # horizontal distance, eye → target
ang_t = (tz - ez) / D                # target elevation angle (rise/run)
```

Then march outward from the eye in fixed steps, sampling the surface and keeping a
**running maximum** of the terrain's elevation angle:

```python
running = torch.full((len(tx),), -math.inf, device=self.device)
for k in range(1, k_max):
    d_k = k * self.step
    ...
    z = top * (1 - wr) + bot * wr    # bilinear surface height at this step
    ang = (z - ez) / d_k             # elevation angle of the terrain here
    ang = torch.where(valid, ang, torch.full_like(ang, -math.inf))
    running = torch.maximum(running, ang)   # accumulate the horizon
```

Finally, a target clears the accumulated horizon iff:

```python
# Order matters: the near-cell override (OR) must apply first so an
# adjacent nodata-free cell isn't lost to rounding, and the nodata veto
# (AND) must apply last so it can still exclude a target even when it
# happens to sit near-cell.
vis = (ang_t_t >= running - self.eps_ang).cpu().numpy()
vis |= near                      # target is the observer's own cell
vis &= np.isfinite(ang_t)        # target over nodata → not visible
```

The step size is deliberately sub-pixel:

```python
STEP = 0.2                # ray-march sample spacing (~1/2 pixel at 0.4 m)
```

**Why this formulation over the alternatives.** The naive "trace the segment, test
each cell for being above the eye→target line" works but recomputes the
eye-relative geometry per cell per target. The running-max elevation-angle
reduction is the standard trick because the *horizon only ever rises* as you walk
outward — so one number (`running`) per ray summarizes everything seen so far, and
the test is a single comparison. Marching at half-pixel steps avoids the classic
failure where a thin wall slips *between* two integer cell samples and a ray
"leaks" through a solid building. A coarser step (say one pixel) would let exactly
that happen at grazing angles; a much finer step just costs time for no fidelity
gain at 0.4 m resolution. Note `STEP` is a fixed number tuned to *this* DEM's 0.4 m
resolution, not derived from it — a future finer-resolution DEM would need this
constant reconsidered by hand, not automatically.

### 4.4 Device-agnostic — and the two MPS gotchas

The project must run unchanged on the remote CUDA box, this Apple-Silicon Mac
(MPS), and CPU-only machines:

```python
def select_device():
    if torch.cuda.is_available():
        return torch.device("cuda")
    if torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")
```

Honoring that convention forced two non-obvious implementation choices, both of
which would be more naturally written a different way on CUDA alone:

**(a) Step-by-step `torch.maximum` instead of `cummax`.** The mathematically tidy
way to get a running maximum is `torch.cummax` over the marched samples. **MPS does
not implement `cummax`.** So the running max is folded one step at a time with
`torch.maximum(running, ang)`. On CUDA `cummax` would be marginally cleaner; on the
Mac it simply would not run. The portable form wins.

**(b) Manual bilinear gather instead of `grid_sample`.** Interpolating the DEM at a
fractional pixel is exactly what `torch.nn.functional.grid_sample` is for. **MPS
does not support its border-padding mode**, so the four-neighbor bilinear lookup is
written by hand against a flattened DEM:

```python
base = r0c * self.W + c0c
z00 = self.dem_flat[base]
z01 = self.dem_flat[base + 1]
z10 = self.dem_flat[base + self.W]
z11 = self.dem_flat[base + self.W + 1]
...
top = z00 * (1 - wc) + z01 * wc
bot = z10 * (1 - wc) + z11 * wc
z   = top * (1 - wr) + bot * wr
```

**If we had reached for `grid_sample`**, the code would pass review, run on the
remote box, and crash (or worse, silently mis-pad at the borders) on the Mac where
most development happens. Doing the gather by hand keeps one code path for all
three devices. It also recurs three separate times in this file (once in
`surface_z`, once in `visible_mask`, once in `first_hit`) — a genuinely repeated
idiom rather than accidental duplication, since each site needs the same
defensive-indexing trick against a differently-shaped batch of points.

The targets are processed in chunks (`chunk=200_000`) so a full-site grid of
millions of cells does not have to materialize as tensors all at once — a memory
guard, not an algorithmic choice.

### 4.5 View constraints as a *post-mask*

Directional cones — sight radius, horizontal azimuth sector, vertical pitch
sector — are applied **after** the LOS pass, not inside it. ("Azimuth" is
compass direction — 0° north, 90° east, clockwise; "pitch" is up/down tilt;
"field of view," or FOV, is how wide the cone is.)

```python
def apply_view_constraints(mask, X, Y, Z, eye_xyz, azimuth, fov, radius,
                           pitch=0.0, vfov=180.0):
    ...
    if azimuth is not None and fov < 360:
        bearing = np.degrees(np.arctan2(X - ex, Y - ey)) % 360
        diff = (bearing - azimuth + 180) % 360 - 180
        mask[(mask == 1) & (np.abs(diff) > fov / 2)] = 0
```

`arctan2(X - ex, Y - ey)` (note x first) gives compass bearing — 0° = North,
90° = East, clockwise — matching how the site plan and a human describe
direction. This is the project's core compass convention and it recurs
independently, hand-written, in several other places in this codebase
(`compute_volume` in this same file, `observer_view.py`'s ray generation,
`blender/build_bagawat_scene.py`'s camera aiming) — see the Hall of Hacks
table below for the full list. The `(diff + 180) % 360 - 180` idiom folds an
azimuth difference into the range `[-180, 180]` specifically so a view-cone
centered near due north (where bearing wraps from 359° back to 0°) is
compared correctly instead of wrapping the wrong way.

**Why a post-mask.** The LOS physics (what is geometrically visible) is independent
of where the observer happens to be *looking*. Keeping the cone separate means the
expensive, validated ray-march never has to know about azimuth or pitch — the cone
is cheap NumPy on the result. **Baking the cone into the ray-march** would entangle
two unrelated concerns, complicate the GPU kernel, and — critically — the
visibility *graph* (which is omnidirectional by definition, since two people
either can or can't see each other regardless of which way either happens to
be facing) would then need a different code path from the rasters. As it
stands, graph and raster share one `visible_mask`; the cone is layered on top
only where it belongs.

### 4.6 First-hit: turning the engine into a renderer

Alongside `visible_mask` (which answers "is this *known point* visible?") the
Scene has a second method, `first_hit`, which answers a different question:
"looking in *this direction*, where does the ray first hit the surface?" This
is what turns the engine into a camera — the basis of every image in
`observer_view.py` (§7). Given a direction (as a compass bearing plus a
slope, i.e. an elevation angle expressed as rise-over-run) it marches the
same way `visible_mask` does, but instead of comparing against one known
target, it watches for the first step where the ground crosses the ray line
and records that distance.

Two design choices are worth calling out by name:

**The eye-anchoring and bilinear-gather tricks are copy-pasted from
`visible_mask`, on purpose, not factored into a shared helper.**
`visible_mask` is the kernel validated at 97–99% agreement against GRASS
r.viewshed and is imported by `compare_baseline.py`; the code says directly
that extracting a shared helper would put that already-validated path under
churn risk for the sake of avoiding roughly a dozen duplicated lines — not
worth it.

**Per-ray march limits are worked out in advance, on the host, before the
expensive device loop starts.** Without this, a ray pointed at empty sky
would march all the way to the far edge of the DEM's bounding box every
single time — about 13,000 tiny steps at 0.2 m spacing — even though it was
obviously never going to hit anything. Two shortcuts prevent that: `d_exit`
(the distance at which the ray leaves the DEM's rectangle entirely — beyond
that, every sample would be off the map) and `d_ceil` (once a rising ray has
climbed above the single highest point anywhere on the DEM, it can
mathematically never come back down to meet the surface again). Together
they let sky rays give up after a few hundred steps instead of thousands —
the difference between a panorama costing about the same as one ordinary
viewshed run, versus an order of magnitude more.

The exact crossing point is refined with a small linear interpolation
between the last "still above ground" sample and the first "now below
ground" one — without it, every depth image would show visible staircase
banding at the 0.2 m march-step resolution. The code is candid about this
refinement's one known weak spot: after a gap of invalid (nodata) samples,
the "last known good" reference point it interpolates from can be a step or
two stale, a small, accepted imprecision rather than a hidden one.

### 4.7 The visibility graph

`build_viewgraph` produces the actual deliverable: an observer↔observer symmetric
matrix plus observer→building edges. Each building edge records visibility two
ways:

```python
edges.append({
    "src_id": labels[i], "dst_type": "building", "dst_id": int(row["ID"]),
    "visible": bool(vis_cent[bi]),               # is the centroid visible?
    "visible_any_vertex": bool(vis_any[bi]),     # is ANY footprint vertex visible?
    ...
})
```

**Why both.** "Can I see this building's centroid (its geometric center
point)?" is strict — a building whose center is occluded by its own near
wall reads as invisible even though its corner is in plain view. "Can I see
*any* vertex (any corner of its outline)?" is the lenient, often more
meaningful question for a dense necropolis. Recording both lets the analysis
(and the mentors) choose the criterion rather than hard-coding one and
losing the other.

### 4.8 The 3D volume — and why it cost no API change

The optional `--volume` mode samples a voxel grid: for each ground column it stacks
several heights and tests visibility at every one. The notable thing is *how it
reuses the engine*:

```python
zabs = ground[:, :, None] + levels[None, None, :]
targets = np.column_stack([
    np.repeat(X[:, :, None], nlev, axis=2).ravel(),
    np.repeat(Y[:, :, None], nlev, axis=2).ravel(),
    zabs.ravel()])
vis = scene.visible_mask(eye_xyz, targets, chunk=chunk).reshape(nrows, ncols, nlev)
```

Because `visible_mask` already accepts an arbitrary per-target `z`, the volume is
just "the same call with more targets at different heights." **No change to
`visible_mask` or `compute_viewshed` signatures.** That mattered concretely:
`compare_baseline.py` imports `compute_viewshed`, so a signature change to add the
volume would have rippled into the comparison script. Designing `visible_mask`
around per-target `(x, y, z)` triples from the start meant a major feature (3D
volumes) landed as additive code — nothing that already worked had to change.

The volume's canonical output is a lightweight CSV of visible voxel centers; PLY,
NPY+JSON, LAS/LAZ, and a top-down QC PNG are optional. CSV-as-default is deliberate
— see `volume_convert.py` (§6).

### 4.9 The volume's *shape* — `--volume-format mesh`

Voxel points show *where* the volume is; the mesh output stores what it *is*
— its boundary surface, as a real triangle mesh with faces and edges, built
by the shared [volume_mesh.py](../scripts/volume_mesh.py) module (also used
by `volume_convert.py`, §6, which is why the module is deliberately kept
free of any `torch` import — see its own note on that). Two design points
carry the whole feature.

**Extraction happens in a clean, abstract grid first; the messy real-world
coordinates are bolted on in exactly one place at the very end.** The voxel
grid is regular in `(row, column, height-level)` terms, but each level is a
height *above that particular column's own local ground* — so turning it
into a true 3D shape means every mesh vertex needs a "drape onto the actual
terrain" step: `world height = ground(x, y) + level`. `index_to_world` is
the one function where index-space and world-space meet, including a subtle
piece of geometry: because rows count *downward* (south) while the
north/south world coordinate increases *upward* (north), converting between
the two silently flips which way is "outward" for the mesh's surface — get
that sign wrong and every triangle's face would point inward instead of
outward, invisible in most 3D viewers and disastrous for the volume
calculation below. The code works out the exact sign rule algebraically in
a comment and applies it in one place, rather than trusting each caller to
remember it.

**The default style, `blocky`, is chosen specifically because it makes the
result checkable.** `blocky` traces the *exact* boundary of the voxel set —
a blocky, Lego-like surface, but one whose enclosed volume can be computed
two independent ways and must agree *exactly*:

```python
check(abs(vol - target) <= max(1e-8 * target, 1e-6),
      "mesh volume == n_voxels x voxel volume",
      f"{vol:,.3f} vs {target:,.3f} m^3")
```

That check — the mesh's geometric volume, computed via a real 3D-geometry
formula, must equal the number of visible voxels times one voxel's volume —
is the strongest self-check in the whole codebase, because it has zero
wiggle room: any single face pointing the wrong way, or any vertex snapped
to the wrong spot, would break this identity immediately and obviously.
`--mesh-style smooth` (marching cubes, a standard algorithm from the
scientific-visualization world, borrowed here via the `scikit-image`
library) produces a nicer-looking, rounded surface for presentation instead
— but rounding off corners means its volume is only *approximately* right,
so its check degrades to "is the volume roughly in a sane range" rather than
"is it exact." This mirrors the same evidence-vs-presentation split used
for the engine snapshots versus Blender renders (§8).

**Two float-precision tricks worth knowing about**, both explained in the
code itself: vertex coordinates are centered (shifted so their average is
near zero) before computing volume, because raw UTM coordinates are large
enough (~2.8 million) that the volume-formula's arithmetic would otherwise
lose real precision to floating-point rounding — a shift that costs nothing,
since a solid shape's volume doesn't change if you slide the whole thing
sideways. And gaps in the ground data (nodata) are filled with the *nearest*
real elevation, not a flat minimum, so that mesh vertices near a data gap
land on plausible nearby terrain instead of an artificial cliff.

---

## 5. `compare_baseline.py` — validating against GRASS

**What it does.** Re-runs the engine on the *same surface* as the user's GRASS
`r.viewshed` baseline and compares cell-for-cell, sweeping observer height.

**The key methodological decision.** The comparison deliberately uses Task_2's own
DEM subset — the baseline's exact grid and datum — even though the production runs
use the regenerated 0.4 m surface:

```python
# Both sides use the SAME surface ... so the comparison isolates
# algorithm + observer height, not DEM differences.
```

**Why this matters.** If the engine and `r.viewshed` ran on *different* DEMs and
disagreed, you could not tell whether the difference came from the algorithm
(what we want to measure) or from resolution/datum/registration (confounds). By
forcing a shared surface, any disagreement is attributable to the one thing under
test. The result — 97–99% agreement — is the *expected, validating* outcome: both
treat buildings as solid today, so they *should* agree, confirming the engine
reproduces the established tool before apertures make it deliberately diverge.

**Reading GRASS's output convention.** `r.viewshed` writes a numeric viewing angle
into every *visible* cell and marks invisible cells as nodata — so "visible"
simply means "this pixel holds any real number at all":

```python
def binarize_baseline(path):
    """r.viewshed output -> visible bool mask (finite, non-nodata = visible)."""
```

This is a fact about GRASS's file format the code has to know, not something
you could work out from the numbers alone.

**Deriving "how many observers" from the data, not repeating a literal
`3` everywhere.** Earlier, every loop over observers used a hard-coded
`(1, 2, 3)` or `range(3)`, five separate times, checked against reality only
once. It's since been consolidated: the observer list is loaded first,
`n_obs = len(obs_xy)` is computed once, and every loop (baseline-file
reading, per-observer metrics, the figures, the low-agreement warning)
counts from that single number instead. **Why this matters**: with the old
version, adding or removing an observer point meant hunting down five
separate places that all needed to agree — miss one and the script would
either skip an observer's baseline entirely or throw a confusing index
error, rather than a clear, single, up-front check. (The one place that
still uses a literal `3` is the building-effect figure's panel layout — that
`3` is describing "this figure has three side-by-side panels," a drawing
decision, not a re-statement of the observer count, so it's left alone and
commented as such.)

**A real self-check, not just an assumed one.** The comparison's validity
depends on both sides seeing the same surface, *including* wherever data is
missing. That used to be asserted with a bare comment rather than checked:

```python
valid = np.ones(shape, dtype=bool)        # subset DEM has no nodata
```

It's now a genuine check against the DEM's actual nodata value, and `valid`
is derived from that reality rather than an assumption — so if a future
version of the subset DEM ever did contain real gaps, they would be
correctly excluded from the comparison (and flagged) instead of silently
being counted as "both sides agree this is hidden," which is what a
blindly-`True` mask would have done.

**A backdrop image that's now consistently contrast-stretched.** The QC
figures need a photographic backdrop to overlay the visibility masks on. Both
of this project's real orthophotos turn out to be single-band (grayscale,
not full color) — so the code path that runs on every actual invocation of
this script was, until this pass, the one branch that *skipped* the
brightness stretch the multi-band branch used, leaving the backdrop shown at
whatever raw brightness the sensor happened to record rather than a
consistent, readable contrast. It now uses the same 2nd/98th-percentile
stretch this project uses everywhere else an orthophoto is displayed
(`build_dome_layer.py`'s QC image, `observer_view.py`'s natural-mode drape).

**Two small "avoid an extra dependency" decisions**, in the same spirit as
the hand-written hillshade:

- The observer height was never recorded in the QGIS project that produced
  the baseline, so rather than guess, the script **sweeps** both `1.5 m` and
  `1.75 m` and reports the sensitivity — the data shows best agreement at
  1.75 m, a clue (not proof) about which height the baseline actually used.
  A finding, not an assumption. Whichever height is listed *first* in
  `--eye-heights` becomes the one used for every figure and saved mask file
  — worth knowing since reordering that list changes which run is
  "featured," not just which is listed first.
- Markdown report tables are built by a six-line helper (`df_to_md`) rather
  than adding the `tabulate` package as a dependency for one generated
  report.

**Regenerating the baseline instead of inheriting it.**
`run_grass_viewshed.py` produces a fresh `r.viewshed` run at native
0.4 m, and `crop_task2_04m.py` cuts the canonical site-wide rasters down
to the same ROI. The reason to bother: the supplied baseline is a 1.5 m
subset, and at that cell size a ~4 m chapel spans fewer than three
cells, so **both** methods under-represent building occlusion and the
comparison partly measures the grid rather than the algorithms. Running
both sides at 0.4 m separates those. It also isolates a residual that
would otherwise look like disagreement: the heightfield's building edges
are bilinearly interpolated ramps between cell centres while a mesh
stands a true vertical wall on the footprint boundary, and that
silhouette-quantisation term is 15.2% of visible ground at 1.5 m and
falls under 1% at 0.4 m. Scaling with cell size is what identifies it as
discretisation rather than a modelling disagreement.

**`compare_apertures.py` — and knowing what a metric cannot see.** It
sweeps solid / doorless / apertured meshes (and domes on-off) against
the same baseline. Its most useful output is a **negative** one,
reported rather than buried: the door effect here is *structurally*
zero, because `target_grid` pins every target's height once from the
with-buildings DEM, so a cell inside a footprint is always tested at
**roof** height in every variant, and a ground-level door can never flip
it. No number of extra observers or buildings fixes that. The report
says so in its own text and points at the visibility graph — which
evaluates centroid visibility at true interior floor height — as the
metric that does register doors. Counting ground cells only is a
correction for the same reason: 77–92% of bare-vs-doorless flips fall
*inside* footprints, biasing every mesh variant regardless of apertures.

---

## 6. `volume_convert.py` — post-processing without recompute

**What it does.** Promotes a saved volume CSV into PLY, a dense NPY occupancy grid,
per-elevation GeoTIFF slices, or the volume's boundary-surface mesh — *without
re-running the ray-casting*.

**Why it is a separate script.** Ray-casting a volume is the expensive step; file
format is a cheap afterthought. Keeping CSV as the canonical lightweight output and
converting offline means you never pay for the compute twice just to view the
result in CloudCompare versus QGIS.

It also carries a guard, because a dense voxel grid can explode in size:

```python
MAX_CELLS = 80_000_000
...
if not check(ncells <= MAX_CELLS, "voxel grid within size limit",
             f"{ncells:,} cells (limit {MAX_CELLS:,}; raise --voxel/--zstep)"):
    return None, None
```

**Otherwise** a careless fine spacing over the whole site would try to allocate a
multi-gigabyte array and run the machine out of memory with no warning. The guard
turns that into a clean, explained failure instead.

**The point-cloud writers used to be duplicated, and had quietly drifted
apart.** `viewshed.py` and this script each had their own byte-for-byte copy
of the plain PLY writer, and near-identical (but not quite matching) copies
of the LAS/LAZ point-cloud writer — this script's copy was missing a guard
the other one had for the empty-point-set case, which meant converting an
empty or fully-occluded volume to `.las` would crash with a raw Python error
instead of a clean, explained skip. Both writers now live once, in
[volume_mesh.py](../scripts/volume_mesh.py) (already the shared,
`torch`-free home for mesh code — a natural place for point-cloud writers
too), imported by both scripts. One copy means one place left to fix if
another gap like that ever turns up.

**Horizontal spacing is inferred from the data; vertical spacing is not, and
now the help text says so.** The x/y coordinates of a saved volume sit on a
clean, regular lattice (the engine's own `--voxel` sampling grid), so their
spacing can be measured directly from the data. The z coordinates, though,
are *absolute elevations* — real terrain heights, not evenly spaced sample
points — so there's no lattice to measure a spacing from; the code always
falls back to a fixed 1 meter bin unless `--zstep` is given explicitly. The
`--zstep` flag's help text used to claim it was "inferred from data if
omitted," directly contradicting what the code actually does — now fixed to
describe the real fallback behavior.

**The mesh path here is honestly labeled as an approximation.** Because the
CSV only stores absolute elevations, re-building a mesh from it means
snapping those elevations onto the fixed z-bins above — a "staircase"
version of the true shape, unlike the terrain-following mesh
`viewshed.py --volume-format mesh` produces directly from the still-available
ground data. The code repeats this caveat right at the point where the mesh
is actually built, not just once in the module's introductory comment.

---

## 7. `observer_view.py` — first-person audit renders

**What it does.** Renders "what the observer actually sees": equirectangular
panoramas ("equirectangular" is the same flat-unrolled projection a world
map uses — compass direction across, up/down angle vertically) and pinhole
perspective views ("pinhole" = an ordinary camera-like view with a fixed
forward direction and field of view, as opposed to a full 360° wraparound)
from each eye point, in three shadings (natural / depth / footprint-ids),
with the other observers drawn as visible-or-occluded markers.

**The kernel: `HeightfieldScene.first_hit`**, introduced in §4.6 above. A
render needs a fundamentally different query than the viewshed rasters do —
"given a *direction*, where does the ray first meet the surface?" rather
than "is *this specific point* visible?" — which is exactly what `first_hit`
computes.

**Shading is presentation, not physics** — the same separation as the
view-cone post-mask (§4.5). One march produces the raw hit field (where each
ray first touches the ground); natural/depth/ids are cheap post-passes over
that same result, so adding a new shading mode can never change what is
deemed visible, only how it's colored.

**Markers audit the graph.** Each other observer is projected into the image
and tested with the *same* eye-point `visible_mask` call `build_viewgraph`
uses — a filled marker in a snapshot *is* a `viewgraph_obs_obs` edge, drawn
exactly where a human can sanity-check it against the skyline in the image.

**Self-check: validate the new kernel against the validated one.** With no
ground truth (see the primer above), the load-bearing check cross-validates
physics against physics: every first-hit point is, by construction, the
first visible surface along its ray, so a sample of hits is handed back to
`visible_mask`, which must agree they are visible (≥98% required; 99.6–100%
in practice — the residual is silhouette-edge hits landing on the bilinear
wall ramp where the two independently-implemented kernels' final samples can
legitimately differ by up to one march step).

**A magic number that is actually load-bearing, not cosmetic:**

```python
ELEV_LIMIT = 85.0     # rays are parameterized by horizontal distance, which
                      # degenerates at +/-90 deg (tan -> inf); stay inside
```

Every ray in this file is described as a horizontal direction plus a slope
(rise over run — the tangent of the elevation angle). A ray pointed exactly
straight up or down has an *infinite* slope in that scheme — undefined
arithmetic, not just an edge case — so every elevation request is clamped
comfortably short of vertical. One related, low-impact rough edge: pinhole
perspective pixels whose direction happens to be nearly perfectly vertical
(only reachable with unusual pitch/field-of-view combinations, not the
defaults) fall back to a fixed "due north" horizontal direction rather than
their true one, to avoid dividing by zero — the elevation gets clipped
correctly, but that handful of pixels' horizontal aim is not quite exact. In
practice this never affects a normal run.

**The compass convention, restated because it's reimplemented by hand four
times.** East = `sin(azimuth)`, north = `cos(azimuth)` (see §4.5) appears
independently, hand-written, in `pano_rays`, `persp_basis`, `observer_marks`,
and `persp_mark_xy` in this file alone — plus again in `viewshed.py` and
`blender/build_bagawat_scene.py`. Every one of those must agree, or that one
view alone would render mirrored or rotated relative to all the others with
no error raised anywhere. The full list is in the Hall of Hacks table below.

**A footprint-ID lookup nudged forward by exactly half a pixel — looks like
arbitrary fudging, is actually a necessary correction:**

```python
nx = res["hx"][hit] + np.asarray(res["ux"])[hit] * scene.px * 0.5
ny = res["hy"][hit] + np.asarray(res["uy"])[hit] * scene.px * 0.5
```

A ray that hits a near-vertical DEM "wall" lands geometrically at the
*base* of that wall — right on the boundary between the wall and the
ground pixel just in front of it. Looked up directly in the separate
building-ID grid, that exact point can read as "ground" (ID 0) instead of
the building it just hit, purely due to which side of a pixel boundary it
landed on. Nudging the sample point forward by half a pixel, *along the
ray's own direction*, reliably lands it inside the building instead.

---

## 8. `export_scene_bundle.py` + `blender/` — external renderers

**What they do.** The exporter writes a self-contained `scene_bundle/`
(heightmap, orthophoto texture, observer positions, dome geometry, plus a
`meta.json` describing all of it) that `blender/build_bagawat_scene.py` turns
into a Cycles-rendered 3D scene, and that Unity's Terrain importer can
separately consume. Roles are deliberately tiered — **evidence**
(`observer_view.py`, the validated kernel), **communication** (Blender, real
lighting, a separate geometry/camera implementation), **experience** (Unity
walkthrough) — see [BLENDER.md](BLENDER.md) and [UNITY.md](UNITY.md).

Three conventions carry the whole export:

**A local, whole-meter coordinate origin.** Both Blender and Unity store
vertex positions in float32; raw UTM northings (~2.8 million) would quantize
down to whole centimeters or worse in that format. Every exported coordinate
is instead written as *(real UTM coordinate) minus (one fixed reference
point near the scene)*, so the numbers Blender and Unity actually work with
stay small and precise — the same underlying precision problem the ray-cast
engine solves with eye-relative coordinates (§4.2), solved the same way here.
One place, `bilinear` in this file, deliberately re-implements the DEM's
bilinear-sampling math standalone rather than spinning up a full
torch-backed `HeightfieldScene` just to look up a handful of observer/dome
elevations — not worth the GPU setup cost for so few lookups.

**Domes are geometry, never baked into the exported heightmap.** The
consumer (Blender or Unity) adds dome shapes itself, under its own flag, so
a bundle can never end up double-domed no matter which combination of export
and render flags gets used.

**One orientation formula, shared by every camera and the sun.** Both the
Blender camera and the sun light are aimed using a single function,
`euler_for(azimuth, pitch)`, and the sun is fixed at the same
compass-direction/altitude the project's QC hillshades use (315°/45°) so
engine images and Blender renders are lit consistently. The formula itself
quietly does two unit conversions at once: Blender's camera points straight
down by default, so a fixed 90° offset tips it to "looking at the horizon";
and Blender's rotation direction is the opposite handedness from this
project's clockwise compass bearings, hence a sign flip. Removing either
adjustment "to simplify the formula" would silently aim every camera and the
sun in the wrong direction — the kind of bug only visible by comparing
against a known-correct render. That's exactly what the first-run
cross-check in BLENDER.md is for: rendering the same eye position and
direction through both the engine and Blender and confirming the same
buildings show up in the same places validates the engine's ray generation
*and* Blender's camera math against each other, in one image pair.

**Two view-transform/coordinate lessons learned by actually testing on
Blender, not by inspection.** The Blender-side script resolves the bundle
folder to an absolute path before any Blender operation touches it — loading
an image via a relative path only works predictably once a scene has been
saved to a real location on disk, and a freshly-created, never-yet-saved
scene has no such location, so a relative path can silently resolve wrong
and later break if the scene is saved. And renders explicitly request
Blender's "Khronos PBR Neutral" color/contrast mode rather than its newer
default (called AgX) — real test renders showed AgX visibly washing out this
project's grayscale-orthophoto drape toward flat neutral gray, which PBR
Neutral does not do.

The bpy script imports nothing from the rest of this project (Blender ships
its own separate Python interpreter), so `scripts/` stays usable without
Blender installed, and the Blender script stays usable without the project's
own Python environment. One exception, added for §9 below: `import bpy` is
now wrapped in a `try/except ImportError` (falling back to `bpy = None`) so
the module's `build_parser()` — argument parsing only, no Blender calls —
can be imported from the plain repo venv too. Nothing that actually touches
`bpy` runs unless the module is genuinely executing inside Blender.

**The walkable variant, and why the DEM choice is a trap.**
`export_walkable_scene.py` + `blender/build_walkable_scene.py` build a
scene you can walk through at eye level, placing the **real chapel
meshes** rather than the extruded heightfield blocks — so doorways are
doorways and you can stand in one. The trap is which surface to use for
the ground: it must be the **bare** DEM, not the DEM-with-buildings.
Using the latter would put buildings in the terrain *and* place meshes
on top of them, so every chapel would sit on a plinth of its own
extruded footprint. Same reason `viewshed.py --mesh` needs
`--mesh-clear-ids`. `--step` decimates the terrain because a walkable
scene wants a frame rate, not 0.4 m fidelity — and that is safe here
precisely because this tier is presentation, never evidence.

---

## 9. `scripts/run_gui.py` — the browser form wrapper

**What it does.** A stdlib-only local web server that turns every script's
existing argparse flags into an HTML form — defaults pre-filled, `choices`
as dropdowns, `store_true` flags as checkboxes — and runs the constructed
command as a subprocess, streaming its output back to the page. Motivation:
several scripts here (`viewshed.py` most of all, with ~25 flags) accumulate
a long command line across repeated exploratory runs, and a single typo'd
flag name fails silently-ish (an unhelpful argparse error) or, worse, is a
valid-but-wrong flag that just runs with the wrong setting.

**The one design decision that matters: no separate schema.** The tempting
naive approach is a hand-written JSON/YAML file describing each script's
fields for the GUI to render. That immediately creates the same
"maintained twice" problem this project's own hygiene passes keep finding
elsewhere (README's `--zstep` help text drifting from the code, `write_ply`
duplicated between two scripts, `FLAT_TYPES` going dead) — the GUI's schema
would silently go stale the next time someone adds or renames a CLI flag
without remembering to update it separately. Instead, every one of the
eight target scripts got a small mechanical refactor: the existing
`p = argparse.ArgumentParser(...)` / `p.add_argument(...)` block (previously
inline in `main()`) now lives in its own `build_parser()` function that
returns `p` before anything parses or runs. `run_gui.py` imports each
module and calls `build_parser()` directly, then walks `parser._actions`
to read each flag's name, help text, default, type, and `choices` straight
from the same object `argparse --help` itself reads. There is exactly one
place flags are defined, for both interfaces.

**Reading argparse internals (`_actions`, `_StoreTrueAction`,
`_AppendAction`) instead of a public API.** argparse has no documented
introspection API for "give me every flag as data" — `_actions` is the
library's own internal implementation detail, technically private (leading
underscore). This project accepts that fragility deliberately rather than
hand-maintaining a schema: an argparse internal changing between Python
versions is a risk that surfaces immediately and loudly (an `AttributeError`
on GUI startup, not a silent mismatch), whereas a hand-copied schema drifting
from the real flags fails silently — someone submits the form, gets output
that quietly used a stale default. Loud-but-rare beats silent-but-common.

**Building the actual argv, not a shell string.** Submitted form values are
assembled into a Python list (`[sys.executable, script, "--flag", "value",
...]`) and passed to `subprocess.Popen` with `shell=False` (the default) —
never concatenated into a shell command string. This isn't a style
preference: it's what keeps arbitrary browser-submitted text from ever being
interpreted by a shell, which is the standard command-injection vector for
exactly this kind of "form builds a command" tool.

**One convention resolves every field uniformly: empty means "omit the
flag."** Rather than separately tracking which flags are truly optional
(`--radius`, default `None`, meaning "whole-DEM window") versus which have a
"real" default (`--eye-height`, `1.5`), every field follows one rule: a
blank/unchecked form field is left out of the constructed argv entirely, so
the script's own `argparse` default applies — identical to what leaving that
flag off a hand-typed command line does. Fields with a concrete default are
simply pre-filled with it, so submitting them unchanged just passes the same
value explicitly (harmless redundancy); fields whose default is `None` are
left blank by default, so omitting them reproduces "unset" exactly. No
special-casing needed per field.

**Repeatable flags (`--point`, `action="append"`) get a textarea, one
entry per line**, rather than trying to represent "repeat this flag N
times" in a single text input — each non-blank line becomes its own
`--point X Y` occurrence in the built command.

**The Blender script needed one more accommodation.** Unlike the other
seven scripts, `build_bagawat_scene.py` is meant to run under Blender's own
bundled Python (`blender -b -P script.py -- args`), not this project's venv
— and it `import bpy`s at module level, which fails immediately outside
Blender. Introspecting its flags the same way as everything else would
therefore crash on GUI startup. Fixed by wrapping that one import in
`try/except ImportError` (§8 above) — the module becomes importable from
the plain venv purely to read its parser, while every function that
actually touches `bpy` still only runs when Blender itself executes the
file. The GUI also prepends an extra, non-argparse "Blender executable"
field for this one tool, since the command needs `blender -b -P ... --`
rather than this venv's `python`.

**A live command preview, computed server-side.** The page shows the exact
command that will run, updated on every field edit — via a `/api/preview`
endpoint that calls the *same* `build_command()` function `/api/run` uses,
rather than re-implementing the argv-building logic in JavaScript. Two
implementations of "how to build the command" drifting apart would be
exactly the bug class this whole design was built to avoid.

---

## 10. `scripts/scene3d.py` + `make_test_building.py` — apertures

**The problem these solve.** The whole project exists because standard
viewshed tools treat buildings as solid blocks. The heightfield engine
(§4) shares one structural limit with them: it stores a single
elevation per (x, y) column, so it cannot represent a wall that is
solid, then open (a doorway), then solid again along the same vertical
line — there is only one z to test against. Real openings need real 3D
geometry.

**The representation: triangle soup from OBJ files.** Buildings with
openings are triangle meshes; everything else stays the validated
heightfield. Wavefront OBJ is the interchange format because Blender
reads/writes it natively — the same loader that takes the synthetic
test building will take the mentors' modeled chapels when they arrive.
The hand-written parser in `volume_mesh.py` (~30 lines: `v` and `f`
records, everything else skipped) keeps the project's no-new-
dependencies stance; no watertightness is required, because the
occlusion test is "does any triangle cut this segment," not an
inside/outside query.

**The composition rule (`HybridScene`).** Wraps an already-built
`HeightfieldScene` — composition, not subclassing, so the validated
kernel is never edited:

- `visible_mask` = heightfield answer AND no triangle cuts the
  eye→target segment. The mesh test only runs on the heightfield's
  survivors (AND doesn't care about order — testing fewer targets is
  pure savings).
- `first_hit` = elementwise minimum of the heightfield hit and the
  mesh hit. Both are in the same parameter: ray directions are
  (ux, uy, slope) with (ux, uy) a *unit horizontal* vector, so the
  raw ray-triangle parameter t **is** the horizontal distance — no
  conversion, and the heightfield hit doubles as the mesh search's
  cutoff (a mesh hit beyond it can never win the min).
- `surface_z` stays heightfield-only. Eye placement and the raster
  grid keep exactly their old semantics; a mesh-aware standing
  surface (observers on mesh roofs) is a noted deferred refinement.
- Grid attributes (`dem_np`, `H`, `W`, transform pieces…) forward to
  the base scene via `__getattr__`, so every raster/QC consumer is
  oblivious to the wrapper.

**The intersection kernel.** Batched two-sided Möller–Trumbore in
torch, on the same cuda→mps→cpu stack. Two idioms carry over from the
heightfield march: triangles are translated to *eye-relative*
coordinates in float64 on the host before the float32 upload (raw UTM
magnitudes would eat the precision), and work is chunked (rays ×
triangles capped per batch) so device memory stays bounded. Because
the ray origin is the zero vector in eye-relative coordinates, two of
the three classic Möller–Trumbore terms become per-triangle constants
computed once per chunk. Per-file bounding boxes let a query skip
whole buildings its rays cannot reach; a 2D grid or BVH stays the
profiling-gated next step if real chapel models ever make brute force
slow (CLAUDE.md's long-standing note).

**The many-observer path, and why it inverts those choices.**
`visible_mask_multi` answers a whole bundle of (eye, target) rays from
*different* eyes in one go — the shape the intentionality test needs,
where ~200 chapels each look at their ~40 neighbours. Doing it as a
loop over `visible_mask` is correct and was the first implementation;
it is also, measured, almost entirely overhead. Both halves were
therefore batched over observers, and each cost something to get
there:

- The **terrain march** generalises its integer cell anchor `(ci, ri)`
  and the observer height to per-ray tensors. That is exactly
  equivalent — the per-eye and batched forms were checked bit for bit
  over 7,823 rays, 0 differing — and runs ~179× faster, because the
  march is launch-bound: ~15 tiny device ops × ~300 steps, once per
  observer.
- The **mesh pass** (`segments_blocked_multi`, `_mt_min_t_multi`)
  cannot keep the eye-relative frame, since there is no single eye, so
  the tvec/qvec terms stop being per-triangle constants and cost ~1.7×
  the arithmetic per pair. It also drops the per-eye bounding-box cull,
  testing every ray against the whole site (~4.2× more pairs). Paying
  ~7× the arithmetic bought ~35× wall-clock, which is the measure of
  how overhead-dominated the loop was.

The order of that work matters more than either number. The march was
optimised first because profiling put it at 79% of a draw; afterwards
the mesh pass was 98.7% and the march 1.3%, on a draw that had barely
got faster. Re-profiling *after* a win, rather than trusting the
original split, is what turned a 1.1× into ~100×.

**The frame is keyed on geometry, not on the rays.** The batched mesh
pass translates to a frame local to the *triangles* (their bounding-box
centre), never to something ray-derived like the mean eye. With a
ray-derived anchor the frame moves when a caller casts only a subset,
so a memoising caller and an exhaustive one round differently on rays
that graze a triangle edge and disagree for no reason but float32.
That is not hypothetical — it was the first implementation, and the
cross-check caught it.

**One self-check had to be *removed* for hybrid scenes** — and the
reason is the feature working. `run_self_checks`' "raising the eye
never reduces the visible count" is a theorem for heightfields, but
with apertures it is simply false: raise the eye above the door head
and the through-the-door cells legitimately disappear. The check now
skips (with a `warn` in the transcript) when the scene has meshes.
Likewise, `observer_view.py`'s first_hit↔visible_mask cross-validation
had to start testing the ray's own 3D hit point rather than
`surface_z` at the hit's plan position — a mesh hit (mid-wall, dome)
floats above the heightfield ground, and the ground *under* it may be
legitimately occluded even though the hit itself is visible.

**`make_test_building.py` — the synthetic proving ground.** The mentor-
specified minimal case: an 8 m hollow cube (0.4 m walls — one DEM
pixel, chapel-like), a hemispherical dome, and a 1.2 × 2.2 m door in
the south wall, standing on a perfectly flat generated DEM at
site-like UTM coordinates. Flat ground + mesh-only building means (a)
no double occlusion by construction, (b) no datastore dependency, and
(c) every expected sightline is *analytic* — the self-checks in
`scene3d.py` assert exact outcomes (door ray passes, above-head ray
blocked by the header, dome blocks an over-the-walls segment, the
through-the-door first hit lands on the far inner wall at exactly
10 + 8 − 0.4 = 17.6 m), not eyeballed ones. A `building_solid.obj`
control (same geometry, no door) turns every demo into a before/after
pair: the inside observer sees 1,804 cells with the door, 324 (its own
interior) without.

---

## 11. The aperture pipeline — registry, extractors, wall builder

**First, the word.** "Aperture" is used here as an umbrella for every
modelled opening, and that is *not* how the excavation report uses it —
the report means a **light opening**, a window ("the chamber was lit by
means of apertures in the three walls"). The umbrella sense is baked
into the path and script names and is not worth renaming, but the prose
below prefers the precise terms: an **opening** is any registry row; a
**perforating opening** (door, window) passes through the wall; a
**recess** (niche, apse) is cut into a face and does not. `KINDS` in
`aperture_registry.py` is where that distinction is declared, and
`--openings {doors,perforating,all}` is where it becomes a build.

**The data problem.** No single document records the chapels'
openings. Three partial sources exist: the georeferenceable site plan
(door *locations* for ~131 labeled chapels, drawn as gaps in wall
linework), seven detailed CAD plans (measured door *widths*), and the
200-page excavation-report scan (the only *height* source — dimension
lines on plates, readable only by eye). The pipeline's design center
is therefore the **human-in-the-loop registry**:
`aperture_inventory.csv`, one row per opening, seeded by automation,
finished by hand, never overwritten by any script (the dome-inventory
rule — reruns emit `siteplan_candidates.csv` instead).

**Why "canonical wall index" needs a shared function.** A row anchors
its opening to wall N of the footprint — but raw footprint rings have
digitizing slivers (0.03 m edges), collinear splits (one wall drawn as
two segments) and densified circles, so a raw ring index is unstable.
`aperture_registry.canonical_walls()` (shared by seeder and builder —
one implementation, or the index means different things in different
scripts) simplifies, merges slivers and near-collinear edges, orients
CCW and starts at the lexicographically-lowest vertex. Each row also
stores the wall's outward azimuth and midpoint; `resolve_wall()`
verifies them at build time and re-matches by nearest midpoint (warn)
or refuses (check-fail) — a silently renumbered wall must never get a
hole cut on faith.

**The source that worked, and the one that didn't.** The plan was the
obvious place to look for doors and it turned out to be the wrong one:
its apparent wall-line gaps are dominated by registration artifacts at
footprint corners, and the one chapel we ground-truthed (180) has
*unbroken* linework across its real entrance. The report, meanwhile,
simply says so in words — "(212) A chapel of Type 1 which opens
south" — for 194 of 263 chapels. `read_report_directions.py` OCRs
Chapter VII with tesseract (psm 3, which keeps the centred `(NNN)`
headings on their own lines where psm 6 swallows them), splits into
per-chapel entries, and matches direction phrases anchored on
opens/entrance/faces so that a passing mention ("niches in the east,
south and north walls") cannot be mistaken for the entrance. Each row
keeps its quote and book page, so any number in the final report can
be traced to a sentence on a page.

Two things this bought beyond coverage. It **caught an error two
independent visual reads had made**: chapel 180 is the Church, whose
ring of plan circles is a *peristyle wrapping the whole building*, not
a portico marking the entrance facade — the report puts the entrance
at the south-west corner. And it produced a checkable distribution:
S 69, W 65, E 51, **zero north**, which is a real property of the site
(the string "opens north" appears once in 95 OCR'd pages, inside
"opens *south* and is at the north end"), not a regex blind spot.

**Georeferencing the site plan without georeferencing.** The PDF is a
print of the CAD, so it has no CRS — but its 131 chapel-number labels
double as control points: an affine fit from label anchors to
footprint centroids lands at ~1 m median residual. One global affine
isn't enough, though: the plan and the photogrammetric footprints are
independent drawings, so individual buildings sit a metre or three
off. A per-building translation search (maximize wall-linework
coverage over a ±3 m grid) fixes registration locally — that one step
took the door-candidate yield from 10 chapels to 76.

**Doors as gaps.** With no pen/layer separation in the print, the
detector reads a door the way a human reads a plan: a 0.5–2.5 m
*uncovered* stretch of an otherwise-drawn wall. Walls with under 50%
linework coverage yield nothing (an undrawn wall is not evidence of a
door), and circular buildings defeat the interior-gap test entirely —
both degrade to the same fallback, the per-chapel QC tile a human
reads anyway. Candidates are seeds marked `confidence=low`, not truth.

**Reading the rest of the report: features, paintings, plates.** The
same OCR'd chapters carry more than entrances, and each reader is a
separate script because each needs a different kind of scepticism.
`read_report_features.py` pulls interior features (niches, apses,
windows) with the wall they sit in, and reads **four page ranges**, not
one — chapels written up at length elsewhere have bare cross-references
in Ch. VII ("described above p. 77-86"), so their detail lives in the
monographic chapters, and each range restarts the numbering, so the
increasing-order heading filter is applied *per range*.
`read_report_paintings.py` pulls named painted scenes; its `--crossval`
compares the parsed running order against the report's own printed
order (17/17), which is real evidence precisely because Fakhry listing
his scenes does not depend on the headings the parser finds.
`extract_plate_figures.py` cuts measurable per-chapel tiles out of the
plates, and `extract_site_cad.py` / `extract_dxf_plans.py` take door
positions and widths off the CAD where the LW2 threshold-mark
convention makes them explicit.

**Curation is a separate step from extraction, on purpose.**
`apertures_from_report.py`, `curate_windows.py` and `curate_niches.py`
are what promote candidates into registry rows, and they are where the
modelling assumptions get made explicit rather than smuggled in. Two
worth knowing:

- **Niche dimensions are n = 1.** The report dimensions exactly *one*
  niche in 263 chapels and states a sill height exactly once. The
  `NICHE_*` constants therefore carry a comment saying plainly that
  they are a single observation standing in for a class. Sweep them
  before quoting any niche-driven result.
- **The depth clamp fires, and it was measured, not assumed.** A 0.15 m
  recess in a one-brick (0.17 m) wall would leave 0.02 m of fabric, and
  any rounding turns a decorative niche into a hole through the chapel.
  The clamp fired 34 times on the real registry; four more recesses
  were dropped outright on zero-thickness walls. Then verified
  directly — 25 real niches probed with a ray from 6 m outside, level
  with the niche: **0 let a ray through**.

**`measure_wall_fabric.py` — thickness is not cosmetic.** It sets how
deep an opening's reveal is, which is what clips an oblique sightline
through it, so a wrong thickness changes visibility rather than just
appearance. The script measures from the CAD plans and the report plate
plans and falls back to typology, recording which of the three each
chapel got (`thickness_source`) so the weak ones stay identifiable —
44 chapels have only a site-wide default, and only 5 are CAD-measured.
It writes `building_fabric_candidates.csv`, never the curated file.

**`check_regression.py` — the gate that makes refactoring safe.** The
mesh pipeline has many small shared functions (`canonical_walls`,
`opening_rect`, `built_thickness`), and the failure mode when one drifts
is not an exception, it is a slightly different hole in a wall that
nothing downstream notices. So the frozen 197 meshes are hashed, and
any change to the registry or the builder must leave them **byte
identical** or explain why. It is what let `opening_rect()` be lifted
out of the builder's inline block with confidence: same bytes, so the
sightline test and the mesh cannot have started disagreeing.

**Targets and envelopes.** `build_visual_targets.py` turns the registry
into the named things a ray can be *aimed at* (`target_inventory.csv`):
entrance-axis points, footprint centroids, and recess targets placed
inside the pocket at mid-depth. `aperture_envelope.py` runs the
question backwards — given one chapel, sweep standing positions around
it and report the envelope from which its interior is visible through
its own openings. The first feeds the statistical tests in §12; the
second is for looking at one building closely.

**The wall builder.** `wall_panel()` generalizes the synthetic
builder's axis-aligned panels: parametrized by distance along
arbitrary 2D endpoints, multiple holes per wall (sorted; inter-hole
strips + header/sill bands), sheared bases (bare-DEM per vertex,
sunk 0.3 m against daylight gaps) and tops (the dome layer's fitted
roof plane, evaluated per vertex so the mesh roofline matches the
QGIS extrusion). Thickness is real: inner faces offset along the
inward normal — extended half a thickness past both ends so corners
*overlap* rather than gap, which an any-hit occlusion test can't see
but a gap would leak light through — with jamb/header/sill reveal
faces, so oblique sightlines through a doorway are clipped by wall
depth (thesis-relevant: zero-thickness holes overstate angular
admittance). Circular or too-small footprints fall back to
zero-thickness single faces with a warn. The roof cap is a
hand-rolled ear-clip; dome caps come from `dome_inventory.csv` (they
must ride along: `--mesh-clear-ids` removes the heightfield block,
dome and all).

**`--self-test` is the contract.** It builds a synthetic square
building from one in-memory registry row, over an in-memory flat DEM,
and runs the same analytic sightline probes the synthetic-building
experiment validated (through-door passes; above-head, blank-wall
blocked; first hit on the far inner wall at exactly
10 + 10 − 0.4 m). Run it after any change to this pipeline — it
exercises registry semantics, wall geometry, and the hybrid engine in
one pass with zero real data.

---

## 12. The analysis tier — where the findings actually come from

Everything above builds a scene and answers "is this visible". This
tier asks questions *of* the site, and it is where every number in
`GSOC_WORK_PRODUCT.md` originates. Its scripts share a shape: each is a
driver that loads the registries, casts against a `HybridScene`, and
writes a report plus figures. What differs is the epistemics, and that
is what this section is about.

### 12.1 `test_entrance_azimuth.py` — constrain before you compute

The cheapest script in the tier and deliberately the first to run: it
loads no meshes and casts no rays. It asks whether the *compass
distribution* of entrances is explained by something other than the
other chapels, testing three rival explanations.

- **Solar.** Not "does it face east" — the sun's rising point sweeps an
  arc over the year, and `solar_arcs()` derives it from
  `cos(A) = sin(d)/cos(φ)` with declination at the solstices. At
  Kharga's φ = 25.44° that is 63.9°–116.1°. The null draws uniformly
  from inside the arc(s).
- **Uniform** over the 8 compass classes.
- **Downhill slope** — the practical explanation: on a slope the
  downhill side is the approach, so doors might simply face it.

Two decisions are worth copying. First, **no Rayleigh/V-test**: on four
spikes at 90° spacing the resultant reflects the 45° binning rather
than the archaeology, and it is the error a reviewer looks for first.
Second, the slope null is a **permutation** test rather than an
analytic one, because entrance directions are binned at 45° while
aspect is continuous, so how many classes fall inside a 45° window
depends on where the aspect sits — a closed-form chance level would be
subtly wrong. Shuffling the observed directions across chapels holds
both marginals fixed and breaks only the pairing.

All three reject. That is what makes §12.2 worth running: it means the
lopsided distribution is not already explained.

### 12.2 `test_intentionality.py` — a pre-registered experiment

The statistic is V: ordered chapel pairs where B's interior entrance-axis
point is visible from a standing position outside A's doorway. The
nulls permute door walls (N1), positions (N2), or both (N3).

**The module docstring is a pre-registration, and it is load-bearing.**
Statistic, α, correction and null definitions were fixed there before
the first run, and every subsequent change is appended as a dated
deviation rather than edited in. That is the only thing separating this
from tuning an analysis until it reports something. If you change what
a draw means, record it there — and bump `DRAW_VERSION`.

Four design points that generalise:

- **N1 reuses the observed directions rather than drawing walls
  uniformly.** A uniform draw would randomise the compass distribution
  too, and the test would reject because no chapel opens north — a fact
  §12.1 already established, and nothing to do with arrangement.
  Preserving the marginal is what makes the test about *which chapel
  got which direction*.
- **Per-draw seeding** is `(seed, stream_id, draw_index)`, not one
  running generator. A resumed run re-derives draw *k* exactly, so the
  checkpoint carries no RNG state.
- **Sequential stopping** (Besag–Clifford): a null halts at the h-th
  exceedance and reports `p = h/L`; one that goes the distance keeps
  `(1+l)/(1+n)`. Switching formulas *with the stopping reason* is what
  makes this exact rather than peeking.
- **`DRAW_VERSION` is in the checkpoint fingerprint.** The fingerprint
  covers configuration; it did not cover *semantics*, so a run resumed
  across a change in what a draw means would have spliced two
  experiments into one null distribution and looked fine doing it.

### 12.3 Two bugs this tier caught, and why they are instructive

Both were found by checks that existed for the purpose, and both are
recorded rather than quietly patched.

**The pair cache was unsound.** It memoised visibility on
`(chapel, wall, chapel, wall)`, on the argument that a sightline
crossing a third chapel C needs two openings while C has one. That
holds for a watertight solid. Chapels are modelled as **wall panels** —
open-topped, with corner gaps — so a ray can take one opening and leave
over a wall top. Cast exhaustively, 13 of 21,468 repeated keys
disagreed: 6.1e-4. The earlier check that licensed the cache compared
474 pairs and found none, which at that rate expects 0.29
counterexamples — it could not have found this. **A test that cannot
detect the thing it is testing for is worse than no test**, because it
converts an assumption into a citation.

**N2/N3 measured an artefact.** They permuted positions in plan only,
so a relocated chapel kept the elevation it came from. The necropolis
spans 38 m of relief; the median permuted chapel ended up 9.1 m off its
new ground against 3.6 m walls, and a floating chapel occludes nothing.
Null V inflated, and both nulls reported "arrangement does not matter"
for a purely mechanical reason. The control that proves the fix is
surgical: **N1 moves nothing and reproduced exactly across it.**

The general lesson is that a null hypothesis is a piece of *geometry*
here, not a formality. Getting it wrong produces a confident number
pointing the wrong way, and no statistical check downstream will catch
it — only looking at what the null scene physically is.

### 12.4 `test_feature_visibility.py` / `test_painting_visibility.py`

These turn interior features from *occluders* into *targets*: not "does
this niche block a ray" but "can this niche be seen, and from where".
Observers stand 1.5 m outside an opening on its axis; external
observers are other chapels' door stations within 60 m.

Two habits from these are worth carrying:

- **The target must agree with the mesh about where the opening is, to
  the millimetre.** Both call `opening_rect()` in `aperture_registry.py`
  — extracted from the wall builder's inline block precisely so a second
  implementation could not quietly disagree. Recess targets sit *inside*
  the pocket at mid-depth, mirroring the builder's face convention;
  placing them out in the room would make the pocket's own reveal
  irrelevant and count a niche as seen from angles that only ever saw
  the wall beside it.
- **`test_painting_visibility.py` computes a bound, not a measurement.**
  It asks how high a sightline can rise inside the chamber given the
  door head, and compares that to the dome's springing line. Every
  choice is set to favour visibility — empty chamber, observer free to
  stand anywhere on the axis, the registry's most generous head height
  — so when it still finds the painted surface out of reach (1.41 m
  against 2.49 m), the conclusion survives the fact that **no opening
  height in the registry is measured**. Building a result to be robust
  to your worst data is cheaper than fixing the data first.

### 12.5 `visible_fraction.py` — and why it is not a bisection

Every other visibility answer in the project is boolean. This one asks
what *fraction* of a surface is visible.

The obvious implementation is a binary search: cast to both ends of a
wall, and if they disagree, bisect for the boundary. It is wrong here,
and the reason is worth stating plainly — **visibility along a wall is
not monotone.** A chapel standing in front can shadow the *middle* of a
wall while both ends stay visible; several occluders give several
bands. Bisection between two disagreeing endpoints finds one boundary,
assumes it is the only one, and returns a confident wrong number. Worse,
when both endpoints *agree*, it reports 1.0 and never looks.

So the search seeds a coarse uniform sample first and refines only the
intervals whose endpoints disagree — Whitted-style adaptive
supersampling. With one boundary it degenerates to exactly the
bisection above; with k boundaries it finds all of them, *provided* no
shadow band falls entirely between two seeds. That proviso is a real
assumption, so `n_seed` is a documented argument, the residual is
returned rather than hidden (every call reports how many intervals were
unresolved, each worth at most `tol/2` of length), and the self-test
**asserts the failure mode** instead of pretending it away.

### 12.6 `data_provenance.py` — generated, because prose drifts

Walks all six registries and writes `docs/DATA_PROVENANCE.md` plus a
per-chapel CSV. It is generated rather than written because a hand-kept
provenance note is wrong the first time anyone edits a registry, and
wrong *silently*: a reader cannot tell a measured 0.86 m door from a
class default of the same number.

Two details worth copying:

- **The vocabularies are data, and unknown values fail loudly.**
  `TOKENS` (sources) and `GRADES` (confidence) are separate dicts —
  rendering a grade through the source table would file "med" under
  "what it rests on" and read as though a grade were a document — and
  `main` fails its self-check if any registry value is missing from
  either. A new token surfaces as an error rather than a blank cell.
- **The denominator comes from the footprints, not the registries.**
  The chapels missing from every registry are exactly what the report
  is for; counting only chapels that appear somewhere would divide the
  gaps by themselves and report full coverage. When the footprint layer
  is unreadable the caller falls back to the registry union and
  **warns**, because that fallback silently changes what the
  percentages are a share of.

The report's own headline is a naming problem it exists to correct:
`source_pos` records which *wall* an opening sits in, not where along
it. Only 3 of 469 openings have a sourced along-wall position. Anything
depending on finer placement is an artefact of a spacing rule — which
is how the "window frames a niche" result was caught and retracted.

---

## Hall of hacks: every non-obvious trick, in one table

Every place in the codebase that does something a newcomer might read as
arbitrary, gathered in one place with the reason and the failure mode.
"Explained in code?" means the source has a comment carrying the same
reasoning — most do; a few gaps are noted as opportunities for a future
pass, not live bugs.

| Where | What it does | Why | What breaks if done the "obvious" way |
|---|---|---|---|
| `viewshed.py`, `HeightfieldScene.visible_mask`/`first_hit` | Marches rays in coordinates relative to the eye's own pixel, not absolute UTM | float32 (all MPS offers) can't represent a 0.2 m step added to a ~2.8-million-magnitude coordinate | Rays would stall/jitter, sampling the same cell repeatedly; wrong answers with no error |
| same, both methods | Hand-rolled 4-corner bilinear lookup instead of `torch.nn.functional.grid_sample` | MPS has no kernel for `grid_sample`'s border-padding mode | Crashes (or silently mis-pads) on Apple Silicon only |
| `visible_mask` | `torch.maximum` in a step loop instead of `torch.cummax` | MPS has no `cummax` kernel | Crashes on Apple Silicon only |
| `HeightfieldScene._pix` | Subtracts 0.5 when converting world coords to pixel indices | Raster coordinates address pixel *corners*; elevations live at pixel *centers* | Every sampled elevation off by half a pixel, silently |
| `build_dem_with_buildings.py` | Sorts footprints ascending by height before rasterizing | `rasterize` burns in order, last wins | A short building could overwrite (hide) a taller overlapping one |
| `apply_view_constraints` / `compute_volume` (viewshed.py) | `arctan2(dx, dy)` — x first, not y first | Produces compass bearing (0=N, clockwise) instead of standard math angle | Directional cones would point the wrong way (rotated 90°, mirrored) |
| `viewshed.py`, `observer_view.py`, `blender/build_bagawat_scene.py` | East = sin(azimuth), north = cos(azimuth), reimplemented by hand in ~6 places | The project's shared compass convention has no single shared helper | Any one occurrence getting swapped would mirror/rotate just that one view relative to all the others |
| `first_hit` (viewshed.py) | Precomputes `d_exit`/`d_ceil` per ray before marching | Otherwise every sky ray marches the full DEM diagonal (~13k steps) for nothing | A panorama would cost ~10x more than it needs to |
| `first_hit` | Linear-interpolates the exact surface crossing instead of stopping at the march step | Removes visible 0.2 m staircase banding from depth images | Depth renders show quantization artifacts |
| `first_hit`/`visible_mask` idioms | Deliberately duplicated rather than shared | `visible_mask` is the r.viewshed-validated kernel; a shared refactor risks it | None today — the discipline is what prevents future risk |
| `observer_view.py` | `ELEV_LIMIT = 85.0` clamp on every elevation request | Rays are parameterized by slope = tan(elevation), infinite at ±90° | Vertical-look requests would produce NaN/inf ray directions |
| `observer_view.py`, `shade_ids` | Sample point nudged forward half a pixel along the ray before an ID lookup | Wall hits land geometrically on the ground/wall boundary pixel | Wall pixels would often read as "ground" instead of the building |
| `volume_mesh.py` | Doubled-integer vertex coordinates during mesh extraction | Makes vertex deduplication exact (no floating-point near-misses) | Shared corners could fail to merge, leaving tiny cracks in the mesh |
| `volume_mesh.py`, `blocky_mesh` | Fixed "corner0-to-corner2" diagonal rule when splitting quads into triangles | After the terrain warp, quads are no longer flat; an inconsistent diagonal breaks the exact-volume identity | The mesh-volume self-check would fail (or worse, silently drift) |
| `volume_mesh.py`, `index_to_world` | Flips mesh-face winding based on the sign of `dx * dy * dz` | Converting (row, col, level) to (x, y, z) swaps axes and negates one, which can invert "outward" | Every mesh face could point inward — usually invisible, and wrong for the volume calculation |
| `volume_mesh.py`, `signed_mesh_volume` | Centers vertices (subtracts their average) before computing volume | Raw UTM coordinates make the volume formula's internal numbers large enough to lose real floating-point precision | The exact-volume self-check could fail even for a genuinely correct mesh |
| `volume_mesh.py`, `fill_nan_nearest` | Fills missing ground data with the *nearest* real value, not a flat minimum | A flat-minimum fill would create a fake cliff right at the edge of any data gap | Mesh vertices near a data gap would warp toward an artificial cliff instead of plausible terrain |
| `build_dome_layer.py`, `detect_dome` | Brightness threshold computed per-building, not once for the whole site | Domes catch the sun; a single global cutoff would fail wherever lighting/shadow varies across the site | Some real domes missed, or shadowed non-domes falsely detected, depending on which part of the site |
| `build_dome_layer.py` | Wall-clearance clamp recomputed at the *detected* dome center, not the room's geometric center | The two points are usually different; clamping by the wrong one's clearance doesn't bound the real one | A dome could still render poking through its nearest wall |
| `build_dome_layer.py`, `roof_plane_z` | Fits a plane through the footprint's *corner* heights rather than sampling ground once at the center | Mirrors QGIS's actual vertex-based roof extrusion, which shears with sloped terrain | Domes on sloped-roof buildings would appear to float above or sink into the rendered roof |
| `build_dome_layer.py` | Sphere center sits 35% of its radius below the computed roofline | QGIS has no hemisphere/dome-cap symbol, only full spheres | A full sphere resting on the roof reads visually as a ball, not a dome |
| `build_dome_layer.py` | Deletes `domes.gpkg` before rewriting it | A GeoPackage holds multiple layers; writing new ones into an existing file leaves stale layers behind | Old, no-longer-valid size-class layers would linger and confuse QGIS |
| `viewshed.py`, `apply_dome_overlay` | Clips every dome cap to its own footprint polygon before baking it into the surface | A radius that looks fine at the center can still spill past an irregular footprint's real outline | A dome could raise a spike of "ground" on neighboring open terrain (a real bug this caught, chapel #150) |
| `compare_baseline.py`, `binarize_baseline` | "Visible" = any finite, non-nodata pixel value, ignoring the value itself | GRASS r.viewshed encodes "not visible" as nodata and stores an angle everywhere else | Treating a specific value range as "visible" would misread the baseline's own convention |
| `compare_baseline.py` | All observer-count loops now derive from `len(obs_xy)` instead of five independent literal `3`s | The old version could only be checked once and silently drift out of sync elsewhere | Adding/removing an observer would need five separate manual updates, easy to miss one |
| `export_scene_bundle.py`, `blender/build_bagawat_scene.py` | Whole-meter local coordinate origin, subtracted from every exported point | Blender/Unity hold vertices in float32, too imprecise for raw UTM magnitudes | Geometry would visibly jitter/quantize at the vertex level |
| `blender/build_bagawat_scene.py`, `euler_for` | `radians(90 + pitch)` and a negated azimuth | Converts this project's "0=horizontal, compass clockwise" convention into Blender's "0=straight down, counterclockwise" default | Every camera and the sun would aim in the wrong (often mirrored) direction |
| `blender/build_bagawat_scene.py` | Resolves `--bundle`/`--render-dir` to absolute paths before any Blender call | A relative path's meaning depends on the current scene's save location, which a fresh unsaved scene doesn't have | Textures/renders could silently break if the scene is later saved or moved |
| `blender/build_bagawat_scene.py` | Forces the "Khronos PBR Neutral" color view transform | Blender's newer default (AgX) visibly washed out this project's grayscale drape in real test renders | Renders would look duller/grayer than the source imagery actually is |
| `blender/build_bagawat_scene.py` | `ensure_nodes()` checks `datablock.node_tree is None`, never reads/writes `use_nodes` directly unless still required | `use_nodes` is deprecated for removal in Blender 6.0 (nodes become the only mode); even *reading* it emits a `DeprecationWarning`, not just setting it | Warnings on every run today; an `AttributeError` once the property is actually removed |
| `scripts/run_gui.py` | Reads each script's flags via `build_parser()` + argparse's private `_actions`, instead of a hand-written schema | One place defines each flag, for both the CLI and the GUI — a separate schema would drift the same way `--zstep`'s help text once did | The GUI could silently show/accept a flag that no longer matches what the script actually does |
| `scene3d.py`, `_mt_min_t` | Triangles translated to eye-relative float64 on host before the float32 upload | Same precision cliff as the heightfield march: float32 can't hold a metre-scale offset against a ~2.8e6 UTM coordinate | Intersection tests would quantize; hits jitter or vanish, silently |
| `scene3d.py`, `_mt_min_t` | Barycentric bounds accept a hair *outside* the triangle (`-1e-6`) | Adjacent triangles share edges exactly; testing strictly inside lets a ray thread the float-rounding crack between them | Occasional one-ray light leaks through solid walls, unreproducible across devices |
| `scene3d.py`, `segment_blocked` | Hits within 5 cm of the target don't count as blockers | A viewshed target can lie exactly ON a wall face; the surface it sits on isn't an occluder of itself | Every cell whose surface point touches a mesh wall would read "not visible" |
| `scene3d.py`, `HybridScene.first_hit` | Passes the heightfield's own hit as the mesh search cutoff | The final answer is min(heightfield, mesh) — a mesh hit farther than the heightfield's can never win | Nothing breaks; it's a free cull that keeps mesh cost proportional to what's actually in view |
| `scene3d.py`, `segments_blocked_multi` | Local frame is the *triangles'* bounding-box centre, never the mean eye or anything else ray-derived | A ray-derived anchor shifts when a caller casts only a subset, so a memoising and an exhaustive caller round differently on edge-grazing rays | Two callers disagree on a handful of pairs with neither being wrong — a bug that looks like a cache staleness bug and isn't |
| `scene3d.py`, `_mt_min_t_multi` | Deliberately drops the per-eye bounding-box cull and pays ~1.7× arithmetic per pair | The loop it replaces spent ~99% of its time in launch/compile/sync overhead, not arithmetic; ~7× the math bought ~35× the wall-clock | Reinstating the "optimisation" would make the many-observer path slower, not faster |
| `test_intentionality.py`, `PairCache` | Off by default, kept only behind a flag | Its key assumes no third chapel's door matters; cast exhaustively, 13 of 21,468 repeated four-tuples disagree (6.1e-4), because chapels are wall panels rather than watertight solids | Silent drift of a few pairs in V per draw — small, but in the direction of the statistic being tested |
| `viewshed.py`, `run_self_checks` | Eye-height monotonicity check skipped when the scene has meshes | Raising the eye above a door head legitimately *loses* the through-the-door cells — the "theorem" only holds for heightfields | A correct aperture run would FAIL its own self-check |
| `observer_view.py`, cross-validation | Tests the ray's own 3D hit point (`eye_z + slope·d`), not `surface_z` at the hit's plan position | A mesh hit (mid-wall, dome) floats above the heightfield ground; the ground under it may be legitimately occluded | The hybrid scene's renders would fail cross-validation at ~93% despite being correct |
| `aperture_registry.canonical_walls` | Ring simplification + sliver merge + collinear drop + lex-lowest start, in one shared function | "Wall index N" must mean the same wall in the seeder and the builder, across digitizing noise | A hole cut into the wrong wall with no error anywhere |
| `aperture_registry`, registry rows | Each row stores its wall's azimuth + midpoint redundantly | Detects wall renumbering after footprint edits or tolerance changes | A drifted index would silently anchor the door to a different wall |
| `extract_site_plan.py`, `best_local_shift` | Per-building ±3 m translation search after the global affine | The plan and the footprints are independent drawings; buildings sit metres off individually | Door-candidate yield drops ~7x (10 vs 76 chapels, measured) |
| `extract_site_plan.py`, `find_gaps` | Gaps only count on walls ≥50% covered by linework | An undrawn wall is absence of data, not evidence of a door | Every sparsely-drawn wall would sprout phantom door candidates |
| `build_aperture_walls.py`, inner faces | Inner wall panels extended half a thickness past both ends | Corner overlap is invisible to an any-hit test; a corner gap leaks light | Oblique rays could slip through wall corners from inside |
| `scene3d.py`, `_gather_reachable` | Concatenates all reachable meshes into ONE triangle array per query | The graph builder issues a `visible_mask` per building; per-file dispatch made that ~49k kernel launches whose upload overhead dwarfed the arithmetic | The visibility graph takes so long it is effectively unrunnable site-wide |
| `read_report_directions.py` | tesseract `--psm 3`, and headings accept `9)` as well as `(9)` | psm 6 merges the centred `(NNN)` heading into the body text and loses the number; OCR drops a bracket often enough to matter | Whole chapels silently vanish from the extraction (chapel 9 did) |
| `read_report_directions.py` | Direction regexes anchored on opens/entrance/faces/façade | Chapel entries name compass points for niches and adjacent walls constantly | Half the chapels would get a direction taken from a niche description |

A handful of smaller, lower-stakes magic numbers exist too (a `1e-9`
division-guard epsilon here, a `97`-element sampling stride there, a `0.1 m`
floor on a log-scaled color range) — each is a defensive constant with low
practical impact, called out at its point of use in the code but not
repeated in this table.

---

## Two cautionary tales that prove the design

### The `load_observers` return-shape break

The Scene seam, the additive `visible_mask` signature, the shared `check` helpers —
these all reduce coupling so that change is safe. But coupling that *does* exist
still bites. `load_observers` was changed to support id-based selection, returning
`(obs_list, has_id)` instead of a bare list:

```python
def load_observers(path, crs):
    """Return ([(oid, x, y), ...], has_id)..."""
```

`compare_baseline.py` still called it the old way and crashed at startup — the
mid-term-gating comparison was silently unrunnable until the caller was updated:

```python
obs_list, _ = load_observers(args.observers, crs)
obs_xy = [(x, y) for _, x, y in obs_list]
```

The lesson reinforces the architecture: the places that *do* share an interface
(the `Scene` contract, `visible_mask`'s target shape) were designed to absorb
change; the one place a helper's return *shape* changed without a guard is exactly
where the break happened. When extending a shared helper, update every importer in
the same pass — or, better, keep the contract additive the way `visible_mask` is.

### The `--from-inventory` flag exists for a reason — a fresh, real example

`build_dome_layer.py`'s own module docstring says it plainly: rerunning the
script *without* `--from-inventory` re-detects every dome from scratch,
discarding any hand edits sitting in `dome_inventory.csv`. During the
verification pass for this very documentation update, that exact thing
happened: a plain rerun (used only to confirm the script still worked after
an unrelated code-hygiene edit) silently overwrote two chapels' dome
positions that had been manually corrected in an earlier session (chapels
#52 and #242 — see PROGRESS.md's 2026-07-04 entries) — their `notes` field,
the one place the manual correction was recorded, came back empty, proof the
CSV had been regenerated from the photo/geometry detection rather than
preserving the edit.

It was caught only by cross-checking the freshly-detected dome/inradius
ratio split (79 ortho-detected / 38 fallback) against the number recorded in
PROGRESS.md from the last time this script's detection logic changed —
noticing the *shape* of the output matched a known-good state was what
prompted checking the two specific chapels known to have manual overrides,
rather than any error or warning firing. Recovery was possible only because
the exact correction (a westward center shift of a known distance) was
written down in the project's own history and could be reapplied and
re-verified against the same `roof_plane_z` math the tool itself uses.

**The lesson, stated plainly for the next person running this script:**
*never* run `build_dome_layer.py` without `--from-inventory` if
`dome_inventory.csv` contains hand edits you want to keep — including for
something as seemingly read-only as "just confirming it still runs." This is
also why the tool's design already separates "detect" from "rebuild outputs
from a possibly-hand-edited registry" into two distinct code paths (§3) —
the flag exists precisely because this failure mode was anticipated; it
still takes active discipline from whoever's hands are on the keyboard to
actually use it.

---

## Design principles, recapped

| Principle | Where it shows up | What it prevents |
|---|---|---|
| Validate inputs before trusting them | `sanity_checks.py`, every `check()` | Silent geospatial wrong-answers (CRS, datum, grid) |
| Hide the changing part behind a seam | `Scene` interface | Aperture support (step 2) requiring a rewrite |
| Keep APIs additive | `visible_mask` takes per-target `(x, y, z)` | A new feature breaking existing callers |
| Work in small numbers near the data | eye-relative pixel coords, local scene-bundle origin | float32 precision loss on huge UTM coords |
| One code path for all devices | `select_device`, manual bilinear, `torch.maximum` | MPS-unsupported ops (`cummax`, `grid_sample`) |
| Separate physics from presentation | view cone as a post-mask, shading as a post-pass | Entangling LOS with where the observer looks, or how the image looks |
| Self-verify instead of fit-to-labels | the self-checks in every script | Sign / handedness / indexing bugs, given no ground truth |
| Cross-validate new physics against proven physics | `observer_view.py`'s first-hit ↔ `visible_mask` check | A second ray-march path silently drifting from the validated one |
| Prefer an exact check over an approximate one | the blocky mesh's volume identity | A geometry bug hiding inside a "looks about right" tolerance |
| Don't recompute what you can convert | `volume_convert.py`, CSV default | Paying twice for expensive ray-casting |
| Stay dependency-light | NumPy hillshade, `df_to_md`, hand-written bilinear sampling | Heavy/platform-variable dependencies across two machines |
| Separate evidence from beauty | engine snapshots vs Blender/Unity tiers | Presentation renders slipping into the validation chain |
| A registry that can be hand-edited must have a "don't re-detect" mode — and it must actually get used | `build_dome_layer.py --from-inventory` | Losing manual corrections to a careless rerun (see the cautionary tale above) |
