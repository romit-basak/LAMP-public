# GSoC status check + extension plan — El Bagawat viewshed

> Written 2026-07-13, right after the midterm pass and the mentors' extension offer.
> Purpose: an honest read of where the proposal deliverable actually stands, and a
> prioritized plan for the extension runway (Aug 24 – Nov 2).

## Dates, as given

- Jul 13 (today) → **Aug 17–24, 18:00 UTC**: standard-track final work product + evaluation window.
- Aug 24–31: mentors submit standard-track evaluations.
- **Aug 24 – Nov 2**: extended-timeline contributors keep coding.
- **Nov 2, 18:00 UTC**: final work product + evaluation deadline for extended contributors.
- Nov 9: mentor evaluation deadline for extended projects.

That's **~5 weeks** to the standard deadline, or **~15.5 weeks** if the extension is taken — roughly **3x** the runway.

## Verdict: not on track for the *complete* proposal deliverable by Aug 17

Be precise about what "the deliverable" is. CLAUDE.md's scope section states it plainly: *"The assigned deliverable is true 3D ray-cast visibility analysis **with building apertures**... a corrected, aperture-aware **visibility graph**."* Checking the repo against that:

- ✅ **Step 1 — 3D ray-casting engine.** Done, and substantially over-built beyond what the proposal asked for: view cone (`--pitch`/`--vfov`), 3D visibility volume (+ now a surface-mesh export), configurable eye height, dome caps, first-person observer snapshots, and a Blender/Unity export pipeline. None of the visualization extras (Blender, Unity, volume mesh, observer-view renders) were required by the proposal — they're additive polish, not deliverable-gating.
- ❌ **Step 2 — window/door apertures.** **Zero implementation exists.** Every mention of "aperture" in the codebase (`grep -rn aperture scripts/`) is a forward-looking comment or docstring — e.g. `viewshed.py`'s own docstring: *"solid buildings, no apertures yet... an aperture-capable explicit-geometry scene [can] replace `HeightfieldScene`... at step 2."* CLAUDE.md itself flags this as **"the likely slip point — budget extra time."** No aperture data model, no digitization from `Site_Plan.pdf`, no design decision on how a heightfield ray-caster represents a hole in a wall at a specific height.
- ⚠️ **Step 3 — comparison report.** Only half-written. The existing `Task_2/comparison/comparison_report.md` compares the (aperture-less) 3D engine against GRASS `r.viewshed` — both treat buildings as solid, so **97–99% agreement is the expected, validating result**, not the headline finding. The report says so directly: *"the 3D-vs-2D divergence comes once apertures land."* The actual thesis-proving number — how much an aperture-aware 3D graph diverges from 2D/solid-3D — cannot be written until step 2 exists.
- ⚠️ Stretch: eye-height sensitivity is done (it's a flag). GNN-over-VGA has not been started — correctly so, since CLAUDE.md marks it a stretch/follow-on, not a substitute.
- ⚠️ **The full observer point set is still TBD with mentors** (`Marks_Brief2.shp` — 3 sample points only). Every viewshed/graph/comparison output to date is a 3-point proof of concept, not the site-wide result a final report needs.

**So: no, the project is not on track to deliver the aperture-aware visibility graph — the actual proposal ask — by Aug 17.** What *is* solid: the ray-casting engine, the validation methodology (self-checks, r.viewshed baseline), and now a richer visualization stack than proposed. The extension isn't a rescue from being behind schedule in general — it's exactly the runway CLAUDE.md already predicted this step would need.

## If you want a credible checkpoint by Aug 17 anyway

Not required (the extension covers this), but a good midterm-to-final bridge if mentors want visible progress before Aug 24:

- Pick the **simplest binary aperture model** first — CLAUDE.md already frames this as the right sequencing: *"the translucent partition / partial-transmission case is a refinement, not the first cut."* A door/window either passes rays or it doesn't; no partial transmission yet.
- Digitize openings for a **small subset of chapels** (the ones nearest the 3 sample observers) from `Task_2/Site_Plan.pdf` rather than all 263 buildings.
- Ship a **first-cut 3D-vs-2D divergence number** on that subset, explicitly labeled preliminary/small-sample, alongside the existing solid-building comparison.
- This gives mentors a real signal that step 2 has started, without pretending the full site is done.

## Extension plan (Aug 24 – Nov 2), in priority order

**1. Apertures — the core unblocked deliverable**
- **Data source (resolved by direct inspection, 2026-07-13 — see [[aperture-data-sources]] memory):** no single document gives both location and height, so combine three sources —
  - `Task_2/Site_Plan.pdf` is literally a "Print to PDF" of `100_Data/120_SiteReport/BaseSiteCAD/SITE CAD WORKING.dwg` (confirmed from the PDF's own embedded metadata title). Open that DWG (or `SITE CAD BUILDINGS ONLY.dwg`) directly in QCAD/LibreCAD/AutoCAD rather than accepting the one printed page as the ceiling — it likely covers more of (or the whole) site. Hand-parsing `BaseSiteCAD/Building23.dxf`'s raw geometry shows small notches/jogs in the wall outline that look like a doorway convention, but confirm in a real CAD viewer.
  - `100_Data/120_SiteReport/SiteReport_missing9-12.pdf` (the same 200-page Fakhry excavation-report scan already mined for chapel typology) contains **dozens of individual chapel plates pairing a plan view with a front elevation/section**, with dimension lines — this is the actual source for opening **heights** (sill/head), which no plan-view drawing alone can give. Likely covers a representative subset of chapels, not all 263.
  - For chapels with no published elevation: a documented **typical-height default**, calibrated from whichever chapels the report does illustrate — disclose this as a modeling assumption, don't silently extrapolate.
  - **Orthophoto and `140_SAR_Imagery` are dead ends for this** — confirmed the latter is actually DigitalGlobe optical (MUL/PAN) imagery, not radar, and both it and the orthophoto are near-nadir, so neither can see a vertical wall face or give height regardless of resolution.
- Design the aperture data model explicitly: which wall, height range (sill/head), width, per opening. Decide whether it lives as a new footprint-adjacent table (parallel to `dome_inventory.csv`'s pattern) or as geometry alongside the per-building `.gpkg` files.
- Resolve the real architectural question: a heightfield has one z per (x, y) cell, so it **cannot represent a hole partway up a wall**. This is why `viewshed.py`'s docstring already earmarks an *"aperture-capable explicit-geometry scene"* behind the same `Scene` interface (`surface_z` / `visible_mask` / `is_visible`) — decide now whether that's a full mesh/voxel scene for buildings-with-openings (heightfield elsewhere), or a hybrid (heightfield + per-building vertical-plane apertures tested by ray-plane intersection layered on top).
- First cut: binary transparent/opaque apertures (per the CLAUDE.md sequencing note above). Partial transmission (translucent screens, the type-9 "small oval aperture for light" noted in the excavation report) is the follow-on refinement, not blocking.
- Reuse everything already built: `HeightfieldScene`'s Scene contract, the self-check philosophy (add reciprocity/monotonicity checks for the new scene the same way `run_self_checks` does today), and `observer_view.py`'s first-person renders as the visual audit tool for "does light plausibly pass through this doorway."
- **Compute:** the remote workstation (RTX 5000 Ada 32 GB, Threadripper Pro 7985WX, 512 GB RAM — see [[remote-workstation-specs]]) already gets picked up automatically by the existing `cuda`-first device selection. If the explicit-geometry scene makes per-ray triangle intersection expensive, its RT cores (via OptiX) are worth evaluating before hand-rolling a BVH.

**2. The real comparison report — the highest-value deliverable**
- Once apertures exist, rerun `compare_baseline.py`'s method (or a new sibling script) with the aperture-aware 3D result as one side. The existing 97–99% solid-vs-solid number stays in the report as the validating baseline; the new number is the actual contribution.
- Report both **raw** and, if time allows, **acuity-weighted** visibility (see item 5) — CLAUDE.md already says to report both once distance attenuation is added.

**3. Get the full/finalized observer point set from mentors**
- This has been "TBD" since the data-transfer phase. It blocks a credible site-wide graph and comparison — chase it early in the extension, not at the end, since it's a mentor-dependent input with its own lead time.
- Once received: regenerate viewsheds, the visibility graph, and the comparison report at full scale (not the 3-point sample), and replicate the shapefile to the remote datastore per the existing TODO in CLAUDE.md's data-assets table.

**4. Close out the standing mentor questions**
- ~~Silver's depthmapX "2.5D" claim~~ — **resolved, drop it.** Mentors had a poor experience with depthmapX and its 2.5D feature is unpublished; r.viewshed is already a 2.5D tool and `compare_baseline.py` already shows the engine reproduces it at 97–99% on solid buildings, so a depthmapX baseline adds little (see [[depthmapx-dropped]]). No need to raise this with mentors.
- The baseline eye-height sweep (1.5 m / 1.75 m) isn't a data gap to resolve — the original proposal work intentionally ran both (1.75 m = r.viewshed's/modern-human default, 1.5 m = the skeletal-remains estimate for the necropolis population), and both stay meaningful in the final comparison as a sensitivity, not a mystery pointing at one "true" value.
- `--domes` is still flagged experimental pending mentor visual review — get that sign-off, or explicitly fold the caveat into the final report if it stays experimental.

**5. Stretch goals, now genuinely in scope given the runway**
- **Distance-based visual obscurity** (acuity/contrast falloff) — CLAUDE.md already has this fully spec'd as a "planned refinement, deliberately deferred." The extension is exactly the room to implement it: an acuity threshold or exponential attenuation weighting layered onto `visible_mask`, reported alongside raw visibility in the comparison.
- **GNN-over-VGA** — explicit stretch goal, not a substitute for the deterministic engine. Worth scoping only after apertures + the real comparison land, since it needs the corrected visibility graph as its input, not the solid-building one.
- **BVH acceleration** — only if the aperture-capable explicit-geometry scene makes per-ray cost blow up; CLAUDE.md already names this as the fallback (*Ray Tracing: The Next Week*).

**6. Consolidate the visualization work into the final narrative**
- The observer-view snapshots, Blender renders, and volume-mesh export are real, working, and *not* proposal-required — make sure the final report explicitly frames them as communication/audit tooling built around the core deliverable, not as the deliverable itself, so reviewers don't mistake polish for the thesis contribution.
- Rerun the Blender/Unity docs' verification steps once the full point set lands, so the final submission's visuals reflect the real site, not the 3-point sample.

## Explicit non-goals for the extension

- **Path/movement analysis** (cost surfaces, entrance-weighted costs, Monte Carlo paths) is the other contributor's half of the proposal — CLAUDE.md says not to add it here unless explicitly asked. Extra runway is not an invitation to scope-creep into it.
- Don't let the already-over-built visualization stack (Blender/Unity/volume mesh) absorb more extension time than apertures — it's finished and working; apertures are not.

## Suggested immediate next step

Send mentors a short version of the priority list above (apertures → real comparison → full point set → open questions → stretch) so the extension has an agreed target before coding resumes, and start the two mentor-dependent asks (point set, `Site_Plan.pdf` sign-off) in parallel with beginning the aperture data-model design — those have their own lead time and shouldn't sit at the end of the queue.
