"""
cadlang/gui.py — local web UI to browse a project and rebuild/view parts.

Starts a stdlib HTTP server on 127.0.0.1:8765 (stable port so the browser
tab survives server restarts; pass --port 0 for ephemeral), serves a
three.js STL viewer as a single HTML page, and exposes endpoints to
trigger part/assembly rebuilds. Synchronous v0 — no SSE streaming, the
browser gets the captured stdout/stderr when the rebuild returns.

Run:
    cadlang gui                    # finds project.cadlang upward from cwd
    cadlang gui path/to/project    # explicit project dir or manifest path
    cadlang gui --no-browser       # skip auto-open
    cadlang gui --port 8765        # pin the port (default: ephemeral)

Endpoints:
    GET  /                         -> index.html (three.js viewer)
    GET  /api/tree                 -> {name, parts:[...], assemblies:[...]}
    GET  /files/<relpath>          -> any file under the project dir
    POST /api/build                -> {target:"part:<rel>"|"assembly:<rel>"|"all"}
                                      -> {ok, stdout, stderr, duration_ms}

Security: binds 127.0.0.1 only; /files/ resolves relative paths and
refuses anything outside the project dir.
"""
from __future__ import annotations
import http.server
import json
import mimetypes
import os
import pathlib
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser


HERE = pathlib.Path(__file__).resolve().parent
CADLANG_PY = HERE / 'cadlang.py'
ASSEMBLY_PY = HERE / 'assembly.py'


def _read_yaml_safe(path: pathlib.Path) -> dict:
    try:
        import yaml
    except ImportError:
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding='utf-8'))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _rel_posix(path: pathlib.Path, root: pathlib.Path) -> str:
    return path.relative_to(root).as_posix()


