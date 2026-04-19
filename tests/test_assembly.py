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
RING_ID_RADIUS_MM     = 120.0         # ring bore
RAIL_BOLT_Y_LOCAL     = -155.2        # rail-local Y of the bolt line
# The rail sits with its L1 flange face (z=5.6, L1 top) tangent to the
# ring's OD. L2 (rail-local z=5.6-11.2) extends radially inward past
# the OD into the ring's empty hollow bore — that's fine, just empty
# space, no overlap with ring WALL material.
RAIL_L1_TOP_Z_LOCAL      = 5.6       # flush face with ring OD
RAIL_L2_TOP_Z_LOCAL      = 11.2      # deepest into ring bore
RAIL_L1_BOTTOM_Z_LOCAL   = 0.0       # outermost (bolt-head side)


@pytest.fixture(scope='module')
def assembly():
    import yaml
    return yaml.safe_load(ASSEMBLY_YAML.read_text(encoding='utf-8'))


@pytest.fixture(scope='module')
def transforms(assembly):
    """Pose transforms AFTER the constraint solver's axis-DOF resolution.

    The solver needs the part meshes to project bboxes onto the mate
    axis and shift the moving part until it no longer overlaps the
    target. Without the mesh loader, `solve_poses` falls back to the
    legacy midpoint-to-midpoint alignment, which allows overlap — so
    tests that assume "parts are flush" have to use the loader.
    """
    import trimesh
    import assembly as _asm
    meshes = {}
    yaml_dir = ASSEMBLY_YAML.parent
    for slot, spec in assembly['parts'].items():
        src = (yaml_dir / spec['source']).resolve()
        stl = _asm._stl_path_for(src)
        meshes[slot] = trimesh.load(str(stl))
    return _asm.solve_poses(assembly, mesh_loader=meshes.get)


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

def test_upper_rail_bolt_axis_coincides_with_ring_tab_axis(transforms):
    """After the constraint solver shifts the rail outward so its
    mating face touches the ring OD, the bolt midpoint no longer
    coincides with the ring tab hole — but the bolt AXIS LINE still
    must, so a physical bolt can pass through both holes. Test: two
    points on the rail's bolt axis in world should lie on the ring
    tab's axis line (phi=0: y=0 and z=z_mid_of_hole_pair).
    """
    T = transforms[('upper_rails', 0)]
    bolt_mid_local = [(-56.917 + -50.25) / 2, RAIL_BOLT_Y_LOCAL, 3.3]
    # Second point: 10 mm further along the rail's bolt axis
    # (axis = [0,0,-1] in rail-local → z-10 moves along the axis).
    second_local = [bolt_mid_local[0], bolt_mid_local[1], bolt_mid_local[2] - 10]
    z_mid_world = (18.333 + 25.0) / 2  # upper hole pair on tab 0
    for pt_local in (bolt_mid_local, second_local):
        pt_world = _apply(T, pt_local)
        # phi=0 tab axis is along +X in world, through (any_r, 0, z_mid).
        assert abs(pt_world[1]) < 0.01, \
            f'bolt-axis point {pt_local} should have y≈0 at phi=0 tab; got y={pt_world[1]:.3f}'
        assert abs(pt_world[2] - z_mid_world) < 0.01, \
            f'bolt-axis point {pt_local} should have z≈{z_mid_world} at the upper hole pair; got z={pt_world[2]:.3f}'


def test_upper_rail_l1_top_touches_ring_od(transforms):
    """The rail's L1 TOP face (the concave surface shaped by sad1,
    rail-local z=5.6 at centerline) must land on the ring's OD radius
    exactly — that's what "flush" means and what the constraint solver
    is for. If this fires, the contains+raycast shift in
    `_resolve_axis_overlap` got the wrong direction or magnitude."""
    T = transforms[('upper_rails', 0)]
    pt_world = _apply(T, [0.0, RAIL_BOLT_Y_LOCAL, RAIL_L1_TOP_Z_LOCAL])
    r = _radius_from_z_axis(pt_world)
    assert r == pytest.approx(RING_OD_RADIUS_MM, abs=0.1), (
        f'rail L1 top at r={r:.3f}; expected flush against ring OD '
        f'{RING_OD_RADIUS_MM} ± 0.1. Constraint solver regressed.'
    )


# =========================================================================
# Orientation — rail body must be INSIDE the ring after the axis flip
# =========================================================================

def test_rail_has_no_vertices_inside_ring_wall(transforms):
    """CONSTRAINT SOLVER REGRESSION GUARD. Every rail vertex must sit
    either outside ring OD (r > OD) or inside ring ID (r < ID, empty
    bore) — NEVER in the wall material (ID < r < OD). The solver's
    job is to guarantee exactly this.

    This is the strict geometric version of "no overlap with ring";
    the CSG intersection check in `_check_intersections` reports the
    total overlap volume, but this catches even tiny penetrations of
    individual vertices into the wall.
    """
    import trimesh
    T = transforms[('upper_rails', 0)]
    rail_stl = (REPO_ROOT / 'example-project' / 'SWQ8-Dewshield-Holder'
                / 'parts' / 'SWQ8_part002.g.stl')
    mesh = trimesh.load(str(rail_stl))
    mesh.apply_transform(T)
    xs, ys, zs = mesh.vertices[:, 0], mesh.vertices[:, 1], mesh.vertices[:, 2]
    radii = np.hypot(xs, ys)
    # Only vertices within the ring's axial extent [0, 30] can penetrate.
    in_ring_axial = (zs >= 0) & (zs <= 30)
    in_wall = ((radii > RING_ID_RADIUS_MM + 0.05) &
               (radii < RING_OD_RADIUS_MM - 0.05) &
               in_ring_axial)
    n_in_wall = int(in_wall.sum())
    assert n_in_wall == 0, (
        f'{n_in_wall} rail vertices fall inside the ring wall '
        f'(ID < r < OD at the ring\'s z-range). Constraint solver '
        f'failed to clear them. Deepest: r={radii[in_wall].min():.3f}.'
    )


def test_no_unexpected_intersections_in_assembly(assembly, transforms):
    """Whole-assembly integration guard. After `solve_poses` with the
    constraint solver, running the same CSG intersection check that
    `cadlang build` uses should report zero overlaps above 1 mm³. If
    this fires, the constraint solver missed a pair or couldn't
    resolve a conflict — inspect the JSON to see which pair."""
    import trimesh
    import assembly as _asm
    yaml_dir = ASSEMBLY_YAML.parent
    placed = []
    for slot, spec in assembly['parts'].items():
        src = (yaml_dir / spec['source']).resolve()
        stl = _asm._stl_path_for(src)
        base = trimesh.load(str(stl))
        count = int(spec.get('count', 1))
        for i in range(count):
            m = base.copy()
            m.apply_transform(transforms[(slot, i)])
            placed.append({'slot': slot, 'instance': i, 'mesh': m, 'transform': transforms[(slot, i)]})

    intersections = _asm._check_intersections(
        placed, mated_pairs=_asm._mated_pair_keys(assembly),
    )
    assert not intersections, (
        'assembly has unexpected CSG intersections after constraint solving:\n'
        + '\n'.join(f"  {x['a']} ∩ {x['b']} = {x['volume_mm3']:.1f} mm³"
                    for x in intersections)
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
