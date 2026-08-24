---
layout: post
title: "A Viewshed That Sees Through Doors"
subtitle: "True 3D visibility analysis for a late-antique necropolis"
date: 2026-08-21
author: "Romit Basak"
summary: >
  Standard GIS viewsheds treat every building as a solid, opaque block.
  This project casts rays through a real 3D scene with modelled doors
  and windows instead, and finds an arrangement of entrances that no
  planimetric tool could have seen.
---

*GSoC 2026 · Late Antiquity Modelling Project (LAMP), HumanAI
Foundation · mentors Dr. Camille Leon Angelo (University of Alabama)
and Dr. Joshua Silver (KIT)*

<div class="byline-row">
<div class="byline-author">
<img class="avatar" src="a-viewshed-that-sees-through-doors/assets/avatar.jpg" alt="Romit Basak">
<span>By Romit Basak</span>
</div>
<img class="program-badge" src="assets/gsoc-humanai-badge.png" alt="Google Summer of Code x HumanAI Foundation">
</div>

![General view of the mudbrick chapels at El Bagawat, photographed among the sandy hills of Kharga Oasis](a-viewshed-that-sees-through-doors/assets/hero-bagawat.jpg)
*El Bagawat, Kharga Oasis, Egypt. Photo: Ktiv, [CC BY-SA
4.0](https://creativecommons.org/licenses/by-sa/4.0/), via Wikimedia
Commons.*

## The site, and the question

El Bagawat is a necropolis of roughly 263 mudbrick chapels cut into the
sandy hills of Egypt's Western Desert, built between the 3rd and 7th
centuries CE as Christianity took hold in the oasis. LAMP's interest
in the site is architectural and social: what did a person standing
anywhere in this landscape actually see, and how did that shape which
structures the community built, adapted and kept using?

Answering that requires a *viewshed* — a model of what is visible from
where. Existing tools can compute one. The trouble is what they leave
out.

## The problem with standard tools

GIS viewshed analysis — GRASS `r.viewshed`, 2D space-syntax visibility
graphs — is planimetric. Every building is a solid block with a
height, nothing more. That's a reasonable simplification for a lot of
terrain analysis, but it throws away exactly what makes a site like
this legible: doorways and windows that let sight and light pass
*through* a structure and *between* structures, not just around them.

A necropolis of individually built, individually oriented chapels, on
sloped and irregular ground, is close to a worst case for that
simplification. If where a chapel's door faces matters — and the
excavation report makes clear it does — a model that can't represent a
door can't test that.

## What I built

A true 3D ray-casting engine. Buildings get real height from the
**difference between two digital elevation models** — one with
structures, one without — rather than from an assumed constant or a
separate imagery source. Rays are cast from an observer's eye (default
1.5 m, reflecting skeletal data for late-antique Egyptian populations
rather than the usual 1.75 m GIS default) through the resulting 3D
scene, checking first-hit geometry the same way a renderer would.

Before adding anything the site plan doesn't already have, the engine
had to earn trust on the plain case: it agrees with GRASS `r.viewshed`
at **97–99%** cell-by-cell agreement on solid, unmodified buildings.
That number isn't the finding — it's the ticket to make the finding
mean something. Once solid-building agreement is that high, anything
that changes afterwards is coming from the openings, not from a bug in
the ray caster.

![A rendered 3D view of several chapels at eye level, showing real building height and roofline variation derived from the DEM differential](a-viewshed-that-sees-through-doors/assets/scene-overview.png)
*The 3D scene the engine casts rays through — chapel geometry
extruded from the DEM differential, rendered here in Blender for
illustration. The ray-casting itself runs on this same geometry, not
on the render.*

## Finding the real doors

The obvious source for door positions is the site's own architectural
plan — a clean 1:5000 line drawing with visible gaps in the wall
linework that look exactly like doorways. It's a false lead. Those
gaps are artifacts of registering the plan against the building
footprints, not real openings: chapel 180's entrance, which the
excavation report places firmly at the south-west corner, shows up as
**unbroken wall** on that very plan. Building a door registry from the
plan would have produced a confident, wrong model across the entire
site.

What worked instead was lower-tech: the excavation report's own
Chapter VII, which states each chapel's entrance in words — "it opens
west." OCR'ing two hundred report pages and parsing that prose gave
entrance directions for 194 of 263 chapels, hand-validated against a
sample. Door widths and positions for a handful of chapels came from
CAD threshold marks in the surviving architectural drawings. Between
the two, the registry now holds **469 openings across 202 chapels** —
197 doors, 93 windows, 172 niches, 7 apses.

No single source gives both an opening's position *and* its
dimensions, so the registry is explicit about what's measured versus
assumed, down to each row. Extraction scripts never overwrite it
directly; they propose candidates, and a person confirms.

