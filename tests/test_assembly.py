"""Tier 1: mate-solver tests for `assembly.solve_poses`.

Exercise the dewshield assembly YAML end-to-end through the solver and
assert on hand-computed world positions — no CSG, no STL emit, no
trimesh boolean work. Each test is a few ms.

These tests double as regression guards for the axis-direction convention
(`bolt axis = head→tip, body on head side`). If anyone flips the sign of
an axis in the YAML or reverses the convention inside `_frame_from_bolt_pair`,
the rail-inside-ring assertion below fires.
"""
from __future__ import annotations
import pathlib
import sys

import numpy as np
import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
ASSEMBLY_YAML = (REPO_ROOT / 'example-project' / 'SWQ8-Dewshield-Holder'
                 / 'assembly.yaml')


# =========================================================================
# Ground-truth values (STATE.md §2 + assembly.yaml declarations):
# =========================================================================

RING_OD_RADIUS_MM     = 125.2         # tabs open at this radius on ring OD
RAIL_BOLT_Y_LOCAL     = -155.2        # rail-local Y of the bolt line
# The rail's mating face with the ring is its z=11.2 surface (top of L2 —
# the raised middle). With axis=[0,0,-1] in the YAML, this should sit
# INSIDE the ring OD. The z=0 bottom is the OUTER surface (bolt heads
# are on this side when installed).
RAIL_MATING_FACE_Z_LOCAL = 11.2
RAIL_OUTER_FACE_Z_LOCAL  = 0.0


@pytest.fixture(scope='module')
def assembly():
    import yaml
    return yaml.safe_load(ASSEMBLY_YAML.read_text(encoding='utf-8'))


@pytest.fixture(scope='module')
def transforms(assembly):
    import assembly as _asm
    return _asm.solve_poses(assembly)


def _apply(T: np.ndarray, pt_local) -> np.ndarray:
    """Transform a 3-point by a 4x4 homogeneous matrix."""
    p = np.array([*pt_local, 1.0])
    return (T @ p)[:3]


def _radius_from_z_axis(pt) -> float:
    return float(np.hypot(pt[0], pt[1]))


# =========================================================================
# Solver sanity
# =========================================================================

def test_anchor_is_identity(transforms):
    T = transforms[('middle_ring', 0)]
    assert np.allclose(T, np.eye(4), atol=1e-9), \
        'anchor part must be at identity transform'


def test_every_instance_is_placed(assembly, transforms):
    for slot, spec in assembly['parts'].items():
        count = int(spec.get('count', 1))
        for i in range(count):
            assert (slot, i) in transforms, f'{slot}[{i}] was not placed'


def test_transforms_are_rigid(transforms):
    """Every T must be a rotation + translation (no scale, no shear)."""
    for key, T in transforms.items():
        R = T[:3, :3]
        # R R^T ≈ I and det(R) ≈ +1
        assert np.allclose(R @ R.T, np.eye(3), atol=1e-6), \
            f'{key}: rotation block isn\'t orthogonal'
        assert abs(np.linalg.det(R) - 1.0) < 1e-6, \
            f'{key}: det(R) != 1 (proper rotation)'


# =========================================================================
# Mate alignment — rail bolts should land on the ring tab radius
# =========================================================================

def test_upper_rail_bolts_land_on_ring_tab(transforms):
    """Each rail's scope_end bolt midpoint should sit on the ring's OD
    radius (125.2 mm from the Z axis). If this test drifts, the mate
    solver has broken at the frame-alignment step."""
    T = transforms[('upper_rails', 0)]
    # Local bolt midpoint for scope_end: midpoint of positions in YAML.
    bolt_mid_local = [(-56.917 + -50.25) / 2, RAIL_BOLT_Y_LOCAL, 3.3]
    bolt_mid_world = _apply(T, bolt_mid_local)
    r = _radius_from_z_axis(bolt_mid_world)
    assert r == pytest.approx(RING_OD_RADIUS_MM, abs=0.05), (
        f'upper_rails[0] scope_end bolt midpoint landed at r={r:.3f}; '
        f'expected {RING_OD_RADIUS_MM} (ring OD). Mate alignment broken.'
    )


# =========================================================================
# Orientation — rail body must be INSIDE the ring after the axis flip
# =========================================================================

def test_rail_mating_face_sits_inside_ring_od(transforms):
    """THE axis-convention regression guard.

    The rail's mating face is its z=11.2 surface (top of L2, the raised
    middle). With `axis: [0, 0, -1]` in the YAML (head→tip convention,
    body on head side), this face must end up INSIDE the ring OD — i.e.
    at a radius smaller than the ring's 125.2 mm. If someone flips the
    axis back to [0, 0, 1], the rail gets installed backwards and this
    face lands OUTSIDE the ring, firing this assertion.
    """
    T = transforms[('upper_rails', 0)]
    # Rail-local centerline at the mating face. Using y=BOLT_Y keeps the
    # point on the radial line through the bolt midpoint, so `r` is
    # unambiguously "radial distance from ring axis" after the transform.
    pt_local = [0.0, RAIL_BOLT_Y_LOCAL, RAIL_MATING_FACE_Z_LOCAL]
    pt_world = _apply(T, pt_local)
    r = _radius_from_z_axis(pt_world)
    assert r < RING_OD_RADIUS_MM, (
        f'rail mating face (rail-local z={RAIL_MATING_FACE_Z_LOCAL}) at '
        f'r={r:.3f} — expected < {RING_OD_RADIUS_MM} (INSIDE the ring OD). '
        'If this fires, the bolt axis in the SWQ8 YAML was likely flipped '
        'back to [0, 0, 1] or the head→tip convention in '
        '`_frame_from_bolt_pair` was reversed.'
    )


def test_rail_outer_face_sits_outside_ring_od(transforms):
    """Symmetric sanity: the rail's z=0 face (L1's bottom, bolt-head
    side) must be OUTSIDE the ring OD. Together with the mating-face
    test this pins the rail orientation in both radial directions."""
    T = transforms[('upper_rails', 0)]
    pt_local = [0.0, RAIL_BOLT_Y_LOCAL, RAIL_OUTER_FACE_Z_LOCAL]
    pt_world = _apply(T, pt_local)
    r = _radius_from_z_axis(pt_world)
    assert r > RING_OD_RADIUS_MM, (
        f'rail outer face (rail-local z={RAIL_OUTER_FACE_Z_LOCAL}) at '
        f'r={r:.3f} — expected > {RING_OD_RADIUS_MM}.'
    )


# =========================================================================
# Tab distribution — 4 rails should land at 0°/90°/180°/270° on the ring
# =========================================================================

def test_upper_rails_distributed_around_ring(transforms):
    """All 4 upper rails should mate to distinct tabs. Their bolt
    midpoints in world should be at 4 distinct phi values 90° apart."""
    bolt_mid_local = [(-56.917 + -50.25) / 2, RAIL_BOLT_Y_LOCAL, 3.3]
    phis = []
    for i in range(4):
        T = transforms[('upper_rails', i)]
        pt = _apply(T, bolt_mid_local)
        phi = float(np.degrees(np.arctan2(pt[1], pt[0]))) % 360.0
        phis.append(phi)
    # Sort and check ~90° spacing.
    phis.sort()
    gaps = [(phis[(i + 1) % 4] - phis[i]) % 360.0 for i in range(4)]
    for g in gaps:
        assert abs(g - 90.0) < 0.1, \
            f'rails should be 90° apart around the ring; got gap {g:.3f}°'
