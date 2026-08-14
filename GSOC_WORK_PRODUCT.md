# Embodied Reconstructions of El Bagawat — work product

**GSoC 2026 · HumanAI · Late Antiquity Modeling Project (LAMP)**
Contributor: Romit Basak (basak.r@northeastern.edu)
Mentors: Camille Leon Angelo (U. Alabama), Joshua Silver (KIT)
Checkpoint date: 2026-08-14

> This is the viewshed/visibility half of the LAMP proposal. Path and
> movement analysis (cost surfaces, Monte Carlo path ensembles) is a
> second contributor's deliverable and is deliberately absent here.

---

## 1. The problem

The Necropolis of El Bagawat (Kharga Oasis, Egypt) is a late-antique
cemetery of ~263 mud-brick funerary chapels. The question the project
exists to answer is what the site was *legible* as: who could see what,
from where.

Standard visibility tools cannot answer it. GRASS `r.viewshed` and 2D
space-syntax VGA are planimetric — they treat every building as a
solid, opaque block. That erases the thing that makes the site
readable: chapels have **doorways and windows**, and sight passes
*through* and *between* them. A tool that models a chapel as a filled
rectangle cannot represent a doorway at all.

**The deliverable is a corrected, aperture-aware visibility graph** —
built by casting real 3D rays through a scene in which buildings have
height and explicitly modelled openings.

There is no ground truth. The site is ~1,700 years old and no measured
visibility data exists to fit against, so the project is built for
**auditability** instead of accuracy-against-labels: deterministic
ray-casting over learned models, analytic self-checks, frozen
regression baselines, and every result reproducible from DEMs +
footprints + an aperture registry.

---

## 2. What was built

### Step 1 — the 3D ray-casting engine (`scripts/viewshed.py`, `scene3d.py`)

A true 3D line-of-sight engine over the 0.4 m DEM, with buildings
derived from the **height differential between two DEMs**
(DEM-with-buildings − bare earth), not from assumed constants.

- `HeightfieldScene` — ray-marched LOS over the terrain surface.
- `HybridScene` — heightfield **composed with** per-building triangle
  meshes, so a wall can be solid, then open, then solid again along
  one vertical line. A heightfield stores one elevation per column and
  physically cannot represent that; the mesh layer is what makes
  apertures expressible.
- Batched two-sided Möller–Trumbore intersection in torch,
  device-agnostic (CUDA → MPS → CPU), no mesh library.
- Beyond the proposal ask: vertical view cone (`--pitch`/`--vfov`), 3D
  visibility **volumes**, adjustable eye height, dome caps, and
  first-person observer snapshots (`observer_view.py`) rendered by the
  same kernel as the viewsheds — the audit-grade "what the observer
  actually sees".

Observer eye height defaults to **1.5 m**, reflecting skeletal estimates
for late-antique Egyptian populations rather than the 1.75 m GIS default;
both values are swept as a sensitivity.

### Step 2 — apertures (`250_Apertures/`, `build_aperture_walls.py`)

The registry is **469 openings across 202 chapels**: 197 doors, 93
windows, 172 niches, 7 apses. Sourcing was the hard part, and the
answer is that no single document gives both position and height:

| source | gives | status |
|---|---|---|
| Excavation report Ch. VII (OCR) | entrance **direction** in words | 186/263 chapels, validated 6/6 by hand |
| Report plates (200 scans) | opening **heights** | the only possible height source — **not yet read**; every height in the model is a class default |
| CAD/DXF plots (7) | door **widths** | LW2 threshold-mark convention |
| Site plan | — | positional data unreliable; see below |

The site plan could easily be mistaken for the primary door source,
given its visible wall gaps. Those gaps, however, are plan-vs-footprint
registration artifacts at corners. Chapel 180 illustrates the problem:
its entrance, recorded in the excavation report as a south-west opening
(p.143), appears as unbroken linework on the plan with no gap to suggest
a doorway. The plan's tiles remain useful for checking wall attributions
against the footprint geometry, but they cannot supply door positions.

Extraction scripts **never** write the registry; they emit
`*_candidates.csv` and a human confirms. Heights and confirmations are
human-in-the-loop by design.

### Step 3 — the comparison (`compare_baseline.py`, `compare_apertures.py`)

Three layers, in increasing order of what they prove:

1. **Engine vs GRASS `r.viewshed`, both solid** — 97–99% cell
   agreement. High agreement is the *validating* result here, as
   expected: the engine correctly reproduces an established tool on the
   common case.
2. **ROI raster sweep with apertures** — and an honest null: the door
   effect is **structurally zero** in this sweep, because `target_grid`
   pins every target at *roof* height, so a ground-level door can never
   flip it. Documented in the report rather than buried; more observers
   would not fix it.
3. **Site-wide visibility graph, graded** — the metric that can carry
   the result, because it evaluates centroid visibility at true
   interior floor height.

---

## 3. Findings

### 3.1 Doors and windows do different jobs

Site-wide 0.4 m graded run, 202 meshed chapels, matched building set at
every step (789 building–observer pairs):

