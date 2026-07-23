"""Surface-mesh extraction for 3D visibility volumes.

Turns a boolean voxel grid (the `--volume` output of the viewshed
engine) into the boundary surface of the visible region — the volume's
actual 3D *shape* (faces + edges) instead of a cloud of voxel centers.

Two styles:
    blocky  exact voxel-boundary faces (pure NumPy). Represents
            precisely the computed voxel set: the mesh's enclosed
            volume equals n_visible x voxel volume *identically*,
            which doubles as a hard self-check. Evidence-grade.
    smooth  marching-cubes isosurface through the voxel boundary
            (scikit-image, imported lazily). Reads organically;
            corners get shaved, so the volume only approximates the
            voxel count. Presentation-grade.

Everything here works in *index space* (`vis[row, col, level]`
fractional indices); `index_to_world` is the single place index axes
meet world axes, including the terrain-following warp that turns
above-ground level heights into absolute elevations.

This module must stay torch-free: `volume_convert.py` imports it, and
that script deliberately avoids the heavyweight torch import that
comes with `viewshed.py`.
"""

from pathlib import Path

import numpy as np
from scipy import ndimage

from sanity_checks import check, warn

# In-plane axes for each face axis, in cyclic order so (axis, u, v) is
# right-handed; the base corner loop below is then counter-clockwise
# seen from the +axis side.
_INPLANE = {0: (1, 2), 1: (2, 0), 2: (0, 1)}


def fill_nan_nearest(a):
    """Fill NaNs with the nearest valid cell's value. Boundary-face
    corners near nodata columns are bilinearly interpolated from up to
    four columns; a global-min fill would drag those vertices toward a
    phantom cliff, while nearest-fill keeps them on adjacent real
    terrain. (The fill can't perturb the mesh-volume identity: any
    per-(x, y) z-shift is a volume-preserving shear.)"""
    mask = np.isnan(a)
    if not mask.any():
        return a
    idx = ndimage.distance_transform_edt(
        mask, return_distances=False, return_indices=True)
    return a[tuple(idx)]


def blocky_mesh(vis):
    """Exact boundary surface of the True-voxel set. Returns
    (verts, faces): verts (V, 3) float64 fractional (row, col, level)
    indices on the half-integer lattice; faces (F, 3) int32 triangles
    wound outward (counter-clockwise seen from outside) for a
    right-handed index frame.

    Corners are assembled in *doubled-index integers* (voxel centers at
    even, face planes at odd values) so vertex dedup via np.unique is
    exact — shared corners collapse bit-identically, making the surface
    closed by construction."""
    p = np.pad(vis, 1).astype(np.int8)
    quads = []
    for a, (u, v) in _INPLANE.items():
        d = np.diff(p, axis=a)
        for sign in (1, -1):
            idx = np.argwhere(d == sign)
            if not len(idx):
                continue
            n = len(idx)
            # Doubled unpadded coords: the face plane sits at the
            # half-integer index between the empty/solid voxel pair;
            # the quad spans +/-0.5 on the two in-plane axes.
            plane = 2 * idx[:, a] - 1
            u_lo, u_hi = 2 * idx[:, u] - 3, 2 * idx[:, u] - 1
            v_lo, v_hi = 2 * idx[:, v] - 3, 2 * idx[:, v] - 1
            c = np.empty((n, 4, 3), dtype=np.int64)
            c[:, :, a] = plane[:, None]
            c[:, 0, u], c[:, 0, v] = u_lo, v_lo
            c[:, 1, u], c[:, 1, v] = u_hi, v_lo
            c[:, 2, u], c[:, 2, v] = u_hi, v_hi
            c[:, 3, u], c[:, 3, v] = u_lo, v_hi
            if sign == 1:
                # diff +1 = solid begins: outward normal is -axis, so
                # reverse the loop — but as (0,3,2,1), keeping corner 0
                # at (u-,v-) so the triangulation diagonal below stays
                # the (-,-)->(+,+) one on both face directions.
                c = c[:, [0, 3, 2, 1], :]
            quads.append(c.reshape(-1, 3))
    if not quads:
        return np.empty((0, 3)), np.empty((0, 3), dtype=np.int32)
    corners = np.concatenate(quads, axis=0)
    verts2, inv = np.unique(corners, axis=0, return_inverse=True)
    q = inv.reshape(-1, 4).astype(np.int32)
    # Fixed plan-diagonal rule: split every quad along corner0-corner2,
    # i.e. (u-,v-) -> (u+,v+). Load-bearing for +/-level faces: after
    # the terrain warp those quads are non-planar, and only a
    # plan-consistent diagonal makes the enclosed volume telescope per
    # column so it stays exactly n x voxel volume. (Vertical faces keep
    # a zero-z normal under the warp, so they never contribute.)
    faces = np.concatenate([q[:, [0, 1, 2]], q[:, [0, 2, 3]]], axis=0)
    return verts2.astype(np.float64) / 2.0, faces


