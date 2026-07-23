# Remote Machine Setup — El Bagawat Viewshed Engine

Clone-to-full-pipeline guide for the remote Windows/CUDA workstation. Follow
this top to bottom on a fresh machine and every script in
[README.md](../README.md)'s pipeline should run end-to-end with no
surprises. For *why* the code is built the way it is, see
[docs/CODE_WALKTHROUGH.md](CODE_WALKTHROUGH.md); for project scope and data
conventions, see [CLAUDE.md](../CLAUDE.md).

## Quick path (if nothing goes wrong)

```powershell
git clone <this-repo-url> LAMP
cd LAMP
mklink /J LAMP_DataStore <path-to-datastore-root>

uv python install 3.13
uv venv --python 3.13 .venv
uv pip install --python .venv --index-url https://download.pytorch.org/whl/cu124 torch==2.6.0 torchaudio==2.6.0 torchvision==0.21.0
uv pip install --python .venv -r requirements.txt

.venv\Scripts\python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
.venv\Scripts\python scripts\sanity_checks.py
```

If the last two commands print `True <your GPU name>` and a clean
self-check run, skip to [Running the full pipeline](#running-the-full-pipeline).
If anything looks off, the sections below explain each step and the
likely fix.

## 1. Before you start

You'll need, on the remote machine already:
- **Windows**, reachable via RealVNC (per [CLAUDE.md](../CLAUDE.md)'s
  Environment section).
- **An up-to-date NVIDIA driver** for the RTX GPU. You don't need the CUDA
  Toolkit installed separately — modern PyTorch wheels bundle the CUDA
  runtime themselves — but you do need a driver new enough to support the
  CUDA version those wheels target (see step 4). Run `nvidia-smi` in a
  terminal; if it prints a driver version and a CUDA version number, you're
  set.
- **Git** (`winget install --id Git.Git` if it isn't already there).
- Access to the full ElBagawat datastore (~200 GB) somewhere on this
  machine's disks — this guide assumes it already exists at some path (call
  it `D:\path\to\datastore-root`, containing an `ElBagawat\` folder) and just
  needs linking into the repo, per the "Data assets" section of CLAUDE.md.
  With 21.83 TB of storage on this workstation, keeping the full datastore
  local (rather than a subset) is the point of using this machine at all —
  in particular `100_Data/140_SAR_Imagery/` (the bulk of the dataset) is
  deliberately never pulled to the local Mac and only makes sense to work
  with here.

## 2. Clone and link the datastore

```powershell
git clone <this-repo-url> LAMP
cd LAMP
```

Every script's default paths (`scripts/sanity_checks.py`'s `ROOT` constant,
inherited by every other script) expect `LAMP_DataStore/ElBagawat/...`
relative to the repo root. Rather than copying ~200 GB into the repo, link
it in as a junction — the junction must resolve so that
`LAMP_DataStore\ElBagawat\...` is a valid path:

```powershell
mklink /J LAMP_DataStore D:\path\to\datastore-root
```

(This is the same junction trick README.md's "Reproducing the QGIS scene on
the remote machine" section uses for `Task2.qgz` — it matters for every
script here, not just QGIS.) Verify it resolved correctly before going
further:

```powershell
dir LAMP_DataStore\ElBagawat\200_Projects\220_BuildingsToDEM
```

You should see `dome_inventory.csv`, `domes.gpkg`, and (if already
generated) a `DEMWithBuildings-0.4m-*.tif`. If this comes back empty or
"cannot find path," the junction target is wrong — re-run `mklink` pointing
at wherever the datastore root actually lives before continuing; every step
below will fail confusingly otherwise.

## 3. Install `uv` and Python 3.13

The project's environment is managed with **uv**, not a system Python
install or conda — this keeps the exact same setup recipe working
identically on this Windows box and the local Mac.

```powershell
winget install --id astral-sh.uv
```

(Or `pip install uv` / see astral.sh's install docs if `winget` isn't
available.) Then let `uv` fetch the exact Python version this project
targets — no need to separately install Python system-wide first:

```powershell
uv python install 3.13
```

## 4. Create the venv and install dependencies — the one step worth doing carefully

```powershell
uv venv --python 3.13 .venv
```

**Do not** `uv pip install --python .venv -r LAMP_DataStore\ElBagawat\requirements.txt`
— that file is the raw freeze *from* this remote machine's original setup,
preserved as a historical record, not something to install from again (see
its own header comment). The project-root `requirements.txt` is the one to
use; it's derived from that freeze with the platform-specific quirks
(CUDA-only packages behind `sys_platform == "win32"` markers, no hard-coded
`+cu124` suffixes) worked out already.

**Torch needs the CUDA package index, not the plain default one.** This is
the single step most likely to go silently wrong: installing plain
`torch==2.6.0` from the ordinary package index can land you a CPU-only
build with no error message — `torch.cuda.is_available()` would just quietly
return `False` forever after, and every script would fall back to running on
the CPU instead of this workstation's RTX card, with nothing in the output
telling you that happened (`select_device()` picks silently and correctly
among CUDA/MPS/CPU — see [CODE_WALKTHROUGH.md §4.4](CODE_WALKTHROUGH.md#44-device-agnostic--and-the-two-mps-gotchas) —
but it can only pick CUDA if a CUDA-capable torch was actually installed).
Install the three torch packages from PyTorch's CUDA 12.4 index *first*
(matching the CUDA version this project's own remote freeze was built
against), then install everything else:

```powershell
uv pip install --python .venv --index-url https://download.pytorch.org/whl/cu124 torch==2.6.0 torchaudio==2.6.0 torchvision==0.21.0
uv pip install --python .venv -r requirements.txt
```

The second command will see torch/torchaudio/torchvision already satisfied
at the right versions and leave them alone rather than reinstalling a
different build over them.

**Verify the GPU is actually visible before running anything else:**

```powershell
.venv\Scripts\python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

This must print `True` and the GPU's name (something like `NVIDIA RTX 5000
Ada Generation`). If it prints `False`, the CUDA-index install above didn't
take — double check the driver (`nvidia-smi`) reports a CUDA version at
least as new as 12.4, and re-run the CUDA-index install command, watching
for it actually resolving `cu124` wheels rather than falling back to a
generic one.

**CuPy is present but currently unused.** `requirements.txt` also installs
`cupy-cuda12x` behind a Windows-only marker — CLAUDE.md documents CuPy as
the project's general NumPy-on-GPU convention for future array code, but as
of this writing no script in `scripts/` actually imports it (everything
GPU-bound today goes through torch). Nothing to configure here; it's just
available if a future script needs it.

## 5. Confirm the data contract

```powershell
.venv\Scripts\python scripts\sanity_checks.py
```

Read the output, not just the exit code — this is the script that would
catch a bad datastore link, a CRS mismatch, or a stale legacy DEM before
anything else runs. `[WARN]` lines are expected and informational (e.g. the
legacy `BuildingsToDEM.tif` datum-mismatch warning, and the viewpoints file's
CRS being lat/lon rather than UTM — both already understood and explained in
[CODE_WALKTHROUGH.md §1](CODE_WALKTHROUGH.md#1-sanity_checksspy--the-data-contract)).
`[FAIL]` lines mean stop and fix the datastore link or file paths before
proceeding.

## Running the full pipeline

In the order the pipeline actually depends on itself
(see [CODE_WALKTHROUGH.md](CODE_WALKTHROUGH.md#the-pipeline) for the full
diagram and rationale). All commands assume you're in the repo root with the
venv active, or prefix each with `.venv\Scripts\python`.

```powershell
$env:PYTHON = ".venv\Scripts\python"

# 1. Regenerate the ray-casting surface (only needed once, or after new DEM/footprint data arrives)
& $env:PYTHON scripts\build_dem_with_buildings.py

# 2. Dome visualization layer (optional but referenced by --domes everywhere below)
& $env:PYTHON scripts\build_dome_layer.py

# 3. The engine itself — full 360 degree run from the 3 sample observers
& $env:PYTHON scripts\viewshed.py

# 4. Validate against the GRASS r.viewshed baseline
& $env:PYTHON scripts\compare_baseline.py

# 5. First-person audit snapshots
& $env:PYTHON scripts\observer_view.py

# 6. Optional: 3D visibility volume + its surface mesh, on this machine's GPU
& $env:PYTHON scripts\viewshed.py --volume --volume-format csv mesh --out-dir LAMP_DataStore\ElBagawat\200_Projects\220_BuildingsToDEM

# 7. Optional: external-renderer export
& $env:PYTHON scripts\export_scene_bundle.py --unity
```

Every one of these prints a `DEVICE: cuda` banner near the top of its
output — that line is your confirmation this run actually used the GPU, not
a silent CPU fallback. With this workstation's hardware, expect the
ray-casting steps to be noticeably faster than on the Mac (MPS) they were
developed against; nothing in the code needs adjusting for that, by design
(see the device-agnostic convention in CLAUDE.md and
[CODE_WALKTHROUGH.md §4.4](CODE_WALKTHROUGH.md#44-device-agnostic--and-the-two-mps-gotchas)).
Every script still exits nonzero and prints a `FAILURES (n):` block if any
self-check fails — treat that as a stop sign, not a warning.

### Blender and Unity (optional; presentation tier only)

```powershell
winget install --id BlenderFoundation.Blender
```

then follow [docs/BLENDER.md](BLENDER.md) starting from step 2 (the
bundle-export step in step 1 is the `export_scene_bundle.py` call above).
For Unity, follow [docs/UNITY.md](UNITY.md) — no Unity project is committed
to this repo; both docs assume the bundle in step 7 above already exists.

## Troubleshooting

- **`mklink` says "Access is denied."** Junctions (`/J`) don't need admin
  rights the way symbolic links (`/D`) do, but the *target* path must exist
  and be readable. Double-check the datastore root path and that you're not
  accidentally trying to overwrite an existing `LAMP_DataStore` folder —
  `mklink` will refuse if the link name already exists as a real directory.
- **`torch.cuda.is_available()` is `False` despite `nvidia-smi` working.**
  Almost always means the CUDA-index install step got skipped or a plain
  `pip install torch` from a different step re-installed a CPU wheel over
  it afterward — reinstall via the `--index-url` command in step 4 last,
  after any other installs, and re-verify.
- **`laspy`/`lazrs` import errors when using `--volume-format las/laz`.**
  Both are already pinned in `requirements.txt`; if they're somehow missing,
  every script that touches LAS/LAZ output degrades gracefully (`warn()`
  and skip that one format) rather than crashing the whole run — this is a
  soft dependency by design (see `volume_mesh.py`'s `write_las` in
  [CODE_WALKTHROUGH.md §6](CODE_WALKTHROUGH.md#6-volume_convertpy--post-processing-without-recompute)).
- **No display / can't see plots.** Every script that makes a figure sets
  `matplotlib.use("Agg")` before importing `pyplot` — this is a
  file-writing-only backend that needs no display, X server, or GUI at all,
  so running headless over RealVNC (or even a plain SSH/PowerShell session
  with no VNC window open) is fine. PNGs land on disk; open them locally or
  copy them off afterward.
- **`SAR_Imagery` / other-contributor data.** `100_Data/140_SAR_Imagery/` is
  the other contributor's input (multispectral/stereo imagery for
  path-analysis) and, per CLAUDE.md, is explicitly **not** a height source
  for anything in this half of the project — no script here reads it. Its
  presence on this machine is about keeping the full ~200 GB dataset in one
  place, not something this pipeline needs.
