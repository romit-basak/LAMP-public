"""Local browser GUI for running the pipeline's CLI scripts.

Every script's argparse flags already exist as the single source of
truth for what a run needs; this reads them straight from each
script's `build_parser()` (no separately-maintained flag list to drift
out of sync) and renders a form: defaults pre-filled, dropdowns for
`choices`, checkboxes for on/off flags. Submitting builds the exact
argv the CLI would take and runs it as a subprocess, streaming output
back to the page. Solves the "long command, one typo breaks the run"
problem for repeated runs — the CLI itself stays the reference
interface; this is a convenience wrapper around the same argv.

Run:  .venv/bin/python scripts/run_gui.py
Opens http://127.0.0.1:8765/ in the default browser (--port to change,
--no-browser to skip auto-open). stdlib-only (http.server, subprocess,
importlib) — no new dependency.
"""

import argparse
import importlib
import json
import subprocess
import sys
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT / "scripts"
BLENDER_DIR = ROOT / "blender"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(BLENDER_DIR))

# module: where build_parser() lives; script: the file actually run;
# interpreter: only set for the one tool not run with this venv's
# python (Blender's bundled Python instead).
TOOLS = [
    {"id": "viewshed", "module": "viewshed",
     "script": SCRIPTS_DIR / "viewshed.py",
     "label": "viewshed.py — cast rays, write viewsheds/graph/volume"},
    {"id": "build_dem_with_buildings", "module": "build_dem_with_buildings",
     "script": SCRIPTS_DIR / "build_dem_with_buildings.py",
     "label": "build_dem_with_buildings.py — regenerate the ray-casting DEM"},
    {"id": "build_dome_layer", "module": "build_dome_layer",
     "script": SCRIPTS_DIR / "build_dome_layer.py",
     "label": "build_dome_layer.py — dome inventory + QGIS layers"},
    {"id": "compare_baseline", "module": "compare_baseline",
     "script": SCRIPTS_DIR / "compare_baseline.py",
     "label": "compare_baseline.py — r.viewshed vs 3D comparison report"},
    {"id": "export_scene_bundle", "module": "export_scene_bundle",
     "script": SCRIPTS_DIR / "export_scene_bundle.py",
     "label": "export_scene_bundle.py — Blender/Unity export bundle"},
    {"id": "observer_view", "module": "observer_view",
     "script": SCRIPTS_DIR / "observer_view.py",
     "label": "observer_view.py — first-person observer snapshots"},
    {"id": "volume_convert", "module": "volume_convert",
     "script": SCRIPTS_DIR / "volume_convert.py",
     "label": "volume_convert.py — convert a saved volume CSV"},
    {"id": "build_bagawat_scene", "module": "build_bagawat_scene",
     "script": BLENDER_DIR / "build_bagawat_scene.py",
     "label": "build_bagawat_scene.py — Blender render (needs Blender)",
     "interpreter": "blender"},
]


def describe_action(action):
    """Turn one argparse action into a JSON-able field description."""
    if action.dest == "help":
        return None
    positional = not action.option_strings
    flag = action.option_strings[0] if action.option_strings else action.dest
    default = action.default
    if isinstance(default, Path):
        default = str(default)
    elif isinstance(default, (list, tuple)):
        default = " ".join(str(v) for v in default)
    elif default is None:
        default = ""
    else:
        default = str(default)
    multi = action.nargs in ("+", "*") or (
        isinstance(action.nargs, int) and action.nargs > 1)
    return {
        "dest": action.dest,
        "flag": flag,
        "positional": positional,
        "help": action.help or "",
        "choices": [str(c) for c in action.choices] if action.choices else None,
        "is_bool": type(action).__name__ == "_StoreTrueAction",
        "append": type(action).__name__ == "_AppendAction",
        "multi": multi,
        "required": bool(action.required),
        "default": default,
    }


def build_registry():
    """Import every tool's module once and read its parser's fields."""
    registry = {}
    for tool in TOOLS:
        mod = importlib.import_module(tool["module"])
        parser = mod.build_parser()
        fields = [f for f in (describe_action(a) for a in parser._actions) if f]
        if "interpreter" in tool:
            fields.insert(0, {
                "dest": "__interpreter__", "flag": None, "positional": False,
                "help": "Blender executable — \"blender\" if it's on "
                        "PATH, or the full path to blender.exe for a "
                        "portable/no-admin install",
                "choices": None, "is_bool": False, "append": False,
                "multi": False, "required": False,
                "default": tool["interpreter"], "meta": True,
            })
        entry = dict(tool)
        entry["script"] = str(tool["script"])
        entry["fields"] = fields
        registry[tool["id"]] = entry
    return registry


