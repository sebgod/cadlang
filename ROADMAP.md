# cadlang — roadmap

Longer-term direction for the DSL. Near-term tasks (tied to whatever
project is currently using cadlang) live in `STATE.md`.

This file is for:
- Sketching out the shape of bigger features before implementing them.
- Giving a future Claude session enough context to pick up the design
  direction.

---

## Where we are today

cadlang v0.2 covers:
- `revolve(...)` — axisymmetric bodies
- `extrude(...)` — prismatic bodies on `XY` or `OffsetPlane('XY', ...)`
- `cut(...)` on top-face `Rect`s, YZ-offset `Circle`s (radial bores +
  saddle spans), XY-offset `Circle`s (axial through-holes), with optional
  circular patterns
- Watertight STL via `manifold3d` CSG + a matplotlib preview (ring/flat
  auto-detect)
- Fusion 360 parametric script with `params` flowing into userParameters
- STEP reader + emitter (`stepimport.py`) producing starter `.cad.py` per
  body, with hole depth measured from trim geometry and lateral saddle
  cuts auto-detected

It is solid for single parts. The remaining gaps:

1. **Finer feature detection** in the importer — non-rectangular outlines,
   bosses on revolves, fillets — §3.
2. **Assembly description** — a declarative file that references multiple
   `.cad.py` parts and captures how they fit. Without it, shared
   dimensions get duplicated across parts and drift silently (§4).

---

## 2. Real CSG — ✅ done

Landed on x64 with `manifold3d`. `_build_mesh` uses `Manifold.revolve`,
`Manifold.extrude`, `Manifold.cylinder`, and boolean `+`/`-` for union/
difference. No hand-rolled mesh stitching.

Follow-ups still open:
- `hole(...)` sugar method on `Design` that wraps the common radial-bore
  cut pattern (nice-to-have, not blocking anything).
- Fillets and chamfers as proper ops rather than baked into revolve
  profiles.

---

## 3. Importer: STL / STEP → cadlang

Goal: point it at an existing CAD file and get a starter `.cad.py` that
describes the same geometry in cadlang.

### STEP → cadlang — partially done

Shipped in `stepimport.py`:
- AP203/AP214 text parser (records + arg trees, no CAD kernel).
- Per-brep walk: classifies each `MANIFOLD_SOLID_BREP` as revolve or
  extrude.
- Revolve body: extracts ID, OD, height, radial bore groups (incl.
  circular-pattern count via opposite-pair heuristic).
- Extrude body: Z-level clustering of VERTEX_POINTs → stacked layers (bbox
  rectangles per layer). Detects axial through-holes and lateral-axis
  (saddle) cylinders, emits them as `cut(...)` calls.
- Hole depth: measured from `CIRCLE` trim edges, with a fallback that
  projects edge VERTEX_POINTs onto the cylinder axis when trims are
  splines.
- Emits one `<src>_partNNN.g.cad.py` per body (the `.g.` infix distinguishes
  auto-generated sources from hand-written `.cad.py` files).

Still missing:
- Non-rectangular layer outlines — extrude layers are bbox'd. Extract
  real polygon outlines by walking face → loop → edge → vertex chains.
- Bosses / outer-proud pads on revolves — planar features, not cylinders,
  so the current revolve path misses them.
- Top-face slots on imports — cadlang's `cut(d.top_face, Rect, Circular)`
  exists but the importer doesn't detect sector slots.
- Fillets / chamfers at import time — usually lost; leave a TODO comment
  in the generated file.
- Nested SHAPE_REPRESENTATION transforms — assumes a single global frame.

### STL → cadlang — not started

STL has no feature info; primitives must be fit. Principal-axis detection,
RANSAC cylinder fits, plane fits, angular-sector anomaly detection for
slots and bosses. Output the same `.cad.py` shape as the STEP path with
confidence annotations. Harder and less critical than the STEP path.

### CLI

```
python stepimport.py thing.step                  # report to stdout
python stepimport.py thing.step -o parts/        # emit per-body .cad.py
python stepimport.py thing.step -o parts/x.cad.py  # single-body file
```

---

## 4. Assembly description — v1 shipped

v1 is in: `assembly.py` parses YAML assemblies, resolves per-instance
transforms via interface-based mates (bolt_pair ↔ tabbed_hole_stack), and
emits a combined STL + preview. Dimensions for the dew-shield test case
(`assemblies/dew_shield.yml`) come entirely from measured interface
geometry — no hand-computed pose numbers in the YAML. Explicit `pose:` and
`circular:` are still supported for parts without interfaces.

Remaining gaps in the assembly layer:

- **Dimension propagation / constraints** — YAML has no `constraints:` block
  yet. Changing a rail bolt position in `_SWQ8_part002_.g.cad.py`'s params
  will break the mate if the interface definition isn't kept in sync. A
  `constraints:` block that injects values into each part's `params` at
  assembly time would close this.
- **More interface kinds** — `push_fit_inner`/`push_fit_outer`, `flat_face`,
  `tube_od`/`tube_id`. Add per project as they're needed.
