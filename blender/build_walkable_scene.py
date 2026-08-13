"""Assemble the walkable El Bagawat scene and save a .blend.

Run headless:

    blender -b --python blender/build_walkable_scene.py

Then open the saved .blend, put the cursor in the viewport and press
Shift+` to walk (W/A/S/D, mouse to look, Q/E for down/up, Tab toggles
gravity). Walk navigation is pre-set to a 1.5 m eye height with gravity
and collision on, so you stand on the ground at the project's own
observer height rather than the Blender default of 1.6 m.

Two things this has to get right or the scene is unusable:

**Everything shares one local origin.** `export_walkable_scene.py`
already shifted the terrain; the chapel OBJs are still in UTM, so they
are shifted here by the same offset from `scene.json`. Getting this
wrong puts the chapels a kilometre from the ground rather than subtly
askew, so it is loud rather than silent.

**The walls are single-sided.** The aperture meshes are open panels,
not closed solids — that is deliberate, since an any-hit occlusion test
never needs a manifold. Blender would light their back faces as if lit
from outside, so interiors would read as black. Backface culling is
therefore left off and the material is given a little translucency-free
flat diffuse, which keeps an interior legible when the only light comes
through a doorway.
"""

import json
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector

SCENE_DIR = Path("/Users/romitbasak/Projects/LAMP/LAMP_DataStore/"
                 "ElBagawat/200_Projects/260_WalkableScene")
OUT_BLEND = SCENE_DIR / "bagawat_walkable.blend"

# Kharga sits at ~25.4 N; a mid-morning sun gives raking light that
# makes doorways and reveals read as depth rather than flat patches.
SUN_ELEVATION_DEG = 42.0
SUN_AZIMUTH_DEG = 115.0
EYE_HEIGHT = 1.5


def clear_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def make_material(name, rgb, roughness=0.85):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (*rgb, 1.0)
    bsdf.inputs["Roughness"].default_value = roughness
    # Single-sided wall panels must render from both sides.
    mat.use_backface_culling = False
    return mat


def import_obj(path):
    """Import one OBJ, Z-up, with no axis conversion.

    The importer defaults to treating an OBJ as Y-up and compensates
    with a 90 deg rotation on the *object*, leaving mesh data correct
    and the world transform tipped on its side. Everything here is
    already Z-up metric survey data, so the conversion is wrong, and
    because it is applied uniformly the scene still looks plausible
    from a free-orbiting viewport — it only bites when you turn on
    gravity and fall sideways."""
    before = set(bpy.data.objects)
    if hasattr(bpy.ops.wm, "obj_import"):
        bpy.ops.wm.obj_import(filepath=str(path), forward_axis="Y",
                              up_axis="Z")
    else:                                   # Blender < 3.3
        bpy.ops.import_scene.obj(filepath=str(path), axis_forward="Y",
                                 axis_up="Z")
    return [o for o in bpy.data.objects if o not in before]


def assert_upright(objs, label):
    """Fail loudly if anything carries a leftover import rotation."""
    bad = [o.name for o in objs
           if max(abs(a) for a in o.rotation_euler) > 1e-6]
    if bad:
        print(f"FATAL: {label} imported rotated: {bad[:5]} "
              f"({len(bad)} objects) — axis conversion leaked in")
        sys.exit(1)