## Doors and windows do different jobs

Adding openings to a validated, solid baseline and re-running the
site-wide comparison gives a clean decomposition:

| variant | ground cells visible | interior-visible pairs |
|---|---|---|
| solid buildings only | 36,520 | 8 |
| + doors | 36,657 (+137) | **11** |
| + windows | 37,858 (+1,201) | 11 (+0) |
| + niches, apses | 37,858 (+0) | 11 (+0) |

![Decomposition of visible ground cells and interior-visible chapel pairs by opening type — solid buildings, then doors, windows, and recesses added in turn](../Task_2/comparison_all_fabric/decomposition_apertures.png)

Doors add comparatively little ground area but are the *only* opening
that puts a building's interior in view. Windows add roughly nine
times more visible ground and precisely zero new interiors. That's not
a coincidence of this site — it's forced by the geometry. A window
sill sits above standing eye height, so a sightline through it has to
rise before it can pass, and it lands high on the far wall rather than
reaching the floor. A door spans floor level, so a sightline through
it can go straight in.

<figure class="schematic" role="img" aria-label="Two side-by-side diagrams: a sightline through a door reaching the interior floor of a far wall, and a sightline through a window, whose sill sits above eye height, rising to strike a far wall well above the floor">
<div class="schematic-row" aria-hidden="true">
<div class="schematic-item">
<svg viewBox="0 0 300 260" xmlns="http://www.w3.org/2000/svg">
  <line x1="10" y1="140" x2="290" y2="140" stroke="#e2e8f0" stroke-width="1" stroke-dasharray="2 4"/>
  <line x1="10" y1="220" x2="290" y2="220" stroke="#94a3b8" stroke-width="2"/>
  <rect x="110" y="64" width="16" height="156" fill="#334155"/>
  <rect x="110" y="104" width="16" height="116" fill="#ffffff" stroke="#0f172a" stroke-width="1"/>
  <rect x="230" y="64" width="16" height="156" fill="#334155"/>
  <circle cx="40" cy="140" r="6" fill="#0f172a"/>
  <line x1="40" y1="146" x2="40" y2="220" stroke="#0f172a" stroke-width="2"/>
  <text x="40" y="118" font-size="13" fill="#0f172a" text-anchor="middle">observer</text>
  <text x="40" y="160" font-size="11" fill="#64748b" text-anchor="middle">eye, 1.5 m</text>
  <text x="118" y="52" font-size="12" fill="#0f172a" text-anchor="middle">door</text>
  <line x1="40" y1="140" x2="238" y2="220" stroke="#f97316" stroke-width="2.5" stroke-dasharray="6 4"/>
  <circle cx="238" cy="220" r="3" fill="#f97316"/>
  <text x="150" y="244" font-size="12" fill="#b45309" text-anchor="middle">reaches the interior floor</text>
</svg>
<p class="schematic-title">Through a door</p>
</div>
<div class="schematic-item">
<svg viewBox="0 0 300 260" xmlns="http://www.w3.org/2000/svg">
  <line x1="10" y1="140" x2="290" y2="140" stroke="#e2e8f0" stroke-width="1" stroke-dasharray="2 4"/>
  <line x1="10" y1="220" x2="290" y2="220" stroke="#94a3b8" stroke-width="2"/>
  <rect x="110" y="64" width="16" height="156" fill="#334155"/>
  <rect x="110" y="90" width="16" height="35" fill="#ffffff" stroke="#0f172a" stroke-width="1"/>
  <rect x="230" y="64" width="16" height="156" fill="#334155"/>
  <circle cx="40" cy="140" r="6" fill="#0f172a"/>
  <line x1="40" y1="146" x2="40" y2="220" stroke="#0f172a" stroke-width="2"/>
  <text x="40" y="118" font-size="13" fill="#0f172a" text-anchor="middle">observer</text>
  <text x="40" y="160" font-size="11" fill="#64748b" text-anchor="middle">eye, 1.5 m</text>
  <text x="118" y="52" font-size="12" fill="#0f172a" text-anchor="middle">window</text>
  <line x1="126" y1="125" x2="142" y2="125" stroke="#64748b" stroke-width="1"/>
  <text x="146" y="129" font-size="10" fill="#64748b">sill</text>
  <line x1="40" y1="140" x2="238" y2="84" stroke="#0891b2" stroke-width="2.5" stroke-dasharray="6 4"/>
  <circle cx="238" cy="84" r="3" fill="#0891b2"/>
  <text x="150" y="244" font-size="12" fill="#0e7490" text-anchor="middle">hits the far wall high</text>
