# Code Walkthrough — El Bagawat Viewshed Engine

> A narrated tour of the viewshed half of LAMP: what each script does, *why* it
> is built the way it is, and what would break if it were built otherwise.
> Read [CLAUDE.md](../CLAUDE.md) for project scope and [PROGRESS.md](../PROGRESS.md)
> for the running history; this document is the "how and why" of the code itself.

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

## The pipeline

```
DEMs + footprint heights ──► build_dem_with_buildings.py ──► DEM-with-buildings
                                                                   (ray-cast surface)
   sanity_checks.py                                                     │
   (validates every asset is coherent before any analysis)             ▼
                                            viewshed.py ──►  viewsheds · visibility graph · 3D volume
                                                  │                     │
                            compare_baseline.py ──┘            volume_convert.py
                            (validate vs GRASS r.viewshed)     (CSV → PLY / NPY / GeoTIFF)
```

Five scripts. [sanity_checks.py](../scripts/sanity_checks.py) is the shared
foundation — the other four import its canonical asset paths and its
`check` / `warn` / `failures` self-check vocabulary.

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
nothing downstream would have complained. That single WARN is what justified
regenerating the surface locally (the next script).

The `check` / `warn` helpers are deliberately tiny and shared everywhere:

```python
def check(ok, label, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(f"{label}: {detail}")
    return ok
```

A `check` records a failure and lets the script decide whether to exit; a `warn`
is advisory and never fails the run. This is the project's whole testing idiom —
no test framework, just invariants printed inline so a mentor reading the console
output can audit the run.

---

## 2. `build_dem_with_buildings.py` — making the surface

**What it does.** Produces the canonical ray-casting surface: rasterize each
footprint's `Elevation` height onto the base-DEM grid, add it to the base DEM,
self-verify the written file, emit QC images.

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
structure is what blocks sight).

**Add only on valid cells; never invent nodata.**

```python
out = base.copy()
out[fp_mask & valid] += heights[fp_mask & valid]
```

A footprint sitting over a nodata hole in the terrain is left as nodata rather
than getting a height bolted onto garbage. **Otherwise** the ray-caster would
later read a fabricated elevation and cast against terrain that does not exist.

**Per-feature coverage audit.** A thin footprint can fall *between* pixel centers
and rasterize to nothing. The script rasterizes the IDs separately and checks
every footprint survived:

```python
burned_ids = set(np.unique(id_grid)) - {0}
missing = sorted(set(gdf["ID"].astype(int)) - burned_ids)
check(not missing, "every footprint covers >=1 pixel",
      f"missing IDs {missing} — consider --all-touched" if missing else
      f"{len(burned_ids)} features burned")
```

**Otherwise** a building could quietly disappear from the surface and you would
only notice when its viewshed shadow was missing — if ever.

**Self-verify by re-reading the written file.** Compression, dtype casts, and
georeferencing are all places a write can go wrong. So after writing, it re-opens
the file and asserts the round-trip is exact:

```python
check(float(np.abs(off).max()) == 0.0,
      "off-footprint pixels unchanged", f"max |diff| {np.abs(off).max()}")
check(float(on_err.max()) < 1e-3,
      "on-footprint pixels = base + height", f"max error {on_err.max():.2e} m")
```

**Why a pure-NumPy hillshade.** The QC images use a hand-written hillshade rather
than GDAL's:

```python
def hillshade(z, res, azimuth=315.0, altitude=45.0):
    """Plain NumPy hillshade (avoids a GDAL dependency)."""
```

GDAL-the-binary is a heavyweight, platform-variable dependency; the engine already
needs rasterio (which wraps GDAL for I/O) but not the GDAL CLI. Twelve lines of
NumPy keep the QC path dependency-light and identical across the Mac and the
remote box. This function is imported by `viewshed.py` too — written once, reused.

---

## 3. `viewshed.py` — the engine

This is the deliverable. It is worth slowing down here.

### 3.1 The `Scene` seam — the most important design choice

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

### 3.2 World → pixel, and why eye-relative coordinates

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
throws; it just makes the physics quietly incorrect.

### 3.3 The line-of-sight test — running-max elevation angle

This is the physics. For one eye and `N` targets, `visible_mask` decides
visibility with the classic viewshed insight: **a target is visible iff nothing
between it and the eye rises above the line of sight to it** — equivalently, iff
the target's elevation angle (as seen from the eye) is at least as high as the
maximum terrain elevation angle accumulated along the way.

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
outward — so one scalar (`running`) per ray summarizes everything seen so far, and
the test is a single comparison. Marching at half-pixel steps avoids the classic
DDA failure where a thin wall slips *between* two integer cell samples and a ray
"leaks" through a solid building. A coarser step (say one pixel) would let exactly
that happen at grazing angles; a much finer step just costs time for no fidelity
gain at 0.4 m resolution.

### 3.4 Device-agnostic — and the two MPS gotchas

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
which would be more naturally written a different way on CUDA:

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
three devices.

The targets are processed in chunks (`chunk=200_000`) so a full-site grid of
millions of cells does not have to materialize as tensors all at once — a memory
guard, not an algorithmic choice.

### 3.5 View constraints as a *post-mask*

Directional cones — sight radius, horizontal azimuth sector, vertical pitch
sector — are applied **after** the LOS pass, not inside it:

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
90° = East, clockwise — matching how the site plan and a human describe direction.