def main():
    meta = json.loads((SCENE_DIR / "scene.json").read_text())
    origin = meta["origin_utm"]
    ox, oy, oz = origin["x"], origin["y"], origin["z"]

    clear_scene()
    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.length_unit = "METERS"
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "Medium Contrast" if "Medium Contrast" in {
        i.identifier for i in
        bpy.types.ColorManagedViewSettings.bl_rna.properties["look"]
        .enum_items} else "None"

    ground_mat = make_material("Bagawat_Ground", (0.42, 0.35, 0.24))
    wall_mat = make_material("Bagawat_Mudbrick", (0.52, 0.43, 0.31))

    # --- ground -------------------------------------------------------
    terr = import_obj(SCENE_DIR / meta["terrain"])
    if not terr:
        print("FATAL: terrain import produced no object")
        sys.exit(1)
    assert_upright(terr, "terrain")
    for o in terr:
        o.name = "Ground"
        o.data.materials.clear()
        o.data.materials.append(ground_mat)
    print(f"ground: {len(terr[0].data.vertices):,} verts")

    # --- chapels ------------------------------------------------------
    coll = bpy.data.collections.new("Chapels")
    scene.collection.children.link(coll)
    n_ok, n_fail = 0, 0
    for b in meta["buildings"]:
        p = Path(b["path"])
        if not p.exists():
            n_fail += 1
            continue
        objs = import_obj(p)
        if not objs:
            n_fail += 1
            continue
        assert_upright(objs, f"chapel {b['id']}")
        for o in objs:
            # Shift UTM -> local origin. The OBJs are written in world
            # coordinates, so this is a mesh-data translation, not just
            # an object transform, to keep the origin at the chapel.
            mesh = o.data
            for v in mesh.vertices:
                v.co = Vector((v.co.x - ox, v.co.y - oy, v.co.z - oz))
            o.name = f"chapel_{b['id']}"
            mesh.materials.clear()
            mesh.materials.append(wall_mat)
            for c in list(o.users_collection):
                c.objects.unlink(o)
            coll.objects.link(o)
        n_ok += 1
    print(f"chapels: {n_ok} placed, {n_fail} missing")

    # --- light --------------------------------------------------------
    sun_data = bpy.data.lights.new("Sun", type="SUN")
    sun_data.energy = 3.0
    sun_data.angle = math.radians(0.53)
    sun = bpy.data.objects.new("Sun", sun_data)
    scene.collection.objects.link(sun)
    el = math.radians(SUN_ELEVATION_DEG)
    az = math.radians(SUN_AZIMUTH_DEG)
    # Compass azimuth -> Blender rotation: X tilt from vertical, Z spin.
    sun.rotation_euler = (math.radians(90.0) - el, 0.0, -az)
    sun.location = (0.0, 0.0, 200.0)

    world = bpy.data.worlds.new("Sky")
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    bg.inputs[0].default_value = (0.42, 0.55, 0.78, 1.0)
    # Sky is ambient fill, not a second sun. Turned up it washes the
    # interiors out, which defeats the point of walking into one.
    bg.inputs[1].default_value = 0.35
    scene.world = world

    # --- viewpoints ---------------------------------------------------
    spawn = (meta["observers"] or meta["spawns"] or [None])[0]
    if spawn is None:
        print("FATAL: no spawn point in scene.json")
        sys.exit(1)
    loc = (spawn["x"], spawn["y"], spawn["z"])

    cam_data = bpy.data.cameras.new("EyeCam")
    cam_data.lens = 24.0                      # ~74 deg horizontal
    cam_data.clip_start = 0.05                # see a 0.17 m wall's face
    cam_data.clip_end = 2000.0
    cam = bpy.data.objects.new("EyeCam", cam_data)
    cam.location = loc
    cam.rotation_euler = (math.radians(90.0), 0.0, 0.0)
    scene.collection.objects.link(cam)
    scene.camera = cam

    for m in meta["observers"] + meta["spawns"]:
        e = bpy.data.objects.new(m["name"], None)
        e.empty_display_type = "SPHERE"
        e.empty_display_size = 0.6
        e.location = (m["x"], m["y"], m["z"])
        scene.collection.objects.link(e)

    # --- walk navigation ----------------------------------------------
    walk = bpy.context.preferences.inputs.walk_navigation
    walk.view_height = EYE_HEIGHT
    walk.walk_speed = 2.2
    walk.use_gravity = True
    walk.teleport_time = 0.2
    bpy.context.preferences.inputs.use_mouse_continuous = True

    # EEVEE is called BLENDER_EEVEE_NEXT from Blender 4.2 and
    # BLENDER_EEVEE before it, so ask this build what it actually has
    # rather than guessing from the version number.
    engines = {e.identifier for e in
               bpy.types.RenderSettings.bl_rna.properties["engine"]
               .enum_items}
    for name in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE", "CYCLES"):
        if name in engines:
            scene.render.engine = name
            break

    # Point any saved viewport at the spawn so walking starts there.
    for screen in bpy.data.screens:
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            for space in area.spaces:
                if space.type == "VIEW_3D":
                    space.region_3d.view_location = Vector(loc)
                    space.region_3d.view_distance = 12.0
                    space.clip_start = 0.05
                    space.clip_end = 2000.0
                    space.shading.type = "MATERIAL"

    OUT_BLEND.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(OUT_BLEND))
    print(f"\nsaved {OUT_BLEND}")
    print(f"spawn at {spawn['name']} {tuple(round(v, 1) for v in loc)}")
    print("open it, hover the viewport, press Shift+` to walk")


main()
