"""Tier 1: STEP importer analysis tests.

Exercise `stepimport` against the SWQ8 dewshield STEP fixture and assert
on the structured body dict. No STL, no CSG, no cadlang execution — each
test is a few ms. Ground truth is physical measurement + hand-read STEP
values, NEVER cadlang's own emitted output (that would be circular).
"""
from __future__ import annotations

import math

import pytest


# =========================================================================
# Ground-truth values. Sourced per STATE.md §2 (physical measurements)
# or by reading the STEP text directly.
# =========================================================================

# Ring ("end ring", brep classified as revolve):
RING_ID_MM           = 240.00
RING_OD_MM           = 250.40
RING_H_MM            =  30.00
RING_HOLE_Z_MM       = [5.0, 11.667, 18.333, 25.0]   # 4 per angular tab
RING_HOLE_DIA_MM     =   4.30                         # r=2.15 in the STEP
RING_TAB_COUNT       =   4                            # 0°/90°/180°/270°

# Support rail (brep classified as extrude):
RAIL_Z_LEVELS_MM     = [0.0, 5.6, 11.2]
RAIL_BOLT_DIA_MM     =   4.30
RAIL_BOLT_Y_MM       = -155.20
RAIL_BOLT_COUNT      =   4

# From reading the STEP: two lateral X-axis cylinders at (y=-155.2, z=130.8),
# radii 125.2 and 120.0. Each is a concave surface patch, not a through-cut
# — their face arcs should cover only a few degrees.
RAIL_LATERAL_SHALLOW_COVERAGE_MAX_DEG = 15.0   # strict: < 15° per group


# =========================================================================
# Smoke / top-level classification
# =========================================================================

def test_parse_db_non_empty(swq8_db):
    assert len(swq8_db) > 100, 'parsed STEP looks empty'
    # At least one solid brep and many advanced faces
    types = [t for (t, _) in swq8_db.values()]
    assert types.count('MANIFOLD_SOLID_BREP') >= 1
    assert types.count('ADVANCED_FACE') > 10


def test_infer_bodies_yields_one_revolve_and_one_extrude(swq8_bodies):
    kinds = sorted(b['kind'] for b in swq8_bodies)
    assert kinds == ['extrude', 'revolve'], \
        f'classifier drifted — got {kinds}; expected one of each'


# =========================================================================
# Ring (revolve) analysis
# =========================================================================

def test_ring_diameters_match_physical_measurement(swq8_ring):
    assert swq8_ring['ring_id'] == pytest.approx(RING_ID_MM, abs=0.05)
    assert swq8_ring['ring_od'] == pytest.approx(RING_OD_MM, abs=0.05)


def test_ring_height_matches_physical_measurement(swq8_ring):
    assert swq8_ring['ring_h'] == pytest.approx(RING_H_MM, abs=0.05)


def test_ring_has_bolt_hole_pattern(swq8_ring):
    groups = swq8_ring['hole_groups']
    # Bolt inserts at 4 tabs × 4 holes = 16 total; may cluster into 1 group
    # by radius (all 4.3 mm dia). The cluster should contain enough holes to
    # cover 4 tabs × 4 z-positions.
    bolt_groups = [g for g in groups
                   if abs(2 * g['radius_mm'] - RING_HOLE_DIA_MM) < 0.05]
    assert bolt_groups, 'no bolt-hole group found with the expected diameter'
    total = sum(g['raw_count'] for g in bolt_groups)
    assert total >= RING_TAB_COUNT * len(RING_HOLE_Z_MM), \
        f'bolt hole count {total} < 4 tabs × {len(RING_HOLE_Z_MM)} z-positions'


def test_ring_bolt_hole_z_positions_match_measurement(swq8_ring):
    # The importer clusters by (radius, angular position); collapse and
    # pull every z it detected, dedupe within tolerance, compare.
    zs = set()
    for g in swq8_ring['hole_groups']:
        if abs(2 * g['radius_mm'] - RING_HOLE_DIA_MM) > 0.05:
            continue
        # each group exposes a list of z-positions for its holes
        zs.update(round(z, 3) for z in g.get('z_positions', []))
    assert zs, 'no bolt-hole z positions exposed'

    def near(target, candidates, tol):
        return any(abs(target - c) < tol for c in candidates)

    missing = [z for z in RING_HOLE_Z_MM if not near(z, zs, 0.1)]
    assert not missing, f'expected hole z-positions missing from import: {missing}'


# =========================================================================
# Rail (extrude) analysis
# =========================================================================

def test_rail_has_expected_z_levels(swq8_rail):
    # _infer_extrude_body emits layers between consecutive z-levels; the set
    # of (z_base, z_base + thickness) should cover RAIL_Z_LEVELS_MM.
    levels = set()
    for L in swq8_rail['layers']:
        levels.add(round(L['z_base'], 3))
        levels.add(round(L['z_base'] + L['thickness'], 3))
    for expected in RAIL_Z_LEVELS_MM:
        assert any(abs(expected - z) < 0.1 for z in levels), \
            f'z level {expected} missing from detected {sorted(levels)}'