| variant | ground cells | Δ | centroid-visible pairs |
|---|---|---|---|
| solid control | 36,520 | — | 8 |
| + doors | 36,657 | +137 | **11 (+37.5%)** |
| + windows | 37,858 | +1,201 | 11 (+0) |
| + niches, apses | 37,858 | **+0** | 11 (+0) |

Doors buy few ground cells but are the *only* opening that puts a
building's interior centre in view. Windows buy ~9× more ground and not
one additional interior. The geometry demands it: a window sill sits
above standing eye height, so a sightline through it rises and lands
high on the far wall, while a door spans floor level.

Niches and apses change nothing on any metric, exactly: 179 recesses,
zero perforations, which is the recess gate validating itself on real
data.

### 3.2 The entrance arrangement is not accidental

A pre-registered Monte Carlo test (`test_intentionality.py`, α = 0.01,
Holm-corrected, 999 draws) counts ordered chapel pairs where one
chapel's interior is visible from a standing position outside
another's doorway.

**Observed V = 377.** Against three nulls:

| null | what it permutes | null median | effect | p | p_holm |
|---|---|---|---|---|---|
| N1 | which wall holds the door | 270 | +3.57 IQR | 0.001 | 0.003 |
| N2 | chapel positions | 252 | +3.79 IQR | 0.001 | 0.003 |
| N3 | both | 264 | +3.32 IQR | 0.001 | 0.003 |

All three reject. **Zero of 999 draws reached the observed value** in
any null — V = 377 exceeds the *maximum* of ~1,000 random
rearrangements by 22–41 pairs, at 4.4–5.1 SD. p = 0.001 represents the
resolution floor of 999 draws rather than a narrow margin.

N1 is the headline: it holds every chapel exactly where it stands and
permutes only *which wall carries the door*, reusing the directions
that actually occur. That last detail matters, because drawing walls
uniformly would have randomised the compass distribution too, causing
the test to reject for that reason rather than for arrangement.

### 3.3 Entrance directions are neither solar nor arbitrary

A separate, cheaper test (`test_entrance_azimuth.py`, n = 194)
constrains the explanation space before any ray is cast:

- **S 38.1%, W 34.0%, E 26.8% — and N/NE/NW exactly 0.**
- Solar nulls (sunrise arc, sunrise+sunset) **rejected in the
  strongest possible way**: they assign probability zero to south,
  which is the single largest class.
- Uniform-over-8-classes **rejected** (G = 366.6, p = 3.4e-75).
- Ground slope does **not** explain it either: entrances sit a median
  71.8° from straight downhill, and only 32.5% fall within 45° of
  downhill against 30.2% expected (p = 0.261).

The compass distribution is therefore unexplained by solar geometry,
chance, or terrain. The explanation, demonstrated in §3.2, is that the
doors are arranged with respect to *each other*.

**No chapel opens north** is a real, hand-verified property of the
data, confirmed against the silent entries as well as the recorded ones.

### 3.4 The frescoes were never meant to be seen from outside

27 named painted scenes were extracted from the two painted chapels
(17 in chapel 30, 10 in chapel 80), with the parser's running order
matching the report's own printed order **17/17**, which is independent
evidence: Fakhry's listing of his scenes does not depend on the
headings the parser finds.

The paintings are **on the dome**. Measured rather than asserted: from
the standing station outside chapel 30's door, the highest visible
interior point reaches only **1.41 m above the floor**, falling 1.08 m
short of the 2.49 m springing line; the bound is deliberately generous
(empty chamber, observer free to stand anywhere on the axis). The door
head caps how steeply a sightline can rise once inside. The entire
painted surface is invisible from outside, and Fakhry says the same in
words: chapel 80 "seems to have been always accessible to visitors who
came to look at the paintings of the dome."

### 3.5 Interior features

Of **175 interior features on 79 chapels**: 44 (25%) visible from their
own doorway, 26 (15%) from another chapel, 121 (69%) from nowhere
tested. Apses do worst: 2 of 7 from their own door, **0 from any other
chapel**.

Where the report explicitly places a niche in the wall *facing the
entrance* (24 chapels), **55.6% of niches are visible from the chapel's
own doorway against 16.7% elsewhere**, a 3.3× difference recovered
from geometry alone, on wall attributions that are evidenced.

### 3.6 Chapels without a recorded doorway were not sealed

