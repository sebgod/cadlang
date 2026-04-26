# cadlang — roadmap

Longer-term direction plus open follow-ups across the tooling. One list
so there's exactly one place to check "what's next". Near-term work tied
to a specific project still lives in the `STATE.md` inside that
project's directory.

Conventions:
- **Shipped** — already implemented and exercised.
- **Next up** — queued work, roughly in priority order per section.
- **Not started** — acknowledged but nobody's scoped it yet.

---

## Where we are today

cadlang v0.2 covers:
- `revolve(...)` — axisymmetric bodies.
- `extrude(...)` — prismatic bodies on `XY` or `OffsetPlane('XY', ...)`.
- `cut(...)` on top-face `Rect`s, YZ-offset `Circle`s (radial bores +
  shallow saddle grooves), XY-offset `Circle`s (axial through-holes),
  with optional circular patterns.
- Watertight STL via `manifold3d` CSG + a matplotlib preview
  (ring/flat auto-detect).
- Fusion 360 parametric script with `params` flowing into
  userParameters.
- STEP reader + emitter (`stepimport.py`) producing starter `.cad.py`
  per body. Every detected cylindrical face is emitted faithfully as a
  `cut(Circle)`; narrow-arc coverage is computed from face edge
  vertices and surfaced as metadata but no longer gates emission.
- Assembly layer v1 (`assembly.py`) — YAML with `interfaces:` and
  `mates:`, solved via frame alignment. Per-instance STL output,
  always-on CSG intersection check that distinguishes mated joints
  from modelling errors.
- `cadlang gui` — local web UI (three.js viewer, tree of parts and
  assemblies, rebuild buttons, hot-reload on source edits,
  intersection-highlight coloring).
- Tests (`tests/`, pytest, 34 tests) — Tier 1 importer + mate-solver
  analysis, Tier 1.5 sketch-solver behaviour, Tier 2 geometry pipeline
  (volumes / bbox / watertight).
- `sketch.Sketch` — constraint-based 2D sketches via
  `python-solvespace`. Lines, circles, fix/H/V/parallel/perpendicular/
  coincident/equal/tangent geometric constraints, distance/length/
  angle/diameter/radius dimensional constraints (expressions reference
  `Design.params`). `Design.extrude(sketch=…)` wires it through to
  manifold3d for STL and to native Fusion sketch + parametric
  `SketchDimensions` for the Fusion script.

---

## Core DSL (`cadlang.py`)

**Next up:**
- Fillets + chamfers as first-class `fillet(...)` / `chamfer(...)` ops.
  Today they're baked into the revolve profile.
- `hole(...)` sugar on `Design` wrapping the common radial-bore cut.
- Non-`XZ` revolve planes and non-`Z` revolve axes.
- `cut(OffsetPlane('XZ'), …)` branch in the STL backend (Fusion
  already handles it via `_PLANE_ATTR`).
- Bounded lateral cut primitive — either a polyline/arc lateral sketch
  or a `cut` that accepts a z-range clamp. Blocks faithful emission of
  over-carving saddle patches from the importer (see below).
- True CSG merge in assembly output — right now the combined STL is
  `trimesh.util.concatenate`-d; shells stay distinct. Swap to a
  manifold3d union once the bodies are known-watertight.

---

## Sketches (`sketch.py`)

**Shipped (v1):**
- `Sketch` recipe: `Point`, `Line`, `Circle`, plus `rectangle()` /
  `polygon()` sugar.
- Geometric constraints: `fix`, `horizontal`, `vertical`, `parallel`,
  `perpendicular`, `coincident`, `equal`, `tangent`.
- Dimensional constraints: `distance`, `length`, `angle`, `diameter`,
  `radius`. Values can be numbers or expression strings against
  `Design.params`.
- Closed-loop auto-detection: a single closed line chain becomes the
  CCW profile; non-closed/disjoint sketches return an empty profile
  (still useful for constraint exploration).
- `Design.extrude(sketch=…)` wired through both STL and Fusion
  backends. Fusion emits native sketch + geometric/dimensional
  constraints with `SketchDimension.parameter.expression` set to the
  cadlang expression — so dimensions track `userParameters` and the
  sketch re-solves natively in Fusion.
