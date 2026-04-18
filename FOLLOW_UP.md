# cadlang — open questions & follow-ups

Collected from ROADMAP, CLAUDE.md, in-file TODOs, and session notes. Not
strictly ordered — pick what's blocking whatever project is currently active.

## Core DSL (`cadlang.py`)

- Non-`XZ` revolve planes and non-`Z` revolve axes
- Fillets + chamfers as first-class feature ops (today you have to bake them
  into the revolve profile)
- `hole(...)` sugar on `Design` wrapping the common radial-bore cut
- `cut(OffsetPlane('XZ'), …)` branch in the STL backend (Fusion already
  handles it via `_PLANE_ATTR`)

## STEP importer (`stepimport.py`)

- Non-rectangular extrude-layer outlines — every slab is currently a bbox
  rectangle. Walk face → loop → edge → vertex chains to extract actual
  polygon outlines.
- Tab bosses / outer-proud pads on revolves — they're planar features, not
  cylindrical surfaces, so the revolve path misses them. STATE-dependent
  parts (e.g. the SWQ8 ring's 4 tabs at 0/90/180/270°) come through as
  uniform annuli.
- Top-face slots on imports — cadlang has the op (`cut(d.top_face, Rect,
  Circular)`), but the importer doesn't detect sector slots.
- Fillet / chamfer detection at import time. Usually lost at the boundary;
  leave a TODO comment in the generated `.g.cad.py`.
- Nested `SHAPE_REPRESENTATION` transforms — currently assumes one global
  frame, which is true for typical single-body Fusion exports but not for
  multi-level assemblies.
- Hole-depth fallback can give odd depths when trim curves are B-splines.
  Switch to a proper face-boundary walk that rejects spline-only trims.

## STL importer — not started

- Principal-axis detection, RANSAC cylinder fits, plane fits, angular-sector
  anomaly detection for slots / bosses.
- Same `.g.cad.py` output shape as the STEP path, with per-feature
  confidence annotations.

## Assembly layer (`assembly.py`)

- `constraints:` block in the assembly YAML for dimension propagation —
  push a single source-of-truth value (e.g. `rail_width`) into the `params`
  of every referenced part before it runs.
- More interface kinds: `push_fit_inner` / `push_fit_outer`, `flat_face`,
  `tube_od` / `tube_id`, `bolt_circle`.
- Fusion assembly script emitter — one `<name>_fusion.py` per assembly
  creating components + rigid joints, so the whole thing opens in Fusion at
  once.
- Interface definitions embedded in parts — today they live in the assembly
  YAML; a sidecar `<name>.interfaces.yml` or an in-`.cad.py` API would let
  each part carry its own mating contract.
- True CSG merge via `manifold3d` — right now parts are
  `trimesh.util.concatenate`-d, so overlapping shells stay distinct. Fine
  for visualisation, not for anything downstream that assumes watertight.
- Cycle detection in the mate graph (today: rejected by running out of
  progress, but the error message could be clearer).
- Better error messages generally when a mate fails to resolve.

## Project CLI (`cadlang build`)

- Watch mode (rerun on `.cad.py` / `.yml` changes).
- Validation step: check that every part in an assembly has an anchor or a
  reachable mate chain.
- Step-level caching: skip rebuilds when source files haven't changed.

## Preview renderer

- matplotlib `Poly3DCollection` depth-sorting struggles with thin/flat
  parts — hole cutouts viewed straight-on can render with their wall
  triangles overdrawing the face. Currently mitigated by auto-swapping to
  three iso views for flat parts; a proper fix would switch to a real 3D
  backend (trimesh's pyglet scene, or a tiny raytracer).
- Assembly preview doesn't adapt to bbox shape the way the per-part
  preview does.

## Documentation / cleanup

- CLAUDE.md's feature coverage table grows with every new op; might want to
  auto-generate it from the backend code someday.
- `README.md` and `CLAUDE.md` overlap in places; decide on a split (user
  vs. contributor, or short vs. long) and deduplicate.
