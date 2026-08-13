"""Which interior features can be seen, from where, and through what.

Niches and apses are pockets, not holes, so they contribute nothing to
what a viewshed *counts* — the sweep that added 168 niches changed the
visible ground by exactly zero cells. That is the right answer and it
is also the uninteresting one. These features are not apertures; they
are the things worth looking at, and the question they exist to answer
runs the other way: does a sightline reach *into* one?

Three questions, three observer classes:

1. **Standing at its own doorway.** A person outside a chapel, on the
   door's axis, at eye height. What of the interior does the doorway
   itself frame?
2. **From other chapels' doorways.** The same stations, applied across
   the site — the inter-visibility question, but aimed at a named
   feature instead of a centroid.
3. **Through one named aperture.** For every door and window in a
   chapel, whether it admits a sightline to each interior feature, and
   how far off that aperture's axis the feature sits.

`through_aperture` is exact rather than angular. The segment is
intersected with the wall slab and the crossing point is required to
land inside the opening's clear rectangle at *both* the outer ring
plane and the inner face plane. An oblique ray can clip the mouth of a
deep reveal and still be stopped by its jamb, so testing only the near
plane would credit sightlines the mesh blocks — and wall depth deciding
that is the entire reason this project models thickness.

Two numbers are reported per aperture-feature pair because they answer
different things. `axis_offset_deg` is pure geometry: the angle between
the aperture's inward axis and the feature, ignoring who is looking. A
small offset means the opening points at the feature. `visible` is the
ray-cast answer from a standing observer outside, which additionally
depends on eye height, terrain and every other building on the site. A
window can point straight at a niche and still show it to nobody.

Observers stand at a fixed 1.5 m eye height whatever the aperture's own
height. A high window that a standing person cannot see through is a
real finding about that window, not a reason to move the eye.
"""

import argparse
import csv
import json
import math
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio

from sanity_checks import (FOOTPRINTS, DEM_BASE_04, DEM_REGEN,
                           check, warn, failures)
from aperture_registry import (APERTURES_DIR, INVENTORY, BUILDING_FABRIC,
                               DOOR_WIDTH, DOOR_HEAD, DOOR_SILL,
                               canonical_walls, largest_poly,
                               resolve_wall, opening_rect)
from build_aperture_walls import built_thickness, load_fabric, plane_fit
from viewshed import select_device, load_dem, HeightfieldScene
from scene3d import HybridScene, flatten_footprints, load_scene_meshes

STAND_OFF_M = 1.5          # observer this far outside the opening
EYE_HEIGHT = 1.5           # project default, not the 1.75 m GIS one
FEATURE_KINDS = ("niche", "apse")
MESH_DIR = APERTURES_DIR / "meshes_all_fabric"


def unit(dx, dy):
    """Normalised 2D direction plus the length it was divided by."""
    L = math.hypot(dx, dy)
    return (dx / L, dy / L, L)


def build_openings(fp, reg, fabric, ground_z, thickness_default):
    """Per-chapel opening geometry, in the mesh builder's own terms."""
    by_id = {}
    for r in reg:
        by_id.setdefault(int(r["ID"]), []).append(r)

    out = {}
    for _, frow in fp.iterrows():
        bid = int(frow["ID"])
        if bid not in by_id:
            continue
        geom = frow.geometry
        walls = canonical_walls(geom)
        if len(walls) < 3:
            continue
        elev = (float(frow["Elevation"])
                if np.isfinite(frow.get("Elevation", np.nan)) else 0.0)
        plane = plane_fit(geom, elev if elev > 0 else 3.5, ground_z)
        nominal = fabric.get(bid, thickness_default)
        th = built_thickness(geom, nominal, nominal)
        inside = largest_poly(geom).representative_point()

        aps = []
        for r in by_id[bid]:
            if r["kind"] not in ("door", "window"):
                continue
            wi = resolve_wall(walls, r, check, warn)
            if wi is None:
                continue
            p0, p1 = walls[wi]
            rect = opening_rect(p0, p1, r, ground_z, plane, DOOR_WIDTH,
                                DOOR_SILL, DOOR_HEAD, warn)
            s0, s1, zl, zh = rect
            if zh <= zl:
                continue
            ux, uy, _ = unit(p1[0] - p0[0], p1[1] - p0[1])
            nx, ny = -uy, ux
            sm = (s0 + s1) / 2.0
            cx, cy = p0[0] + ux * sm, p0[1] + uy * sm
            if (inside.x - cx) * nx + (inside.y - cy) * ny < 0:
                nx, ny = -nx, -ny        # inward
            gz = float(ground_z([(cx, cy)])[0])
            aps.append(dict(
                ID=bid, ap_id=r["ap_id"], kind=r["kind"], wall=wi,
                p0=p0, u=(ux, uy), n_in=(nx, ny), rect=rect,
                thickness=th,
                centre=(cx, cy, (zl + zh) / 2.0),
                eye=(cx - nx * STAND_OFF_M, cy - ny * STAND_OFF_M,
                     gz + EYE_HEIGHT)))
        if aps:
            out[bid] = aps
    return out