- Re-entrant `Sketch.solve(params)` — the same recipe can be solved
  multiple times with different param sets without rebuild.
- Tier 1.5 tests: solver behaviour (over-/under-constrained,
  perpendicular angle assertion, exact-corner solve).

**Next up:**
- `cut(sketch=Sketch(…))` — wire the sketch path into the cut backend
  (currently still uses `Rect`/`Circle` primitives only).
- Arcs (`Sketch.arc(...)`), tangent-arc constraints, `slot()` sugar.
  Unblocks fillets-via-sketch and the saddle/lateral cuts the importer
  detects.
- Multiple loops in one sketch — outer + inner (holes), or several
  disjoint outers as multi-profile extrudes. Profile detector returns
  a list, manifold3d already supports holes via `CrossSection`.
- Sketches on non-`XY` workplanes (`'XZ'`, `'YZ'`, and arbitrary
  offset-and-rotated planes for sketches on body faces).
- Surface a friendlier DOF / over-constrained report — solvespace
  returns a raw failure-handle list; map back to constraint objects
  with names. Today we expose the raw indices on `SketchSolveError`.
- STEP importer: optionally emit `Sketch(...)` recipes instead of
  point lists. Bigger generated `.g.cad.py` but easier to hand-tune
  the importer's first-pass shape.
- Migrate hand-written parts in `example-project/` to sketches once
  the cut path lands (the ring's heat-insert bores are the natural
  candidate).

**Not started:**
- 3D constraint problems (e.g. expressing assembly mates as 3D
  constraints rather than the current frame-alignment solver).
  Out of scope for v1; the sketch API is intentionally 2D-only.

---

## STEP importer (`stepimport.py`)

**Shipped:**
- AP203/AP214 text parser (records + arg trees, no CAD kernel).
- Per-brep walk: classifies each `MANIFOLD_SOLID_BREP` as revolve or
  extrude.
- Revolve body: extracts ID, OD, height, radial bore groups (incl.
  circular-pattern count via opposite-pair heuristic).
- Extrude body: Z-level clustering of VERTEX_POINTs → stacked layers
  (bbox rectangles per layer). Detects axial through-holes and
  lateral-axis (saddle) cylinders.
- Trim-aware saddle classification — each cylindrical face's angular
  coverage is measured from edge-loop vertex points and attached to the
  cut group as `arc_coverage_deg` / `narrow_arc` metadata.  Emission is
  unconditional: every detected cylindrical face becomes a
  `cut(Circle)` call so the output stays faithful to the STEP.
- Hole depth from `CIRCLE` trim edges, fallback projecting edge
  vertex points onto the cylinder axis when trims are B-splines.

**Next up:**
- **z-level detector: ignore cylindrical-face trim vertices.** The
  extrude classifier clusters every VERTEX_POINT in the brep by z to
  find layer boundaries. But a saddle cylinder's trim arc has
  endpoints at z values that are NOT planar-face corners — e.g. the
  SWQ8 rail's sad1/sad2 cylinders contribute vertices at z≈11.22 that
  the detector mis-reads as a third layer, creating a spurious "L2"
  slab. It currently works out because sad1 then carves L2 away, but
  only by accident. Fix: when collecting vertices for z-clustering,
  reject those that only bound cylindrical / other non-planar faces.
- Non-rectangular extrude-layer outlines — every slab is currently a
  bbox rectangle. Walk face → loop → edge → vertex chains to extract
  actual polygon outlines.
- Tab bosses / outer-proud pads on revolves — planar features, not
  cylinders, so the revolve path misses them (the SWQ8 ring's 4 tabs
  at 0/90/180/270° currently come through as a uniform annulus).
- Top-face slots on imports — cadlang has `cut(d.top_face, Rect,
  Circular)`, the importer just doesn't detect sector slots.
- Fillet / chamfer detection at import time. Usually lost at the
  boundary; leave a TODO comment in the emitted `.g.cad.py`.
