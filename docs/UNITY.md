# Unity walkthrough — interactive first-person exploration

Unity's role here is **experience**: walking the necropolis at eye
height in real time (or VR later). It is *not* a report artifact — a
Unity build is neither headless-reproducible nor produced by the
validated engine — so treat it as a demo layer on top of the evidence
(`scripts/observer_view.py`) and communication (Blender) tiers. See the
table in `docs/BLENDER.md`.

No Unity project is committed to this repo. Everything below starts
from the exported scene bundle:

```bash
.venv/bin/python scripts/export_scene_bundle.py --unity
```

which adds `heightmap_unity.raw` (16-bit, square, `--unity-res` 1025 by
default) and a `unity` block to `meta.json` with the exact numbers the
import dialog asks for.

## 1. Terrain import (Unity 2022.3 LTS or Unity 6 LTS)

1. New 3D project → GameObject → 3D Object → **Terrain**.
2. Select the Terrain → gear tab (Terrain Settings) → *Texture
   Resolutions* → **Import Raw…** → pick `heightmap_unity.raw`:
   - **Depth**: Bit 16
   - **Resolution**: the `unity.resolution` value from meta.json (1025)
   - **Byte order**: **Windows** (the file is little-endian)
   - **Flip Vertically**: start **off** — the file is written row 0 =
     south, matching Unity's terrain origin at the SW corner. If the
     site comes in mirrored north–south (compare against
     `Task_2/Site_Plan.pdf`; the layout is asymmetric enough to make it
     obvious), re-import with the toggle on and note it in PROGRESS.md.
   - **Terrain Size**: X = `terrain_width_m`, Z = `terrain_length_m`,
     Y (height) = `terrain_height_m` — all in the meta `unity` block.
3. Set the Terrain GameObject's **position Y = `y_offset_m`** (the
   window's lowest elevation). World Y now equals real elevation in
   meters, so the `eye_z` values in `observers.json` are directly
   usable as camera heights.
4. Heightmap resolution must be (2^n)+1; export `--unity-res 2049` for
   a sharper terrain if the machine handles it.

## 2. Orthophoto drape

Copy `ortho.png` into `Assets/`, then Terrain → Paint Terrain → *Edit
Terrain Layers* → Create Layer with it. In the layer, set **Size** to
(`terrain_width_m`, `terrain_length_m`) and offset (0, 0) so the image
stretches exactly once across the terrain. It is a grayscale image;
tint the layer toward sand if it reads too flat.

## 3. Coordinate mapping (bundle → Unity)

Unity is left-handed, Y-up: **x = east, z = north, y = elevation**.
The bundle's local frame has its origin at the window *center*; Unity's
terrain origin is its **SW corner**. For any bundle point
(`x_local`, `y_local`, z):

```text
unity_x = x_local - x_first_local
unity_z = y_local - (y_first_local - (height_px - 1) * px_m)
unity_y = z                      # terrain Y-offset already applied
```

(`x_first_local`, `y_first_local`, `height_px`, `px_m` are in
meta.json; the half-pixel cell-center offset is ignorable at
walkthrough scale.)

## 4. Observer spawn + first-person controller

Install the free **Starter Assets – FirstPersonController** package
(Package Manager / Asset Store), drop the PlayerCapsule prefab in, and
place it with this component (values copied from `observers.json` /
`meta.json` into the Inspector — no JSON parsing needed for a demo):

```csharp
using UnityEngine;

// Positions this object at a scene-bundle observer. Paste x_local /
// y_local / eye_z from observers.json and the three meta.json grid
// values; see docs/UNITY.md section 3 for the mapping.
public class PlaceAtObserver : MonoBehaviour
{
    public float xLocal, yLocal, eyeZ;          // observers.json
    public float xFirstLocal, yFirstLocal;      // meta.json
    public float pxM = 0.4f;
    public int heightPx;

    void Start()
    {
        float unityX = xLocal - xFirstLocal;
        float unityZ = yLocal - (yFirstLocal - (heightPx - 1) * pxM);
        transform.position = new Vector3(unityX, eyeZ + 0.1f, unityZ);
    }
}
```

Set the controller's camera root to 1.5 m above the feet (the
project's eye-height convention — Starter Assets defaults to ~1.375 m).
For automation later, `JsonUtility.FromJson` over `observers.json` in
`StreamingAssets/` replaces the hand-copied fields.

## 5. Domes

`domes.json` → default spheres: GameObject → 3D Object → Sphere, scale
= `radius_m * 2` on all axes, position via the section-3 mapping with
y = `spring_z`. The lower hemisphere ends up submerged in the roof —
the same hemisphere-cap convention as the QGIS and Blender layers. A
dozen key chapels by hand is plenty for a demo; scripting all 117 is an
editor-script exercise if the walkthrough graduates beyond demo status.

## When Unity earns its keep — and when it doesn't

Worth it: live mentor demos ("stand where the observer stands"),
embodied-experience arguments, an eventual VR showing. Not worth it:
anything that ends up in the comparison report — those images must come
from `observer_view.py` (validated kernel) or, for beauty shots,
Blender (scriptable, reproducible). Unity sessions are hand-driven by
nature; keep them out of the evidence chain. Revisit after the
apertures milestone (build-order step 2) — apertures are also when an
interactive walkthrough becomes genuinely interesting, since doorways
and windows will actually see through.
