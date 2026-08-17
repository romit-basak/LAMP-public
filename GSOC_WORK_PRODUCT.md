# Embodied Reconstructions of El Bagawat — GSoC 2026 final work product

**Google Summer of Code 2026 · HumanAI Foundation · Late Antiquity
Modeling Project (LAMP)**

| | |
|---|---|
| **Contributor** | Romit Basak — basak.r@northeastern.edu · [github.com/romit-basak](https://github.com/romit-basak) |
| **Mentors** | Camille Leon Angelo (University of Alabama), Joshua Silver (KIT) |
| **Project** | True 3D ray-cast visibility analysis with building apertures |
| **Code** | [`scripts/`](scripts/) — 39 Python modules, ~15.9k lines |
| **Design rationale** | [`docs/CODE_WALKTHROUGH.md`](docs/CODE_WALKTHROUGH.md) |
| **Data provenance** | [`docs/DATA_PROVENANCE.md`](docs/DATA_PROVENANCE.md) |
| **Dated work log** | [`PROGRESS.md`](PROGRESS.md) |
| **What's next** | [`FUTURE_WORK.md`](FUTURE_WORK.md) |

> This is the **viewshed / visibility** half of the LAMP proposal. Path
> and movement analysis (cost surfaces, Monte Carlo path ensembles) is
> a second contributor's deliverable and is deliberately absent here.

---

## 1. Goals

The Necropolis of El Bagawat (Kharga Oasis, Egypt) is a late-antique
cemetery of ~263 mud-brick funerary chapels. The project asks what the
site was *legible* as: who could see what, from where.

Existing tools cannot answer that. GRASS `r.viewshed` and 2D
space-syntax VGA are planimetric — they treat every building as a
solid, opaque block. That erases the thing that makes the site
readable: chapels have **doorways and windows**, and sight passes
*through* and *between* them. A tool that models a chapel as a filled
rectangle cannot represent a doorway at all.

**The deliverable: a corrected, aperture-aware visibility graph**, built
by casting real 3D rays through a scene in which buildings have height
and explicitly modelled openings.

The proposal set three build steps — (1) a 3D ray-casting engine,
(2) window/door apertures, (3) a baseline-vs-3D comparison report —
with step 2 flagged in advance as the likely slip point.

**There is no ground truth.** The site is ~1,700 years old and no
measured visibility data exists to fit against. So the project is built
for **auditability** rather than accuracy-against-labels: deterministic
ray-casting over learned models, analytic self-checks, frozen
regression baselines, and every result reproducible from DEMs +
footprints + an aperture registry.

## 2. What I did

### Step 1 — the 3D ray-casting engine

`scripts/viewshed.py`, `scripts/scene3d.py`

A true 3D line-of-sight engine over the 0.4 m DEM, with building
heights derived from the **differential between two DEMs**
(with-buildings − bare earth), not from assumed constants.

- `HeightfieldScene` — ray-marched LOS over the terrain surface.
- `HybridScene` — the heightfield **composed with** per-building
  triangle meshes, so a wall can be solid, then open, then solid again
  along one vertical line. A heightfield stores one elevation per
  column and physically cannot represent that; the mesh layer is what
  makes apertures expressible at all.
- Batched two-sided Möller–Trumbore intersection in torch,
  device-agnostic (CUDA → MPS → CPU), no mesh library.
- Beyond the proposal: vertical view cone (`--pitch`/`--vfov`), 3D
  visibility **volumes**, adjustable eye height, dome caps, and
  first-person observer snapshots rendered by the same kernel as the
  viewsheds — the audit-grade "what the observer actually sees".

Observer eye height defaults to **1.5 m**, from skeletal estimates for
late-antique Egyptian populations rather than the 1.75 m GIS default;
both are swept as a sensitivity.

### Step 2 — openings (doors, windows, niches, apses)

`scripts/build_aperture_walls.py`, `scripts/aperture_registry.py`, extractors and curators

> **A word on "aperture".** The excavation report uses it narrowly, for
> a **light opening** — "the chamber was lit by means of apertures in
> the three walls" — so to anyone reading the source it means *window*.
> This project adopted it as an umbrella for every modelled opening,
> which is the broader, non-standard sense, and it is baked into path
> and script names. Precise vocabulary, used from here on:
>
> - **opening** — any of the four kinds below; one registry row
> - **perforating opening** — passes through the wall: **door**, **window**
> - **recess** — cut into a wall face, does not pass through: **niche**, **apse**
>
> The distinction drives the result in §3: doors are the only kind that
> puts an interior in view, windows reveal ground and no interiors, and
> recesses change nothing at all.

The registry holds **469 openings across 202 chapels** — 197 doors,
93 windows, 172 niches, 7 apses. Sourcing was the hard part: no single
document gives both an opening's position and its height.

| Source | Gives | Status |
|---|---|---|
| Excavation report Ch. VII (OCR) | entrance **direction** in words | 194/263 chapels, validated 6/6 by hand |
| Report plates (200 scans) | opening **heights** | the only possible source — **not yet read** |
| CAD/DXF plots (7) | door **width and position** | LW2 threshold marks; 3 chapels |
| Site plan | — | **proven unusable**, see §9 |

Extraction scripts **never** write the registry; they emit
`*_candidates.csv` and a human confirms. That rule is what lets any
extractor be re-run without losing hand edits.

### Step 3 — the comparison

`scripts/compare_baseline.py`, `scripts/compare_apertures.py`

Three layers, in increasing order of what they prove:

1. **Engine vs GRASS `r.viewshed`, both solid** — 97–99% cell
   agreement. High agreement is the *validating* result, not the
   finding: it shows the engine reproduces an established tool on the
   common case.
2. **ROI raster sweep with apertures** — reports an honest null: the
   door effect is **structurally zero** here, because `target_grid`
   pins every target at *roof* height, so a ground-level door can never
   flip it. No number of extra observers fixes that.
3. **Site-wide visibility graph, graded** — the metric that carries the
   result, because it tests centroid visibility at true interior floor
   height.

### Beyond the build order — statistical tests of arrangement

`scripts/test_entrance_azimuth.py`, `scripts/test_intentionality.py`, `scripts/test_feature_visibility.py`, `scripts/test_painting_visibility.py`

Not in the original proposal, and where the site-level findings live.

## 3. Results

### Doors and windows do different jobs

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
one additional interior. The geometry forces it: a window sill sits
above standing eye height, so a sightline through it rises and lands
high on the far wall, while a door spans floor level. Niches and apses
change nothing on any metric, exactly — 179 recesses, zero
perforations, the recess gate validating itself on real data.

### The entrance arrangement is not accidental

A **pre-registered** Monte Carlo test (α = 0.01, Holm-corrected, 999
draws) counting ordered chapel pairs where one chapel's interior is
visible from a standing position outside another's doorway:

| null | permutes | null median | effect | p | p_holm |
|---|---|---|---|---|---|
| N1 | which wall holds the door | 270 | +3.57 IQR | 0.001 | 0.003 |
| N2 | chapel positions | 252 | +3.79 IQR | 0.001 | 0.003 |
| N3 | both | 264 | +3.32 IQR | 0.001 | 0.003 |

**Observed V = 377.** All three reject. Zero of 999 draws reached the
observed value in any null — V = 377 exceeds the *maximum* of ~1,000
random rearrangements by 22–41 pairs, at 4.4–5.1 SD. p = 0.001 is the
resolution floor of 999 draws, not a narrow margin.

N1 is the headline: it holds every chapel exactly where it stands and
permutes only *which wall carries the door*, reusing the directions
that actually occur — so it cannot reject merely because the compass
distribution is lopsided.

### Entrance directions are neither solar nor arbitrary

`test_entrance_azimuth.py`, n = 194, constrains the explanation space
before any ray is cast:

- **S 38.1%, W 34.0%, E 26.8% — and N/NE/NW exactly 0.**
- Solar nulls (sunrise arc, sunrise+sunset) **rejected in the strongest
  possible way**: they assign probability zero to south, the single
  largest class.
- Uniform over 8 compass classes **rejected** (G = 366.6, p = 3.4e-75).
- Ground slope does **not** explain it: entrances sit a median 71.8°
  from straight downhill, and 32.5% fall within 45° of downhill against
  30.2% expected (p = 0.261).

Not the sun, not chance, not the terrain — and then the ray-cast test
says the doors are arranged with respect to *each other*.

### The frescoes were never meant to be seen from outside

27 named painted scenes extracted from the two painted chapels (17 in
chapel 30, 10 in chapel 80), the parser's running order matching the
report's own printed order **17/17** — independent evidence, since
Fakhry listing his scenes doesn't depend on the headings the parser
finds.

The paintings are **on the dome**. Measured, not asserted: from the
standing station outside chapel 30's door, the highest visible interior
point reaches **1.41 m above the floor** against a springing line at
**2.49 m** — a 1.08 m shortfall, with every choice made generously
(empty chamber, observer free to stand anywhere on the axis). The door
head caps how steeply a sightline can rise once inside.

### Interior features, and chapels without doors

Of **175 interior features on 79 chapels**: 44 (25%) visible from their
own doorway, 26 (15%) from another chapel, 121 (69%) from nowhere
tested. Apses do worst — 2 of 7 from their own door, **0 from any other
chapel**. Where the report places a niche in the wall *facing the
entrance* (24 chapels), **55.6% are visible from that chapel's own
doorway against 16.7% elsewhere**.

The 66 chapels with no recorded doorway were tested as possible sealed
mausolea and **are not**: the report describes door bolts ("the places
of their bolts are still preserved in many"), thresholds and brick
jambs as general features, and calls no chapel entrance-less. The
sealing is one level down — the burial shaft in the floor, which Fakhry
excluded from his plans by choice.

## 4. Current state

| Deliverable | State |
|---|---|
| 3D ray-casting engine | **Complete**, validated against `r.viewshed` at 97–99%, and ~100× faster than at midterm |
| Aperture model | **Complete** as a pipeline; 469 openings over 202 chapels. Positions and heights are largely defaults — see §6 |
| Comparison report | **Complete**, three layers, including an honest negative result |
| Statistical tests | **Complete**, pre-registered, all deviations recorded |
| Visualization / audit tooling | **Complete** — first-person renders, Blender/Unity export, 3D volumes |
| Full observer point set | **Outstanding** — mentor-dependent; every site-wide number is a 3-observer sample |

Everything runs end to end from a clean clone plus the data subset. All
self-checks and the frozen mesh regression pass.

## 5. How it is validated

No ground truth means validation has to be structural:

- **Analytic self-checks on synthetic geometry** — a cube + dome + door
  with known answers: through-door passes, above-head blocked,
  blank-wall blocked, exact first-hit distances, reciprocity.
- **Frozen regression** — 197 meshes hashed; refactors must leave them
  byte-identical (`check_regression.py`).
- **Cross-validation between independent implementations** —
  `observer_view.py`'s first-person renders cross-checked ≥98% against
  `visible_mask`.
- **Equivalence gates on every optimisation** — the batched ray cast was
  proven bit-identical to the per-eye path (0 of 7,823 terrain rays;
  0 of 46,122 mesh rays over 6 draws) *before* being used.
- **Pre-registration** — the intentionality test's statistic, α,
  correction and null definitions were fixed in the module docstring
  before the first run, and every later deviation is recorded there as
  a dated entry rather than quietly applied.

## 6. Known limitations

Stated because the report is stronger for naming them. A generated
row-by-row audit lives in
[`docs/DATA_PROVENANCE.md`](docs/DATA_PROVENANCE.md). Two figures frame
everything else: **466 of 469 openings are placed by a spacing rule**
rather than a source, and **466 of 469 carry class-default dimensions**.

| Limitation | Consequence |
|---|---|
| **3 observers** (`Marks_Brief2`); full set pending from mentors | every site-wide number is a 3-point sample |
| **Door position along a wall is a spacing rule**, not a source | only wall *attribution* is evidenced; nothing finer is claimable |
| **No opening height was read off the plates** — all 197 door heads default to 2.10 m | height-sensitive results rest on assumption |
| **Chapel 80 (Chapel of Peace) absent** — described in Ch. V, parser reads Ch. VII | the most-studied chapel on the site is missing from the model |
| **Per-point floor datum** — bare DEM varies up to 4.85 m under one footprint | comparable to a niche's whole height |
| **Niche dimensions are n = 1** — the report dimensions exactly one niche | sweep before quoting any niche-driven result |
| **`--domes` experimental**, pending mentor review | off by default; the validated baseline is unaffected |
| **No distance attenuation** | geometrically true, perceptually overstated; specified and deferred |

## 7. What's left to do

Full detail in [`FUTURE_WORK.md`](FUTURE_WORK.md). In priority order:

1. **The full observer point set** — mentor-dependent, gates every
   site-wide number.
2. **Read opening heights off the report plates** — the largest
   evidence gap.
3. **Chapel 80** — a parse-range fix on pages already OCR'd.
4. **Per-chapel floor datum**, behind a flag so the frozen baseline
   still reproduces.
5. **Distance-based visual obscurity** (acuity falloff), reported
   alongside raw visibility.
6. **GNN over the corrected visibility graph** — a follow-on, and only
   meaningful once it consumes *that* graph rather than a
   solid-building one.

Work continues with the mentors past the program.

## 8. The code

All work is in this repository. There is no upstream fork — the project
began as a new codebase, so **every commit is mine** and the whole
repository is the contribution.

- **26 commits**, 2026-06-21 → 2026-08-16
- **~15.9k lines of Python** across 39 modules in `scripts/` and
  `blender/`
- **~4.5k lines of Markdown** documentation
- No external dependencies added beyond the scientific Python stack
  already in `requirements.txt`; the ray tracer is hand-written rather
  than pulled from a mesh library, per the project's dependency-light
  stance

Commit history: `git log --oneline`. Notable milestones —

| Commit | Milestone |
|---|---|
| `9beecdc` | Initial engine, files transferred |
| `2f3715e` | Aperture coverage from multiple sources, validated on synthetic buildings |
| `92a95ce` | Batched the ray cast over many observers, single-eye path bit-identical |
| `85e3031` | Pair cache off by default; permuted chapels re-datumed onto their new ground |
| `6e2dda2` | Generated provenance audit of the registries and the gaps behind them |
| `5afb871` | Checkpoint work product |

> **Note for submission:** the code also needs to land in the
> HumanAI Foundation organisation repository under the project's folder,
> in an individual contributor folder. Add that link here once the PR is
> open, and note the last GSoC commit if work continues afterwards.

## 9. Challenges, and what I learned

**The obvious data source was the wrong one, and only ground-truthing
found that out.** The site plan is a clean 1:5000 line drawing with
visible gaps in the wall linework — it looks exactly like a door
source. It isn't: those gaps are plan-vs-footprint registration
artifacts at corners, and chapel 180, whose entrance the excavation
report places at the south-west corner (p.143), shows *unbroken*
linework there. Building doors from it would have produced a confident,
wrong model across the whole site. The source that worked was prose —
the report simply says "it opens west" — which is lower-tech and far
more reliable.

**Optimising the biggest bottleneck just promotes the second biggest.**
Profiling put the terrain march at 79% of a run, so I batched it and
got 179×. The run got 1.1× faster. Re-profiling showed the march was
now 1.3% and the mesh pass was 98.7%. Batching that too gave ~100×
overall. I guessed the bottleneck wrong three times before measuring
settled it — the lesson is not "profile first" but "profile *again*
after every win", because a win changes what the bottleneck is.

**A test with no power is not evidence of absence.** A memoisation
shortcut was licensed by a check that compared 474 repeated pairs and
found zero disagreements. The real disagreement rate is 6.1e-4, at
which that check *expected* 0.29 counterexamples — finding none was the
most likely outcome whether or not the assumption held. It was like
testing a loaded die with two rolls. I now compute the expected
detection count before trusting a negative result.

**A null hypothesis has to be a rival explanation, not a formality.**
Two of the three nulls originally permuted chapel positions in plan
only, so a moved chapel kept the elevation of the plot it came from.
The site spans 38 m of relief, so the median permuted chapel floated
9.1 m above its new ground against 3.6 m walls — and a floating
building occludes nothing. The nulls confidently reported "arrangement
doesn't matter". After re-datuming, the same nulls reported the
opposite. The result was backwards for a purely mechanical reason, and
nothing about the p-value would have revealed it.

**Metadata has to answer the question you think it answers.** The
registry's `source_pos` column records the provenance of an opening's
*wall attribution* — not its position along that wall. The name invites
the opposite reading, and for a while a wall attribution stated plainly
in the excavation report was lending its authority to a position that
is a spacing rule. Only 3 of 469 openings have a genuinely sourced
along-wall position. Auditing that produced the most useful document in
the project.

**Generate documentation that describes data; don't write it.** A
hand-kept provenance note is wrong the first time someone edits a
registry, and wrong *silently* — a reader cannot tell a measured 0.86 m
door from a class default of the same number. `data_provenance.py`
derives the whole audit from the registries and fails if any provenance
token lacks a documented meaning.

**Reporting a negative result clearly is worth more than burying it.**
The aperture raster sweep shows a door effect of exactly zero, and the
report says so in its own text, explains that this is structural rather
than a sample-size problem, and points at the metric that *can* see
doors. That is more useful to the next person than a number massaged
until it looked positive.

## 10. Reproducing this

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv -r requirements.txt

.venv/bin/python scripts/sanity_checks.py          # validate inputs first
.venv/bin/python scripts/scene3d.py --self-test    # analytic geometry checks
.venv/bin/python scripts/check_regression.py --only meshes
.venv/bin/python scripts/test_intentionality.py --n-draws 999 --nulls N1 N2 N3
```

The ~200 GB dataset is not in this repository. A working subset lives
under `LAMP_DataStore/ElBagawat/` (gitignored); the layout it mirrors is
documented in [`CLAUDE.md`](CLAUDE.md), and
[`docs/REMOTE_SETUP.md`](docs/REMOTE_SETUP.md) is a clone-to-running
walkthrough for the CUDA workstation.

- [`README.md`](README.md) — flag reference for all 39 scripts
- [`docs/CODE_WALKTHROUGH.md`](docs/CODE_WALKTHROUGH.md) — narrated
  design tour: what each script does, why, and what breaks otherwise
- [`docs/DATA_PROVENANCE.md`](docs/DATA_PROVENANCE.md) — what every
  modelled fact rests on, and the nine gaps where nothing does
- [`PROGRESS.md`](PROGRESS.md) — dated running record, newest first
- [`FUTURE_WORK.md`](FUTURE_WORK.md) — what to do next, and why