def crosses_opening(eye, tgt, ap):
    """Does the segment pass through this opening's clear rectangle?

    Checked at the outer ring plane and, where the wall has depth,
    again at the inner face. The inner panel is a translate of the
    outer one whose parametrisation starts one thickness earlier and
    whose openings are shifted by the same amount, so an opening keeps
    the same `s` on both planes and one set of bounds serves for
    both."""
    p0, u, n = ap["p0"], ap["u"], ap["n_in"]
    s0, s1, zl, zh = ap["rect"]
    dx, dy, dz = tgt[0] - eye[0], tgt[1] - eye[1], tgt[2] - eye[2]
    den = dx * n[0] + dy * n[1]
    if abs(den) < 1e-9:
        return False
    offsets = (0.0, ap["thickness"]) if ap["thickness"] > 0 else (0.0,)
    for off in offsets:
        px, py = p0[0] + n[0] * off, p0[1] + n[1] * off
        t = ((px - eye[0]) * n[0] + (py - eye[1]) * n[1]) / den
        if not 0.0 < t < 1.0:
            return False
        x, y, z = eye[0] + t * dx, eye[1] + t * dy, eye[2] + t * dz
        s = (x - p0[0]) * u[0] + (y - p0[1]) * u[1]
        if not (s0 <= s <= s1 and zl <= z <= zh):
            return False
    return True


def axis_offset(ap, tgt):
    """Degrees between an aperture's inward axis and a feature."""
    cx, cy, cz = ap["centre"]
    vx, vy, vz = tgt[0] - cx, tgt[1] - cy, tgt[2] - cz
    L = math.sqrt(vx * vx + vy * vy + vz * vz)
    if L < 1e-9:
        return 0.0, 0.0
    c = (vx * ap["n_in"][0] + vy * ap["n_in"][1]) / L
    return math.degrees(math.acos(max(-1.0, min(1.0, c)))), L


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--footprints", type=Path, default=FOOTPRINTS)
    p.add_argument("--registry", type=Path, default=INVENTORY)
    p.add_argument("--fabric", type=Path, default=BUILDING_FABRIC)
    p.add_argument("--targets", type=Path,
                   default=APERTURES_DIR / "target_inventory.csv")
    p.add_argument("--mesh-dir", type=Path, default=MESH_DIR,
                   help="aperture-aware building meshes; must be the "
                        "variant that carries recesses, or every "
                        "feature reads as buried in a solid wall")
    p.add_argument("--dem", type=Path, default=DEM_REGEN)
    p.add_argument("--bare-dem", type=Path, default=DEM_BASE_04)
    p.add_argument("--thickness", type=float, default=0.4)
    p.add_argument("--radius", type=float, default=60.0,
                   help="max distance for an external observer (m)")
    p.add_argument("--out-dir", type=Path,
                   default=APERTURES_DIR / "feature_visibility")
    return p