def test_rail_axial_bolt_holes(swq8_rail):
    groups = swq8_rail.get('hole_groups_axial', [])
    bolt_groups = [g for g in groups
                   if abs(2 * g['radius_mm'] - RAIL_BOLT_DIA_MM) < 0.05]
    assert bolt_groups, 'no bolt-hole group with the expected dia'
    total = sum(g['raw_count'] for g in bolt_groups)
    assert total >= RAIL_BOLT_COUNT, \
        f'expected >= {RAIL_BOLT_COUNT} bolt holes, got {total}'

    # Every detected bolt hole should sit at the rail's measured y row.
    ys = []
    for g in bolt_groups:
        for (_hx, hy) in g['positions_xy']:
            ys.append(hy)
    assert ys, 'bolt groups have no positions_xy entries'
    assert all(abs(y - RAIL_BOLT_Y_MM) < 0.1 for y in ys), \
        f'bolt holes drifted off y={RAIL_BOLT_Y_MM}: ys={ys}'


# =========================================================================
# Rail lateral cuts — the regression guard for the sad1/sad2 over-carve bug
# =========================================================================

def test_rail_lateral_cylinders_are_narrow_arc_faces(swq8_rail):
    """Regression guard. Earlier the importer emitted sad1 (r=125.2) and sad2
    (r=120) as through-cuts, which carved away nearly all of L2 and
    collapsed the rail's step. The fix reads trim-arc coverage per face;
    both cylinders span only a few degrees (narrow surface patch).

    What happens per patch depends on the would-be carve depth, and is
    decided at emit time — see test_rail_only_over_carving_saddle_is_skipped.
    """
    lats = swq8_rail['lateral_cuts']
    assert lats, 'no lateral cuts detected — either STEP changed or classifier ' \
                 'regressed (the rail definitely has 2 lateral cylinders)'

    for i, lc in enumerate(lats):
        cov = lc.get('arc_coverage_deg')
        assert cov is not None, f'lateral cut #{i}: arc_coverage_deg not populated'
        assert cov < RAIL_LATERAL_SHALLOW_COVERAGE_MAX_DEG, (
            f'lateral cut #{i} covers {cov:.1f}° — expected < '
            f'{RAIL_LATERAL_SHALLOW_COVERAGE_MAX_DEG}° (narrow-arc surface patch). '
            'If this fires, the arc-coverage computation or the 60° '
            'classification threshold in `_saddle_cut_groups` has drifted.'
        )
        assert lc['narrow_arc'] is True, \
            f'lateral cut #{i} at cov={cov:.1f}° should have narrow_arc=True'


def test_rail_saddles_are_emitted_in_per_layer_order(swq8_rail):
    """Emitter-level guard. The per-layer tall-extrude + immediate-saddle
    ordering is what preserves both the step AND the concave tops:

        extrude L1 (tall)
        cut saddle (r=125.2, matches L1's x-extent)   ← carves L1's top
        extrude L2 (tall)
        cut saddle_2 (r=120, matches L2's x-extent)   ← carves L2's top
        cut bolt_holes

    If someone flattens this to `all-extrudes then all-cuts` again,
    sad1 destroys L2 and the rail collapses to a flat bar at ~13 cm^3.
    This test locks in the interleaved ordering.
    """
    import stepimport
    src = stepimport.emit_cadpy(swq8_rail, source_step='SWQ8DewShieldHolder.step')
    # Both saddles must be emitted as cuts.
    assert "name='saddle'" in src, 'sad1 must be emitted'
    assert "name='saddle_2'" in src, 'sad2 must be emitted'
    # 3 cuts total: 2 saddles + 1 bolt-holes group.
    assert src.count('d.cut(') == 3, \
        f'expected 3 d.cut(...) calls (2 saddles + bolt_holes); got {src.count("d.cut(")}'
    # Ordering: L1 extrude → sad1 → L2 extrude → sad2 → bolt_holes.
    idx_base   = src.index("name='base'")
    idx_sad1   = src.index("name='saddle'")
    idx_layer2 = src.index("name='layer_2'")
    idx_sad2   = src.index("name='saddle_2'")
    idx_bolts  = src.index("name='bolt_holes'")
    assert idx_base < idx_sad1 < idx_layer2 < idx_sad2 < idx_bolts, (
        f'emit order drifted: base={idx_base} sad1={idx_sad1} '
        f'layer_2={idx_layer2} sad2={idx_sad2} bolts={idx_bolts}'
    )


def test_rail_lateral_cut_radii_match_step(swq8_rail):
    """These are what the STEP file encodes — r=125.2 (matches ring OD)
    and r=120.0 — not derived by us. If these drift, the cylinder reader
    is misparsing radii."""
    radii = sorted(round(lc['radius_mm'], 2) for lc in swq8_rail['lateral_cuts'])
    assert radii == [120.00, 125.20], f'lateral cut radii drifted: {radii}'