**Why a post-mask.** The LOS physics (what is geometrically visible) is independent
of where the observer happens to be *looking*. Keeping the cone separate means the
expensive, validated ray-march never has to know about azimuth or pitch — the cone
is cheap NumPy on the result. **Baking the cone into the ray-march** would entangle
two unrelated concerns, complicate the GPU kernel, and — critically — the
visibility *graph* (which is omnidirectional by definition) would then need a
different code path from the rasters. As it stands, graph and raster share one
`visible_mask`; the cone is layered on top only where it belongs.

### 3.6 The visibility graph

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

**Why both.** "Can I see this building's centroid?" is strict — a building whose
center is occluded by its own near wall reads as invisible even though its corner
is in plain view. "Can I see *any* vertex?" is the lenient, often more meaningful
question for a dense necropolis. Recording both lets the analysis (and the mentors)
choose the criterion rather than hard-coding one and losing the other.

### 3.7 The 3D volume — and why it cost no API change

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
around `(N, 3)` world targets from the start meant a major feature (3D volumes)
landed as additive code.

The volume's canonical output is a lightweight CSV of visible voxel centers; PLY,
NPY+JSON, and a top-down QC PNG are optional. CSV-as-default is deliberate — see
`volume_convert.py` below.

### 3.8 Self-checks instead of ground truth

With no measured visibility to fit, the engine validates against **physical
invariants** that any correct viewshed must satisfy:

```python
check(hi >= lo, "raising the eye never reduces visible count", ...)   # monotonicity
check(fwd == rev, "forward/reverse LOS agree on a sample cell")       # reciprocity
check(bool((obs == obs.T).all()), "observer-observer matrix symmetric")
```

These catch whole categories of bug a label set never would: a sign error in the
elevation angle breaks monotonicity; a coordinate-handedness error breaks
reciprocity; an indexing bug breaks symmetry. One check earned its keep in the
field — the "observer sees its surroundings" test originally demanded all four
neighbors be visible and **false-failed** on a building-adjacent observer whose own
wall legitimately occludes one side. It was relaxed to "≥1 of 8 neighbors visible,"
which still catches total blindness (a sign/index bug) but tolerates real walls:

```python
# a building-adjacent observer can legitimately have some sides occluded,
# so don't demand all of them.
check(nvis >= 1, "observer sees its immediate surroundings", f"{nvis}/8 neighbours")
```

That distinction matters because the mentors' forthcoming full observer set is
building-adjacent by design.

---

## 4. `compare_baseline.py` — validating against GRASS

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

**Two small dependency decisions** echo the project's keep-it-light philosophy:

- The observer height was never recorded in the QGIS project, so rather than guess,
  the script **sweeps** `[1.5, 1.75]` and reports the sensitivity — the data showed
  best agreement at 1.75 m, a clue the baseline used GRASS's default. A finding, not
  an assumption.
- Markdown tables are emitted by a six-line helper rather than pulling in the
  `tabulate` package:

  ```python
  def df_to_md(df):
      """Minimal GitHub-markdown table (avoids a tabulate dependency)."""
  ```

---

## 5. `volume_convert.py` — post-processing without recompute

**What it does.** Promotes a saved volume CSV into PLY, a dense NPY occupancy grid,
or per-elevation GeoTIFF slices — *without re-running the ray-casting*.

**Why it is a separate script.** Ray-casting a volume is the expensive step; file
format is a cheap afterthought. Keeping CSV as the canonical lightweight output and
converting offline means you never pay for the compute twice just to view the
result in CloudCompare versus QGIS:

```
The viewshed engine writes the volume as a lightweight CSV ... This converts that
CSV — without recomputing the ray-casting — into [PLY / NPY / GeoTIFF slices].
```

It also carries a guard, because a dense voxel grid can explode:

```python
MAX_CELLS = 80_000_000
...
if not check(ncells <= MAX_CELLS, "voxel grid within size limit",
             f"{ncells:,} cells (limit {MAX_CELLS:,}; raise --voxel/--zstep)"):
    return None, None
```

**Otherwise** a careless `--voxel 0.1` over the whole site would try to allocate a
multi-gigabyte array and OOM the machine with no warning. The guard turns that into
a clean, explained failure.

---

## A cautionary tale that proves the design

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

---

## Design principles, recapped

| Principle | Where it shows up | What it prevents |
|---|---|---|
| Validate inputs before trusting them | `sanity_checks.py`, every `check()` | Silent geospatial wrong-answers (CRS, datum, grid) |
| Hide the changing part behind a seam | `Scene` interface | Aperture support (step 2) requiring a rewrite |
| Keep APIs additive | `visible_mask` takes `(N,3)` targets | A new feature breaking existing callers |
| Work in small numbers near the data | eye-relative pixel coords | float32 precision loss on huge UTM coords |
| One code path for all devices | `select_device`, manual bilinear, `torch.maximum` | MPS-unsupported ops (`cummax`, `grid_sample`) |
| Separate physics from presentation | view cone as a post-mask | Entangling LOS with where the observer looks |
| Self-verify instead of fit-to-labels | the 5 baked-in checks | Sign / handedness / indexing bugs, given no ground truth |
| Don't recompute what you can convert | `volume_convert.py`, CSV default | Paying twice for expensive ray-casting |
| Stay dependency-light | NumPy hillshade, `df_to_md` | Heavy/platform-variable deps across two machines |
