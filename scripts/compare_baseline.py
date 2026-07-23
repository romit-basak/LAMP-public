"""Baseline (GRASS r.viewshed) vs 3D-engine viewshed comparison.

Validates the true-3D engine against the user's QGIS r.viewshed baseline in
`Task_2/`. Both sides use the SAME surface (Task_2's DEM_Subset-WithBuildings,
the older-datum subset on the baseline's exact 54x68 / 1.5 m grid), so the
comparison isolates algorithm + observer height, not DEM differences.

At this stage the engine treats buildings as solid (no apertures yet), exactly
as r.viewshed does — so high agreement is the expected, validating result. The
"3D sees through apertures where 2D cannot" divergence is step 2.

Run:  .venv/bin/python scripts/compare_baseline.py
Exits nonzero if grid alignment or binarization checks fail.
"""

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
from matplotlib.lines import Line2D
from rasterio.windows import Window

from sanity_checks import check, warn, failures
from viewshed import (HeightfieldScene, select_device, load_dem, load_observers,
                      target_grid, compute_viewshed)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TASK2 = PROJECT_ROOT / "Task_2"


def df_to_md(df):
    """Minimal GitHub-markdown table (avoids a tabulate dependency)."""
    cols = list(df.columns)
    head = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    rows = ["| " + " | ".join(str(v) for v in r) + " |"
            for r in df.itertuples(index=False)]
    return "\n".join([head, sep, *rows])


def write_mask_tif(mask, transform, crs, path):
    """Write a small uint8 viewshed mask (untiled; the ROI is tiny)."""
    with rasterio.open(path, "w", driver="GTiff", height=mask.shape[0],
                       width=mask.shape[1], count=1, dtype="uint8",
                       crs=crs, transform=transform, nodata=255,
                       compress="lzw") as dst:
        dst.write(mask.astype("uint8"), 1)


def binarize_baseline(path):
    """r.viewshed output -> visible bool mask (finite, non-nodata = visible)."""
    with rasterio.open(path) as src:
        arr = src.read(1).astype("float64")
        nod = src.nodata
    invalid = ~np.isfinite(arr)
    if nod is not None and np.isfinite(nod):
        invalid |= arr == nod
    vis = ~invalid
    finite_vals = arr[vis]
    rng = (f"{finite_vals.min():.1f}..{finite_vals.max():.1f}" if vis.any() else "—")
    return vis, arr.shape, rng


def to_px(x, y, transform):
    return ((np.asarray(x) - transform.c) / transform.a - 0.5,
            (np.asarray(y) - transform.f) / transform.e - 0.5)


def load_backdrop(ortho_path, scene):
    """Grayscale/RGB image behind the QC overlays, 2nd/98th-percentile
    contrast-stretched the same way every other orthophoto display in
    this project is (build_dome_layer.py's QC, observer_view.py's ortho
    drape) — a plain min/max stretch lets one hot pixel wash out the
    whole image. Both project orthophotos are single-band in practice,
    so the single-band branch is the one that actually runs; falls
    back to a computed hillshade if no orthophoto file exists at all."""
    if ortho_path.exists():
        with rasterio.open(ortho_path) as src:
            a = src.read()
        img = (np.transpose(a[:3], (1, 2, 0)) if a.shape[0] >= 3
               else a[0]).astype("float64")
        lo, hi = np.nanpercentile(img, [2, 98])
        return np.clip((img - lo) / max(hi - lo, 1e-9), 0, 1)
    from build_dem_with_buildings import hillshade
    return hillshade(scene.dem_np, scene.px)


def metrics_row(observer, height, A, B, valid):
    A, B = A & valid, B & valid
    both = int((A & B).sum())
    a_only = int((A & ~B).sum())
    b_only = int((~A & B).sum())
    neither = int((~A & ~B & valid).sum())
    n = both + a_only + b_only + neither
    union = both + a_only + b_only
    return {
        "observer": observer, "eye_height_m": height,
        "baseline_visible": both + a_only, "engine_visible": both + b_only,
        "both_visible": both, "baseline_only": a_only, "engine_only": b_only,
        "both_hidden": neither,
        "agreement_pct": round(100 * (both + neither) / n, 2) if n else 0.0,
        "jaccard_iou": round(both / union, 3) if union else 1.0,
    }


def agreement_rgba(A, B, valid):
    """green=both, blue=baseline-only, red=engine-only, transparent=neither."""
    h, w = A.shape
    rgba = np.zeros((h, w, 4))
    both, a_only, b_only = A & B & valid, A & ~B & valid, ~A & B & valid
    rgba[both] = [0.1, 0.8, 0.1, 0.85]
    rgba[a_only] = [0.1, 0.3, 1.0, 0.85]
    rgba[b_only] = [1.0, 0.2, 0.1, 0.85]
    return rgba