def _is_inside(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _walk_project(project_dir: pathlib.Path) -> dict:
    """Collect parts (any *.cad.py under the project) and assemblies
    (top-level *.yaml / *.yml with a `parts:` or `mates:` section)."""
    manifest = _read_yaml_safe(project_dir / 'project.cadlang')
    name = manifest.get('name', project_dir.name)

    parts = []
    for cad_py in sorted(project_dir.rglob('*.cad.py')):
        stem = cad_py.name[:-len('.cad.py')]  # preserves trailing `.g` for imports
        parent = cad_py.parent
        stl = parent / f'{stem}.stl'
        png = parent / f'{stem}_preview.png'
        meas = parent / f'{stem}.measurements.json'
        parts.append({
            'stem': stem,
            'cad_py': _rel_posix(cad_py, project_dir),
            'stl': _rel_posix(stl, project_dir) if stl.exists() else None,
            'preview_png': _rel_posix(png, project_dir) if png.exists() else None,
            'measurements': _rel_posix(meas, project_dir) if meas.exists() else None,
            'mtime': cad_py.stat().st_mtime,
            'stl_mtime': stl.stat().st_mtime if stl.exists() else None,
        })

    assemblies = []
    for yml in sorted(list(project_dir.glob('*.yaml')) + list(project_dir.glob('*.yml'))):
        data = _read_yaml_safe(yml)
        if 'parts' not in data and 'mates' not in data:
            continue
        aname = data.get('name', yml.stem)
        stl = project_dir / f'{aname}.stl'
        png = project_dir / f'{aname}_preview.png'
        parts_dir = project_dir / f'{aname}_parts'
        inter_path = project_dir / f'{aname}_intersections.json'
        # Per-instance STLs (listed so the viewer can load each and color
        # intersecting ones). Falls back to the combined .stl if the
        # sidecar directory doesn't exist (older assemblies).
        instances = []
        if parts_dir.is_dir():
            for s in sorted(parts_dir.glob('*.stl')):
                instances.append({
                    'key': s.stem,
                    'stl': f'{aname}_parts/{s.name}',
                })
        intersections = []
        if inter_path.is_file():
            try:
                intersections = (_read_yaml_safe(inter_path) or {}).get('intersections') or []
            except Exception:
                intersections = []
        meas = project_dir / f'{aname}.measurements.json'
        assemblies.append({
            'name': aname,
            'yaml': yml.name,
            'stl': f'{aname}.stl' if stl.exists() else None,
            'preview_png': f'{aname}_preview.png' if png.exists() else None,
            'measurements': f'{aname}.measurements.json' if meas.exists() else None,
            'stl_mtime': stl.stat().st_mtime if stl.exists() else None,
            'instances': instances,
            'intersections': intersections,
        })

    return {'name': name, 'root': project_dir.name,
            'parts': parts, 'assemblies': assemblies}


def _run_build(project_dir: pathlib.Path, target: str):
    """Dispatch a rebuild. Returns (ok, stdout, stderr, duration_ms)."""
    t0 = time.time()
    if target == 'all':
        argv = [sys.executable, str(CADLANG_PY), 'build', str(project_dir)]
        cwd = project_dir
    elif target.startswith('part:'):
        rel = target[len('part:'):]
        abs_path = (project_dir / rel).resolve()
        if not _is_inside(abs_path, project_dir) or not abs_path.is_file():
            return False, '', f'bad part path: {rel}', 0
        argv = [sys.executable, str(abs_path)]
        cwd = abs_path.parent
    elif target.startswith('assembly:'):
        rel = target[len('assembly:'):]
        abs_path = (project_dir / rel).resolve()
        if not _is_inside(abs_path, project_dir) or not abs_path.is_file():
            return False, '', f'bad assembly path: {rel}', 0
        argv = [sys.executable, str(ASSEMBLY_PY), str(abs_path)]
        cwd = project_dir
    else:
        return False, '', f'unknown target: {target!r}', 0

    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'  # cadlang prints unicode arrows
    try:
        proc = subprocess.run(
            argv, cwd=str(cwd), capture_output=True,
            encoding='utf-8', errors='replace', timeout=600, env=env,
        )
    except subprocess.TimeoutExpired as e:
        ms = int((time.time() - t0) * 1000)
        return False, e.stdout or '', f'timeout after {ms}ms', ms
    ms = int((time.time() - t0) * 1000)
    return proc.returncode == 0, proc.stdout, proc.stderr, ms


def _make_handler(project_dir: pathlib.Path):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = 'cadlang-gui/0.1'

        def log_message(self, fmt, *args):
            sys.stderr.write(f'[cadlang gui] {self.address_string()} - {fmt % args}\n')

        def _send_json(self, code, payload):
            body = json.dumps(payload).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, code, content_type, data):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            path = url.path
            if path in ('/', '/index.html'):
                self._send_bytes(200, 'text/html; charset=utf-8',
                                 INDEX_HTML.encode('utf-8'))
                return
            if path == '/api/tree':
                self._send_json(200, _walk_project(project_dir))
                return
            if path.startswith('/files/'):
                rel = urllib.parse.unquote(path[len('/files/'):])
                abs_path = (project_dir / rel).resolve()
                if not _is_inside(abs_path, project_dir) or not abs_path.is_file():
                    self._send_json(404, {'error': 'not found', 'path': rel})
                    return
                suffix = abs_path.suffix.lower()
                if suffix == '.stl':
                    mime = 'model/stl'
                elif suffix in ('.py', '.yaml', '.yml', '.md', '.cadlang', '.txt'):
                    mime = 'text/plain; charset=utf-8'
                else:
                    mime, _ = mimetypes.guess_type(str(abs_path))
                    mime = mime or 'application/octet-stream'
                self._send_bytes(200, mime, abs_path.read_bytes())
                return
            self._send_json(404, {'error': 'not found', 'path': path})

        def do_POST(self):
            url = urllib.parse.urlparse(self.path)
            if url.path != '/api/build':
                self._send_json(404, {'error': 'not found'})
                return
            length = int(self.headers.get('Content-Length') or '0')
            raw = self.rfile.read(length) if length else b''
            try:
                body = json.loads(raw.decode('utf-8')) if raw else {}
            except Exception as e:
                self._send_json(400, {'error': f'bad json: {e}'})
                return
            target = body.get('target')
            if not isinstance(target, str) or not target:
                self._send_json(400, {'error': 'missing/invalid target'})
                return
            ok, out, err, ms = _run_build(project_dir, target)
            self._send_json(200, {'ok': ok, 'stdout': out, 'stderr': err,
                                  'duration_ms': ms, 'target': target})

    return Handler


class _ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


_WATCHED_MODULES = ('gui.py', 'cadlang.py', 'assembly.py', 'stepimport.py')


def _watch_and_reload(interval=1.0):
    """Daemon-thread hot-reloader: poll watched module mtimes, exec self on
    change. The browser tab survives because --port is stable by default."""
    paths = [HERE / n for n in _WATCHED_MODULES]
    snapshot = {}
    for p in paths:
        try:
            snapshot[p] = p.stat().st_mtime
        except OSError:
            pass
    while True:
        time.sleep(interval)
        for p in paths:
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if p not in snapshot:
                snapshot[p] = m
                continue
            if m != snapshot[p]:
                print(f'[cadlang gui] source changed: {p.name} — reloading')
                sys.stdout.flush()
                os.environ['CADLANG_GUI_RELOADED'] = '1'
                try:
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                except OSError as e:
                    print(f'[cadlang gui] exec failed: {e}; exiting')
                    os._exit(3)