def main():
    args = build_parser().parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    device = select_device()

    fp = gpd.read_file(args.footprints)
    fp["ID"] = fp["ID"].astype(int)
    reg = list(csv.DictReader(open(args.registry)))
    fabric = load_fabric(args.fabric, check, warn)

    tgt = pd.read_csv(args.targets)
    feats = tgt[tgt["kind"].isin(FEATURE_KINDS)].reset_index(drop=True)
    check(len(feats) > 0, "interior feature targets loaded",
          f"{len(feats)} on {feats['ID'].nunique()} chapels")

    bare = rasterio.open(args.bare_dem)

    def ground_z(pts):
        return np.array([v[0] for v in bare.sample(
            [(float(x), float(y)) for x, y in pts])], float)

    aps_by_id = build_openings(fp, reg, fabric, ground_z, args.thickness)
    n_ap = sum(len(v) for v in aps_by_id.values())
    check(n_ap > 0, "apertures resolved",
          f"{n_ap} on {len(aps_by_id)} chapels")

    mesh_paths = sorted(args.mesh_dir.glob("building_*.obj"))
    check(len(mesh_paths) > 100, "building meshes found",
          f"{len(mesh_paths)} in {args.mesh_dir.name}")
    if failures:
        sys.exit(1)
    mesh_ids = {int(p.stem.split("_")[1]) for p in mesh_paths}

    dem, tr, _c, nod, _p = load_dem(args.dem)
    geoms = [r.geometry for _, r in fp.iterrows()
             if int(r["ID"]) in mesh_ids]
    dem, n_cleared = flatten_footprints(dem, tr, nod, geoms,
                                        bare_dem_path=args.bare_dem)
    scene = HybridScene(HeightfieldScene(dem, tr, nod, device),
                        load_scene_meshes(mesh_paths))
    print(f"\nscene: {len(mesh_ids)} meshed chapels, "
          f"{n_cleared:,} cells flattened")

    # One station per chapel: outside its door, on the door's axis.
    stations = {}
    for bid, aps in aps_by_id.items():
        doors = [a for a in aps if a["kind"] == "door"]
        if doors:
            stations[bid] = doors[0]
    check(len(stations) > 150, "door stations built",
          f"{len(stations)} chapels")

    fxyz = feats[["x", "y", "z"]].to_numpy(float)
    fid = feats["target_id"].tolist()
    fowner = feats["ID"].to_numpy(int)

    # --- who sees what ------------------------------------------------
    seen_by = {t: [] for t in fid}              # target -> [observer ID]
    sight = []
    for obs_id, ap in sorted(stations.items()):
        eye = ap["eye"]
        d = np.hypot(fxyz[:, 0] - eye[0], fxyz[:, 1] - eye[1])
        sel = np.flatnonzero(d <= args.radius)
        if not len(sel):
            continue
        vis = np.asarray(scene.visible_mask(eye, fxyz[sel])) == 1
        for j, ok in zip(sel, vis):
            if not ok:
                continue
            owner = int(fowner[j])
            seen_by[fid[j]].append(obs_id)
            # Which of the target chapel's own openings let it through?
            via = [a for a in aps_by_id.get(owner, [])
                   if crosses_opening(eye, fxyz[j], a)]
            sight.append(dict(
                observer_ID=obs_id, observer="own_door" if obs_id == owner
                else "other_door", target_id=fid[j], target_ID=owner,
                kind=feats.at[j, "kind"], dist_m=round(float(d[j]), 2),
                via_ap="|".join(f"{a['kind']}{a['ap_id']}" for a in via),
                n_via=len(via)))

    # --- aperture-by-aperture alignment, within a chapel ---------------
    pairs = []
    for j in range(len(feats)):
        owner = int(fowner[j])
        t = fxyz[j]
        for ap in aps_by_id.get(owner, []):
            off, dist = axis_offset(ap, t)
            eye = ap["eye"]
            through = crosses_opening(eye, t, ap)
            vis = bool(np.asarray(
                scene.visible_mask(eye, t[None, :]))[0] == 1)
            pairs.append(dict(
                ID=owner, ap_id=ap["ap_id"], ap_kind=ap["kind"],
                ap_wall=ap["wall"], target_id=fid[j],
                feat_kind=feats.at[j, "kind"], feat_wall=feats.at[j, "wall"],
                dist_m=round(dist, 2), axis_offset_deg=round(off, 1),
                through_aperture=through, visible=vis,
                framed=bool(through and vis)))

    sdf = pd.DataFrame(sight)
    pdf = pd.DataFrame(pairs)
    check(len(pdf) > 0, "aperture-feature pairs tested", f"{len(pdf)}")

    # --- per-feature summary -------------------------------------------
    rows = []
    for j in range(len(feats)):
        tid, owner = fid[j], int(fowner[j])
        obs = seen_by[tid]
        ext = [o for o in obs if o != owner]
        dists = sdf[(sdf["target_id"] == tid) &
                    (sdf["observer"] == "other_door")]["dist_m"]
        sub = pdf[pdf["target_id"] == tid]
        fr = sub[sub["framed"]]
        rows.append(dict(
            target_id=tid, ID=owner, kind=feats.at[j, "kind"],
            wall=feats.at[j, "wall"],
            seen_from_own_door=owner in obs,
            n_external_seers=len(ext),
            nearest_external_m=(round(float(dists.min()), 1)
                                if len(dists) else ""),
            external_seers="|".join(str(o) for o in sorted(ext)[:8]),
            n_apertures_framing=len(fr),
            framing_apertures="|".join(
                f"{r.ap_kind}{r.ap_id}" for r in fr.itertuples()),
            min_axis_offset_deg=(round(float(sub["axis_offset_deg"].min()), 1)
                                 if len(sub) else "")))
    fdf = pd.DataFrame(rows)

    fdf.to_csv(args.out_dir / "feature_visibility.csv", index=False)
    sdf.to_csv(args.out_dir / "feature_sightlines.csv", index=False)
    pdf.to_csv(args.out_dir / "aperture_feature_pairs.csv", index=False)

    n_f = len(fdf)
    n_own = int(fdf["seen_from_own_door"].sum())
    n_ext = int((fdf["n_external_seers"] > 0).sum())
    n_any = int((fdf["seen_from_own_door"] |
                 (fdf["n_external_seers"] > 0)).sum())
    print(f"\nFEATURES ({n_f} on {fdf['ID'].nunique()} chapels)")
    print(f"  seen from its own doorway   {n_own:>4}  "
          f"({n_own / n_f:.1%})")
    print(f"  seen from another chapel    {n_ext:>4}  "
          f"({n_ext / n_f:.1%})")
    print(f"  seen from nowhere           {n_f - n_any:>4}  "
          f"({1 - n_any / n_f:.1%})")
    for kind, g in fdf.groupby("kind"):
        print(f"    {kind:6s} {len(g):>4}: own door "
              f"{int(g['seen_from_own_door'].sum()):>3}, external "
              f"{int((g['n_external_seers'] > 0).sum()):>3}")

    print("\nAPERTURE -> FEATURE")
    for kind, g in pdf.groupby("ap_kind"):
        fr = g[g["framed"]]
        n_through = int(g["through_aperture"].sum())
        print(f"  {kind:6s} {len(g):>4} pairs, {n_through:>3} pass the "
              f"clear opening, {len(fr):>3} framed "
              f"({fr['ID'].nunique()} chapels)")

    write_report(args, fdf, pdf, fabric_types(args.fabric))
    plot(args.out_dir / "feature_visibility.png", fdf, pdf)
    (args.out_dir / "feature_visibility_meta.json").write_text(json.dumps(
        dict(radius_m=args.radius, stand_off_m=STAND_OFF_M,
             eye_height_m=EYE_HEIGHT, mesh_dir=args.mesh_dir.name,
             features=n_f, chapels=int(fdf["ID"].nunique()),
             seen_own_door=n_own, seen_external=n_ext,
             seen_nowhere=n_f - n_any), indent=2))
    print(f"\nwrote {args.out_dir}")
    if failures:
        sys.exit(1)


