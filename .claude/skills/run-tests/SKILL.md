---
name: run-tests
description: Run the cadlang test suite with `pytest tests/` — Tier 1 importer analysis (fast, pure-parse) and Tier 2 geometry pipeline (CSG, ~500 ms). Use when the user asks to run tests, verify a refactor, or check that a change to `stepimport.py` / `cadlang.py` hasn't regressed a known-good output.
---

# Run the cadlang test suite

Two tiers, both under `tests/`:

- **Tier 1** (`tests/test_stepimport.py`) — parses the SWQ8 STEP fixture once and asserts on the structured body dict from `infer_bodies`. No CSG, no cadlang execution. Each test is ~1 ms.
- **Tier 2** (`tests/test_geometry.py`) — loads each `.cad.py` via `importlib`, builds the mesh via `cadlang._build_mesh(..., n_seg=60)`, and asserts watertight + volume + bbox. Runs in ~500 ms total.

## Run everything

```bash
pytest tests/ -v
```

## Run just one tier

```bash
pytest tests/test_stepimport.py -v     # Tier 1: importer analysis
pytest tests/test_geometry.py -v       # Tier 2: geometry pipeline
```

## Run a single test by name

```bash
pytest tests/ -v -k lateral_cylinders   # regression guard for the sad1 over-carve bug
pytest tests/ -v -k volume              # the two hand-calc volume checks
```

## If pytest isn't installed

```bash
pip install .[dev]        # installs cadlang + pytest
# or
pip install pytest
```

CI already runs `pytest tests/ -v` via `.github/workflows/ci.yml` after `pip install .[dev]` on Python 3.10/3.11/3.12 against Ubuntu.

## Ground-truth rule (don't break this)

Every assertion must compare against a value derived **independently** of cadlang itself:
- Physical measurement (e.g. `STATE.md §2` — ring ID/OD/height, rail bolt y).
- Hand-calculation from declared `params` (e.g. rail vol = L1 + L2 − bolt holes = 23.31 cm³).
- A value read once from the STEP text by eye (e.g. lateral cylinder radii 125.2 / 120.0).

**Never** assert against cadlang's previously-emitted STL, Fusion script, or importer output — that's circular and catches nothing. If you add a test, put the ground-truth source in a comment above the expected value.

## Known guards worth preserving

- `test_rail_lateral_cylinders_are_both_shallow_saddles` — fires if the trim-arc coverage math in `_saddle_cut_groups` drifts. Both rail cylinders currently report ~9° coverage.
- `test_rail_volume_matches_hand_calc` — fires if the rail volume drops back to ~13.4 cm³ (the sad1 over-carve regression). Failure message points directly at `_saddle_cut_groups`.
- `test_overlap_ring_volume_matches_hand_calc` — 154.8 cm³ revolve volume, catches regressions in the revolve path or cut path independently of the importer.