def serve(project_path=None, host='127.0.0.1', port=8765,
          open_browser=True, reload=True):
    import cadlang  # reuse _find_project
    proj = cadlang._find_project(project_path)
    project_dir = proj.parent.resolve()
    handler = _make_handler(project_dir)
    server = _ThreadingServer((host, port), handler)
    bound_host, bound_port = server.server_address[:2]
    url = f'http://{bound_host}:{bound_port}/'
    reloaded = bool(os.environ.get('CADLANG_GUI_RELOADED'))
    print(f'[cadlang gui] project: {project_dir}')
    print(f'[cadlang gui] {"reloaded" if reloaded else "serving"}: {url}  (Ctrl+C to stop)')
    if reload and port == 0:
        print('[cadlang gui] WARN: --reload + --port 0 means the port will change on reload; '
              'the browser tab will dangle. Pin a port to keep hot-reload usable.')
    if open_browser and not reloaded:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    if reload:
        threading.Thread(target=_watch_and_reload, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n[cadlang gui] stopping')
    finally:
        server.server_close()
    return 0


# =========================================================================
# Embedded single-page viewer. three.js + STLLoader + OrbitControls via ESM
# CDN; no build step, no npm. Z-up orientation to match cadlang convention.
# =========================================================================

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>cadlang gui</title>
<style>
  :root { color-scheme: dark; --bg:#1e1e1e; --fg:#ddd; --muted:#888;
          --panel:#252526; --border:#3c3c3c; --accent:#0e639c; }
  html,body { margin:0; height:100%; background:var(--bg); color:var(--fg);
              font:13px/1.4 -apple-system, "Segoe UI", sans-serif; }
  #app { display:grid; grid-template-columns:300px 1fr;
         grid-template-rows:44px 1fr 5px var(--bot-h, 260px);
         height:100vh; --bot-h:260px; }
  #gripper { grid-column:2; grid-row:3; cursor:row-resize;
             background:var(--border); }
  #gripper:hover, #gripper.dragging { background:var(--accent); }
  header { grid-column:1/3; display:flex; align-items:center; gap:8px;
           padding:0 12px; background:var(--panel);
           border-bottom:1px solid var(--border); }
  header .name { font-weight:600; }
  header .sel  { color:var(--muted); }
  header .spacer { flex:1; }
  button { background:var(--accent); color:white; border:none;
           padding:6px 12px; border-radius:3px; cursor:pointer; font:inherit; }
  button:hover:not(:disabled) { background:#1177bb; }
  button:disabled { background:#444; color:#999; cursor:not-allowed; }
  #tree { grid-row:2/5; grid-column:1; overflow-y:auto; background:var(--panel);
          border-right:1px solid var(--border); padding:8px 0; }
  #tree .group { padding:8px 12px 4px; color:var(--muted); font-size:11px;
                 text-transform:uppercase; letter-spacing:0.05em; }
  #tree .item { padding:4px 12px; cursor:pointer; display:flex;
                align-items:center; gap:6px; }
  #tree .item:hover { background:#2a2d2e; }
  #tree .item.selected { background:#094771; }
  #tree .item .stem { flex:1; overflow:hidden; text-overflow:ellipsis;
                      white-space:nowrap; }
  #tree .item .badge { font-size:10px; color:var(--muted); }
  #viewer { grid-row:2; grid-column:2; position:relative;
            overflow:hidden; background:#0a0a0a; }
  #viewer canvas { display:block; }
  #stats { position:absolute; top:8px; right:8px; font-size:11px;
           color:var(--muted); background:rgba(0,0,0,0.5);
           padding:4px 8px; border-radius:3px; pointer-events:none; }
  #measurements { position:absolute; top:40px; right:8px; max-width:260px;
                  max-height:calc(100% - 56px); overflow-y:auto;
                  background:rgba(20,20,20,0.88); border:1px solid var(--border);
                  border-radius:4px; padding:6px 10px 8px; font-size:11px;
                  color:var(--fg); display:none; }
  #measurements.visible { display:block; }
  #measurements h4 { margin:6px 0 3px; font-size:10px; text-transform:uppercase;
                     letter-spacing:0.05em; color:var(--muted); font-weight:600; }
  #measurements h4:first-child { margin-top:0; }
  #measurements table { width:100%; border-collapse:collapse; }
  #measurements td { padding:1px 2px; font-variant-numeric:tabular-nums; }
  #measurements td.label { color:var(--muted); }
  #measurements td.value { text-align:right; color:var(--fg); }
  #measurements td.unit  { color:var(--muted); padding-left:4px;
                           width:1.6em; }
  #cam-tools { position:absolute; top:8px; left:8px; display:flex; gap:3px;
               background:rgba(0,0,0,0.55); padding:4px; border-radius:4px; }
  #cam-tools button { background:#2a2a2a; color:var(--fg); border:none;
                      padding:4px 9px; font-size:11px; border-radius:3px;
                      cursor:pointer; min-width:44px; }
  #cam-tools button:hover { background:#3a3a3a; }
  #bottom { grid-row:4; grid-column:2; display:flex; flex-direction:column;
            background:#111; border-top:1px solid var(--border); min-height:0; }
  .tabs { display:flex; background:#1a1a1a; border-bottom:1px solid var(--border);
          flex:0 0 auto; }
  .tab { background:transparent; color:var(--muted); border:none;
         padding:6px 14px; font:inherit; cursor:pointer; border-radius:0;
         border-bottom:2px solid transparent; }
  .tab:hover { background:#252526; color:var(--fg); }
  .tab.active { color:var(--fg); border-bottom-color:var(--accent); }
  .tab .path { color:var(--muted); font-size:10px; margin-left:6px; }
  .pane { flex:1 1 auto; overflow:auto; padding:6px 12px;
          font-family:Consolas, Menlo, monospace; font-size:11px;
          white-space:pre-wrap; word-break:break-word; }
  .pane.hidden { display:none; }
  #log .err { color:#f48771; }
  #log .ok  { color:#89d185; }
  #log .dim { color:var(--muted); }
  #source  { color:#d4d4d4; tab-size:2; padding:0; }
  #source .empty { color:var(--muted); font-style:italic; padding:6px 12px;
                   display:block; }
  #source code.hljs { display:block; padding:6px 12px; background:transparent; }