This was tested because it is a reasonable hypothesis: 66 chapels have
no entrance in the data, and mausolea genuinely can be sealed. The report
rules it out: bolts imply repeated opening ("the places of their bolts
are still preserved in many"), thresholds and brick door jambs are
general features, and no chapel anywhere is described as entrance-less.
The sealing applies one level down: the **burial shaft** in the floor,
which Fakhry excluded from his plans by choice. 46 of the 66 are never
described in the report at all, appearing in contiguous ID runs that
suggest a reporting gap rather than a distinct architectural class.

---

## 4. How it is validated

No ground truth means validation is structural:

- **Analytic self-checks on synthetic geometry** — a cube + dome + door
  with known answers: through-door passes, above-head blocked,
  blank-wall blocked, exact first-hit distances, reciprocity.
- **Frozen regression** — 197 meshes hashed; refactors must leave them
  byte-identical (`check_regression.py`).
- **Cross-validation between independent implementations** — e.g.
  `observer_view.py`'s first-person renders cross-checked ≥98% against
  `visible_mask`.
- **Equivalence gates on every optimisation** — the batched ray cast
  was proven bit-identical to the per-eye path (0 of 7,823 terrain
  rays; 0 of 46,122 mesh rays over 6 draws) *before* being used.
- **Pre-registration** — the intentionality test's statistic, α,
  correction and null definitions were fixed in the module docstring
  before the first run, with every subsequent deviation recorded there
  as a dated entry rather than quietly applied.

**Two of those checks caught real errors**, which is the argument for
them:

- The pair-visibility memo assumed no third chapel's door mattered.
  Cast exhaustively, 13 of 21,468 repeated keys disagreed (6.1e-4) —
  chapels are wall *panels*, not watertight solids, so a sightline can
  take one opening and leave over a wall top. An earlier check of 474
  pairs had licensed the memo; at that rate it expected 0.29
  counterexamples and could not have found this. The memo is now off by
  default.
- N1/N2/N3 originally permuted chapel positions **in plan only**, so a
  moved chapel kept its old elevation. The site spans 38 m of relief;
  the median permuted chapel floated **9.1 m** against 3.6 m walls, and
  a floating chapel occludes nothing. N2/N3 reported "arrangement
  doesn't matter" (p 0.83, 1.0) for a purely mechanical reason. After
  re-datuming, effects went from −1.5/−1.9 to **+3.8/+3.3**. N1 — which
  moves nothing — reproduced exactly across the fix, which is the
  control proving the correction touched only what it should.

---

## 5. Known limitations

Stated because the report is stronger for naming them. A full
row-by-row provenance audit — which document supplied each fact, and
which values are defaults standing in for evidence — is generated at
[`docs/DATA_PROVENANCE.md`](docs/DATA_PROVENANCE.md), with a per-chapel
table beside it. Two figures from it set the frame for everything
below: **466 of 469 openings are placed by a spacing rule** rather than
a source, and **466 of 469 carry class-default dimensions**.

| limitation | consequence |
|---|---|
| **3 observers** (`Marks_Brief2`); full point set still pending from mentors | every site-wide number is a 3-point sample, not the finished graph |
| **Chapel 80 (Chapel of Peace) absent** — its description is in Ch. V, and the parser reads Ch. VII | the most-studied chapel on the site is missing from the model |
| **Per-point floor datum** — bare DEM varies up to 4.85 m under one footprint (median 0.79 m) | comparable to a niche's whole height; a live confound in §3.5 |
| **Door position along a wall is a spacing rule**, not a source | only wall *attribution* is evidenced; nothing finer is claimable |
| **No opening height was read off the plates** — all 197 door heads sit at a default 2.10 m | height-sensitive results rest on assumption; §3.4's fresco bound is built to survive it by using the *most favourable* head height and still finding the dome out of sight |
| **Niche dimensions are n = 1** — the report dimensions exactly one niche in 263 chapels | sweep before quoting any niche-driven result |
| **`--domes` experimental**, pending mentor visual review | off by default; the validated baseline is unaffected |
| **No distance attenuation** — an unbounded ray counts a distant cell as visible | geometrically true, perceptually overstated; specified and deferred |

---

## 6. What comes next

In priority order for the extension runway (Aug 24 – Nov 2):

1. **The full observer point set** — mentor-dependent, with lead time;
   it gates every site-wide number above.
2. **Chapel 80** — a parse-range fix on pages already OCR'd.
3. **Per-chapel floor datum** behind a flag, with per-point kept as
   default so the frozen baseline reproduces.
4. **Distance-based visual obscurity** — acuity/contrast falloff,
   reported alongside raw visibility.
5. **GNN-over-VGA** — explicit stretch, and only meaningful once it can
   consume the *corrected* graph.

BVH acceleration, listed as a contingency in the original plan, is
**no longer needed**: the site totals 7,376 triangles and batching
removed the bottleneck (a draw went 104 s → 0.65 s, ~100×).

---

## 7. Reproducing this

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv -r requirements.txt

.venv/bin/python scripts/sanity_checks.py          # validate inputs first
.venv/bin/python scripts/scene3d.py --self-test    # analytic geometry checks
.venv/bin/python scripts/check_regression.py --only meshes
.venv/bin/python scripts/test_intentionality.py --n-draws 999 --nulls N1 N2 N3
```

- [`docs/DATA_PROVENANCE.md`](docs/DATA_PROVENANCE.md) — what every
  modelled fact rests on, and the nine documented gaps where nothing
  does (generated by `scripts/data_provenance.py`)
- [`README.md`](README.md) — flag reference for every script
- [`docs/CODE_WALKTHROUGH.md`](docs/CODE_WALKTHROUGH.md) — narrated
  design tour: what each script does, why, and what breaks otherwise
- [`PROGRESS.md`](PROGRESS.md) — dated running record, newest first
- [`CLAUDE.md`](CLAUDE.md) — scope, data assets, conventions