def build_argv(fields, values):
    """Reconstruct the CLI argv a terminal invocation would use.

    Empty/unchecked fields are omitted so the script's own default
    applies — identical to leaving a flag off on the command line.
    """
    argv = []
    for field in fields:
        if field.get("meta"):
            continue
        raw = values.get(field["dest"], "")
        if field["is_bool"]:
            if raw in (True, "true", "on", "1"):
                argv.append(field["flag"])
            continue
        text = (raw or "").strip()
        if not text:
            continue
        if field["append"]:
            for line in text.splitlines():
                line = line.strip()
                if line:
                    argv.append(field["flag"])
                    argv.extend(line.split())
            continue
        tokens = text.split() if field["multi"] else [text]
        if not field["positional"]:
            argv.append(field["flag"])
        argv.extend(tokens)
    return argv


def build_command(tool, values):
    argv = build_argv(tool["fields"], values)
    if "interpreter" in tool:
        interp = (values.get("__interpreter__") or tool["interpreter"]).strip()
        return [interp, "-b", "-P", tool["script"], "--"] + argv
    return [sys.executable, tool["script"]] + argv


JOBS = {}
JOBS_LOCK = threading.Lock()


def start_job(cmd):
    job_id = uuid.uuid4().hex
    state = {"lines": [], "done": False, "returncode": None, "proc": None}
    JOBS[job_id] = state

    def run():
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1)
            with JOBS_LOCK:
                state["proc"] = proc
            for line in proc.stdout:
                with JOBS_LOCK:
                    state["lines"].append(line)
            proc.wait()
            with JOBS_LOCK:
                state["returncode"] = proc.returncode
                state["done"] = True
        except OSError as exc:
            with JOBS_LOCK:
                state["lines"].append(f"[failed to launch] {exc}\n")
                state["returncode"] = -1
                state["done"] = True

    threading.Thread(target=run, daemon=True).start()
    return job_id


INDEX_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>El Bagawat pipeline runner</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 900px;
         margin: 2rem auto; padding: 0 1rem; color: #222; }
  h1 { font-size: 1.3rem; }
  select, input[type=text] { width: 100%; box-sizing: border-box;
    padding: 0.3rem; font-family: inherit; }
  textarea { width: 100%; box-sizing: border-box; font-family: monospace;
    padding: 0.3rem; }
  label.field { display: block; margin: 0.9rem 0; }
  .field-name { font-weight: 600; font-family: monospace; }
  .field-help { color: #555; font-size: 0.85rem; margin-top: 0.15rem; }
  .checkbox-row { display: flex; align-items: center; gap: 0.5rem; }
  .checkbox-row input { width: auto; }
  #cmd { background: #f4f4f4; border: 1px solid #ccc; padding: 0.6rem;
    font-family: monospace; white-space: pre-wrap; word-break: break-all; }
  #log { background: #111; color: #ddd; padding: 0.8rem; height: 320px;
    overflow-y: auto; white-space: pre-wrap; font-family: monospace;
    font-size: 0.85rem; }
  button { padding: 0.5rem 1.2rem; font-size: 1rem; cursor: pointer; }
  #status { font-weight: 600; }
  .required::after { content: " (required)"; color: #a33; font-weight: normal;
    font-size: 0.8rem; }
</style>
</head>
<body>
<h1>El Bagawat pipeline runner</h1>
<p>Picks up each script's own <code>--help</code> flags directly —
nothing here is hand-duplicated, so it can't drift out of sync with the
CLI. The terminal command is still the authoritative form; this just
builds it for you.</p>

<label class="field">
  <span class="field-name">Script</span>
  <select id="tool-select"></select>
</label>

<div id="fields"></div>

<p><strong>Command that will run:</strong></p>
<div id="cmd">(pick a script above)</div>

<p>
  <button id="run-btn">Run</button>
  <button id="stop-btn" disabled>Stop</button>
  <span id="status"></span>
</p>

<pre id="log"></pre>

<script>
let registry = {};
let currentTool = null;
let pollTimer = null;

async function loadRegistry() {
  const res = await fetch("/api/tools");
  registry = await res.json();
  const sel = document.getElementById("tool-select");
  sel.innerHTML = "";
  for (const id in registry) {
    const opt = document.createElement("option");
    opt.value = id;
    opt.textContent = registry[id].label;
    sel.appendChild(opt);
  }
  sel.addEventListener("change", () => renderTool(sel.value));
  renderTool(sel.value);
}