</style>
<link rel="stylesheet"
      href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.10.0/styles/github-dark.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.10.0/highlight.min.js"></script>
</head>
<body>
<div id="app">
  <header>
    <span class="name" id="proj-name">…</span>
    <span class="sel" id="sel-label">(nothing selected)</span>
    <span class="spacer"></span>
    <button id="btn-rebuild" disabled>Rebuild selected</button>
    <button id="btn-rebuild-all">Rebuild all</button>
    <button id="btn-refresh">Refresh</button>
  </header>
  <aside id="tree"></aside>
  <main id="viewer">
    <div id="cam-tools">
      <button data-view="fit"   title="Fit object in view (F)">Fit</button>
      <button data-view="iso"   title="Isometric (home)">Iso</button>
      <button data-view="top"   title="Top (looking down −Z)">Top</button>
      <button data-view="front" title="Front (looking at +Y)">Front</button>
      <button data-view="right" title="Right (looking at −X)">Right</button>
    </div>
    <div id="stats"></div>
    <div id="measurements"></div>
  </main>
  <div id="gripper" title="Drag to resize"></div>
  <div id="bottom">
    <div class="tabs">
      <button class="tab active" data-tab="log">Log</button>
      <button class="tab" data-tab="source">Source<span class="path" id="source-path"></span></button>
    </div>
    <div class="pane" id="log"></div>
    <pre class="pane hidden" id="source"><span class="empty">Select a part or assembly to view its source.</span></pre>
  </div>
</div>
<script type="module">
import * as THREE from 'https://esm.sh/three@0.160.0';
import { STLLoader } from 'https://esm.sh/three@0.160.0/examples/jsm/loaders/STLLoader.js';
import { OrbitControls } from 'https://esm.sh/three@0.160.0/examples/jsm/controls/OrbitControls.js';

const viewer = document.getElementById('viewer');
const statsEl = document.getElementById('stats');
const measurementsEl = document.getElementById('measurements');
const logEl = document.getElementById('log');
const treeEl = document.getElementById('tree');
const sourceEl = document.getElementById('source');
const sourcePathEl = document.getElementById('source-path');
const projName = document.getElementById('proj-name');
const selLabel = document.getElementById('sel-label');
const btnRebuild = document.getElementById('btn-rebuild');
const btnRebuildAll = document.getElementById('btn-rebuild-all');
const btnRefresh = document.getElementById('btn-refresh');

// Z-up scene so cadlang parts appear with their design axes aligned.
THREE.Object3D.DEFAULT_UP.set(0, 0, 1);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0a0a0a);
const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 10000);
camera.up.set(0, 0, 1);
camera.position.set(250, -250, 200);
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(window.devicePixelRatio);
viewer.appendChild(renderer.domElement);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
// Fusion / Autodesk-style mouse mapping: MMB pans, RMB pans too,
// LMB rotates, scroll zooms. Shift+LMB also pans as a laptop fallback.
controls.mouseButtons = {
  LEFT: THREE.MOUSE.ROTATE,
  MIDDLE: THREE.MOUSE.PAN,
  RIGHT: THREE.MOUSE.PAN,
};
controls.screenSpacePanning = true;

scene.add(new THREE.AmbientLight(0x808080, 0.9));
const key = new THREE.DirectionalLight(0xffffff, 0.9);
key.position.set(200, -200, 400); scene.add(key);
const fill = new THREE.DirectionalLight(0xffffff, 0.3);
fill.position.set(-200, 100, -100); scene.add(fill);

