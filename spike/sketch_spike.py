"""
Spike: prove the python-solvespace round-trip works end-to-end.

Goal:
  1. Build a 2D sketch with primitive entities + geometric constraints
     + dimensional constraints (referencing Design.params) using
     python-solvespace.
  2. Solve. Read back the resolved 2D loop.
  3. Feed that loop into the existing Design.extrude(profile=...) path.
  4. emit_stl. Assert the resulting STL volume matches length*width*height.

If this works the dependency footprint is acceptable and the v1
plan is concrete enough to start on.
"""
from __future__ import annotations

import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from python_solvespace import ResultFlag, SolverSystem  # noqa: E402

from cadlang import Design  # noqa: E402


def solve_rectangle(length: float, width: float) -> list[tuple[float, float]]:
    """Define a rectangle from primitives + constraints, solve, return the
    closed loop in CCW order.

    Construction:
      - 4 free 2D points with rough initial guesses
      - 4 lines connecting them
      - p1 dragged at origin (anchors the rectangle)
      - bottom + top horizontal; left + right vertical
      - bottom length = `length`, left length = `width`
    """
    sys_ = SolverSystem()
    wp = sys_.create_2d_base()  # XY workplane

    # Rough initial guesses — solver moves them onto the constraint manifold.
    p1 = sys_.add_point_2d(0.0, 0.0, wp)
    p2 = sys_.add_point_2d(length * 0.9, 0.1, wp)
    p3 = sys_.add_point_2d(length * 0.9, width * 0.9, wp)
    p4 = sys_.add_point_2d(-0.1, width * 0.9, wp)

    bottom = sys_.add_line_2d(p1, p2, wp)
    right  = sys_.add_line_2d(p2, p3, wp)
    top    = sys_.add_line_2d(p3, p4, wp)
    left   = sys_.add_line_2d(p4, p1, wp)

    # Anchor p1 — fixes the 2 translation DOFs.
    sys_.dragged(p1, wp)

    # Geometric constraints.
    sys_.horizontal(bottom, wp)
    sys_.horizontal(top, wp)
    sys_.vertical(left, wp)
    sys_.vertical(right, wp)

    # Dimensional constraints. point-to-point distance = scalar.
    sys_.distance(p1, p2, length, wp)
    sys_.distance(p1, p4, width, wp)

    dof_before = sys_.dof()
    result = sys_.solve()
    dof_after = sys_.dof()

    if result != ResultFlag.OKAY:
        raise RuntimeError(
            f'solve failed: result={ResultFlag(result).name}, '
            f'dof_before={dof_before}, dof_after={dof_after}, '
            f'failures={sys_.failures()}'
        )

    print(f'  dof: before={dof_before} after={dof_after}, '
          f'cons={sys_.cons_len()}, params={sys_.param_len()}')

    # Read back solved coords. params(entity.params) returns the float
    # list backing that entity — for a 2D point that's [u, v].
    coords = []
    for pt in (p1, p2, p3, p4):
        u, v = sys_.params(pt.params)
        coords.append((u, v))
    return coords


def probe_underconstrained():
    """Drop the width dim. We expect dof > 0 and want to confirm the
    solver still returns OKAY (it just picks *some* solution) — that's
    the case where v1 should warn the user."""
    sys_ = SolverSystem()
    wp = sys_.create_2d_base()
    p1 = sys_.add_point_2d(0.0, 0.0, wp)
    p2 = sys_.add_point_2d(45.0, 0.1, wp)
    p3 = sys_.add_point_2d(45.0, 28.0, wp)
    p4 = sys_.add_point_2d(0.1, 28.0, wp)
    bottom = sys_.add_line_2d(p1, p2, wp)
    right  = sys_.add_line_2d(p2, p3, wp)
    top    = sys_.add_line_2d(p3, p4, wp)
    left   = sys_.add_line_2d(p4, p1, wp)
    sys_.dragged(p1, wp)
    sys_.horizontal(bottom, wp)
    sys_.horizontal(top, wp)
    sys_.vertical(left, wp)
    sys_.vertical(right, wp)
    sys_.distance(p1, p2, 50.0, wp)
    # NOTE: width dim deliberately omitted.
    print(f'  under-constrained: dof_before={sys_.dof()}')
    result = sys_.solve()
    print(f'  result={ResultFlag(result).name}, dof_after={sys_.dof()}')


def probe_overconstrained():
    """Add a redundant *conflicting* dim — distance + an equal that
    contradicts. Expect INCONSISTENT."""
    sys_ = SolverSystem()
    wp = sys_.create_2d_base()
    p1 = sys_.add_point_2d(0.0, 0.0, wp)
    p2 = sys_.add_point_2d(50.0, 0.0, wp)
    p3 = sys_.add_point_2d(50.0, 30.0, wp)
    p4 = sys_.add_point_2d(0.0, 30.0, wp)
    bottom = sys_.add_line_2d(p1, p2, wp)
    right  = sys_.add_line_2d(p2, p3, wp)
    top    = sys_.add_line_2d(p3, p4, wp)
    left   = sys_.add_line_2d(p4, p1, wp)
    sys_.dragged(p1, wp)
    sys_.horizontal(bottom, wp)
    sys_.horizontal(top, wp)
    sys_.vertical(left, wp)
    sys_.vertical(right, wp)
    sys_.distance(p1, p2, 50.0, wp)
    sys_.distance(p1, p4, 30.0, wp)
    sys_.distance(p1, p2, 99.0, wp)  # conflicts with the 50.0 above
    result = sys_.solve()
    print(f'  over-constrained (conflicting): result={ResultFlag(result).name}, '
          f'failures={sys_.failures()}')


def main():
    LENGTH, WIDTH, HEIGHT = 50.0, 30.0, 5.0

    print(f'solving rectangle: length={LENGTH} width={WIDTH}')
    loop = solve_rectangle(LENGTH, WIDTH)
    for i, (u, v) in enumerate(loop, 1):
        print(f'  p{i} = ({u:+.4f}, {v:+.4f})')

    # Round-trip into the existing parametric pipeline. We feed the solved
    # numeric points as the profile. Note: Design.params still drives the
    # height — only the in-plane sketch geometry came from solvespace.
    d = Design('SpikeRect', units='mm', params={'h': HEIGHT})
    d.extrude(name='plate', on='XY', height='h', profile=loop)

    out_stl = HERE / 'sketch_spike.stl'
    d.emit_stl(str(out_stl))

    # Verify volume matches the analytic answer. trimesh reports volume in
    # the part's units cubed (mm^3 here).
    import trimesh
    mesh = trimesh.load(out_stl)
    expected = LENGTH * WIDTH * HEIGHT
    actual = mesh.volume
    err = abs(actual - expected) / expected
    print(f'volume: expected={expected:.3f} mm^3, '
          f'actual={actual:.3f} mm^3, err={err * 100:.4f}%')
    assert err < 1e-6, f'volume mismatch: {err * 100}%'
    print('OK')

    print('\nprobing under-constrained sketch:')
    probe_underconstrained()
    print('\nprobing over-constrained (conflicting) sketch:')
    probe_overconstrained()


if __name__ == '__main__':
    main()
