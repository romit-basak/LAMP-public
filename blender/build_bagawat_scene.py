"""Build and render the El Bagawat scene in Blender from a scene bundle.

Consumes the folder written by scripts/export_scene_bundle.py: terrain
heightmap -> grid mesh, orthophoto -> drape texture, observers ->
cameras at eye height, domes.json -> hemisphere caps. Runs inside
Blender's bundled Python (bpy + numpy + stdlib only — no repo imports),
headless or interactive:

    blender -b -P blender/build_bagawat_scene.py -- \
        --bundle LAMP_DataStore/ElBagawat/200_Projects/220_BuildingsToDEM/scene_bundle \
        --camera id1 --azimuth 90 --domes

Everything sits in the bundle's *local* frame (meters, +X east,
+Y north, +Z up; origin in meta.json), so Blender's float32 vertex
coordinates stay exact — never feed it raw UTM values.

Engine and division of labor: renders use Cycles (EEVEE headless needs
a GL context and is flaky under -b). These images are communication /
presentation material; the audit-grade "what the observer sees" images
come from scripts/observer_view.py, which shares the kernel the
viewshed products were validated with. See docs/BLENDER.md.
"""

import argparse
import json
import math
import sys
from pathlib import Path

import bpy
import numpy as np

SAND_DARK = (0.45, 0.38, 0.28, 1.0)
SAND_LIGHT = (0.93, 0.87, 0.72, 1.0)
SUN_AZ, SUN_ALT = 315.0, 45.0      # matches the repo's QC hillshade


def fail(msg):
    print(f"[FAIL] {msg}")
    sys.exit(1)


def euler_for(azimuth, pitch):
    """XYZ euler that aims an object's -Z axis (camera forward, sun
    beam) along compass `azimuth` at elevation `pitch`, +Y up-ish:
    forward = (sin az * cos p, cos az * cos p, sin p) in (E, N, U)."""
    return (math.radians(90.0 + pitch), 0.0, math.radians(-azimuth))


def look_at(src, dst):
    """(azimuth, pitch) pointing from src to dst (local-frame meters)."""
    dx, dy, dz = (d - s for s, d in zip(src, dst))
    dist = math.hypot(dx, dy)
    return math.degrees(math.atan2(dx, dy)), math.degrees(math.atan2(dz, dist))


def build_terrain(hm, meta, stride):
    """Grid mesh from the heightmap via foreach_set (from_pydata is far
    too slow at ~1M vertices). Quad winding is CCW seen from +Z so the
    normals point up; needs Blender >= 3.6 (loop_total is derived)."""
    hm = hm[::stride, ::stride]
    h, w = hm.shape
    px = meta["px_m"] * stride
    xs = meta["x_first_local"] + np.arange(w) * px
    ys = meta["y_first_local"] - np.arange(h) * px
    gx, gy = np.meshgrid(xs, ys)
    co = np.column_stack([gx.ravel(), gy.ravel(),
                          hm.ravel()]).astype(np.float32)

    mesh = bpy.data.meshes.new("terrain")
    mesh.vertices.add(h * w)
    mesh.vertices.foreach_set("co", co.ravel())

    r = np.arange(h - 1, dtype=np.int64)[:, None]
    c = np.arange(w - 1, dtype=np.int64)[None, :]
    i00 = (r * w + c).ravel()
    quads = np.column_stack([i00, i00 + w, i00 + w + 1, i00 + 1])
    n_f = len(quads)
    mesh.loops.add(4 * n_f)
    mesh.polygons.add(n_f)
    mesh.polygons.foreach_set("loop_start",
                              np.arange(n_f, dtype=np.int64) * 4)
    mesh.loops.foreach_set("vertex_index", quads.ravel())

    uv = mesh.uv_layers.new(name="UVMap")
    vcol = quads.ravel() % w
    vrow = quads.ravel() // w
    # Texture row 0 is the window's north edge; image v=1 is the top.
    uvs = np.column_stack([vcol / max(w - 1, 1),
                           1.0 - vrow / max(h - 1, 1)]).astype(np.float32)
    uv.data.foreach_set("uv", uvs.ravel())

    mesh.update()
    mesh.validate()
    mesh.polygons.foreach_set("use_smooth", np.ones(n_f, dtype=bool))
    obj = bpy.data.objects.new("terrain", mesh)
    bpy.context.scene.collection.objects.link(obj)
    print(f"  terrain: {h}x{w} verts (stride {stride}), {n_f:,} quads")
    return obj