// 1000 mm grid on XY so typical-scale assemblies (dewshield ≈ Ø250 × 500 tall)
// stay inside the floor when viewed from above.
const grid = new THREE.GridHelper(1000, 100, 0x444444, 0x222222);
grid.rotation.x = Math.PI / 2;          // lay on XY (Z-up)
scene.add(grid);
scene.add(new THREE.AxesHelper(100));

let currentMeshes = [];                 // array of THREE.Mesh currently on screen
let currentAnchors = [];                // array of {line, sprite} for dimension markers
let selected = null;                    // {target, label, stl, instances, intersections}

function resize() {
  const w = viewer.clientWidth, h = viewer.clientHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h || 1;
  camera.updateProjectionMatrix();
}
window.addEventListener('resize', resize);
new ResizeObserver(resize).observe(viewer);
resize();

function sceneBox() {
  if (currentMeshes.length) {
    const box = new THREE.Box3();
    for (const m of currentMeshes) box.expandByObject(m);
    return box;
  }
  return new THREE.Box3(new THREE.Vector3(-100,-100,-100),
                        new THREE.Vector3( 100, 100, 100));
}

function frame(obj) { setView('iso'); }

// Named viewpoints. Z-up convention: top view forces up=Y to avoid a gimbal
// singularity; every other view keeps up=Z (camera.up was set once at init).
//
// Framing target is the world origin (0,0,0) — keeps the design-frame axes
// anchored in-view regardless of where the part's bbox sits. Distance is
// sized to fit the bbox *expanded to include the origin*, so off-origin
// parts (rails at x=60+) stay visible.
function setView(name) {
  const box = sceneBox().clone().expandByPoint(new THREE.Vector3(0, 0, 0));
  const size = box.getSize(new THREE.Vector3());
  const center = new THREE.Vector3(0, 0, 0);
  const d = (Math.max(size.x, size.y, size.z) || 150) * 2.0;
  if (name === 'fit') {
    // Keep current direction, just reframe distance.
    const dir = new THREE.Vector3().subVectors(camera.position, controls.target);
    if (dir.lengthSq() < 1e-6) dir.set(1, -1, 0.7);
    dir.normalize().multiplyScalar(d);
    camera.position.copy(center).add(dir);
  } else {
    const views = {
      iso:   { off: [ d, -d, d * 0.7], up: [0, 0, 1] },
      top:   { off: [ 0,  0,  d],      up: [0, 1, 0] },
      front: { off: [ 0, -d,  0],      up: [0, 0, 1] },
      right: { off: [ d,  0,  0],      up: [0, 0, 1] },
    };
    const v = views[name] || views.iso;
    camera.position.set(center.x + v.off[0], center.y + v.off[1], center.z + v.off[2]);
    camera.up.set(v.up[0], v.up[1], v.up[2]);
  }
  camera.lookAt(center);
  controls.target.copy(center);
  controls.update();
}

for (const b of document.querySelectorAll('#cam-tools button')) {
  b.addEventListener('click', () => setView(b.dataset.view));
}
window.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  const key = e.key.toLowerCase();
  if (key === 'f') setView('fit');
  else if (key === 'i') setView('iso');
  else if (key === 't') setView('top');
});

function log(msg, cls) {
  const s = document.createElement('span');
  if (cls) s.className = cls;
  s.textContent = msg + '\n';
  logEl.appendChild(s);
  logEl.scrollTop = logEl.scrollHeight;
}

function _clearMeshes() {
  for (const m of currentMeshes) {
    scene.remove(m);
    m.geometry.dispose();
    m.material.dispose();
  }
  currentMeshes = [];
}

const LOADER = new STLLoader();
const MAT_OK  = { color: 0xb0c8e0, specular: 0x222222, shininess: 30, flatShading: false };
const MAT_BAD = { color: 0xe04848, specular: 0x552222, shininess: 30, flatShading: false,
                  emissive: 0x2a0000 };

async function _loadOne(relpath, material) {
  const res = await fetch('/files/' + relpath + '?t=' + Date.now());
  if (!res.ok) throw new Error('HTTP ' + res.status + ' on ' + relpath);
  const buf = await res.arrayBuffer();
  const geom = LOADER.parse(buf);
  geom.computeVertexNormals();
  const mesh = new THREE.Mesh(geom, new THREE.MeshPhongMaterial(material));
  scene.add(mesh);
  currentMeshes.push(mesh);
  return geom;
}

