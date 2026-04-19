"""Tier 2: end-to-end geometry pipeline tests.

Load a `.cad.py`, build the mesh via `cadlang._build_mesh`, and assert on
its watertightness + volume + bounding box. Ground truth comes from
hand-calculation against the part's declared `params` — never from
cadlang's own previously-recorded output (circular).

Slower than Tier 1 (CSG on manifold3d), so tessellation is dialled down
via `n_seg=60` — still produces a watertight mesh, and reduces volume
precision to roughly 0.5% for 250 mm-scale parts, which is fine at the
tolerances we assert.
"""
from __future__ import annotations
import importlib.util
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
PARTS_DIR = (REPO_ROOT / 'example-project' / 'SWQ8-Dewshield-Holder' / 'parts')

N_SEG = 60   # tessellation — trade precision for speed


def _load_design(cad_py_path: pathlib.Path):
    """Execute a `.cad.py` file as a module and return its top-level `d`
    (the `Design` instance each part exposes). The `if __name__ ==
    '__main__'` guard in each part prevents STL/Fusion emission on import,
    so this doesn't touch disk.
    """
    spec = importlib.util.spec_from_file_location(cad_py_path.stem, cad_py_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert hasattr(mod, 'd'), f'{cad_py_path.name} has no module-level `d`'
    return mod.d


def _mesh_of(cad_py_path: pathlib.Path, n_seg: int = N_SEG):
    import cadlang
    d = _load_design(cad_py_path)
    return cadlang._build_mesh(d, n_seg)


# =========================================================================
# overlap_ring — hand-written revolve with notches + heat-insert bores.
#
# Params (verified in overlap_ring.cad.py and STATE.md §3):
#   ring_id=240.5, ring_od=250.0, ring_h=46.0
#   notch_arc=20.4, notch_depth=32.0, chamfer_len=2.0, chamfer_flare=1.0
#   hole_dia=4.0, hole_depth=4.0, hole_z1=5.0, hole_z2=11.667
#
# STATE.md §3: "vol=154.8 cm^3 (matches hand calc)" — used here as ground
# truth because it was independently hand-calculated by the user, not
# read back out of cadlang's emitted STL.
# =========================================================================

EXPECTED_OVERLAP_RING_VOL_CM3  = 154.8
EXPECTED_OVERLAP_RING_OD_MM   = 250.0
EXPECTED_OVERLAP_RING_HEIGHT_MM = 46.0


@pytest.fixture(scope='module')
def overlap_ring_mesh():
    return _mesh_of(PARTS_DIR / 'overlap_ring.cad.py')


def test_overlap_ring_is_watertight(overlap_ring_mesh):
    assert overlap_ring_mesh.is_watertight, \
        'overlap_ring mesh is not watertight — CSG failed or profile is open'


def test_overlap_ring_volume_matches_hand_calc(overlap_ring_mesh):
    vol_cm3 = overlap_ring_mesh.volume / 1000.0
    assert vol_cm3 == pytest.approx(EXPECTED_OVERLAP_RING_VOL_CM3, rel=0.015), \
        f'overlap_ring volume {vol_cm3:.2f} cm^3 differs from hand calc ' \
        f'{EXPECTED_OVERLAP_RING_VOL_CM3} cm^3 by more than 1.5%'


def test_overlap_ring_bounding_box(overlap_ring_mesh):
    dx, dy, dz = overlap_ring_mesh.bounding_box.extents
    # Circular part; x and y extents equal the OD.
    assert dx == pytest.approx(EXPECTED_OVERLAP_RING_OD_MM, abs=0.5)
    assert dy == pytest.approx(EXPECTED_OVERLAP_RING_OD_MM, abs=0.5)
    assert dz == pytest.approx(EXPECTED_OVERLAP_RING_HEIGHT_MM, abs=0.05)


# =========================================================================
# SWQ8_part002 (rail) — importer-generated extrude.
#
# GROUND TRUTH — DO NOT REWRITE THIS NUMBER TO MATCH BROKEN OUTPUT.
# If this test fires, the importer has regressed. Fix the importer, not
# the expected value. (This file has a history of the expected being
# rewritten to match buggy output; that's the wrong direction.)
#
# Physical rail is a stepped bar: L1 full-length base + L2 raised middle.
# Hand-calc:
#   L1 slab: 120.5 × 20 × 5.6             = 13,496 mm^3
#   L2 slab:  90.5 × 20 × 5.6             = 10,136 mm^3
#   4 bolt holes: 4 × π × 2.15^2 × 5.618  =    326 mm^3
#   Total ≈ 23.3 cm^3
#
# The lateral cylindrical saddle faces sad1 (r=125.2) and sad2 (r=120)
# are NOT cuts — they're concave boundary surfaces on the rail's top
# that cadlang can't faithfully represent with the current `cut(Circle)`
# primitive (which extrudes an infinite cylinder). Emitting them as
# cuts destroys L2. The importer now skips them with a comment; proper
# boundary-surface handling is queued in ROADMAP.
# =========================================================================

EXPECTED_RAIL_VOL_CM3      = 23.3
EXPECTED_RAIL_BBOX_X_MM   = 120.5
EXPECTED_RAIL_BBOX_Y_MM    = 20.0
EXPECTED_RAIL_BBOX_Z_MM    = 11.2


@pytest.fixture(scope='module')
def rail_mesh():
    return _mesh_of(PARTS_DIR / 'SWQ8_part002.g.cad.py')


def test_rail_is_watertight(rail_mesh):
    assert rail_mesh.is_watertight


def test_rail_volume_matches_hand_calc(rail_mesh):
    vol_cm3 = rail_mesh.volume / 1000.0
    assert vol_cm3 == pytest.approx(EXPECTED_RAIL_VOL_CM3, abs=0.5), (
        f'rail volume {vol_cm3:.2f} cm^3 off hand calc {EXPECTED_RAIL_VOL_CM3} '
        f'cm^3. If this fell to ~13.4, the importer is emitting lateral '
        'cylindrical faces as cuts again, which destroys L2. The correct '
        'fix is in `stepimport._emit_extrude_cadpy` (skip lateral cuts), '
        'NOT to lower the expected value here.'
    )


def test_rail_bounding_box(rail_mesh):
    dx, dy, dz = rail_mesh.bounding_box.extents
    assert dx == pytest.approx(EXPECTED_RAIL_BBOX_X_MM, abs=0.1)
    assert dy == pytest.approx(EXPECTED_RAIL_BBOX_Y_MM, abs=0.1)
    # Z tolerance is loose because tessellation step size perturbs the
    # arc's peak by up to a few tenths of a mm at n_seg=60.
    assert dz == pytest.approx(EXPECTED_RAIL_BBOX_Z_MM, abs=0.3)
