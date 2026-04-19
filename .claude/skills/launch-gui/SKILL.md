---
name: launch-gui
description: Start the cadlang local web UI with `cadlang gui [PATH]` — browser tree of parts/assemblies with a three.js STL viewer and rebuild buttons. Use when the user wants to iterate on parts visually (tweak → rebuild → see the model update) instead of opening STLs in Windows 3D Viewer.
---

# Launch the cadlang GUI

`cadlang gui` serves a local web UI at **127.0.0.1:8765** from the stdlib HTTP server — no extra dependencies, no UAC prompt. Port is stable by default so the browser tab survives server restarts; pass `--port 0` for ephemeral. One-page three.js STL viewer with a tree of parts and assemblies on the left and a rebuild button that runs the same subprocess the CLI would.

## From a project directory

```bash
cd <project-dir>          # any dir containing (or nested inside) project.cadlang
cadlang gui
```

Prints the bound URL and auto-opens the browser. Ctrl+C to stop.

## Explicit project path or pinned port

```bash
cadlang gui path/to/project
cadlang gui --port 8765 --no-browser
```

`--no-browser` is useful when running over SSH or inside CI.

## Fallback when `cadlang` isn't on PATH

```bash
python cadlang.py gui <project-dir>
```

## What the UI does

- **Left tree**: assemblies (from any root-level `*.yaml` with `parts:`/`mates:`) then parts (any `*.cad.py` under the project). Click an item to load its STL into the viewer.
- **Rebuild selected**: runs `python <part>.cad.py` for parts, `python assembly.py <yaml>` for assemblies. Synchronous — the log panel shows captured stdout/stderr when the rebuild returns.
- **Rebuild all**: runs `cadlang build` against the project (full STEP-import → parts → assembly pipeline).
- **Refresh**: re-scans the project tree; pick this up when you've added a new `.cad.py` outside the UI.

STL reloads are cache-busted per request so re-selecting after a rebuild always shows the fresh mesh.

## When to reach for this vs `invoke-local`

- Reach for **launch-gui** when the user is iterating on a single part or assembly and wants to see the result immediately — the edit → rebuild → view loop stays in one window.
- Reach for **invoke-local** for one-shot regenerations, scripted/batch rebuilds, or anything headless.

## Endpoints (for automation / debugging)

- `GET /api/tree` → `{name, parts:[...], assemblies:[...]}`
- `GET /files/<relpath>` → any file under the project dir (path traversal refused)
- `POST /api/build` with `{"target": "part:<rel>" | "assembly:<rel>" | "all"}` → `{ok, stdout, stderr, duration_ms}`

## Known limitations (v0)

- Synchronous rebuild — a long assembly build (30s+) blocks the UI until it returns. SSE streaming is on the follow-up list.
- No `.cad.py` editor in the browser; edit the file in your editor, then hit Rebuild.
- Viewer is Z-up with a 500 mm grid on XY; very large or very small parts may need a manual camera fit (just click the item again).