async function loadSTL(opts) {
  _clearMeshes();
  const label = opts.label;
  // Assembly with per-instance STLs: load each, tint intersecting ones red.
  if (opts.instances && opts.instances.length) {
    const bad = new Set();
    for (const it of (opts.intersections || [])) {
      bad.add(it.a); bad.add(it.b);
    }
    let totalTris = 0;
    try {
      for (const inst of opts.instances) {
        const material = bad.has(inst.key) ? MAT_BAD : MAT_OK;
        const geom = await _loadOne(inst.stl, material);
        totalTris += geom.attributes.position.count / 3;
      }
    } catch (e) {
      log(`assembly load failed: ${e.message || e}`, 'err');
    }
    // Camera fit on the union of placed meshes.
    const union = new THREE.Group();
    for (const m of currentMeshes) union.add(m.clone());
    frame(union);
    const badCount = bad.size;
    const badTxt = badCount
      ? ` — ${badCount} part${badCount === 1 ? '' : 's'} in unexpected overlap`
      : '';
    statsEl.textContent = `${label} — ${opts.instances.length} parts, `
      + `${totalTris.toLocaleString()} tris${badTxt}`;
    if (badCount) log(`${label}: ${opts.intersections.length} unexpected intersection(s)`, 'err');
    return;
  }
  // Single part (or legacy assembly with no sidecar) — fall back to one STL.
  if (!opts.stl) { statsEl.textContent = `${label} — no STL yet`; return; }
  try {
    const geom = await _loadOne(opts.stl, MAT_OK);
    frame(currentMeshes[0]);
    const tris = geom.attributes.position.count / 3;
    statsEl.textContent = `${label} — ${tris.toLocaleString()} tris`;
  } catch (e) {
    log(`load failed: ${opts.stl}: ${e.message || e}`, 'err');
    statsEl.textContent = `${label} — load failed`;
  }
}

function keepSpritesOnScreen() {
  // Scale each anchor's label so it projects to a constant ~44-pixel
  // height on screen regardless of camera distance. This keeps labels
  // readable when zoomed out on a large assembly AND when zoomed in on
  // a small part. Width follows the canvas aspect.
  if (!currentAnchors.length) return;
  const h = renderer.domElement.clientHeight || 1;
  const vFOV = THREE.MathUtils.degToRad(camera.fov);
  const targetPx = 22;
  for (const a of currentAnchors) {
    if (!a.sprite) continue;
    const dist = camera.position.distanceTo(a.sprite.position);
    const worldPerPx = (2 * Math.tan(vFOV / 2) * dist) / h;
    const spriteH = worldPerPx * targetPx;
    const aspect = a.sprite.userData.aspect || 4;
    a.sprite.scale.set(spriteH * aspect, spriteH, 1);
  }
}

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  keepSpritesOnScreen();
  renderer.render(scene, camera);
}
animate();

function mkItem(label, opts) {
  const el = document.createElement('div');
  el.className = 'item';
  el.dataset.target = opts.target;
  const stem = document.createElement('span');
  stem.className = 'stem'; stem.textContent = label;
  el.appendChild(stem);
  if (opts.badge) {
    const b = document.createElement('span');
    b.className = 'badge'; b.textContent = opts.badge;
    el.appendChild(b);
  }
  el.addEventListener('click', () => selectItem(el, { ...opts, label }));
  return el;
}

function selectItem(el, opts) {
  document.querySelectorAll('#tree .item.selected').forEach(e => e.classList.remove('selected'));
  if (el) el.classList.add('selected');
  selected = opts;
  selLabel.textContent = opts.label;
  btnRebuild.disabled = !opts.target;
  loadSTL(opts);
  loadSource(opts.source);
  loadMeasurements(opts.measurements);
}

function _fmtValue(v) {
  if (typeof v === 'number') {
    if (Number.isInteger(v)) return String(v);
    return v.toFixed(Math.abs(v) < 10 ? 3 : 2);
  }
  return String(v);
}

function _clearAnchors() {
  for (const a of currentAnchors) {
    scene.remove(a.line);
    a.line.geometry.dispose();
    a.line.material.dispose();
    if (a.sprite) {
      scene.remove(a.sprite);
      if (a.sprite.material.map) a.sprite.material.map.dispose();
      a.sprite.material.dispose();
    }
  }
  currentAnchors = [];
}

function _makeLabelSprite(text, color) {
  // Render the label text into a 2D canvas at high res so it scales to any
  // camera distance without blurring. Width is sized to the text — we
  // store the aspect ratio on the sprite so the render loop can keep it
  // a constant pixel height on screen.
  const fontPx = 60, pad = 14;
  const measure = document.createElement('canvas').getContext('2d');
  measure.font = `bold ${fontPx}px "Segoe UI", sans-serif`;
  const tw = Math.ceil(measure.measureText(text).width);
  const canvas = document.createElement('canvas');
  canvas.width = tw + pad * 2;
  canvas.height = fontPx + pad * 2;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = 'rgba(0,0,0,0.82)';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.strokeStyle = 'rgba(255,255,255,0.12)';
  ctx.lineWidth = 2;
  ctx.strokeRect(1, 1, canvas.width - 2, canvas.height - 2);
  ctx.fillStyle = color;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.font = `bold ${fontPx}px "Segoe UI", sans-serif`;
  ctx.fillText(text, canvas.width / 2, canvas.height / 2);
  const tex = new THREE.CanvasTexture(canvas);
  tex.minFilter = THREE.LinearFilter;
  tex.anisotropy = 4;
  tex.needsUpdate = true;
  const mat = new THREE.SpriteMaterial({ map: tex, depthTest: false,
                                         transparent: true });
  const s = new THREE.Sprite(mat);
  // Remember the canvas aspect so the animate loop can pick a world-scale
  // that projects to a constant pixel height (see keepSpritesOnScreen).
  s.userData.aspect = canvas.width / canvas.height;
  return s;
}

