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

Pulls in `numpy`, `trimesh`, `matplotlib`, and `manifold3d` (the CSG kernel).

## Folder layout

```
cadlang/
├── cadlang.py          # Design + STL/Fusion backends + `cadlang build` CLI
├── stepimport.py       # STEP reader + feature recogniser + .cad.py emitter
├── assembly.py         # YAML assembly loader + mate solver + combined STL
├── CLAUDE.md           # detailed pipeline + DSL reference (generic)
├── ROADMAP.md          # longer-term direction
├── FOLLOW_UP.md        # open questions / TODOs
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
  bodies
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
- Assembly layer (mating-aware description referencing multiple parts) is
  the next big thing — see `ROADMAP.md §4`.
