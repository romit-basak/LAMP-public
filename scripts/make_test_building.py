"""Synthetic test building for the aperture-aware hybrid scene.

Generates the assets for step 2's first experiment (a mentor-specified
minimal case): a hollow cube chapel with a hemispherical dome and a
single door opening in the south wall, on a perfectly flat ground
plane. Because the ground is flat and the building exists only as a
triangle mesh, every occlusion in a run against these assets is
attributable to the mesh alone — no DEM extrusion to double-occlude,
no datastore dependency, and every expected sightline is analytic
(scripts/scene3d.py checks them).

Outputs (in --out-dir):
    building.obj        the chapel, door cut through the south wall
    building_solid.obj  identical geometry, no door — the control
    flat_dem.tif        constant-elevation ground, project CRS
    footprint.gpkg      the outer square (ID=1, Elevation=wall height)
    observers.gpkg      id 1 = 10 m south of the door, on axis
                        id 2 = building centre (inside)
                        id 3 = 10 m east (blank-wall control)
    building_qc.png     3D render of the mesh with key dimensions
    params.json         every parameter, for the self-checks

The synthetic site sits at real-site-like UTM coordinates so float32
precision behaviour in the engine matches production runs. Feed the
assets to the standard drivers:

    .venv/bin/python scripts/viewshed.py --dem <out>/flat_dem.tif \
        --footprints <out>/footprint.gpkg --observers <out>/observers.gpkg \
        --ids 2 --mesh <out>/building.obj --radius 40 --no-graph
"""

import argparse
import json
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Point, box

from sanity_checks import check, failures
from volume_mesh import write_obj

DEFAULT_CRS_SOURCE = Path(__file__).resolve().parent.parent / \
    "Task_2/DEM_Subset-Original.tif"


def rect(p00, p10, p11, p01, verts, faces):
    """Append a planar quad (two triangles) to the soup."""
    i = len(verts)
    verts += [p00, p10, p11, p01]
    faces += [[i, i + 1, i + 2], [i, i + 2, i + 3]]


def wall_x_span(y, x0, x1, z0, z1, verts, faces, hole=None):
    """Vertical wall panel in the y=const plane spanning x0..x1, z0..z1.
    `hole` = (hx0, hx1, hz0, hz1) cuts a rectangular opening: the panel
    becomes left/right jamb strips + header band (+ sill band when the
    hole floats above the panel base)."""
    def r(xa, xb, za, zb):
        if xb > xa and zb > za:
            rect((xa, y, za), (xb, y, za), (xb, y, zb), (xa, y, zb),
                 verts, faces)
    if hole is None:
        r(x0, x1, z0, z1)
        return
    hx0, hx1, hz0, hz1 = hole
    r(x0, hx0, z0, z1)                 # left of the opening
    r(hx1, x1, z0, z1)                 # right of the opening
    r(hx0, hx1, hz1, z1)               # header band above
    r(hx0, hx1, z0, hz0)               # sill band below (if any)


def wall_y_span(x, y0, y1, z0, z1, verts, faces):
    """Vertical wall panel in the x=const plane (no openings needed)."""
    rect((x, y0, z0), (x, y1, z0), (x, y1, z1), (x, y0, z1), verts, faces)


def dome(cx, cy, z_base, radius, n_lon, n_lat, verts, faces):
    """Lat-long hemisphere sitting on z_base; apex vertex is exact."""
    i0 = len(verts)
    for i in range(n_lat):                       # rings, equator upward
        th = (np.pi / 2) * i / n_lat
        r, z = radius * np.cos(th), z_base + radius * np.sin(th)
        for j in range(n_lon):
            ph = 2 * np.pi * j / n_lon
            verts.append((cx + r * np.cos(ph), cy + r * np.sin(ph), z))
    apex = len(verts)
    verts.append((cx, cy, z_base + radius))
    for i in range(n_lat - 1):
        for j in range(n_lon):
            a = i0 + i * n_lon + j
            b = i0 + i * n_lon + (j + 1) % n_lon
            faces += [[a, b, b + n_lon], [a, b + n_lon, a + n_lon]]
    top = i0 + (n_lat - 1) * n_lon
    for j in range(n_lon):
        faces.append([top + j, top + (j + 1) % n_lon, apex])