- **Fusion assembly script** — combined STL only; no `<name>_fusion.py` yet
  that creates components + rigid joints in Fusion.
- **Interface definitions embedded in parts** — currently interfaces live
  in the assembly YAML. Future: allow a sidecar `<name>.interfaces.yml` or
  inline in `.cad.py` so each part carries its own mating contract.
- **True boolean union** — parts are `trimesh.util.concatenate`-d, not
  CSG-merged. Fine for visualisation; a proper merge would use manifold3d.

The paragraphs below describe the shape of the full v2+ solution.

### Shape of the solution

Describe an assembly as a separate declarative file — likely **YAML**,
since it's a pure description (no code execution). The assembly file
references existing `.cad.py` parts and records:

- which parts participate (and in what multiplicity / pattern)
- which **interfaces** each part exposes (named mating features)
- which pairs of interfaces are **mated** to each other
- which **dimensions** are authoritative and flow downstream from their
  source

### Sketch of the format

```yaml
name: <assembly-name>
units: mm

parts:
  <slot_name>:
    source: parts/<somepart>.cad.py
    count: 4                      # optional — for patterned instances
    pattern: {type: circular, axis: Z, count: 4}

interfaces:
  <part_slot>:
    <iface_name>:
      kind: rail_tenon            # one of a starter library of kinds
      width:       <expr>
      thickness:   <expr>
      hole_pattern: [{z: 0}, {z: hole_spacing}]

mates:
  - parts: [<from>, <to>]
    via:   [<from>.<iface>, <to>.<iface>]

# Dimensions with a single source of truth.
constraints:
  rail_width: 20.0
  some_part.notch_arc: "{rail_width} + 0.4"
```

### Why a separate YAML instead of extending the Python DSL

- Clean separation: part files stay focused on the geometry of one thing;
  assembly concerns (how things relate) live in their own layer.
- Easy to diff and review — a pure description, no imports or execution
  context.
- Re-use: the same `.cad.py` can participate in multiple assemblies with
  different mate contexts.
- Tooling: `cadlang assembly <file.yml>` can validate mate compatibility,
  push dimensions into the referenced part files' params, render a combined
  STL, and emit a Fusion assembly script — without having to load and union
  parts manually.

### Interface types — starter library

| kind            | meaning                                                            |
|-----------------|--------------------------------------------------------------------|
| `rail_tenon`    | rectangular protrusion, mates with `rail_mortise`                  |
| `rail_mortise`  | rectangular slot, accepts `rail_tenon` with specified clearance    |
| `tube_od`       | cylindrical OD at a named face                                     |
| `tube_id`       | cylindrical ID at a named face (clamps onto `tube_od`)             |
| `push_fit_outer`| cylindrical surface for press/slide fit (with clearance spec)      |
| `push_fit_inner`| mating ID                                                          |
| `bolt_circle`   | N holes in circular pattern (diameter, PCD, count)                 |
| `flat_face`     | flat surface for gluing / mating planes                            |

Each kind is a schema — a set of expected fields plus a compatibility rule
with its counterpart kind. A mate validates compatibility and pushes
dimensions from the authoritative side into the mating side's params.

### Validation + emission pipeline

```
<assembly>.assembly.yml
        │
        ├── load referenced .cad.py files (don't run them yet)
        ├── read interface definitions (either in YAML or in part files)
        ├── resolve mates
        │     • fit check: clearance, compat kinds, hole-pattern agreement
        │     • push dimensions: mate-driven params overwrite part params
        │     • cycle check: reject cyclic dimension flows (start strict)
        ├── per part: emit .cad.py → STL + Fusion with driven params
        └── combined outputs:
              • <assembly>.stl       — all parts in assembly pose, one mesh
              • <assembly>_fusion.py — components + joints in Fusion
```

### Rules of thumb

- **Source of truth per dimension**: the tenon defines the nominal; the
  mortise adds clearance. If both try to specify the same dimension, it's
  an error.
- **No cycles at first**: reject mate graphs that propagate constraints
  cyclically. Solve iteratively only if a real case needs it.
- **Coordinate frames**: use `trimesh.transformations` for 4×4 matrix
  composition; do not roll your own linear algebra.
- **Cache meshes** by parameter hash — assembly regenerations add up fast.

### Scope for v1 of the assembly layer

Pick the two or three interface kinds that the current real project needs
(see `STATE.md`) and implement those end-to-end: schema, mate validation,
dimension push. Add more kinds as new projects introduce them. Don't try
to cover every shape up front.

---

## 5. Suggested order of attack

1. **Whatever single-part work the current project needs** — see
   `STATE.md`.
2. **Assembly format** (§4) — as soon as there are two parts that share a
   dimension, the assembly layer pays for itself. Start with the real
   project as the first test case.
3. **Importer polish** (§3 still-missing list) — as needed per actual
   imports.
4. **STL importer** — only if a part comes through without a STEP.

Each step stays useful even if the next never happens.