</svg>
<p class="schematic-title">Through a window</p>
</div>
</div>
<figcaption>Why doors and windows do different jobs: a door spans floor
level, so a sightline through it can reach the interior. A window sill
sits above eye height, so the same geometry sends the sightline up and
into the far wall instead. Each panel is its own building — the two do
not share a wall.</figcaption>
</figure>

Niches and apses — recesses cut into a wall face rather than passing
through it — change nothing on any metric, exactly. That's not a null
result; it's the model correctly reporting that a feature which
doesn't perforate the wall can't affect what's visible through it.

## Is the arrangement intentional?

The decomposition above says what a single doorway does. A separate
question is whether entrances across the *whole site* are arranged
with respect to each other — whether standing at one chapel's door
puts another chapel's interior in view more often than chance would
produce on this terrain, with this density of buildings.

A pre-registered Monte Carlo test (α = 0.01, Holm-corrected, 999
draws) counts ordered chapel pairs where one chapel's interior is
visible from a standing position outside another's doorway. Against
three nulls — permuting which wall carries the door, permuting chapel
positions, and permuting both — the observed count of **377** such
pairs is rejected by all three: zero of 999 random draws in any null
reached it.

**What this does and doesn't establish.** The nulls test whether the
arrangement is *random*, not *why* it isn't. Permuting which wall
carries a chapel's door, for instance, holds the overall compass
distribution fixed but also erases any relationship a door has to
whatever sits near it locally — so any systematic relationship between
doors and local geometry would clear this bar, not only mutual
arrangement between chapels. The result that survives is that the
entrance arrangement is **non-random with respect to something local**;
377 is the measurement, not yet the explanation of what produced it.

A companion test narrows the field a different way: entrance
directions are not solar (they reject sunrise and sunset alignments in
the strongest possible way, by putting zero weight on south — the
single largest observed class), not uniform, and not explained by
ground slope (entrances sit a median 72° off the downhill direction).
Not the sun, not chance, not the terrain. What's left — circulation,
mutual arrangement between buildings, or both — is exactly what the
inter-visibility test above measures, without yet separating which.

## Limits of the data available

No surviving source gives both an opening's position along its wall
and its dimensions at once. The excavation report states, in words,
which wall a chapel's entrance is on — enough to build the registry
above — but not a surveyed position along that wall or a measured
height; CAD threshold marks give both for a handful of chapels, which
is where the only 3 of 469 sourced positions and 3 sourced dimensions
come from. Everywhere else, position and dimensions fall back to a
spacing rule and a class default, because nothing in the surviving
record fixes them more precisely. What's genuinely evidenced is
**which wall** an opening sits in — not exactly where along it. Every
number in this post holds at that resolution, and that ceiling comes
from the sources that survive for this site, not from a step left
undone.

There's also no measured visibility data to validate against — the
site is roughly 1,700 years old, and no one recorded who could see whom
from where. Validation instead means: agreement with an independent,
established tool on the case both should get right; analytic
self-tests on synthetic geometry with known answers; and visual audit
of outputs against the site plan by eye. That's a different, and I'd
argue more honest, standard than fitting a label set — but it's worth
saying plainly rather than letting a clean number imply more certainty
than it has.

## Why this matters beyond one site

None of this is specific to mudbrick chapels in a desert oasis.
Archaeology, urban history and architecture all lean on visibility to
explain why people moved, gathered, or built the way they did, and
every one of those fields has had to accept "buildings are solid
blocks" as a shortcut for lack of a better tool. A physically real,
aperture-aware 3D approach removes that shortcut. The engine, the
aperture pipeline and the statistical tests are all published — see
below — precisely so the method can be pointed at somewhere else.

---

**Code:** [github.com/romit-basak/LAMP-public](https://github.com/romit-basak/LAMP-public)
— see [`GSOC_WORK_PRODUCT.md`](https://github.com/romit-basak/LAMP-public/blob/main/GSOC_WORK_PRODUCT.md)
for the full checkpoint write-up, [`docs/CODE_WALKTHROUGH.md`](https://github.com/romit-basak/LAMP-public/blob/main/docs/CODE_WALKTHROUGH.md)
for a narrated tour of the scripts, and [`docs/DATA_PROVENANCE.md`](https://github.com/romit-basak/LAMP-public/blob/main/docs/DATA_PROVENANCE.md)
for a row-by-row audit of what's measured versus assumed. The site
dataset itself — excavation report scans, satellite imagery, precise
survey coordinates — isn't public and isn't mine to release; see
[`PUBLIC_COPY.md`](https://github.com/romit-basak/LAMP-public/blob/main/PUBLIC_COPY.md)
for what was kept out and why.

**Contact:** Romit Basak — [basak.r@northeastern.edu](mailto:basak.r@northeastern.edu)
· [github.com/romit-basak](https://github.com/romit-basak)
