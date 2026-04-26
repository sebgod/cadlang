"""
sketch.py — 2D constraint-based sketches for cadlang.

A `Sketch` is a recipe of entities (Point / Line / Circle) plus geometric
and dimensional constraints. Calling ``solve(params)`` builds a fresh
solvespace system from the recipe, applies the constraints (evaluating
expression strings against ``params``), and returns a `SolvedSketch`
with the resolved 2D loop ready for the existing extrude/cut pipeline.

Two backends consume the same recipe:

* STL backend — calls ``solve(d.params)``, hands the resolved CCW loop
  to manifold3d.
* Fusion backend — walks the same recipe and emits native
  ``sketchCurves.sketchLines.addByTwoPoints``, ``geometricConstraints.add*``
  and ``sketchDimensions.add*`` calls, so Fusion re-solves the sketch
  natively against ``userParameters``.

Example::

    sk = Sketch()                                    # XY workplane
    p1 = sk.point(0, 0, fix=True)
    p2 = sk.point(50, 0)
    p3 = sk.point(50, 30)
    p4 = sk.point(0, 30)
    l1 = sk.line(p1, p2);   l2 = sk.line(p2, p3)
    l3 = sk.line(p3, p4);   l4 = sk.line(p4, p1)
    sk.horizontal(l1, l3);  sk.vertical(l2, l4)
    sk.distance(p1, p2, 'L')
    sk.distance(p1, p4, 'W')

    d.extrude(name='plate', on='XY', height='H', sketch=sk)

Or with sugar::

    sk = Sketch()
    sk.rectangle(corner=(0, 0), width='L', height='W')

v1 limitations:

* Workplane must be 'XY'.
* Profile = either a single closed line chain (added in cycle order),
  or one bare circle, or one closed line chain plus separate circles
  (treated as inner holes — but holes are not yet wired through, so
  for now use a separate ``cut()`` for holes).
* Arcs and tangent-on-arc constraints not yet supported.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


# =========================================================================
# Errors
# =========================================================================

class SketchError(Exception):
    pass


class SketchSolveError(SketchError):
    """Raised when solvespace returns a non-OKAY result. Carries the
    original ResultFlag enum and the failing constraint indices when
    available."""
    def __init__(self, message, *, flag=None, failures=None):
        super().__init__(message)
        self.flag = flag
        self.failures = failures or []


# =========================================================================
# Entity dataclasses (opaque handles returned to the user)
# =========================================================================

@dataclass(eq=False)
class Point:
    sketch: 'Sketch'
    eid: int                # stable id within this sketch
    u: float                # initial-guess u-coord (sketch-local)
    v: float                # initial-guess v-coord
    fix: bool = False
    name: str | None = None

    def __repr__(self):
        n = f' {self.name!r}' if self.name else ''
        return f'<Point #{self.eid}{n} ({self.u:.3g}, {self.v:.3g}){" fix" if self.fix else ""}>'


@dataclass(eq=False)
class Line:
    sketch: 'Sketch'
    eid: int
    start: Point
    end: Point
    name: str | None = None

    def __repr__(self):
        n = f' {self.name!r}' if self.name else ''
        return f'<Line #{self.eid}{n} {self.start.eid}->{self.end.eid}>'


@dataclass(eq=False)
class Circle:
    sketch: 'Sketch'
    eid: int
    center: Point
    radius: Any            # numeric or expression string
    name: str | None = None

    def __repr__(self):
        n = f' {self.name!r}' if self.name else ''
        return f'<Circle #{self.eid}{n} c={self.center.eid} r={self.radius!r}>'


# =========================================================================
# Solved result
# =========================================================================

@dataclass
class SolvedCircle:
    cx: float
    cy: float
    r: float
    name: str | None = None


@dataclass
class SolvedSketch:
    plane: str
    points: dict[int, tuple[float, float]]   # eid → (u, v)
    profile: list[tuple[float, float]]       # outer closed loop, CCW
    circles: list[SolvedCircle]              # solved circles (currently
                                             # used as the bare-circle
                                             # profile if profile is empty)
    dof: int                                 # raw post-solve DOF
    n_constraints: int

    def has_circle_profile(self) -> bool:
        """True iff the sketch's profile is one bare circle (no line
        chain). The STL backend tessellates this differently."""
        return not self.profile and len(self.circles) == 1


# =========================================================================
# Sketch
# =========================================================================

class Sketch:
    """A 2D constraint-based sketch.

    The sketch is a *recipe*: entities + constraints, all expressed
    against user-named parameters. ``solve(params)`` builds a fresh
    solver system, applies the recipe, and returns a SolvedSketch.

    Sketches are deliberately mutable while you build them and frozen
    once you call ``solve()`` (re-solving with new params is fine —
    it just re-applies the recipe to a fresh system).
    """

    def __init__(self, plane: str = 'XY'):
        if plane != 'XY':
            raise NotImplementedError(
                f'sketch plane {plane!r} not yet supported (use XY for now)')
        self.plane = plane
        self._next_eid = 0
        self._points: list[Point] = []
        self._lines: list[Line] = []
        self._circles: list[Circle] = []
        # Geometric constraints: {kind, args: [entities…]}
        self._geom_cons: list[dict] = []
        # Dimensional constraints: {kind, args: [entities…], expr: <expr>,
        #                            factor: float}
        # Stored value applied to solvespace = ev(expr) * factor.
        # (factor=2.0 lets `radius` be expressed via `diameter`.)
        self._dim_cons: list[dict] = []

    # ---- entity constructors ----

    def _new_eid(self) -> int:
        eid = self._next_eid
        self._next_eid += 1
        return eid

    def point(self, u, v, *, fix: bool = False, name: str | None = None) -> Point:
        pt = Point(self, self._new_eid(), float(u), float(v), fix=fix, name=name)
        self._points.append(pt)
        return pt

    def line(self, a: Point, b: Point, *, name: str | None = None) -> Line:
        if a.sketch is not self or b.sketch is not self:
            raise SketchError('points belong to a different sketch')
        ln = Line(self, self._new_eid(), a, b, name=name)
        self._lines.append(ln)
        return ln

    def circle(self, center: Point, radius, *, name: str | None = None) -> Circle:
        if center.sketch is not self:
            raise SketchError('center point belongs to a different sketch')
        c = Circle(self, self._new_eid(), center, radius, name=name)
        self._circles.append(c)
        return c

    # ---- geometric constraints ----

    def fix(self, pt: Point) -> 'Sketch':
        """Pin a point at its initial-guess (u, v)."""
        if pt.sketch is not self:
            raise SketchError('point belongs to a different sketch')
        pt.fix = True
        return self

    def horizontal(self, *lines: Line) -> 'Sketch':
        for ln in lines:
            self._geom_cons.append({'kind': 'horizontal', 'args': [ln]})
        return self

    def vertical(self, *lines: Line) -> 'Sketch':
        for ln in lines:
            self._geom_cons.append({'kind': 'vertical', 'args': [ln]})
        return self

    def parallel(self, l1: Line, l2: Line) -> 'Sketch':
        self._geom_cons.append({'kind': 'parallel', 'args': [l1, l2]})
        return self

    def perpendicular(self, l1: Line, l2: Line) -> 'Sketch':
        self._geom_cons.append({'kind': 'perpendicular', 'args': [l1, l2]})
        return self

    def coincident(self, e1, e2) -> 'Sketch':
        self._geom_cons.append({'kind': 'coincident', 'args': [e1, e2]})
        return self

    def equal(self, e1, e2) -> 'Sketch':
        """Equal length (line/line) or equal radius (circle/circle)."""
        self._geom_cons.append({'kind': 'equal', 'args': [e1, e2]})
        return self

    def tangent(self, e1, e2) -> 'Sketch':
        self._geom_cons.append({'kind': 'tangent', 'args': [e1, e2]})
        return self

    # ---- dimensional constraints ----

    def distance(self, e1, e2, value) -> 'Sketch':
        """Distance between two points, or between a point and a line."""
        self._dim_cons.append({'kind': 'distance', 'args': [e1, e2],
                               'expr': value, 'factor': 1.0})
        return self

    def length(self, line: Line, value) -> 'Sketch':
        return self.distance(line.start, line.end, value)

    def angle(self, l1: Line, l2: Line, value) -> 'Sketch':
        """Angle between two lines, in degrees."""
        self._dim_cons.append({'kind': 'angle', 'args': [l1, l2],
                               'expr': value, 'factor': 1.0})
        return self

    def diameter(self, circle: Circle, value) -> 'Sketch':
        self._dim_cons.append({'kind': 'diameter', 'args': [circle],
                               'expr': value, 'factor': 1.0})
        return self

    def radius(self, circle: Circle, value) -> 'Sketch':
        # solvespace exposes diameter; double the value at apply-time.
        self._dim_cons.append({'kind': 'diameter', 'args': [circle],
                               'expr': value, 'factor': 2.0})
        return self

    # ---- sugar ----

    def rectangle(self, corner=(0.0, 0.0), width=None, height=None,
                  *, fix_corner: bool = True, name: str | None = None):
        """Sugar: 4 points + 4 lines + H/V + 2 dim constraints.

        Returns the four corner points in CCW order: lower-left,
        lower-right, upper-right, upper-left.
        """
        if width is None or height is None:
            raise SketchError('rectangle requires both width and height')
        cx, cy = float(corner[0]), float(corner[1])
        # initial-guess sizes — used only as a starting point for the solver
        w0 = float(width) if not isinstance(width, str) else 50.0
        h0 = float(height) if not isinstance(height, str) else 30.0

        nm = lambda suf: f'{name}.{suf}' if name else None
        p1 = self.point(cx,      cy,      fix=fix_corner, name=nm('ll'))
        p2 = self.point(cx + w0, cy,      name=nm('lr'))
        p3 = self.point(cx + w0, cy + h0, name=nm('ur'))
        p4 = self.point(cx,      cy + h0, name=nm('ul'))
        l_b = self.line(p1, p2, name=nm('bottom'))
        l_r = self.line(p2, p3, name=nm('right'))
        l_t = self.line(p3, p4, name=nm('top'))
        l_l = self.line(p4, p1, name=nm('left'))
        self.horizontal(l_b, l_t)
        self.vertical(l_l, l_r)
        self.distance(p1, p2, width)
        self.distance(p1, p4, height)
        return [p1, p2, p3, p4]

    def polygon(self, points_uv, *, closed=True, fix_first=True):
        """Sugar: chain points into lines. Each (u, v) becomes a Point;
        consecutive pairs become Lines; if ``closed``, a final line
        closes back to the first point.

        No dimensional constraints are added — this is the "rough sketch"
        primitive; the caller adds geometric/dimensional constraints to
        pin it down.
        """
        pts = []
        for i, (u, v) in enumerate(points_uv):
            pts.append(self.point(u, v, fix=(fix_first and i == 0)))
        for i in range(len(pts) - 1):
            self.line(pts[i], pts[i + 1])
        if closed and len(pts) >= 3:
            self.line(pts[-1], pts[0])
        return pts

    # ---- solving ----

    def solve(self, params: dict | None = None) -> SolvedSketch:
        """Build a fresh solvespace system from the recipe, apply
        constraints (evaluating expressions against ``params``), solve,
        and return a SolvedSketch."""
        from python_solvespace import ResultFlag, SolverSystem

        ns = _eval_namespace(params or {})

        def ev(expr) -> float:
            if isinstance(expr, (int, float)):
                return float(expr)
            if isinstance(expr, str):
                return float(eval(expr, {'__builtins__': {}}, ns))
            raise TypeError(f'cannot eval {expr!r}')

        sys_ = SolverSystem()
        wp = sys_.create_2d_base()

        # eid → solvespace handle
        h_pt: dict[int, Any] = {}
        h_ln: dict[int, Any] = {}
        h_ci: dict[int, Any] = {}
        # Per-circle radius distance handle, kept so we can read it back.
        h_ci_rdist: dict[int, Any] = {}

        # Build entities in declaration order.
        for pt in self._points:
            h = sys_.add_point_2d(pt.u, pt.v, wp)
            h_pt[pt.eid] = h
            if pt.fix:
                sys_.dragged(h, wp)

        for ln in self._lines:
            h = sys_.add_line_2d(h_pt[ln.start.eid], h_pt[ln.end.eid], wp)
            h_ln[ln.eid] = h

        for c in self._circles:
            # Initial-guess radius for the solver. Dimensional constraints
            # (sk.diameter / sk.radius) will pin it down on solve.
            r0 = ev(c.radius) if c.radius is not None else 1.0
            nm = sys_.add_normal_2d(wp)
            rdist = sys_.add_distance(r0, wp)
            h = sys_.add_circle(nm, h_pt[c.center.eid], rdist, wp)
            h_ci[c.eid] = h
            h_ci_rdist[c.eid] = rdist

        def handle_of(ent):
            if isinstance(ent, Point):  return h_pt[ent.eid]
            if isinstance(ent, Line):   return h_ln[ent.eid]
            if isinstance(ent, Circle): return h_ci[ent.eid]
            raise SketchError(f'unknown entity {ent!r}')

        # Geometric constraints.
        for gc in self._geom_cons:
            kind = gc['kind']; args = gc['args']
            if kind == 'horizontal':
                sys_.horizontal(handle_of(args[0]), wp)
            elif kind == 'vertical':
                sys_.vertical(handle_of(args[0]), wp)
            elif kind == 'parallel':
                sys_.parallel(handle_of(args[0]), handle_of(args[1]), wp)
            elif kind == 'perpendicular':
                sys_.perpendicular(handle_of(args[0]), handle_of(args[1]), wp)
            elif kind == 'coincident':
                sys_.coincident(handle_of(args[0]), handle_of(args[1]), wp)
            elif kind == 'equal':
                sys_.equal(handle_of(args[0]), handle_of(args[1]), wp)
            elif kind == 'tangent':
                sys_.tangent(handle_of(args[0]), handle_of(args[1]), wp)
            else:
                raise SketchError(f'unknown geometric constraint {kind!r}')

        # Dimensional constraints.
        for dc in self._dim_cons:
            kind = dc['kind']; args = dc['args']
            value = ev(dc['expr']) * dc.get('factor', 1.0)
            if kind == 'distance':
                sys_.distance(handle_of(args[0]), handle_of(args[1]),
                              value, wp)
            elif kind == 'angle':
                sys_.angle(handle_of(args[0]), handle_of(args[1]),
                           value, wp)
            elif kind == 'diameter':
                sys_.diameter(handle_of(args[0]), value)
            else:
                raise SketchError(f'unknown dimensional constraint {kind!r}')

        result = sys_.solve()
        dof_after = sys_.dof()
        n_cons = sys_.cons_len()

        if result != ResultFlag.OKAY:
            failures = list(sys_.failures())
            raise SketchSolveError(
                f'sketch solve failed: result={ResultFlag(result).name}, '
                f'dof={dof_after}, failures={failures}',
                flag=ResultFlag(result), failures=failures,
            )

        # Read solved coordinates.
        solved_pts: dict[int, tuple[float, float]] = {}
        for pt in self._points:
            u, v = sys_.params(h_pt[pt.eid].params)
            solved_pts[pt.eid] = (float(u), float(v))

        solved_circles: list[SolvedCircle] = []
        for c in self._circles:
            cx, cy = solved_pts[c.center.eid]
            r = float(sys_.params(h_ci_rdist[c.eid].params)[0])
            solved_circles.append(SolvedCircle(cx, cy, r, name=c.name))

        profile = _chain_lines_to_profile(self._lines, solved_pts)

        return SolvedSketch(
            plane=self.plane,
            points=solved_pts,
            profile=profile,
            circles=solved_circles,
            dof=dof_after,
            n_constraints=n_cons,
        )


# =========================================================================
# Helpers
# =========================================================================

def _eval_namespace(params: dict) -> dict:
    """Build the eval namespace exposed to expression strings. Keep this
    in sync with `Design.E` so a sketch sees the same vocabulary as the
    rest of cadlang."""
    return {'math': math, 'pi': math.pi,
            'sin': math.sin, 'cos': math.cos, **params}


def _chain_lines_to_profile(lines, solved_pts):
    """Walk the line graph and return a single closed CCW polygon, or
    ``[]`` when the lines don't form exactly one closed chain.

    Best-effort: this is purely a convenience for downstream consumers
    (extrude / cut) that want a profile. Sketches that aren't a closed
    chain (open chain, disjoint chains, dangling edges) are perfectly
    valid as constraint systems — we just don't auto-extract a profile
    from them. The caller decides whether the empty result is an error.
    """
    if not lines:
        return []

    # Build adjacency: pid → list of (other_pid, line_index).
    adj: dict[int, list[tuple[int, int]]] = {}
    for i, ln in enumerate(lines):
        a, b = ln.start.eid, ln.end.eid
        adj.setdefault(a, []).append((b, i))
        adj.setdefault(b, []).append((a, i))

    # Single closed chain ⇔ every point has degree 2 AND |points| == |lines|.
    if any(len(nbrs) != 2 for nbrs in adj.values()):
        return []
    if len(adj) != len(lines):
        return []

    # Walk a cycle starting from lines[0].start.
    start_pid = lines[0].start.eid
    chain = [start_pid]
    visited_lines: set[int] = set()
    cur = start_pid
    while True:
        nxt = None
        for other, li in adj[cur]:
            if li in visited_lines:
                continue
            nxt = (other, li); break
        if nxt is None:
            break
        other, li = nxt
        visited_lines.add(li)
        if other == start_pid:
            break
        chain.append(other)
        cur = other

    # Disjoint cycles: we walked a sub-cycle but lines remain.
    if len(visited_lines) != len(lines):
        return []

    profile = [solved_pts[pid] for pid in chain]
    # Force CCW. manifold3d treats positive signed area as fill.
    if _signed_area(profile) < 0:
        profile = list(reversed(profile))
    return profile


def _signed_area(poly):
    """Shoelace; positive iff CCW."""
    n = len(poly)
    s = 0.0
    for i in range(n):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % n]
        s += x0 * y1 - x1 * y0
    return s / 2.0