def terrain_material(bundle, meta, height_ramp):
    mat = bpy.data.materials.new("terrain_mat")
    mat.use_nodes = True
    nodes = mat.node_tree.nodes
    links = mat.node_tree.links
    bsdf = nodes["Principled BSDF"]
    bsdf.inputs["Roughness"].default_value = 0.95
    ortho = bundle / (meta.get("ortho") or "")
    if not height_ramp and meta.get("ortho") and ortho.exists():
        tex = nodes.new("ShaderNodeTexImage")
        tex.image = bpy.data.images.load(str(ortho))
        # Warm the grayscale drape toward sand instead of rendering it
        # as bare gray.
        mix = nodes.new("ShaderNodeMixRGB")
        mix.blend_type = "MULTIPLY"
        mix.inputs["Fac"].default_value = 0.85
        mix.inputs["Color2"].default_value = (0.95, 0.84, 0.66, 1.0)
        links.new(tex.outputs["Color"], mix.inputs["Color1"])
        links.new(mix.outputs["Color"], bsdf.inputs["Base Color"])
        print("  material: orthophoto drape")
    else:
        geo = nodes.new("ShaderNodeNewGeometry")
        sep = nodes.new("ShaderNodeSeparateXYZ")
        rng = nodes.new("ShaderNodeMapRange")
        rng.inputs["From Min"].default_value = meta["z_min"]
        rng.inputs["From Max"].default_value = meta["z_max"]
        ramp = nodes.new("ShaderNodeValToRGB")
        ramp.color_ramp.elements[0].color = SAND_DARK
        ramp.color_ramp.elements[1].color = SAND_LIGHT
        links.new(geo.outputs["Position"], sep.inputs["Vector"])
        links.new(sep.outputs["Z"], rng.inputs["Value"])
        links.new(rng.outputs["Result"], ramp.inputs["Fac"])
        links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])
        print("  material: elevation ramp (no ortho)")
    return mat