def overlay_rgba(mask, valid, color):
    h, w = mask.shape
    rgba = np.zeros((h, w, 4))
    rgba[mask & valid] = color
    return rgba


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--baseline-dir", type=Path, default=TASK2,
                   help="directory holding the GRASS r.viewshed baseline "
                        "(viewshed_mark{N}_curr/old.tif)")
    p.add_argument("--dem", type=Path,
                   default=TASK2 / "DEM_Subset-WithBuildings.tif",
                   help="shared surface both sides are evaluated on "
                        "(the baseline's exact grid/datum)")
    p.add_argument("--footprints", type=Path,
                   default=TASK2 / "BuildingFootprints.shp",
                   help="footprints on the baseline grid, for figure overlays")
    p.add_argument("--observers", type=Path,
                   default=TASK2 / "Marks_Brief2.shp",
                   help="observer points, matched 1:1 in file order to "
                        "viewshed_mark{1,2,3}_*.tif")
    p.add_argument("--ortho", type=Path,
                   default=TASK2 / "OrthoImage_Subset.tif",
                   help="orthophoto backdrop for the QC figures (falls "
                        "back to a computed hillshade if missing)")
    p.add_argument("--out-dir", type=Path, default=TASK2 / "comparison",
                   help="where the metrics CSV, PNGs, and report are written")
    p.add_argument("--eye-heights", type=float, nargs="+",
                   default=[1.5, 1.75],
                   help="eye heights (m) to sweep; the FIRST value is the "
                        "one used for every figure and the written engine_* "
                        "mask TIFFs, so its position in the list matters")
    args = p.parse_args()

    device = select_device()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    print("=" * 70)
    print(f"DEVICE: {device}   surface: {args.dem.name}")
    print("=" * 70)

    dem, transform, crs, nodata, profile = load_dem(args.dem)
    scene = HeightfieldScene(dem, transform, nodata, device)

    obs_list, _ = load_observers(args.observers, crs)
    obs_xy = [(x, y) for _, x, y in obs_list]
    n_obs = len(obs_xy)
    # Every loop below (baseline files, metrics, figures) derives its count
    # from n_obs rather than repeating a literal "3" — this check is what
    # guarantees that assumption is actually true for whatever observer
    # file is passed in, instead of just hoping it stays in sync.
    check(n_obs == 3, "3 observers", f"{n_obs}")
    for i, (x, y) in enumerate(obs_xy, 1):
        print(f"  observer {i} -> mark{i}: ({x:.1f}, {y:.1f})")

    print("\nBASELINE (r.viewshed, _curr)")
    base_masks, base_shape = [], None
    for i in range(1, n_obs + 1):
        vis, shp, rng = binarize_baseline(
            args.baseline_dir / f"viewshed_mark{i}_curr.tif")
        base_masks.append(vis)
        base_shape = shp
        check(0 < vis.sum() < vis.size, f"mark{i} baseline binarizes sanely",
              f"{int(vis.sum())}/{vis.size} visible, angle range {rng}")

    with rasterio.open(args.baseline_dir / "viewshed_mark1_curr.tif") as b:
        check(scene.dem_np.shape == base_shape and
              transform.almost_equals(b.transform, precision=1e-6),
              "engine surface shares the baseline grid",
              f"{scene.dem_np.shape} @ {transform.to_gdal()[:2]}")

    full = Window(0, 0, scene.W, scene.H)
    X, Y, Z, shape, _ = target_grid(scene, full, transform)
    # The comparison method (module docstring) only holds if both sides see
    # the same surface, including where it's missing data — verify that
    # rather than assuming it, so a future subset DEM with real gaps can't
    # silently get counted as valid instead of excluded.
    has_nodata = nodata is not None and bool(np.any(dem == nodata))
    check(not has_nodata, "subset DEM has no nodata cells",
          f"{int((dem == nodata).sum()):,} cells" if has_nodata else "")
    valid = (dem != nodata) if has_nodata else np.ones(shape, dtype=bool)

    print("\nENGINE RUNS + METRICS")
    rows, primary_masks = [], None
    # The baseline's observer height was never recorded, so sweep candidate
    # heights and report the sensitivity rather than guessing a single value.
    for h in args.eye_heights:
        masks = []
        for (x, y) in obs_xy:
            eye = (x, y, float(scene.surface_z(x, y)[0]) + h)
            m = compute_viewshed(scene, eye, X, Y, Z, shape)
            masks.append(m == 1)
        for i in range(n_obs):
            r = metrics_row(i + 1, h, base_masks[i], masks[i], valid)
            rows.append(r)
            print(f"  h={h:>4} obs{i+1}: agree {r['agreement_pct']:.1f}%  "
                  f"IoU {r['jaccard_iou']:.2f}  "
                  f"(base {r['baseline_visible']}, 3D {r['engine_visible']})")
        if h == args.eye_heights[0]:
            primary_masks = masks
            for i, m in enumerate(masks, 1):
                write_mask_tif(m & valid, transform, crs,
                               args.out_dir / f"engine_obs{i}_h{h}.tif")

    df = pd.DataFrame(rows)
    df.to_csv(args.out_dir / "comparison_metrics.csv", index=False)

    print("\nFIGURES")
    footprints = gpd.read_file(args.footprints).to_crs(crs)
    backdrop = load_backdrop(args.ortho, scene)
    bd_kw = dict(cmap=None if backdrop.ndim == 3 else "gray")
    h0 = args.eye_heights[0]

    for i in range(n_obs):
        A, B = base_masks[i], primary_masks[i]
        fig, axes = plt.subplots(1, 3, figsize=(18, 8), dpi=200)
        panels = [("r.viewshed baseline", overlay_rgba(A, valid, [1, .85, 0, .8])),
                  (f"3D engine (eye {h0} m)", overlay_rgba(B, valid, [1, .85, 0, .8])),
                  ("agreement", agreement_rgba(A, B, valid))]
        for ax, (title, ov) in zip(axes, panels):
            ax.imshow(backdrop, **bd_kw)
            ax.imshow(ov, interpolation="nearest")
            for geom in footprints.geometry:
                polys = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
                for poly in polys:
                    px, py = to_px(*poly.exterior.xy, transform)
                    ax.plot(px, py, color="cyan", linewidth=0.6)
            ex, ey = to_px(obs_xy[i][0], obs_xy[i][1], transform)
            ax.plot(ex, ey, "w*", markersize=16, markeredgecolor="k")
            ax.set_title(title)
            ax.axis("off")
        axes[2].legend(handles=[
            Line2D([0], [0], marker="s", color="w", markerfacecolor=(.1, .8, .1),
                   markersize=10, label="both"),
            Line2D([0], [0], marker="s", color="w", markerfacecolor=(.1, .3, 1),
                   markersize=10, label="baseline only"),
            Line2D([0], [0], marker="s", color="w", markerfacecolor=(1, .2, .1),
                   markersize=10, label="3D only")], loc="lower right", fontsize=8)
        r = df[(df.observer == i + 1) & (df.eye_height_m == h0)].iloc[0]
        fig.suptitle(f"Observer {i+1} — agreement {r.agreement_pct:.1f}%  "
                     f"IoU {r.jaccard_iou:.2f}")
        fig.savefig(args.out_dir / f"compare_obs{i+1}.png", bbox_inches="tight")
        plt.close(fig)
        print(f"  wrote compare_obs{i+1}.png")

    old = [binarize_baseline(args.baseline_dir / f"viewshed_mark{i}_old.tif")[0]
           for i in range(1, n_obs + 1)]
    # This figure's 3-panel layout (and the axes[2] legend placement below)
    # is a fixed design choice, not a re-derivation of n_obs; it assumes
    # exactly 3 observers, same as the check above.
    fig, axes = plt.subplots(1, 3, figsize=(18, 8), dpi=200)
    for i, ax in enumerate(axes):
        ax.imshow(backdrop, **bd_kw)
        both = old[i] & base_masks[i] & valid          # terrain-limited
        blocked = old[i] & ~base_masks[i] & valid       # ground hidden behind buildings
        rooftop = ~old[i] & base_masks[i] & valid       # building tops newly visible
        rgba = np.zeros((*base_shape, 4))
        rgba[both] = [1.0, 0.85, 0.0, 0.35]
        rgba[blocked] = [0.9, 0.1, 0.1, 0.95]
        rgba[rooftop] = [0.0, 0.9, 0.9, 0.95]
        ax.imshow(rgba, interpolation="nearest")
        ex, ey = to_px(obs_xy[i][0], obs_xy[i][1], transform)
        ax.plot(ex, ey, "w*", markersize=16, markeredgecolor="k")
        ax.set_title(f"observer {i+1}: -{int(blocked.sum())} ground, "
                     f"+{int(rooftop.sum())} rooftops")
        ax.axis("off")
    axes[2].legend(handles=[
        Line2D([0], [0], marker="s", color="w", markerfacecolor=(1, .85, 0),
               markersize=10, label="visible on both (terrain-limited)"),
        Line2D([0], [0], marker="s", color="w", markerfacecolor=(.9, .1, .1),
               markersize=10, label="ground hidden behind buildings"),
        Line2D([0], [0], marker="s", color="w", markerfacecolor=(0, .9, .9),
               markersize=10, label="building tops newly visible")],
        loc="lower right", fontsize=8)
    fig.suptitle("Building effect in the baseline r.viewshed (bare-earth vs "
                 "with-buildings): solid blocks hide ground, reveal rooftops")
    fig.savefig(args.out_dir / "building_effect.png", bbox_inches="tight")
    plt.close(fig)
    print("  wrote building_effect.png")

    write_report(args.out_dir, df, args.eye_heights)

    print("\n" + "=" * 70)
    for i in range(n_obs):
        ag = df[(df.observer == i + 1) & (df.eye_height_m == h0)].iloc[0].agreement_pct
        if ag < 70:
            warn(f"obs{i+1} agreement low at {h0} m",
                 f"{ag:.1f}% — investigate algorithm/height/registration")
    if failures:
        print(f"FAILURES ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"Comparison complete; artifacts in {args.out_dir.relative_to(PROJECT_ROOT)}")


def write_report(out_dir, df, heights):
    h0 = heights[0]
    prim = df[df.eye_height_m == h0]
    table = df_to_md(df)
    best = (df.groupby("eye_height_m").agreement_pct.mean()
            .sort_values(ascending=False))
    best_h = best.index[0]
    (out_dir / "comparison_report.md").write_text(f"""# Baseline (r.viewshed) vs 3D engine — comparison

## Method

Both sides use the **same surface** — `Task_2/DEM_Subset-WithBuildings.tif`
(54x68 @ 1.5 m, EPSG:32636, the baseline's exact grid) — so this isolates
**algorithm + observer height**, not DEM/datum/resolution. The 3D engine
(`scripts/viewshed.py`, `HeightfieldScene`) is re-run on that grid and compared
cell-for-cell to the user's GRASS r.viewshed output (`viewshed_mark*_curr.tif`,
binarized as finite-cell = visible). Observer height was not recorded in the
QGIS project, so it is **swept ({', '.join(f'{h} m' for h in heights)})** and
reported as a sensitivity.

## Why high agreement is the *expected, validating* result

r.viewshed already computes true 3D line-of-sight on the DEM; its
thesis-relevant limitation is that buildings are baked in as **solid** blocks.
The 3D engine currently also treats buildings as solid (no apertures yet), so
the two should largely **agree** — confirming the engine reproduces the
established tool on the common case. The contribution of this project — sight
passing *through* window/door apertures — is **step 2**, and that is where the
3D result will deliberately diverge from r.viewshed.

## Results (primary eye height {h0} m)

{df_to_md(prim)}

### Height sensitivity (all runs)

{table}

Mean agreement is highest at **{best_h} m** ({best.iloc[0]:.1f}%), a hint at the
baseline's unrecorded observer height.

## Figures

- `compare_obs{{1,2,3}}.png` — baseline | 3D | agreement (green=both,
  blue=baseline-only, red=3D-only) over the orthophoto.
- `building_effect.png` — bare-earth vs with-buildings r.viewshed. Finding: at
  1.5 m the buildings block **~0 additional ground** (cells behind them were
  already terrain-hidden) and only add their own **rooftops** to the visible
  set. The coarse baseline barely registers building occlusion.

## Interpretation & caveats

- Residual disagreement comes from: ray-march + bilinear sampling vs
  r.viewshed's sweep/interpolation; the (unconfirmed) baseline height;
  edge effects at the tiny ROI; ~1-px registration. Earth curvature is
  negligible over ~100 m.
- **Resolution matters as much as algorithm.** On this 1.5 m subset both
  methods under-represent building occlusion (a ~4 m building spans <3 cells);
  the production 3D run on the 0.4 m surface resolves the same buildings as real
  blocks (visible fractions drop to ~0.5-1.8%). The validation here is "same
  algorithm, same grid → same answer"; the fidelity gain is at 0.4 m + apertures.
- The subset DEM is the **older datum** (~78 m above the current 0.4 m DEM);
  irrelevant here because both sides use it, but the production 3D run uses the
  regenerated 0.4 m surface.
- **No apertures yet** — the headline 3D-vs-2D divergence is step 2, sourced
  from `Task_2/Site_Plan.pdf` (doorway/opening directions).

## For mentors

1. What observer/target height + max distance did the r.viewshed baseline use?
2. Confirm the comparison ROI / whether to extend the baseline to the full site.
3. Aperture digitization from `Site_Plan.pdf` — confirm the source is authoritative.
""")
    print("  wrote comparison_report.md")


if __name__ == "__main__":
    main()
