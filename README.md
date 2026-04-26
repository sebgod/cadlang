# cadlang

Small Python DSL for describing 3D-printable parts declaratively. One
description emits both a watertight STL preview and a Fusion 360 parametric
script. A STEP importer can reverse-engineer existing CAD files into
starter `.cad.py` descriptions.

## Why

- Keep part descriptions readable, diffable, version-controllable.
- Avoid re-learning the Fusion 360 Python API every time you tweak a dim.
- Still open the result in Fusion for fine editing / heat-insert placement /
  print prep — `params` flow into Fusion's parameter dialog.

## Install

Python 3.10+ on Windows x64:

```
python -m pip install -r requirements.txt
```

Pulls in `numpy`, `trimesh`, `matplotlib`, `manifold3d` (the CSG kernel),
and `python-solvespace` (the 2D constraint solver behind `sketch.py`).

cadlang is **GPLv3+** (forced by `python-solvespace`'s license — see
`LICENSE`).

## Folder layout

```
cadlang/
├── cadlang.py          # Design + STL/Fusion backends + `cadlang build` / `cadlang gui` CLI
├── stepimport.py       # STEP reader + feature recogniser + .cad.py emitter
├── assembly.py         # YAML assembly loader + mate solver + intersection check
├── gui.py              # local web UI (stdlib server + three.js viewer)
├── tests/              # pytest suite — importer + mate-solver + geometry pipeline
├── CLAUDE.md           # detailed pipeline + DSL reference (contributor/agent)
├── ROADMAP.md          # direction + open follow-ups across the tooling
├── requirements.txt
└── example-project/<name>/
    ├── project.cadlang         # the project manifest (`cadlang build` entry)
    ├── assembly.yaml           # optional multi-part assembly
    ├── STATE.md                # per-project notes
    ├── *.step                  # STEP source imports
    └── parts/                  # .cad.py files + their generated artifacts
```

Generated artifacts land next to their source — no separate `out/` tree.
Treat them as disposable; `cadlang build` will regenerate them.

## Quick start — a whole project

```
cd example-project/<some-project>
python ../../cadlang.py build
```

Runs every step in that project's `project.cadlang` (e.g. import STEPs →
assemble). Combined outputs (`<assembly>.stl`, `<assembly>_preview.png`)
land at the project root; per-part outputs land in `parts/`.

## Quick start — one part at a time

```
python parts/<some-part>.cad.py          # one hand-written part
python assembly.py <some-assembly.yaml>  # one assembly
python stepimport.py thing.step -o parts/  # one STEP import
```

## Browse a project visually

```
cadlang gui                     # serves http://127.0.0.1:8765
```

Left-pane tree of parts + assemblies, three.js STL viewer, rebuild
buttons, hot-reload on source edits, red-highlight on any assembly
instances that intersect unexpectedly.

## Run the tests

```
pip install .[dev]
pytest tests/ -v
```

Tier 1 importer / mate-solver analysis runs in ms (no CSG); Tier 2
geometry pipeline runs in ~500 ms (volumes + watertight). All 24 tests
under 1 second.

## Importing an existing STEP

```
python stepimport.py thing.step -o parts/
```

Writes one starter `.cad.py` per body in the STEP file (named
`<source>_partNNN.g.cad.py` — the `.g.` infix marks auto-generated sources
distinct from hand-written `.cad.py` files). Review it, edit params, re-run.

## Opening the Fusion output

1. Fusion → **Utilities → Scripts and Add-Ins → Scripts** tab.
2. Green **+** (Create) → **From Existing** → pick the generated `_fusion.py`.
3. Highlight it → **Run**. The body appears in the design timeline.
4. **Modify → Change Parameters** to tweak any dimension; the timeline
   recomputes.

## Editing a part

Edit `parts/<name>.cad.py`, re-run it. All values live in the `params` dict.
Expressions like `'outer_r/2 + 1'` reference those params by name. Never
edit the generated files — they're overwritten.

## Current feature coverage

- `revolve(plane='XZ', axis='Z', profile=…)` — axisymmetric bodies
- `extrude(on='XY' | OffsetPlane('XY', …), profile=…, height=…)` — prismatic
  bodies. Also accepts `sketch=Sketch(…)` for constraint-based 2D sketches
  (lines + circles + H/V/parallel/perpendicular/coincident/distance/angle/
  diameter constraints, solved by `python-solvespace`); the Fusion script
  emits a native sketch with parametric dimensions.
- `cut(on=top_face(body) | OffsetPlane('YZ'|'XY', …), sketch=[Rect|Circle…],
  depth=…, pattern=Circular(…))`
- STEP importer: revolve detection, layered extrude detection, radial bore
  clustering, axial through-hole detection, lateral saddle-cut detection,
  hole-depth measurement from trim geometry.
- Assembly layer: YAML description with interfaces (`bolt_pair`,
  `tabbed_hole_stack`) + mates → combined STL. Explicit `pose:` / `circular:`
  fallback still supported.

See `CLAUDE.md` for the full DSL reference and pipeline diagram.

## Known limitations

- Non-XZ revolve planes / non-Z axes not yet supported.
- Fillets/chamfers are currently baked into revolve profiles, not a first-
  class op.
- Importer emits bbox-rectangle outlines per extrude layer — curved or
  chamfered outlines lose shape.
- Lateral cylindrical saddles that would over-carve the body (e.g. the
  dewshield rail's r=125.2 face) are skipped with a warning rather than
  emitted, pending a bounded-cut primitive.

See `ROADMAP.md` for direction and the full open-items list.