def build_mesh(prm, with_door):
    """The chapel as (verts (V,3) float64, faces (F,3) int64)."""
    cx, cy = prm["center"]
    gz, wh, th = prm["ground_z"], prm["wall_height"], prm["wall_thickness"]
    ho = prm["size"] / 2.0                       # outer half-width
    hi = ho - th                                 # inner half-width
    z0, z1 = gz, gz + wh
    dw2 = prm["door_width"] / 2.0
    sill, head = gz + prm["door_sill"], gz + prm["door_head"]
    hole = (cx - dw2, cx + dw2, sill, head) if with_door else None

    verts, faces = [], []
    # South wall (outer + inner face) carries the door.
    wall_x_span(cy - ho, cx - ho, cx + ho, z0, z1, verts, faces, hole)
    wall_x_span(cy - hi, cx - hi, cx + hi, z0, z1, verts, faces, hole)
    wall_x_span(cy + ho, cx - ho, cx + ho, z0, z1, verts, faces)   # north
    wall_x_span(cy + hi, cx - hi, cx + hi, z0, z1, verts, faces)
    wall_y_span(cx - ho, cy - ho, cy + ho, z0, z1, verts, faces)   # west
    wall_y_span(cx - hi, cy - hi, cy + hi, z0, z1, verts, faces)
    wall_y_span(cx + ho, cy - ho, cy + ho, z0, z1, verts, faces)   # east
    wall_y_span(cx + hi, cy - hi, cy + hi, z0, z1, verts, faces)
    if with_door:
        # The tunnel through the wall thickness: jambs + header soffit
        # (+ sill top when the opening floats above ground).
        yo, yi = cy - ho, cy - hi
        rect((cx - dw2, yo, sill), (cx - dw2, yi, sill),
             (cx - dw2, yi, head), (cx - dw2, yo, head), verts, faces)
        rect((cx + dw2, yo, sill), (cx + dw2, yi, sill),
             (cx + dw2, yi, head), (cx + dw2, yo, head), verts, faces)
        rect((cx - dw2, yo, head), (cx + dw2, yo, head),
             (cx + dw2, yi, head), (cx - dw2, yi, head), verts, faces)
        if prm["door_sill"] > 0:
            rect((cx - dw2, yo, sill), (cx + dw2, yo, sill),
                 (cx + dw2, yi, sill), (cx - dw2, yi, sill), verts, faces)
    # Flat roof cap the dome sits on (keeps over-wall rays honest: the
    # only way past the roofline is above the dome).
    rect((cx - ho, cy - ho, z1), (cx + ho, cy - ho, z1),
         (cx + ho, cy + ho, z1), (cx - ho, cy + ho, z1), verts, faces)
    dome(cx, cy, z1, prm["dome_radius"], prm["dome_lon"], prm["dome_lat"],
         verts, faces)
    return (np.asarray(verts, dtype=np.float64),
            np.asarray(faces, dtype=np.int64))


def tri_area_sum(verts, faces):
    t = verts[faces]
    return float(np.linalg.norm(
        np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]), axis=1).sum() / 2)


def qc_png(verts, faces, prm, path):
    fig = plt.figure(figsize=(9, 7))
    ax = fig.add_subplot(projection="3d")
    cx, cy = prm["center"]
    v = verts - [cx, cy, prm["ground_z"]]        # local frame for reading
    polys = v[faces]
    col = Poly3DCollection(polys, facecolor="#d9c79a", edgecolor="#6b5a3e",
                           linewidth=0.3, alpha=0.95)
    ax.add_collection3d(col)
    r = prm["size"] / 2 + 2
    ax.set_xlim(-r, r); ax.set_ylim(-r, r); ax.set_zlim(0, 2 * r)
    ax.set_box_aspect((1, 1, 1))
    ax.set_title(f"synthetic chapel — {prm['size']:g} m cube, "
                 f"{prm['dome_radius']:g} m dome, "
                 f"{prm['door_width']:g}x{prm['door_head']:g} m door (S)")
    ax.view_init(elev=22, azim=-125)
    fig.savefig(path, dpi=200, bbox_inches="tight")
    plt.close(fig)


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", type=Path,
                   default=Path("viewshed_runs/synthetic_building/assets"),
                   help="where the assets are written")
    p.add_argument("--center", type=float, nargs=2, default=[254000.0,
                   2820500.0], metavar=("X", "Y"),
                   help="building centre in the project CRS (site-like "
                        "UTM magnitudes so float32 behaviour matches "
                        "production)")
    p.add_argument("--ground-z", type=float, default=100.0,
                   help="flat ground elevation (m)")
    p.add_argument("--size", type=float, default=8.0,
                   help="outer wall length of the square building (m)")
    p.add_argument("--wall-height", type=float, default=4.0,
                   help="wall height above ground (m)")
    p.add_argument("--wall-thickness", type=float, default=0.4,
                   help="wall thickness (m); one DEM pixel, chapel-like")
    p.add_argument("--door-width", type=float, default=1.2,
                   help="door opening width (m), centred on the south wall")
    p.add_argument("--door-head", type=float, default=2.2,
                   help="door head height above ground (m)")
    p.add_argument("--door-sill", type=float, default=0.0,
                   help="door sill height above ground (m); 0 = threshold "
                        "at grade")
    p.add_argument("--dome-radius", type=float, default=None,
                   help="dome radius (m); default size/2 (full-width dome)")
    p.add_argument("--dome-lon", type=int, default=24,
                   help="dome tessellation: longitude segments")
    p.add_argument("--dome-lat", type=int, default=8,
                   help="dome tessellation: latitude rings")
    p.add_argument("--px", type=float, default=0.4,
                   help="flat DEM pixel size (m); matches the production "
                        "0.4 m grid")
    p.add_argument("--dem-cells", type=int, default=256,
                   help="flat DEM width/height in pixels")
    p.add_argument("--crs", default=None,
                   help="CRS for the outputs (e.g. EPSG:32636); default "
                        "read from Task_2/DEM_Subset-Original.tif")
    return p