- Face-normal orientation check (`ORIENTED_FACE` / `FACE_SURFACE`
  direction flags) so lateral cylinders can be classified as concave
  cuts vs convex kept surfaces. Today we always assume "lateral
  cylinder ⇒ cut".
- Derive the cut's depth directly from the trim's angular span rather
  than inferring from cylinder+body geometry — makes the importer
  independent of the heuristic 20% over-carve threshold.
- Hole-depth fallback for B-spline trims can give odd depths; switch
  to a face-boundary walk that rejects spline-only trims.
- Nested `SHAPE_REPRESENTATION` transforms — the importer assumes one
  global frame, fine for single-body Fusion exports but not for
  multi-level assemblies.

---

## STL importer — not started

STL has no feature info; primitives have to be fit. Principal-axis
detection, RANSAC cylinder fits, plane fits, angular-sector anomaly
detection for slots and bosses. Output the same `.g.cad.py` shape as
the STEP path, with per-feature confidence annotations. Harder and
less critical than the STEP path — only worth doing if a part arrives
without a STEP.

---

## Assembly layer (`assembly.py`)

**Shipped:**
- YAML with `interfaces:` (bolt_pair, tabbed_hole_stack) + `mates:`,
  solved via frame alignment. Legacy explicit `pose:` / `circular:` is
  still supported.
- Bolt-axis convention: `axis` = bolt head→tip, body on the head
  side. Flip the sign to flip which side of the mating plane the body
  ends up on.
- Per-instance STL output under `<name>_parts/<slot>_<n>.stl` alongside
  the combined STL.
- Always-on CSG intersection check between placed instances (AABB
  pre-filter, pair-wise manifold3d). 1500 mm³ threshold for mated pairs
  — swallows the expected threaded-insert overlap at a bolted joint.
  1 mm³ for non-mated pairs — any more is a modelling error. Writes
  `<name>_intersections.json`; GUI reads it and paints offending
  instances red.

**Next up:**
- `constraints:` block for dimension propagation — push a single
  source-of-truth value (e.g. `rail_width`) into the `params` of every
  referenced part before it runs.
- More interface kinds: `push_fit_inner` / `push_fit_outer`,
  `flat_face`, `tube_od` / `tube_id`, `bolt_circle`.
- Fusion assembly script emitter — one `<name>_fusion.py` per assembly
  creating components + rigid joints, so the whole thing opens in
  Fusion at once.
- Interface definitions embedded in parts — today they live in the
  assembly YAML; a sidecar `<name>.interfaces.yml` or an in-`.cad.py`
  API would let each part carry its own mating contract.
- Cycle detection in the mate graph (today: rejected when progress
  stalls, but the error message could be clearer).
- Better error messages generally when a mate fails to resolve.

### Target shape for v2 (assembly constraints)

```yaml
name: <assembly>
units: mm

parts:
  <slot>:
    source: parts/<name>.cad.py
    count: 4

interfaces:
  <part_type>:
    <iface>:
      kind: rail_tenon      # richer kinds from the table below
      width:       <expr>
      thickness:   <expr>
      hole_pattern: [{z: 0}, {z: hole_spacing}]

mates:
  - parts: [<from>, <to>]
    via:   [<from>.<iface>, <to>.<iface>]

constraints:                # push dims into parts at assembly time
  rail_width: 20.0
  some_part.notch_arc: "{rail_width} + 0.4"
```

**Starter library of interface kinds**:

| kind             | meaning                                                    |
|------------------|------------------------------------------------------------|
| `rail_tenon`     | rectangular protrusion, mates with `rail_mortise`          |
| `rail_mortise`   | rectangular slot, accepts `rail_tenon` with clearance      |
| `tube_od`        | cylindrical OD at a named face                             |
| `tube_id`        | cylindrical ID at a named face (clamps onto `tube_od`)     |
| `push_fit_outer` | cylindrical surface for press/slide fit + clearance spec   |
| `push_fit_inner` | mating ID                                                  |
| `bolt_circle`    | N holes in circular pattern (diameter, PCD, count)         |
| `flat_face`      | flat surface for gluing / mating planes                    |