def smooth_mesh(vis):
    """Marching-cubes isosurface at the voxel boundary (level 0.5 on a
    one-voxel False padding, so the surface closes at grid edges).
    Same return convention as blocky_mesh; (None, None) + a failed
    check when scikit-image is missing."""
    try:
        from skimage.measure import marching_cubes
    except ImportError as exc:
        check(False, "scikit-image available for --mesh-style smooth",
              str(exc))
        return None, None
    p = np.pad(vis, 1).astype(np.float32)
    verts, faces, _, _ = marching_cubes(p, level=0.5)
    verts = verts.astype(np.float64) - 1.0        # undo the padding shift
    faces = np.asarray(faces, dtype=np.int32)
    # Normalize orientation: marching_cubes' winding convention is not
    # worth memorizing — for a closed surface, positive index-space
    # signed volume *is* the outward orientation.
    if signed_mesh_volume(verts, faces) < 0:
        faces = faces[:, ::-1].copy()
    return verts, faces


def index_to_world(verts, faces, x0, dx, y0, dy, z0, dz, ground=None):
    """Map fractional (row, col, level) vertices to world coordinates.
    The one place index axes meet world axes: col -> x, row -> y (pass
    dy NEGATIVE for the engine's north->south descending rows), level
    -> z. `ground` (2D, NaN-free, indexed [row, col]) adds the
    terrain-following warp for AGL grids: z = ground(x, y) + level.
    A mirrored map (dx*dy*dz < 0) flips face winding once here so the
    outward orientation survives."""
    x = x0 + verts[:, 1] * dx
    y = y0 + verts[:, 0] * dy
    z = z0 + verts[:, 2] * dz
    if ground is not None:
        # Sampled once per deduped vertex, so faces sharing a corner
        # stay bit-identical there (keeps the surface closed). Boundary
        # faces overhang the center lattice by half a step; nearest
        # clamping matches how the volume itself treats its rim.
        z = z + ndimage.map_coordinates(
            ground, [verts[:, 0], verts[:, 1]], order=1, mode="nearest")
    # Orientation: the (row, col, level) -> (x, y, z) relabeling swaps
    # the first two axes (det -1), and the scaling contributes
    # sign(dx*dy*dz). Winding must flip iff the total map is mirrored,
    # i.e. when dx*dy*dz is POSITIVE. The engine's dx>0, dy<0, dz>0
    # therefore needs no flip — the axis swap and the negative dy
    # cancel. (The terrain shear never changes the determinant.)
    if dx * dy * dz > 0:
        faces = faces[:, ::-1].copy()
    return np.column_stack([x, y, z]), faces


def signed_mesh_volume(verts, faces):
    """Enclosed volume of a closed triangle mesh (divergence theorem:
    sum of signed tetrahedra to the centroid). Vertices are centered
    first — raw UTM coords (~2.8e6 m) push the triple products to
    ~1e19, where float64 rounds at ~1e3 m^3 per term; centered coords
    keep the roundoff ~1e-12 relative, and the signed volume of a
    closed surface is translation-invariant anyway."""
    v = verts - verts.mean(axis=0)
    a, b, c = v[faces[:, 0]], v[faces[:, 1]], v[faces[:, 2]]
    return float(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0)