def add_domes(domes, mat):
    for d in domes:
        bpy.ops.mesh.primitive_uv_sphere_add(
            radius=d["radius_m"], segments=24, ring_count=12,
            location=(d["cx_local"], d["cy_local"], d["spring_z"]))
        obj = bpy.context.active_object
        obj.name = f"dome_{d['id']}"
        obj.data.materials.append(mat)
        bpy.ops.object.shade_smooth()
    print(f"  domes: {len(domes)} hemisphere caps "
          "(lower half submerged in the roof)")


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    p = argparse.ArgumentParser(description=__doc__, prog="build_bagawat_scene")
    p.add_argument("--bundle", type=Path, required=True,
                   help="scene_bundle folder from export_scene_bundle.py")
    p.add_argument("--render-dir", type=Path, default=None,
                   help="output folder (default <bundle>/../blender_renders)")
    p.add_argument("--camera", nargs="+", default=["all"],
                   help="observer id(s) to render from (default all)")
    p.add_argument("--azimuth", type=float, nargs="+", default=[90.0],
                   help="compass azimuth(s) per camera (default 90)")
    p.add_argument("--pitch", type=float, default=0.0,
                   help="camera elevation angle, degrees (0=horizontal, "
                        "+ up; applied to every azimuth/camera)")
    p.add_argument("--fov", type=float, default=60.0,
                   help="horizontal field of view, degrees (default 60)")
    p.add_argument("--size", type=int, nargs=2, default=[720, 480],
                   metavar=("W", "H"), help="render resolution in pixels")
    p.add_argument("--samples", type=int, default=64,
                   help="Cycles samples (16 for drafts)")
    p.add_argument("--stride", type=int, default=1,
                   help="heightmap decimation for drafts (1 = full res)")
    p.add_argument("--domes", action="store_true",
                   help="add hemisphere caps from domes.json")
    p.add_argument("--height-ramp", action="store_true",
                   help="color by elevation instead of the orthophoto")
    p.add_argument("--overview", action="store_true",
                   help="also render a high-oblique site overview")
    p.add_argument("--no-render", action="store_true",
                   help="build the scene only (use with --save-blend)")
    p.add_argument("--save-blend", action="store_true",
                   help="save <bundle>/scene.blend for interactive work")
    args = p.parse_args(argv)
    # Absolutize before anything touches Blender: image paths loaded
    # relative to an *unsaved* blend resolve unpredictably, and saving
    # the scene would remap them into a broken chain of ../ segments.
    args.bundle = args.bundle.resolve()
    if args.render_dir is not None:
        args.render_dir = args.render_dir.resolve()

    for name in ("meta.json", "heightmap.npy", "observers.json",
                 "domes.json"):
        if not (args.bundle / name).exists():
            fail(f"bundle incomplete: missing {name} in {args.bundle}")
    meta = json.loads((args.bundle / "meta.json").read_text())
    hm = np.load(args.bundle / "heightmap.npy")
    observers = json.loads((args.bundle / "observers.json").read_text())
    domes = json.loads((args.bundle / "domes.json").read_text())
    render_dir = (args.render_dir if args.render_dir
                  else args.bundle.parent / "blender_renders")

    print("=" * 70)
    print(f"BAGAWAT SCENE   bundle: {args.bundle}")
    print(f"  origin UTM {meta['origin_utm']}  ({meta['source_dem']})")
    print("=" * 70)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.unit_settings.system = "METRIC"

    terrain = build_terrain(hm, meta, max(args.stride, 1))
    terrain.data.materials.append(
        terrain_material(args.bundle, meta, args.height_ramp))
    if args.domes:
        dome_mat = bpy.data.materials.new("dome_mat")
        dome_mat.use_nodes = True
        bsdf = dome_mat.node_tree.nodes["Principled BSDF"]
        bsdf.inputs["Base Color"].default_value = (0.91, 0.83, 0.67, 1.0)
        bsdf.inputs["Roughness"].default_value = 0.9
        add_domes(domes, dome_mat)

    sun_data = bpy.data.lights.new("sun", type="SUN")
    sun_data.energy = 4.0
    sun_data.angle = math.radians(0.75)
    sun = bpy.data.objects.new("sun", sun_data)
    sun.rotation_euler = euler_for((SUN_AZ + 180.0) % 360.0, -SUN_ALT)
    sc.collection.objects.link(sun)

    world = bpy.data.worlds.new("world")
    world.use_nodes = True
    bg = world.node_tree.nodes["Background"]
    bg.inputs[0].default_value = (0.55, 0.70, 0.90, 1.0)
    bg.inputs[1].default_value = 0.7
    sc.world = world

    cam_data = bpy.data.cameras.new("obscam")
    cam_data.sensor_fit = "HORIZONTAL"
    cam_data.angle = math.radians(args.fov)
    # Defaults clip at 100 m — the site is ~2 km across.
    cam_data.clip_start = 0.05
    cam_data.clip_end = 10000.0
    cam = bpy.data.objects.new("obscam", cam_data)
    sc.collection.objects.link(cam)
    sc.camera = cam

    sc.render.engine = "CYCLES"
    sc.cycles.samples = args.samples
    sc.cycles.use_denoising = True
    sc.render.resolution_x, sc.render.resolution_y = args.size
    sc.render.image_settings.file_format = "PNG"
    # The default AgX view transform desaturates the drape toward
    # neutral gray; PBR Neutral keeps the albedo readable. Fall back
    # silently where the transform doesn't exist (pre-4.1).
    try:
        sc.view_settings.view_transform = "Khronos PBR Neutral"
    except TypeError:
        pass

    by_id = {o["id"]: o for o in observers}
    wanted = list(by_id) if args.camera == ["all"] else args.camera
    missing = [c for c in wanted if c not in by_id]
    if missing:
        fail(f"unknown camera id(s) {missing}; bundle has {list(by_id)}")

    if not args.no_render:
        render_dir.mkdir(parents=True, exist_ok=True)
        for cid in wanted:
            o = by_id[cid]
            cam.location = (o["x_local"], o["y_local"], o["eye_z"])
            for az in args.azimuth:
                cam.rotation_euler = euler_for(az, args.pitch)
                out = render_dir / (f"render_{cid}_"
                                    f"az{int(round(az)) % 360:03d}.png")
                sc.render.filepath = str(out)
                print(f"  rendering {out.name} "
                      f"({args.size[0]}x{args.size[1]}, "
                      f"{args.samples} samples)...")
                bpy.ops.render.render(write_still=True)
        if args.overview:
            xs0 = meta["x_first_local"]
            ys0 = meta["y_first_local"]
            cx = xs0 + (meta["width_px"] - 1) * meta["px_m"] / 2
            cy = ys0 - (meta["height_px"] - 1) * meta["px_m"] / 2
            span = max(meta["width_px"], meta["height_px"]) * meta["px_m"]
            eye = (cx, cy - 0.75 * span, meta["z_max"] + 0.45 * span)
            az, pitch = look_at(eye, (cx, cy, (meta["z_min"]
                                               + meta["z_max"]) / 2))
            cam.location = eye
            cam.rotation_euler = euler_for(az, pitch)
            sc.render.filepath = str(render_dir / "render_overview.png")
            print("  rendering render_overview.png ...")
            bpy.ops.render.render(write_still=True)

    if args.save_blend:
        blend = args.bundle / "scene.blend"
        bpy.ops.wm.save_as_mainfile(filepath=str(blend))
        print(f"  saved {blend}")

    print("Scene build complete.")


if __name__ == "__main__":
    main()