def main():
    args = build_parser().parse_args()
    if args.crs is None:
        check(DEFAULT_CRS_SOURCE.exists(), "CRS source raster exists",
              str(DEFAULT_CRS_SOURCE))
        if failures:
            sys.exit(1)
        with rasterio.open(DEFAULT_CRS_SOURCE) as src:
            args.crs = str(src.crs)
    prm = {
        "center": list(args.center), "ground_z": args.ground_z,
        "size": args.size, "wall_height": args.wall_height,
        "wall_thickness": args.wall_thickness,
        "door_width": args.door_width, "door_head": args.door_head,
        "door_sill": args.door_sill,
        "dome_radius": (args.dome_radius if args.dome_radius is not None
                        else args.size / 2.0),
        "dome_lon": args.dome_lon, "dome_lat": args.dome_lat,
        "px": args.px, "dem_cells": args.dem_cells, "crs": args.crs,
    }
    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    cx, cy = prm["center"]
    gz, ho = prm["ground_z"], prm["size"] / 2.0

    verts, faces = build_mesh(prm, with_door=True)
    verts_s, faces_s = build_mesh(prm, with_door=False)
    write_obj(out / "building.obj", verts, faces,
              comment="synthetic chapel: cube + dome + south door")
    write_obj(out / "building_solid.obj", verts_s, faces_s,
              comment="synthetic chapel: cube + dome, no door (control)")

    # Flat ground raster centred on the building.
    n, px = prm["dem_cells"], prm["px"]
    half_ext = n * px / 2.0
    transform = from_origin(cx - half_ext, cy + half_ext, px, px)
    with rasterio.open(
            out / "flat_dem.tif", "w", driver="GTiff", width=n, height=n,
            count=1, dtype="float32", crs=prm["crs"], transform=transform,
            compress="lzw") as dst:
        dst.write(np.full((n, n), gz, dtype=np.float32), 1)

    gpd.GeoDataFrame(
        {"ID": [1], "Elevation": [prm["wall_height"]]},
        geometry=[box(cx - ho, cy - ho, cx + ho, cy + ho)],
        crs=prm["crs"]).to_file(out / "footprint.gpkg")
    gpd.GeoDataFrame(
        {"id": [1, 2, 3],
         "role": ["outside_door", "inside", "blank_wall"]},
        geometry=[Point(cx, cy - ho - 10.0), Point(cx, cy),
                  Point(cx + ho + 10.0, cy)],
        crs=prm["crs"]).to_file(out / "observers.gpkg")
    (out / "params.json").write_text(json.dumps(prm, indent=2) + "\n")
    qc_png(verts, faces, prm, out / "building_qc.png")

    # Exact identities of the parametrization.
    door_area = prm["door_width"] * (prm["door_head"] - prm["door_sill"])
    # The south wall appears twice (outer + inner face), so the door
    # removes twice its area; jambs/header/sill add tunnel faces that the
    # solid variant lacks — compare wall faces only via the area budget.
    tunnel = (2 * (prm["door_head"] - prm["door_sill"])
              + prm["door_width"]) * prm["wall_thickness"] \
        + (prm["door_width"] * prm["wall_thickness"]
           if prm["door_sill"] > 0 else 0)
    a_door = tri_area_sum(verts, faces)
    a_solid = tri_area_sum(verts_s, faces_s)
    check(abs((a_solid - a_door) - (2 * door_area - tunnel)) < 1e-9,
          "door variant area = solid - 2x door + tunnel faces",
          f"{a_solid - a_door:.6f} vs {2 * door_area - tunnel:.6f} m^2")
    lo, hi = verts.min(axis=0), verts.max(axis=0)
    apex = gz + prm["wall_height"] + prm["dome_radius"]
    check(np.allclose(lo, [cx - ho, cy - ho, gz], atol=1e-9)
          and np.allclose(hi, [cx + ho, cy + ho, apex], atol=1e-9),
          "mesh AABB matches the parametrization exactly")
    t = verts[faces]
    areas = np.linalg.norm(
        np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]), axis=1) / 2
    check(bool((areas > 1e-9).all()), "no degenerate triangles",
          f"{len(faces):,} triangles, min area {areas.min():.2e} m^2")
    check(any(np.allclose(v, [cx, cy, apex]) for v in verts[-2:]),
          "dome apex vertex exact", f"z = {apex:g}")

    for name in ("building.obj", "building_solid.obj", "flat_dem.tif",
                 "footprint.gpkg", "observers.gpkg", "params.json",
                 "building_qc.png"):
        print(f"  wrote {out / name}")
    if failures:
        print(f"\n{len(failures)} check(s) failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