def fabric_types(path):
    """{ID: chapel type} for the report tables, empty if unavailable.

    Typology is presentation only here — it labels a row so a reader
    can see whether a result clusters on one type — so a missing or
    reshaped fabric table degrades the tables rather than failing the
    run that produced the measurements."""
    try:
        f = pd.read_csv(path)
        return dict(zip(f["ID"].astype(int), f["type"]))
    except (OSError, KeyError):
        return {}


def plot(path, fdf, pdf):
    """Two panels: where features are seen from, and how squarely
    apertures face them.

    The off-axis histogram is split by aperture kind because doors and
    windows sit at different heights and so fail for different
    reasons; pooling them would hide that."""
    fig, ax = plt.subplots(1, 2, figsize=(10.5, 3.9))
    cats = ["own door\nonly", "own +\nexternal", "external\nonly",
            "nowhere"]
    own = fdf["seen_from_own_door"]
    ext = fdf["n_external_seers"] > 0
    vals = [int((own & ~ext).sum()), int((own & ext).sum()),
            int((~own & ext).sum()), int((~own & ~ext).sum())]
    ax[0].bar(cats, vals, color=["#3b6ea5", "#57a773", "#d9a441",
                                 "0.7"], edgecolor="k", linewidth=0.5)
    for i, v in enumerate(vals):
        ax[0].text(i, v, str(v), ha="center", va="bottom", fontsize=9)
    ax[0].set_ylabel("interior features")
    ax[0].set_title("Where each niche or apse is seen from", fontsize=10)

    for kind, colour in (("door", "#3b6ea5"), ("window", "#d9a441")):
        g = pdf[pdf["ap_kind"] == kind]
        if len(g):
            ax[1].hist(g["axis_offset_deg"], bins=np.arange(0, 185, 10),
                       histtype="step", lw=1.8, color=colour,
                       label=f"{kind} ({len(g)})")
    ax[1].set_xlabel("angle between the aperture's axis and the feature "
                     "(deg)")
    ax[1].set_ylabel("pairs")
    ax[1].set_title("How squarely apertures face interior features",
                    fontsize=10)
    ax[1].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def write_report(args, fdf, pdf, types):
    """Markdown summary, with the observer convention stated up front.

    Every number here depends on where the observer was put and how
    high their eye was, so the report leads with those rather than
    burying them, and names the mesh variant — reading these counts off
    a doors-only scene would silently measure a site with no recesses
    in it at all."""
    n_f = len(fdf)
    own = fdf["seen_from_own_door"]
    ext = fdf["n_external_seers"] > 0
    framed = pdf[pdf["framed"]]
    win = framed[framed["ap_kind"] == "window"]
    lines = [
        "# Interior features: what is visible, and through what",
        "",
        f"Observers stand {STAND_OFF_M} m outside an opening on its own "
        f"axis, at {EYE_HEIGHT} m eye height. External observers are "
        f"other chapels' door stations within {args.radius:.0f} m. "
        f"Scene is `{args.mesh_dir.name}`, the mesh variant that "
        "carries recesses.", "",
        "## Coverage", "",
        f"- {n_f} interior features on {fdf['ID'].nunique()} chapels "
        f"({int((fdf['kind'] == 'niche').sum())} niches, "
        f"{int((fdf['kind'] == 'apse').sum())} apses).",
        f"- Seen from its own doorway: **{int(own.sum())}** "
        f"({own.mean():.1%}).",
        f"- Seen from at least one other chapel's doorway: "
        f"**{int(ext.sum())}** ({ext.mean():.1%}).",
        f"- Seen from nowhere tested: **{int((~own & ~ext).sum())}**.",
        "",
        "## Framed by a named aperture", "",
        f"A pair is *framed* when the sightline both passes through that "
        f"opening's clear rectangle at each wall face and survives the "
        f"ray-cast. {len(framed)} of {len(pdf)} aperture-feature pairs "
        f"qualify, on {framed['ID'].nunique() if len(framed) else 0} "
        f"chapels.", "",
    ]
    for kind, g in pdf.groupby("ap_kind"):
        f = g[g["framed"]]
        med = (f["axis_offset_deg"].median() if len(f)
               else float("nan"))
        lines.append(f"- **{kind}**: {len(f)} framed of {len(g)} pairs "
                     f"tested; median off-axis angle of the framed ones "
                     f"{med:.0f} deg.")
    lines += ["", "### Windows that frame an interior feature", ""]
    if len(win):
        lines.append("| chapel | type | window | feature | off-axis | "
                     "range |")
        lines.append("|---|---|---|---|---|---|")
        for r in win.sort_values(["ID", "ap_id"]).itertuples():
            lines.append(f"| {r.ID} | {types.get(r.ID, '?')} | "
                         f"ap {r.ap_id} (wall {r.ap_wall}) | "
                         f"{r.feat_kind} on wall {r.feat_wall} | "
                         f"{r.axis_offset_deg:.0f} deg | {r.dist_m} m |")
    else:
        lines.append("None. No window in the registry admits a sightline "
                     "to an interior feature from outside.")
    lines += ["", "## Most-seen features", ""]
    top = fdf.sort_values("n_external_seers", ascending=False).head(12)
    lines.append("| feature | chapel | type | external seers | nearest |")
    lines.append("|---|---|---|---|---|")
    for r in top.itertuples():
        lines.append(f"| {r.target_id} | {r.ID} | {types.get(r.ID, '?')} "
                     f"| {r.n_external_seers} | {r.nearest_external_m} m |")
    (args.out_dir / "feature_visibility_report.md").write_text(
        "\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
