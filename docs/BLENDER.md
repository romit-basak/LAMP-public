# Blender pipeline — photorealistic site renders

High-quality stills of the El Bagawat scene (Cycles lighting, shadows,
denoising) for mentor-facing material. This complements — never
replaces — the engine's own first-person snapshots:

| | produced by | role |
|---|---|---|
| **Evidence** | `scripts/observer_view.py` | audit-grade "what the observer sees" — rendered by the *same kernel* the viewshed products were validated with (97–99% vs GRASS r.viewshed) |
| **Communication** | `blender/build_bagawat_scene.py` | presentation-quality renders; real lighting, but a *separate* projection/geometry stack, so not evidence |
| **Experience** | Unity (see `docs/UNITY.md`) | interactive first-person walkthrough |

## Install

- **Blender 4.2+ or 5.x**. Mac: `brew install --cask blender` (installs
  the latest, currently 5.x — works; exercised with 5.1.2). Remote
  Windows box: `winget install BlenderFoundation.Blender` or the
  installer. Nothing is installed into the repo's Python — the scene
  script runs inside Blender's own bundled Python (bpy + numpy +
  stdlib only).
- The mesh builder needs Blender ≥ 3.6 (it relies on `loop_total`
  being derived from `loop_start`). On 5.x the script prints benign
  `use_nodes` DeprecationWarnings (property slated for removal in 6.0).

## Workflow

1. **Export the bundle** (repo Python; local coordinates, see below):

   ```bash
   .venv/bin/python scripts/export_scene_bundle.py
   ```

   Writes `200_Projects/220_BuildingsToDEM/scene_bundle/`: heightmap,
   ortho drape texture, `observers.json` (eye positions), `domes.json`,
   `meta.json` (grid geometry + the UTM origin). Domes are **never
   baked into the heightmap** — the Blender script adds them as
   hemisphere caps under `--domes`, so nothing can double-dome.

2. **Build + render** (headless; `--` separates Blender's args from the
   script's):

   ```bash
   blender -b -P blender/build_bagawat_scene.py -- \
       --bundle LAMP_DataStore/ElBagawat/200_Projects/220_BuildingsToDEM/scene_bundle \
       --camera id1 --azimuth 90 --domes
   ```

   **Outputs — exactly what a run writes, nothing more:** one
   `render_{id}_az{AZ}.png` per camera × azimuth into
   `200_Projects/220_BuildingsToDEM/blender_renders/` (so the command
   above yields a single PNG), plus `render_overview.png` when
   `--overview` is passed, plus `scene_bundle/scene.blend` when
   `--save-blend` is passed (open that file in Blender for interactive
   work — combine with `--no-render` to skip rendering). The scene is
   otherwise rebuilt from the bundle on every invocation and discarded.
   Useful flags: `--samples 16 --stride 2` for drafts, `--camera all`
   `--azimuth 0 90 180 270` for a look-around set, `--height-ramp` if
   the ortho is unavailable.

3. **First-run cross-check (do this once per machine).** Render the
   Blender view and the engine view of the same eye/direction:

   ```bash
   blender -b -P blender/build_bagawat_scene.py -- --bundle <bundle> \
       --camera id1 --azimuth 90 --fov 60 --size 720 480
   .venv/bin/python scripts/observer_view.py --ids 1 --persp 90 \
       --no-pano --modes natural
   ```

   Side-by-side, the same chapels must appear at the same image
   positions with the same silhouettes. If they do, the two projection
   stacks (engine ray generation and Blender camera orientation) agree
   and both are trustworthy. Known failure smells:
   - **mirrored / rotated site** → texture V-flip or the camera euler
     (the site's asymmetric layout makes this obvious immediately);
   - **empty render** → camera clip distance (the script sets
     `clip_end = 10 km`; a hand-made camera won't have it).

## Conventions the script encodes (don't re-derive them)

- **Local frame**: all bundle coordinates are `UTM − origin_utm`
  (whole-meter origin in `meta.json`). Blender works in float32;
  raw UTM eastings (~2.5 × 10⁵) and northings (~2.8 × 10⁶) would
  quantize to whole centimeters or worse. Never feed UTM to Blender.
- **Axes**: +X east, +Y north, +Z up, meters. Heightmap row 0 is the
  window's **north** edge (texture v = 1).
- **Camera orientation**: one formula, used everywhere —
  `rotation_euler = (radians(90 + pitch), 0, radians(−azimuth))`
  aims −Z (camera forward / sun beam) along compass `azimuth` at
  elevation `pitch`.
- **Sun** at compass 315°, altitude 45° — deliberately identical to the
  QC hillshades (`build_dem_with_buildings.hillshade` defaults), so
  Blender and engine images shade the same way.
- **Cycles, not EEVEE**: EEVEE needs a GPU/GL context that headless
  `-b` runs often lack (especially over VNC on the remote box). Cycles
  renders on CPU anywhere; enabling GPU (Metal/CUDA) in Preferences →
  System is a per-machine nicety, not a requirement.

## Importing the visibility-volume mesh

`viewshed.py --volume --volume-format mesh` writes the 3D viewshed's
boundary surface as `viewshed_volume_{id}_mesh.ply` — File > Import >
Stanford PLY drops it straight into a scene. Two practical notes:

- The mesh carries raw UTM coordinates (~2.8 × 10⁶ m northings), far
  from Blender's origin: give it a semi-transparent material and
  translate it (and any terrain you compare it against) toward the
  origin together, or expect float32 viewport jitter. The scene-bundle
  terrain lives in the *local* frame, so to overlay them subtract the
  bundle's `origin_utm` from the mesh's location.
- `--mesh-style blocky` (default) is the exact voxel boundary the
  self-checks certify; `smooth` renders more organically. Either way
  the mesh is an analysis artifact — evidence tier, unlike the Cycles
  renders around it.

## Known limits

- **Eye-level ground looks smooth and near-uniform — that is physics,
  not a bug.** At 1.5 m eye height the terrain is seen at grazing
  incidence, so each pixel integrates hundreds of ortho texels and
  converges to their mean gray (Cycles does no texture mip-filtering
  to change the look, only the noise). The drape reads properly from
  oblique and overhead cameras (`--overview`); eye-level imagery is
  the engine snapshots' job anyway. Verified by probing: same
  material shows full texture on a face-on surface in the same frame.
- The terrain is the DEM-with-buildings **heightfield**: walls carry a
  one-pixel (0.4 m) slope skirt rather than being perfectly vertical,
  exactly as the ray-casting engine sees them. Wall pixels sample the
  ortho at the wall's footprint strip, smearing roof/ground tones down
  the facade — the standard 2.5D-drape artifact. Crisp
  extruded-footprint walls would be a separate mesh path — possible
  follow-on, currently out of scope.
- The orthophoto is single-band; the drape is grayscale warmed toward
  sand in the material (`MixRGB multiply`), not true color. Renders
  use the **Khronos PBR Neutral** view transform — the AgX default
  desaturates the drape toward neutral gray.
- BlenderGIS (addon) can import the GeoTIFFs directly as a manual/GUI
  alternative — fine for exploration, but the scripted bundle is the
  reproducible path (same reasoning as `Task2.qgz` for QGIS).

## Status

Exercised 2026-07-06 with Blender 5.1.2 (brew) on the local Mac:
**cross-check passed** — `render_id1_az090.png` and
`view_id1_persp090_natural.png` show the same chapels at the same
image positions with matching silhouettes, validating the engine ray
generation and the Blender camera formula against each other. Still
untested on the remote Windows box.