function fieldInput(f) {
  if (f.is_bool) {
    return `<div class="checkbox-row">
      <input type="checkbox" data-dest="${f.dest}">
      <span>on</span></div>`;
  }
  if (f.append) {
    return `<textarea rows="2" data-dest="${f.dest}"
      placeholder="one per line, e.g. 254210 2820958"></textarea>`;
  }
  if (f.choices && !f.multi) {
    const opts = f.choices.map(c =>
      `<option value="${c}" ${c === f.default ? "selected" : ""}>${c}</option>`
    ).join("");
    return `<select data-dest="${f.dest}">${opts}</select>`;
  }
  let placeholder = f.multi ? "space-separated values" : "";
  if (f.choices) placeholder += ` (choices: ${f.choices.join(", ")})`;
  return `<input type="text" data-dest="${f.dest}" value="${f.default}"
    placeholder="${placeholder}">`;
}

function renderTool(id) {
  currentTool = id;
  const tool = registry[id];
  const container = document.getElementById("fields");
  container.innerHTML = "";
  for (const f of tool.fields) {
    const label = document.createElement("label");
    label.className = "field";
    const nameClass = f.required ? "field-name required" : "field-name";
    label.innerHTML = `<span class="${nameClass}">${f.flag}</span>
      ${fieldInput(f)}
      <div class="field-help">${f.help}</div>`;
    container.appendChild(label);
  }
  container.querySelectorAll("[data-dest]").forEach(el =>
    el.addEventListener("input", updatePreview));
  updatePreview();
}

function collectValues() {
  const values = {};
  document.querySelectorAll("#fields [data-dest]").forEach(el => {
    values[el.dataset.dest] = el.type === "checkbox" ? el.checked : el.value;
  });
  return values;
}

async function updatePreview() {
  const res = await fetch("/api/preview", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({tool: currentTool, values: collectValues()}),
  });
  const data = await res.json();
  document.getElementById("cmd").textContent = data.cmd;
}

async function runTool() {
  document.getElementById("log").textContent = "";
  document.getElementById("status").textContent = "running...";
  document.getElementById("run-btn").disabled = true;
  const res = await fetch("/api/run", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({tool: currentTool, values: collectValues()}),
  });
  const data = await res.json();
  document.getElementById("stop-btn").disabled = false;
  document.getElementById("stop-btn").dataset.job = data.job_id;
  pollTimer = setInterval(() => pollLog(data.job_id), 1000);
}

async function pollLog(jobId) {
  const res = await fetch("/api/log?job=" + jobId);
  const data = await res.json();
  document.getElementById("log").textContent = data.log;
  document.getElementById("log").scrollTop = 1e9;
  if (data.done) {
    clearInterval(pollTimer);
    document.getElementById("status").textContent =
      "finished (exit code " + data.returncode + ")";
    document.getElementById("run-btn").disabled = false;
    document.getElementById("stop-btn").disabled = true;
  }
}

async function stopJob() {
  const jobId = document.getElementById("stop-btn").dataset.job;
  if (!jobId) return;
  await fetch("/api/stop?job=" + jobId, {method: "POST"});
}

document.getElementById("run-btn").addEventListener("click", runTool);
document.getElementById("stop-btn").addEventListener("click", stopJob);
loadRegistry();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    registry = {}

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        if self.path == "/":
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/tools":
            self._send_json(self.registry)
        elif self.path.startswith("/api/log"):
            job_id = self.path.split("job=", 1)[-1]
            with JOBS_LOCK:
                state = JOBS.get(job_id)
                if state is None:
                    self._send_json({"error": "unknown job"}, 404)
                    return
                self._send_json({
                    "log": "".join(state["lines"]),
                    "done": state["done"],
                    "returncode": state["returncode"],
                })
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/api/preview":
            data = self._read_json()
            tool = self.registry.get(data.get("tool"))
            if tool is None:
                self._send_json({"error": "unknown tool"}, 404)
                return
            cmd = build_command(tool, data.get("values", {}))
            self._send_json({"cmd": " ".join(cmd)})
        elif self.path == "/api/run":
            data = self._read_json()
            tool = self.registry.get(data.get("tool"))
            if tool is None:
                self._send_json({"error": "unknown tool"}, 404)
                return
            cmd = build_command(tool, data.get("values", {}))
            job_id = start_job(cmd)
            self._send_json({"job_id": job_id, "cmd": " ".join(cmd)})
        elif self.path.startswith("/api/stop"):
            job_id = self.path.split("job=", 1)[-1]
            with JOBS_LOCK:
                state = JOBS.get(job_id)
                proc = state["proc"] if state else None
            if proc is not None:
                proc.terminate()
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        pass


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=8765,
                   help="local port to serve on (default 8765)")
    p.add_argument("--no-browser", action="store_true",
                   help="don't auto-open the page")
    args = p.parse_args()

    print("Loading tool registry (importing every script once)...")
    Handler.registry = build_registry()
    print(f"  {len(Handler.registry)} tools loaded: "
          f"{', '.join(Handler.registry)}")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Serving on {url} (Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
