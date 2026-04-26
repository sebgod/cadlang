"""Tests for the constraint-based Sketch system.

Tier 1.5 (fast, no CSG): solve a sketch and assert on the resolved
coordinates / DOF / failure modes.

Tier 2 (slower, CSG): round-trip a Sketch-backed Design through
manifold3d to STL and assert the volume matches the analytical answer.
"""
from __future__ import annotations
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sketch import Sketch, SketchSolveError  # noqa: E402


# =========================================================================
# Tier 1.5 — solve only
# =========================================================================

def test_rectangle_solves_to_exact_corners():
    sk = Sketch()
    p1 = sk.point(0, 0, fix=True)
    p2 = sk.point(45, 1)            # off the manifold deliberately
    p3 = sk.point(45, 28)
    p4 = sk.point(1, 28)
    l1 = sk.line(p1, p2); l2 = sk.line(p2, p3)
    l3 = sk.line(p3, p4); l4 = sk.line(p4, p1)
    sk.horizontal(l1, l3)
    sk.vertical(l2, l4)
    sk.distance(p1, p2, 'L')
    sk.distance(p1, p4, 'W')

    s = sk.solve({'L': 50.0, 'W': 30.0})
    assert s.profile == [(0.0, 0.0), (50.0, 0.0), (50.0, 30.0), (0.0, 30.0)]


def test_rectangle_sugar_matches_explicit():
    sk = Sketch()
    sk.rectangle(corner=(0, 0), width='L', height='W')
    s = sk.solve({'L': 50.0, 'W': 30.0})
    # CCW from lower-left.
    assert s.profile == [(0.0, 0.0), (50.0, 0.0), (50.0, 30.0), (0.0, 30.0)]


def test_solve_rebuilds_with_new_params():
    """The same Sketch should solve correctly with different param sets —
    the recipe is reusable."""
    sk = Sketch()
    sk.rectangle(corner=(0, 0), width='L', height='W')
    s1 = sk.solve({'L': 50.0, 'W': 30.0})
    s2 = sk.solve({'L': 12.0, 'W': 7.0})
    assert s1.profile[1][0] == 50.0
    assert s2.profile[1][0] == 12.0
    assert s2.profile[2][1] == 7.0


def test_overconstrained_conflicting_dims_raises():
    sk = Sketch()
    p1 = sk.point(0, 0, fix=True)
    p2 = sk.point(50, 0)
    p3 = sk.point(50, 30)
    p4 = sk.point(0, 30)
    l1 = sk.line(p1, p2); l2 = sk.line(p2, p3)
    l3 = sk.line(p3, p4); l4 = sk.line(p4, p1)
    sk.horizontal(l1, l3)
    sk.vertical(l2, l4)
    sk.distance(p1, p2, 50.0)
    sk.distance(p1, p4, 30.0)
    sk.distance(p1, p2, 99.0)   # contradicts the first

    with pytest.raises(SketchSolveError) as exc:
        sk.solve({})
    assert 'INCONSISTENT' in str(exc.value)
    # solvespace reports failing constraint indices
    assert exc.value.failures


def test_disjoint_chain_yields_empty_profile():
    """v1 only auto-extracts a profile from a single closed chain.
    Multiple disjoint chains return an empty profile — solving still
    succeeds (constraints are valid), but ``extrude(sketch=)`` would
    later raise."""
    sk = Sketch()
    a1 = sk.point(0, 0, fix=True)
    a2 = sk.point(10, 0)
    a3 = sk.point(10, 10)
    a4 = sk.point(0, 10)
    sk.line(a1, a2); sk.line(a2, a3); sk.line(a3, a4); sk.line(a4, a1)
    b1 = sk.point(50, 50)
    b2 = sk.point(60, 50)
    b3 = sk.point(60, 60)
    b4 = sk.point(50, 60)
    sk.line(b1, b2); sk.line(b2, b3); sk.line(b3, b4); sk.line(b4, b1)

    s = sk.solve({})
    assert s.profile == []   # not a single closed chain


def test_perpendicular_constraint_makes_a_right_angle():
    """Two lines with perpendicular constraint — angle should be 90°."""
    import math
    sk = Sketch()
    p0 = sk.point(0, 0, fix=True)
    p1 = sk.point(10, 0.5)            # nearly horizontal
    p2 = sk.point(0.3, 10)            # nearly vertical
    l1 = sk.line(p0, p1)
    l2 = sk.line(p0, p2)
    sk.horizontal(l1)                  # pin l1 along +U
    sk.perpendicular(l1, l2)           # l2 must be perpendicular to l1
    sk.distance(p0, p1, 10.0)
    sk.distance(p0, p2, 10.0)
    s = sk.solve({})

    x1, y1 = s.points[p1.eid]
    x2, y2 = s.points[p2.eid]
    # l1 along +U: y1 ≈ 0
    assert abs(y1) < 1e-6
    # l2 perpendicular to l1 ⇒ x2 ≈ 0
    assert abs(x2) < 1e-6


# =========================================================================
# Tier 2 — full STL round-trip
# =========================================================================

def test_sketched_extrude_volume_matches_analytical(tmp_path):
    """The flagship spike: rectangle Sketch → Design.extrude(sketch=) →
    manifold3d → STL. Volume must equal L*W*H to FP precision."""
    import trimesh
    from cadlang import Design

    L, W, H = 50.0, 30.0, 5.0
    d = Design('SketchTest', units='mm', params={'L': L, 'W': W, 'H': H})
    sk = Sketch()
    sk.rectangle(corner=(0, 0), width='L', height='W')
    d.extrude(name='plate', on='XY', height='H', sketch=sk)

    out = tmp_path / 'plate.stl'
    d.emit_stl(str(out))
    mesh = trimesh.load(out)
    expected = L * W * H
    assert abs(mesh.volume - expected) / expected < 1e-6
    assert mesh.is_watertight


def test_sketched_extrude_emits_parametric_fusion(tmp_path):
    """The Fusion script should reference user parameters by name in
    sketch dimensions (not bake numeric values)."""
    from cadlang import Design

    d = Design('SketchTest', units='mm', params={'L': 50, 'W': 30, 'H': 5})
    sk = Sketch()
    sk.rectangle(corner=(0, 0), width='L', height='W')
    d.extrude(name='plate', on='XY', height='H', sketch=sk)

    out = tmp_path / 'plate_fusion.py'
    d.emit_fusion(str(out))
    text = out.read_text(encoding='utf-8')

    # Native sketch + constraints + dimensions are present.
    assert 'sketchCurves.sketchLines' in text
    assert 'addHorizontal' in text and 'addVertical' in text
    assert 'addDistanceDimension' in text
    # Dimensions reference user parameters (the whole point of (b)
    # backend strategy from the plan).
    assert ".parameter.expression = 'L'" in text
    assert ".parameter.expression = 'W'" in text
    # And userParameters L, W, H are declared at the top.
    assert "P('L', 50, 'mm')" in text


def test_legacy_profile_path_still_works(tmp_path):
    """Existing profile=[...] callers must keep working unchanged."""
    import trimesh
    from cadlang import Design

    d = Design('LegacyPlate', units='mm', params={'H': 4.0})
    d.extrude(name='plate', on='XY', height='H',
              profile=[(0, 0), (20, 0), (20, 10), (0, 10)])
    out = tmp_path / 'legacy.stl'
    d.emit_stl(str(out))
    mesh = trimesh.load(out)
    assert mesh.is_watertight
    assert abs(mesh.volume - 20 * 10 * 4) < 1e-6