function drawAnchors(doc) {
  _clearAnchors();
  // Concentric diameters all pass through the origin so their midpoints
  // collide. Alternate them across the line so nothing stacks.
  const tByKind = {
    diameter: [0.68, 0.32, 0.82, 0.18, 0.92, 0.08],
    radius:   [0.8, 0.6, 0.4, 0.2],
    linear:   [0.5, 0.65, 0.35, 0.8, 0.2],
  };
  const kindCounts = { diameter: 0, radius: 0, linear: 0 };
  for (const sec of (doc.sections || [])) {
    for (const row of (sec.rows || [])) {
      if (!row.anchor) continue;
      const a = row.anchor;
      const kind = a.kind || 'linear';
      const colorHex = kind === 'diameter' ? 0xffeb3b
                      : kind === 'radius'  ? 0xff9800 : 0x4fc3f7;
      const colorCss = '#' + colorHex.toString(16).padStart(6, '0');
      const p0 = new THREE.Vector3(a.from[0], a.from[1], a.from[2]);
      const p1 = new THREE.Vector3(a.to[0],   a.to[1],   a.to[2]);
      const geom = new THREE.BufferGeometry().setFromPoints([p0, p1]);
      const mat = new THREE.LineBasicMaterial({
        color: colorHex, depthTest: false, transparent: true, opacity: 0.95,
      });
      const line = new THREE.Line(geom, mat);
      line.renderOrder = 999;
      scene.add(line);
      const prefix = kind === 'diameter' ? 'Ø ' : (kind === 'radius' ? 'R ' : '');
      const sprite = _makeLabelSprite(
        prefix + row.label + ' = ' + _fmtValue(row.value) +
        (typeof row.value === 'number' ? ' ' + (row.unit || doc.units || 'mm') : ''),
        colorCss,
      );
      const ts = tByKind[kind] || tByKind.linear;
      const t = ts[kindCounts[kind] % ts.length];
      kindCounts[kind]++;
      sprite.position.copy(p0).lerp(p1, t);
      // Initial scale is nominal — the animate loop rescales every frame
      // so sprites are a constant pixel height regardless of zoom.
      sprite.scale.set(20 * sprite.userData.aspect, 20, 1);
      sprite.renderOrder = 1000;
      scene.add(sprite);
      currentAnchors.push({ line, sprite });
    }
  }
}

async function loadMeasurements(relpath) {
  _clearAnchors();
  measurementsEl.classList.remove('visible');
  measurementsEl.innerHTML = '';
  if (!relpath) return;
  let doc;
  try {
    const res = await fetch('/files/' + relpath + '?t=' + Date.now());
    if (!res.ok) throw new Error('HTTP ' + res.status);
    doc = await res.json();
  } catch (e) {
    log(`measurements load failed: ${e.message || e}`, 'err');
    return;
  }
  const unit = doc.units || 'mm';
  for (const sec of (doc.sections || [])) {
    const h = document.createElement('h4');
    h.textContent = sec.title;
    measurementsEl.appendChild(h);
    const t = document.createElement('table');
    for (const row of (sec.rows || [])) {
      const tr = document.createElement('tr');
      const tdL = document.createElement('td'); tdL.className = 'label';
      tdL.textContent = row.label;
      const tdV = document.createElement('td'); tdV.className = 'value';
      tdV.textContent = _fmtValue(row.value);
      const tdU = document.createElement('td'); tdU.className = 'unit';
      tdU.textContent = row.unit || (typeof row.value === 'number' ? unit : '');
      tr.appendChild(tdL); tr.appendChild(tdV); tr.appendChild(tdU);
      t.appendChild(tr);
    }
    measurementsEl.appendChild(t);
  }
  if (measurementsEl.children.length) measurementsEl.classList.add('visible');
  drawAnchors(doc);
}

function _langForPath(relpath) {
  const p = relpath.toLowerCase();
  if (p.endsWith('.py')) return 'python';
  if (p.endsWith('.yaml') || p.endsWith('.yml') || p.endsWith('.cadlang')) return 'yaml';
  if (p.endsWith('.json')) return 'json';
  if (p.endsWith('.md')) return 'markdown';
  return 'plaintext';
}