Each kind is a schema. A mate validates compatibility and pushes
dimensions from the authoritative side into the mating side's params.

**Rules of thumb:**
- Source of truth per dimension: the tenon defines the nominal; the
  mortise adds clearance. If both specify the same dimension, error.
- Reject cyclic mate graphs at first; solve iteratively only if a
  real case needs it.
- Use `trimesh.transformations` for 4×4 composition; don't roll your
  own linear algebra.
- Cache meshes by parameter hash; assembly regenerations add up fast.

Implement the handful of interface kinds the current real project
needs (see `STATE.md`). Add more as new projects introduce them.

---

## Local GUI (`gui.py` + `cadlang gui`)

**Shipped:**
- stdlib HTTP server (default port 8765), three.js viewer via ESM
  CDN, tree of parts + assemblies.
- Camera toolbar (Fit / Iso / Top / Front / Right), Fusion-style MMB
  pan, Z-up grid.
- Rebuild buttons that run the same subprocesses the CLI would; log
  panel with captured stdout/stderr.
- Source tab showing the selected item's `.cad.py` or YAML (raw).
- Hot-reload watcher on `gui.py`, `cadlang.py`, `assembly.py`,
  `stepimport.py` — the browser tab survives restarts because the
  port is stable.
- Per-instance assembly loading with red coloring for instances that
  appear in the intersections sidecar.

**Next up:**
- **Referenced-import viewer** — render the `.step` files that
  `project.cadlang` pulls in, so you can visually diff against
  cadlang's emitted STL. Two approaches worth comparing:
  1. WASM in the browser (`occt-import-js`, OpenCascade compiled to
     wasm, ~4 MB once-cached). Fetch the `.step` via `/files/`, pass
     the ArrayBuffer to the loader, add the returned mesh to the
     scene in a distinct tint. Zero server-side Python deps.
  2. Server-side tessellation via `cadquery-ocp` / `build123d` /
     FreeCAD CLI, converting STEP → STL at build time. Heavier
     install but faster first render.
  Start with (1) since the GUI philosophy is "stdlib server + ESM
  CDN for client libs, no npm".
- SSE-streamed rebuild output so 30+ second assembly builds don't
  block the UI.
- File watcher for auto-rebuild on `.cad.py` edits (not just server
  auto-restart).
- In-browser `.cad.py` editor (CodeMirror?), so the edit → rebuild
  → view loop stays in one window.

---

## Project CLI (`cadlang build`)

**Next up:**
- Watch mode (rerun on `.cad.py` / `.yml` changes).
- Validation step: every part in an assembly must have an anchor or a
  reachable mate chain.
- Step-level caching: skip rebuilds when source files haven't changed.

---

## Preview renderer

**Next up:**
- matplotlib `Poly3DCollection` depth-sorting struggles with
  thin/flat parts; hole cutouts viewed straight-on can render with
  their wall triangles overdrawing the face. Currently mitigated by
  auto-swapping to three iso views for flat parts. Proper fix: swap
  to a real 3D backend (trimesh's pyglet scene, or a tiny raytracer).
- Assembly preview doesn't adapt to bbox shape the way the per-part
  preview does.

---

## Documentation / housekeeping

- `CLAUDE.md` feature coverage table grows with every new op; might
  auto-generate it from the backend code someday.
- `README.md` and `CLAUDE.md` overlap in places; they split roughly as
  "user-facing short" vs "contributor/agent long" — keep that split,
  deduplicate anything that drifts.

---

## Suggested order of attack

1. **Whatever single-part work the current project needs** — see
   `STATE.md` inside the project directory.
2. **Bounded-cut primitive** — unblocks faithful emission of the
   over-carving lateral cylinders currently skipped by the importer.
3. **Assembly `constraints:`** — the next lever for keeping a real
   project's dimensions single-sourced.
4. **Referenced-import viewer** — quality-of-life for visually
   diffing cadlang output against its STEP source.
5. **Importer polish** (non-rect outlines, tab bosses, fillets) — as
   real imports demand it.
6. **STL importer** — only if a part arrives STEP-less.

Each step stays useful even if the next never happens.
