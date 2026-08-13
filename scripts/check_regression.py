"""Assert the frozen baseline still reproduces, byte for byte.

The interior work (per-building wall thickness, niches, apses, pillars)
touches the same builder that produced the published aperture result —
+115 ground cells / +0.28% and +23% centroid-visible graph pairs over
197 buildings. Those numbers are quoted to mentors, so "did I break
them?" has to be answerable mechanically rather than by memory.

The contract this enforces: every registry row today is `kind=door`
with the new schema cells blank, so the perforation gate must resolve
them exactly as before and every OBJ must hash identically. Once
per-building thickness lands, `--thickness-mode legacy` must still
land here unchanged; only `fabric` mode is allowed to move.

Baseline lives in viewshed_runs/frozen_baseline_<date>/ as one
`<meshdir>.sha256` per mesh variant, written when the baseline was
frozen. Rebuild the meshes first, then run this.

    .venv/bin/python scripts/check_regression.py
    .venv/bin/python scripts/check_regression.py --only meshes meshes_solid

Exits nonzero on any drift, naming the files that moved.
"""

import argparse
import hashlib
import sys
from pathlib import Path

from sanity_checks import check, failures
from aperture_registry import APERTURES_DIR

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FROZEN = PROJECT_ROOT / "viewshed_runs/frozen_baseline_20260811"


def sha256(path):
    """Hash a file in chunks, so a large mesh never lands in memory."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_manifest(path):
    """`shasum -a 256` output -> {filename: digest}."""
    out = {}
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        digest, name = line.split(None, 1)
        out[name.strip()] = digest
    return out


def compare_dir(mesh_dir, manifest_path):
    """One mesh variant against its frozen manifest."""
    want = read_manifest(manifest_path)
    if not check(mesh_dir.is_dir(), f"{mesh_dir.name}: directory exists",
                 str(mesh_dir)):
        return
    have = {p.name: sha256(p) for p in sorted(mesh_dir.glob("*.obj"))}

    missing = sorted(set(want) - set(have))
    added = sorted(set(have) - set(want))
    moved = sorted(n for n in set(want) & set(have) if want[n] != have[n])

    check(not missing, f"{mesh_dir.name}: no meshes missing",
          f"{len(missing)}: {missing[:6]}{' ...' if len(missing) > 6 else ''}")
    check(not added, f"{mesh_dir.name}: no unexpected meshes",
          f"{len(added)}: {added[:6]}{' ...' if len(added) > 6 else ''}")
    check(not moved, f"{mesh_dir.name}: every mesh byte-identical",
          f"{len(moved)} changed: "
          f"{moved[:6]}{' ...' if len(moved) > 6 else ''}")
    if not (missing or added or moved):
        print(f"  {mesh_dir.name}: {len(have)} meshes unchanged")


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--frozen", type=Path, default=FROZEN,
                   help="frozen baseline directory holding the "
                        "<meshdir>.sha256 manifests")
    p.add_argument("--mesh-root", type=Path, default=APERTURES_DIR,
                   help="directory containing the mesh variant folders")
    p.add_argument("--only", nargs="+", default=None,
                   help="check only these mesh variants (default: every "
                        "manifest present in --frozen)")
    return p


def main():
    args = build_parser().parse_args()
    if not check(args.frozen.is_dir(), "frozen baseline exists",
                 str(args.frozen)):
        sys.exit(1)

    manifests = sorted(args.frozen.glob("*.sha256"))
    if args.only:
        want = set(args.only)
        manifests = [m for m in manifests if m.stem in want]
        unknown = want - {m.stem for m in manifests}
        check(not unknown, "all --only variants have a manifest",
              f"unknown: {sorted(unknown)}")

    if not check(manifests, "at least one manifest to check",
                 f"none found in {args.frozen}"):
        sys.exit(1)

    print(f"baseline: {args.frozen.relative_to(PROJECT_ROOT)}")
    for m in manifests:
        compare_dir(args.mesh_root / m.stem, m)

    if failures:
        print(f"\nREGRESSION: {len(failures)} check(s) failed")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nbaseline reproduces byte for byte")


if __name__ == "__main__":
    main()