async function loadSource(relpath) {
  sourceEl.innerHTML = '';
  if (!relpath) {
    const empty = document.createElement('span');
    empty.className = 'empty';
    empty.textContent = '(no source file for this item)';
    sourceEl.appendChild(empty);
    sourcePathEl.textContent = '';
    return;
  }
  sourcePathEl.textContent = ' — ' + relpath;
  try {
    const res = await fetch('/files/' + relpath + '?t=' + Date.now());
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const text = await res.text();
    const code = document.createElement('code');
    code.className = 'language-' + _langForPath(relpath);
    code.textContent = text;
    sourceEl.appendChild(code);
    if (window.hljs) hljs.highlightElement(code);
  } catch (e) {
    sourceEl.innerHTML = `<span class="empty">load failed: ${e.message || e}</span>`;
  }
}

// Draggable gripper between viewer and bottom pane — adjusts the `--bot-h`
// CSS var that drives the grid's bottom-row height.
(function initGripper() {
  const gripper = document.getElementById('gripper');
  const app = document.getElementById('app');
  let dragging = false;
  gripper.addEventListener('mousedown', e => {
    dragging = true;
    gripper.classList.add('dragging');
    document.body.style.userSelect = 'none';
    e.preventDefault();
  });
  window.addEventListener('mousemove', e => {
    if (!dragging) return;
    const h = Math.max(80, Math.min(window.innerHeight - 120,
                                    window.innerHeight - e.clientY));
    app.style.setProperty('--bot-h', h + 'px');
  });
  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    gripper.classList.remove('dragging');
    document.body.style.userSelect = '';
    resize();
  });
})();

for (const tab of document.querySelectorAll('.tab')) {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t === tab));
    const active = tab.dataset.tab;
    document.querySelectorAll('.pane').forEach(p => p.classList.toggle('hidden', p.id !== active));
  });
}

function selectByTarget(target) {
  for (const el of treeEl.querySelectorAll('.item')) {
    if (el.dataset.target === target) { el.click(); return true; }
  }
  return false;
}

async function refreshTree() {
  const res = await fetch('/api/tree');
  const data = await res.json();
  projName.textContent = data.name;
  document.title = `cadlang gui — ${data.name}`;
  treeEl.innerHTML = '';
  if (data.assemblies.length) {
    const g = document.createElement('div');
    g.className = 'group'; g.textContent = 'Assemblies';
    treeEl.appendChild(g);
    for (const a of data.assemblies) {
      const bad = (a.intersections || []).length;
      const badge = bad ? `${bad} overlap${bad === 1 ? '' : 's'}`
                        : (a.stl ? '' : 'no stl');
      treeEl.appendChild(mkItem(a.name, {
        target: 'assembly:' + a.yaml,
        stl: a.stl, source: a.yaml,
        measurements: a.measurements,
        instances: a.instances || [],
        intersections: a.intersections || [],
        badge,
      }));
    }
  }
  if (data.parts.length) {
    const g = document.createElement('div');
    g.className = 'group'; g.textContent = 'Parts';
    treeEl.appendChild(g);
    for (const p of data.parts) {
      treeEl.appendChild(mkItem(p.stem, {
        target: 'part:' + p.cad_py,
        stl: p.stl, source: p.cad_py,
        measurements: p.measurements,
        badge: p.stl ? '' : 'no stl',
      }));
    }
  }
  return data;
}

async function build(target, label) {
  log(`→ ${label} (${target})`, 'dim');
  const prevTarget = selected?.target;
  btnRebuild.disabled = btnRebuildAll.disabled = btnRefresh.disabled = true;
  try {
    const res = await fetch('/api/build', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target }),
    });
    const data = await res.json();
    if (data.stdout) log(data.stdout.trimEnd());
    if (data.stderr) log(data.stderr.trimEnd(), 'err');
    log(`${data.ok ? '✓' : '✗'} ${label} (${data.duration_ms} ms)`,
        data.ok ? 'ok' : 'err');
    await refreshTree();
    if (prevTarget) selectByTarget(prevTarget);
  } catch (e) {
    log(`build error: ${e.message || e}`, 'err');
  } finally {
    btnRebuildAll.disabled = btnRefresh.disabled = false;
    btnRebuild.disabled = !selected?.target;
  }
}

btnRebuild.addEventListener('click',
  () => selected && build(selected.target, selected.label));
btnRebuildAll.addEventListener('click', () => build('all', 'project'));
btnRefresh.addEventListener('click', refreshTree);

log('cadlang gui ready', 'dim');
refreshTree();
</script>
</body>
</html>
"""


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(prog='cadlang gui')
    p.add_argument('project', nargs='?', default=None)
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8765)
    p.add_argument('--no-browser', action='store_true')
    p.add_argument('--no-reload', action='store_true',
                   help='disable auto-restart on cadlang source edits')
    args = p.parse_args()
    sys.exit(serve(args.project, host=args.host, port=args.port,
                   open_browser=not args.no_browser,
                   reload=not args.no_reload))