def run_mesh_checks(verts, faces, nvis, voxvol, bounds, steps, style):
    """Physical invariants of the extracted mesh. `bounds` are the
    voxel-CENTER extents (xlo, xhi, ylo, yhi, zlo, zhi); boundary faces
    legitimately overhang them by half a step. Returns the signed
    volume so callers can report it."""
    check(len(faces) > 0, "mesh non-empty for visible volume",
          f"{nvis:,} voxels -> {len(faces):,} faces")
    if not len(faces):
        return 0.0
    check(int(faces.min()) >= 0 and int(faces.max()) < len(verts),
          "mesh face indices in range")
    xlo, xhi, ylo, yhi, zlo, zhi = bounds
    sx, sy, sz = (0.5 * s + 1e-6 for s in steps)
    inb = ((verts[:, 0] >= xlo - sx) & (verts[:, 0] <= xhi + sx) &
           (verts[:, 1] >= ylo - sy) & (verts[:, 1] <= yhi + sy) &
           (verts[:, 2] >= zlo - sz) & (verts[:, 2] <= zhi + sz))
    check(bool(inb.all()), "mesh vertices inside the volume extent",
          f"{int((~inb).sum()):,} outside" if not inb.all() else "")

    vol = signed_mesh_volume(verts, faces)
    target = nvis * voxvol
    if style == "blocky":
        # Exact identity: the boundary mesh of a voxel set encloses
        # exactly its voxels (the terrain warp is a shear, see the
        # diagonal-rule comment in blocky_mesh). The band sits ~4
        # orders above centered-float64 roundoff yet still catches a
        # single miswound face (~half a voxel volume).
        check(abs(vol - target) <= max(1e-8 * target, 1e-6),
              "mesh volume == n_voxels x voxel volume",
              f"{vol:,.3f} vs {target:,.3f} m^3")
    else:
        check(vol > 0, "mesh volume positive (winding sane)",
              f"{vol:,.3f} m^3")
        ratio = vol / target if target else 0.0
        # Marching cubes shaves corners (an isolated voxel keeps only
        # 1/6 of its volume); blob-dominated volumes land ~0.8-1.0.
        if not 0.5 <= ratio <= 1.05:
            warn("smooth mesh volume far from voxel volume",
                 f"ratio {ratio:.2f} — fragmented voxel set? "
                 "--mesh-style blocky is exact")

    # Closed surface: every undirected edge borders an even number of
    # faces (2 normally; 4 where voxels touch only along an edge — a
    # legitimate non-manifold seam in a voxel solid).
    edges = np.sort(faces[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2), axis=1)
    _, cnt = np.unique(edges, axis=0, return_counts=True)
    closed = bool((cnt % 2 == 0).all())
    detail = f"{int((cnt % 2 != 0).sum()):,} unpaired edges"
    if style == "blocky":
        check(closed, "mesh closed (all edges pair up)",
              "" if closed else detail)
    elif not closed:
        warn("smooth mesh not perfectly closed",
             detail + " (known marching-cubes-on-binary quirk)")
    return vol


def write_ply(path, pts, count=None):
    """ASCII PLY, vertices only (a voxel-center point cloud, not a
    mesh — no faces). Shared by viewshed.py and volume_convert.py so
    the point-cloud writer exists in exactly one place."""
    header = ["ply", "format ascii 1.0", f"element vertex {len(pts)}",
              "property float x", "property float y", "property float z"]
    if count is not None:
        header.append("property int count")
    header.append("end_header")
    with open(path, "w") as f:
        f.write("\n".join(header) + "\n")
        if count is None:
            np.savetxt(f, pts, fmt="%.3f")
        else:
            np.savetxt(f, np.column_stack([pts, count]),
                       fmt=["%.3f", "%.3f", "%.3f", "%d"])


def write_las(path, pts, crs, count=None):
    """Point cloud as LAS/LAZ (.las/.laz by path suffix); `count` (a
    combined-volume observer tally) is stored as intensity. The CRS is
    written into the header so QGIS/CloudCompare georeference it.
    Needs laspy (+ lazrs for .laz); missing deps or an empty point set
    are reported through `warn` and skipped rather than raised, same as
    every other optional-format writer in this project."""
    try:
        import laspy
    except ImportError as exc:
        warn("LAS/LAZ skipped", f"install laspy (+lazrs for .laz): {exc}")
        return
    if len(pts) == 0:
        warn("LAS/LAZ skipped", f"no points for {Path(path).name}")
        return
    header = laspy.LasHeader(point_format=3, version="1.4")
    header.offsets = pts.min(axis=0)
    header.scales = [0.001, 0.001, 0.001]   # mm precision on large UTM coords
    if crs is not None:
        from pyproj import CRS as PyCRS
        header.add_crs(PyCRS.from_user_input(str(crs)))
    las = laspy.LasData(header)
    las.x, las.y, las.z = pts[:, 0], pts[:, 1], pts[:, 2]
    if count is not None:
        las.intensity = np.asarray(count, dtype=np.uint16)
    las.write(str(path))


def write_mesh_ply(path, verts, faces, comment=""):
    """ASCII PLY with vertices AND triangle faces. Coordinates are
    declared double: float32 would quantize UTM northings (~2.8e6 m)
    to ~0.25 m — the same precision cliff the scene bundle sidesteps
    with a local origin."""
    header = ["ply", "format ascii 1.0"]
    if comment:
        header.append(f"comment {comment}")
    header += [f"element vertex {len(verts)}",
               "property double x",
               "property double y",
               "property double z",
               f"element face {len(faces)}",
               "property list uchar int vertex_indices",
               "end_header"]
    with open(path, "w") as f:
        f.write("\n".join(header) + "\n")
        np.savetxt(f, verts, fmt="%.3f")
        np.savetxt(f, np.column_stack([np.full(len(faces), 3), faces]),
                   fmt="%d")
