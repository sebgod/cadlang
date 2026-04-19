"""
cadlang/stepimport — STEP file reader, axisymmetric-feature recogniser, and
starter-.cad.py emitter.

ROADMAP §3. Parse a STEP AP203/AP214 text file, pull out cylindrical surfaces
and planes, recognise a uniform annulus as a revolve, cluster radial bores into
a circular-pattern cut, and write the result as a cadlang `.cad.py`.

CLI:
    python cadlang/stepimport.py path/to/thing.step                  # report
    python cadlang/stepimport.py path/to/thing.step -o out.cad.py    # emit

Assumes the STEP places entity geometry in a single global frame — true for
typical single-body Fusion exports. Nested SHAPE_REPRESENTATION transforms are
NOT resolved yet; if positions look translated/rotated, that's why.
Emitter v1 recognises a uniform annular revolve and 1 radial-bore group. More
complex geometry (tab bosses, non-annular profiles, slots) prints a TODO.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path
import numpy as np


# =========================================================================
# Raw STEP record parsing
# =========================================================================

_RECORD = re.compile(r'#(\d+)\s*=\s*([A-Z_0-9]+)\s*\((.*?)\)\s*;', re.S)


def parse(text: str) -> dict[int, tuple[str, list]]:
    """Return {ref_number: (type_name, parsed_arg_list)}."""
    out: dict[int, tuple[str, list]] = {}
    for m in _RECORD.finditer(text):
        ref = int(m.group(1))
        typ = m.group(2)
        args = _split_args(m.group(3))
        out[ref] = (typ, args)
    return out


def _split_args(s: str) -> list:
    """Split a STEP arg list by top-level commas, respecting parens / strings."""
    out = []
    depth = 0
    buf = []
    in_str = False
    i = 0
    while i < len(s):
        c = s[i]
        if in_str:
            buf.append(c)
            if c == "'":
                in_str = False
            i += 1
            continue
        if c == "'":
            in_str = True
            buf.append(c)
        elif c == '(':
            depth += 1
            buf.append(c)
        elif c == ')':
            depth -= 1
            buf.append(c)
        elif c == ',' and depth == 0:
            out.append(_coerce(''.join(buf).strip()))
            buf = []
        else:
            buf.append(c)
        i += 1
    if buf:
        out.append(_coerce(''.join(buf).strip()))
    return out


def _coerce(tok: str):
    """Convert a single arg token to ref-int / float / string / tuple / None."""
    if not tok:
        return None
    if tok.startswith('#'):
        return int(tok[1:])
    if tok.startswith("'") and tok.endswith("'"):
        return tok[1:-1]
    if tok.startswith('(') and tok.endswith(')'):
        return tuple(_split_args(tok[1:-1]))
    if tok == '.T.':
        return True
    if tok == '.F.':
        return False
    if tok.startswith('.') and tok.endswith('.'):
        return tok[1:-1]  # enum value
    try:
        return float(tok)
    except ValueError:
        return tok


# =========================================================================
# Geometric helpers
# =========================================================================

def point3(db, ref):
    typ, args = db[ref]
    assert typ == 'CARTESIAN_POINT', typ
    return np.array(args[1], dtype=float)


def direction3(db, ref):
    typ, args = db[ref]
    assert typ == 'DIRECTION', typ
    v = np.array(args[1], dtype=float)
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def axis2_frame(db, ref):
    """Return (origin, z_axis, x_axis) for an AXIS2_PLACEMENT_3D."""
    typ, args = db[ref]
    assert typ == 'AXIS2_PLACEMENT_3D', typ
    origin = point3(db, args[1])
    z = direction3(db, args[2]) if args[2] is not None else np.array([0., 0., 1.])
    x = direction3(db, args[3]) if args[3] is not None else np.array([1., 0., 0.])
    return origin, z, x


# =========================================================================
# Feature extraction
# =========================================================================

def cylinders(db):
    """Yield dicts with axis origin, axis direction, radius for every CYLINDRICAL_SURFACE.

    `ref_x` is the cylinder's local 0-degree direction (perpendicular to the
    axis) — needed to compute angular coordinates for trim analysis."""
    for ref, (typ, args) in db.items():
        if typ != 'CYLINDRICAL_SURFACE':
            continue
        placement = args[1]
        radius = float(args[2])
        origin, z, x = axis2_frame(db, placement)
        yield {
            'ref': ref,
            'origin': origin,
            'axis': z,
            'ref_x': x,
            'radius': radius,
        }


def planes(db):
    for ref, (typ, args) in db.items():
        if typ != 'PLANE':
            continue
        origin, z, x = axis2_frame(db, args[1])
        yield {'ref': ref, 'origin': origin, 'normal': z}


def brep_surface_refs(db, brep_ref):
    """Return set of surface refs for a MANIFOLD_SOLID_BREP."""
    typ, args = db[brep_ref]
    assert typ == 'MANIFOLD_SOLID_BREP', typ
    shell_t, shell_args = db[args[1]]
    assert shell_t == 'CLOSED_SHELL', shell_t
    out = set()
    for fr in shell_args[1]:
        f_t, f_args = db[fr]
        if f_t == 'ADVANCED_FACE':
            out.add(f_args[2])
    return out


def brep_points(db, brep_ref):
    """Collect VERTEX_POINT positions for a brep — only actual solid vertices,
    not axis-placement reference points (cylinder/plane origins are not real
    corners of the geometry and would pollute Z-level detection)."""
    typ, args = db[brep_ref]
    if typ != 'MANIFOLD_SOLID_BREP':
        return np.zeros((0, 3))
    shell_t, shell_args = db[args[1]]
    if shell_t != 'CLOSED_SHELL':
        return np.zeros((0, 3))
    pts = []
    for face_ref in shell_args[1]:
        f_t, f_args = db[face_ref]
        if f_t != 'ADVANCED_FACE':
            continue
        for br in f_args[1]:
            b_t, b_args = db[br]
            if b_t not in ('FACE_BOUND', 'FACE_OUTER_BOUND'):
                continue
            l_t, l_args = db[b_args[1]]
            if l_t != 'EDGE_LOOP':
                continue
            for oe_ref in l_args[1]:
                oe_t, oe_args = db[oe_ref]
                if oe_t != 'ORIENTED_EDGE':
                    continue
                e_t, e_args = db[oe_args[3]]
                if e_t != 'EDGE_CURVE':
                    continue
                for vref in (e_args[1], e_args[2]):
                    v_t, v_args = db[vref]
                    if v_t == 'VERTEX_POINT':
                        pts.append(point3(db, v_args[1]))
    return np.array(pts) if pts else np.zeros((0, 3))


def breps(db):
    """Yield (brep_ref, cylinders[], planes[], points[]) for every MANIFOLD_SOLID_BREP."""
    all_cyls = list(cylinders(db))
    all_pls = list(planes(db))
    for ref, (typ, _) in db.items():
        if typ != 'MANIFOLD_SOLID_BREP':
            continue
        surf_refs = brep_surface_refs(db, ref)
        bcyls = [c for c in all_cyls if c['ref'] in surf_refs]
        bpls = [p for p in all_pls if p['ref'] in surf_refs]
        bpts = brep_points(db, ref)
        yield ref, bcyls, bpls, bpts


def _surface_face_index(db):
    """surface_ref -> list of ADVANCED_FACE refs using that surface."""
    idx = {}
    for ref, (typ, args) in db.items():
        if typ == 'ADVANCED_FACE':
            idx.setdefault(args[2], []).append(ref)
    return idx


def _face_edge_refs(db, face_ref):
    """Yield EDGE_CURVE refs for each edge of this face."""
    typ, args = db[face_ref]
    if typ != 'ADVANCED_FACE':
        return
    for br in args[1]:
        b_t, b_args = db[br]
        if b_t not in ('FACE_BOUND', 'FACE_OUTER_BOUND'):
            continue
        l_t, l_args = db[b_args[1]]
        if l_t != 'EDGE_LOOP':
            continue
        for oe_ref in l_args[1]:
            oe_t, oe_args = db[oe_ref]
            if oe_t == 'ORIENTED_EDGE':
                yield oe_args[3]


def _edge_vertex_points(db, edge_ref):
    """Return the two endpoint positions of an EDGE_CURVE."""
    typ, args = db[edge_ref]
    if typ != 'EDGE_CURVE':
        return []
    out = []
    for vref in (args[1], args[2]):
        v_t, v_args = db[vref]
        if v_t == 'VERTEX_POINT':
            out.append(point3(db, v_args[1]))
    return out


def cylinder_depth(db, cyl, face_idx):
    """Measure a finite cylinder's axial depth.

    Tries CIRCLE trim curves first (gives clean exact depth for simple holes).
    Falls back to projecting edge VERTEX_POINTs onto the axis — works when
    Fusion exported trim curves as B-splines instead of circles.
    """
    faces = face_idx.get(cyl['ref'], [])
    if not faces:
        return None
    axis = cyl['axis']; origin = cyl['origin']; r = cyl['radius']
    projections = []
    for fr in faces:
        for edge_ref in _face_edge_refs(db, fr):
            e_t, e_args = db[edge_ref]
            if e_t != 'EDGE_CURVE':
                continue
            # Prefer CIRCLE curves — cleanest, gives bounding circle center
            curve_ref = e_args[3]
            c_t, c_args = db[curve_ref]
            if c_t == 'CIRCLE' and abs(float(c_args[2]) - r) < 0.01:
                c_origin, _, _ = axis2_frame(db, c_args[1])
                projections.append(float(np.dot(c_origin - origin, axis)))
                continue
            # Fallback: edge endpoints
            for pt in _edge_vertex_points(db, edge_ref):
                projections.append(float(np.dot(pt - origin, axis)))
    if len(projections) < 2:
        return None
    return abs(max(projections) - min(projections))


# =========================================================================
# Reporting
# =========================================================================

def report(path: Path):
    text = path.read_text(encoding='utf-8', errors='ignore')
    db = parse(text)
    cyls = list(cylinders(db))
    pls = list(planes(db))
    print(f'[{path.name}] entities={len(db)} cylinders={len(cyls)} planes={len(pls)}')

    # Classify cylinders by axis direction (axial vs radial vs other) and radius.
    # Axial = axis parallel to global Z; radial = axis in global XY plane; other = off-axis.
    axial, radial, other = [], [], []
    for c in cyls:
        az = abs(c['axis'][2])
        axy = np.hypot(c['axis'][0], c['axis'][1])
        if az > 0.99:
            axial.append(c)
        elif axy > 0.99:
            radial.append(c)
        else:
            other.append(c)

    print(f'  axial cyls (|axis_z|≈1): {len(axial)}')
    print(f'  radial cyls (axis in XY): {len(radial)}')
    if other:
        print(f'  off-axis cyls: {len(other)}')

    if axial:
        print('\n  axial cylinders by radius:')
        by_r = _cluster(sorted(axial, key=lambda c: c['radius']), key='radius', tol=0.05)
        for rr, group in by_r:
            z_range = (min(c['origin'][2] for c in group), max(c['origin'][2] for c in group))
            print(f'    r={rr:7.3f}  n={len(group):3d}  z∈[{z_range[0]:.2f}, {z_range[1]:.2f}]')

    if radial:
        print('\n  radial cylinders by radius (likely bolt/insert bores):')
        by_r = _cluster(sorted(radial, key=lambda c: c['radius']), key='radius', tol=0.05)
        for rr, group in by_r:
            print(f'    r={rr:7.3f}  n={len(group):3d}:')
            # For a radial cylinder, axis direction points outward from the Z-axis
            # at the hole's angular position. Azimuth comes from the axis DIRECTION,
            # not from the placement origin (which is an arbitrary point on the line).
            rows = []
            for c in group:
                ax = c['axis']
                phi = float(np.degrees(np.arctan2(ax[1], ax[0])) % 360)
                rows.append((phi, c['origin'][2], c['ref']))
            rows.sort()
            for phi, z, ref in rows:
                print(f'      #{ref:<4d}  phi={phi:6.2f}°  z={z:7.3f}')

    if pls:
        # report z of all planes whose normal is ±Z, since those bound axial extents
        z_planes = [p for p in pls if abs(p['normal'][2]) > 0.99]
        if z_planes:
            zs = sorted({round(p['origin'][2], 3) for p in z_planes})
            print(f'\n  Z-normal planes at z = {zs}')


def _cluster(items, key, tol):
    """Group consecutive items whose `key` value is within tol of the group average."""
    out = []
    cur_k = None
    cur = []
    for it in items:
        v = it[key]
        if cur and abs(v - cur_k) <= tol:
            cur.append(it)
            cur_k = sum(c[key] for c in cur) / len(cur)
        else:
            if cur:
                out.append((cur_k, cur))
            cur = [it]
            cur_k = v
    if cur:
        out.append((cur_k, cur))
    return out


# =========================================================================
# Feature inference (STEP entities -> cadlang-shaped design)
# =========================================================================

def infer_bodies(db, base_name: str):
    """Classify every MANIFOLD_SOLID_BREP in `db` as a revolve or an extrude
    body. Returns list of body dicts; each is ready for `emit_cadpy`.
    """
    face_idx = _surface_face_index(db)
    out = []
    for i, (brep_ref, bcyls, bpls, bpts) in enumerate(breps(db), 1):
        kind = _classify_body(bcyls, bpls)
        suffix = '' if len(list(_count_breps(db))) <= 1 else f'_{i}'
        body_name = base_name + suffix
        if kind == 'revolve':
            out.append(_infer_revolve_body(bcyls, bpls, body_name, brep_ref, db, face_idx))
        elif kind == 'extrude':
            out.append(_infer_extrude_body(bcyls, bpls, bpts, body_name, brep_ref, db, face_idx))
        else:
            print(f'[stepimport] brep #{brep_ref}: unknown body type, skipping')
    return out


def _count_breps(db):
    for ref, (typ, _) in db.items():
        if typ == 'MANIFOLD_SOLID_BREP':
            yield ref


def _classify_body(bcyls, bpls):
    """'revolve' if there's a Z-axial cylinder pair with sensible ID/OD radii,
    else 'extrude' if there are ≥2 Z-normal planes forming a prism, else None.
    """
    axial = [c for c in bcyls if abs(c['axis'][2]) > 0.99 and c['radius'] > 10]
    if len({round(c['radius'], 3) for c in axial}) >= 2:
        return 'revolve'
    z_pls = [p for p in bpls if abs(p['normal'][2]) > 0.99]
    if len({round(p['origin'][2], 3) for p in z_pls}) >= 2:
        return 'extrude'
    return None


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n else None


def _radial_bore_groups(bcyls, db=None, face_idx=None):
    """Cluster radial cylinders (r>=1mm) in a brep into hole groups."""
    radial = [c for c in bcyls
              if abs(c['axis'][2]) < 0.01
              and np.hypot(c['axis'][0], c['axis'][1]) > 0.99
              and 1.0 <= c['radius'] < 10]
    radial.sort(key=lambda c: c['radius'])
    groups = []
    for rr, grp in _cluster(radial, key='radius', tol=0.05):
        zs = sorted({round(c['origin'][2], 3) for c in grp})
        axis_phis = {round(np.degrees(np.arctan2(c['axis'][1], c['axis'][0])) % 180, 1)
                     for c in grp}
        depth = None
        if db is not None and face_idx is not None:
            depths = [cylinder_depth(db, c, face_idx) for c in grp]
            depths = [d for d in depths if d is not None]
            depth = _median(depths) if depths else None
        groups.append({
            'radius_mm': rr,
            'z_positions': zs,
            'pattern_count': 2 * len(axis_phis),
            'raw_count': len(grp),
            'depth_mm': depth,
        })
    groups.sort(key=lambda g: -g['raw_count'])
    return groups


def _cylinder_axis_span(db, cyl, face_idx):
    """Return (min_proj, max_proj) of bounding vertex points projected onto
    the cylinder axis, in world units offset from the axis placement origin."""
    faces = face_idx.get(cyl['ref'], [])
    if not faces:
        return None, None
    axis = cyl['axis']; origin = cyl['origin']
    projections = []
    for fr in faces:
        for edge_ref in _face_edge_refs(db, fr):
            e_t, e_args = db[edge_ref]
            if e_t != 'EDGE_CURVE':
                continue
            for pt in _edge_vertex_points(db, edge_ref):
                projections.append(float(np.dot(pt - origin, axis)))
    if len(projections) < 2:
        return None, None
    return min(projections), max(projections)


def _cylinder_angular_coverage_deg(db, cyls, face_idx):
    """Return the angular coverage (in degrees, 0..360) of all face patches
    on the cylinders in `cyls` (expected to share a cylinder axis line).
    Used to tell a shallow saddle surface (narrow arc) from an actual
    through-cut (~360°).

    Method: gather edge-vertex points on every face, project each into the
    plane perpendicular to the axis, compute its polar angle around
    cyl['ref_x'], and report `360 - (largest gap between consecutive
    sorted angles)`. A continuous face covering [265°, 275°] reports 10°;
    a full cylinder represented as two 180° halves reports ~180° per half
    or ~360° when both halves are combined.
    """
    if not cyls:
        return None
    # Use the first cylinder's frame as the reference (they're co-axial by
    # grouping).
    axis = np.array(cyls[0]['axis'])
    origin = np.array(cyls[0]['origin'])
    rx_raw = cyls[0].get('ref_x')
    if rx_raw is None:
        # No explicit ref_x stored — pick any vector perpendicular to the axis.
        ref_x = np.array([0.0, 1.0, 0.0]) if abs(axis[0]) > 0.9 else np.array([1.0, 0.0, 0.0])
    else:
        ref_x = np.array(rx_raw, dtype=float)
    # Gram-Schmidt to guarantee perpendicularity.
    ref_x = ref_x - np.dot(ref_x, axis) * axis
    if np.linalg.norm(ref_x) < 1e-9:
        return None
    ref_x = ref_x / np.linalg.norm(ref_x)
    axis_y = np.cross(axis, ref_x)

    phis: list[float] = []
    for c in cyls:
        faces = face_idx.get(c['ref'], [])
        for fr in faces:
            for edge_ref in _face_edge_refs(db, fr):
                e_t, _ = db[edge_ref]
                if e_t != 'EDGE_CURVE':
                    continue
                for pt in _edge_vertex_points(db, edge_ref):
                    perp = pt - origin - np.dot(pt - origin, axis) * axis
                    u = float(np.dot(perp, ref_x))
                    v = float(np.dot(perp, axis_y))
                    if abs(u) < 1e-9 and abs(v) < 1e-9:
                        continue
                    phi = float(np.degrees(np.arctan2(v, u))) % 360.0
                    phis.append(phi)
    uniq = sorted({round(p, 2) for p in phis})
    if len(uniq) < 2:
        return None
    # Largest gap around the circle = uncovered region; coverage is the
    # complement.
    gaps = [uniq[i + 1] - uniq[i] for i in range(len(uniq) - 1)]
    gaps.append(uniq[0] + 360.0 - uniq[-1])   # wrap gap
    return 360.0 - max(gaps)


def _saddle_cut_groups(bcyls, db=None, face_idx=None):
    """Detect lateral-axis large-radius cylinders that represent concave
    cut-outs (e.g. the rail's ring-mating saddle).

    Returns a list of dicts: {radius_mm, axis_dir ('X' or 'Y'), center_uv,
    start, end, raw_count}.  `start`/`end` are world positions along the
    axis direction, bounding the cut."""
    cands = [c for c in bcyls
             if abs(c['axis'][2]) < 0.01
             and np.hypot(c['axis'][0], c['axis'][1]) > 0.99
             and c['radius'] > 10]
    if not cands:
        return []

    def _key(c):
        ax = c['axis']
        # Canonicalise axis direction — pick the +X or +Y hemisphere
        if abs(ax[0]) > 0.99:
            sign = 1 if ax[0] > 0 else -1
            dir_key = 'X'
        elif abs(ax[1]) > 0.99:
            sign = 1 if ax[1] > 0 else -1
            dir_key = 'Y'
        else:
            return None
        # Line key: project origin into the plane perpendicular to the axis.
        if dir_key == 'X':
            uv = (round(c['origin'][1], 3), round(c['origin'][2], 3))
        else:
            uv = (round(c['origin'][0], 3), round(c['origin'][2], 3))
        return (dir_key, uv, round(c['radius'], 3), sign)

    groups: dict = {}
    for c in cands:
        k = _key(c)
        if k is None:
            continue
        # collapse opposite-direction entries onto the same group (same axis line)
        positive_k = k[:3] + (1,)
        groups.setdefault(positive_k, []).append(c)

    out = []
    for (dir_key, uv, r), grp in [(k[:3], g) for k, g in groups.items()]:
        # world span along the axis
        lo_world, hi_world = None, None
        if db is not None and face_idx is not None:
            for c in grp:
                lo, hi = _cylinder_axis_span(db, c, face_idx)
                if lo is None:
                    continue
                # offset by the projection of the axis-placement origin itself
                # onto the axis direction — gives world coords along that axis
                ax = np.array(c['axis'])
                origin_proj = float(np.dot(c['origin'], ax))
                canonical_sign = 1.0
                if abs(ax[0]) > 0.99 and ax[0] < 0:
                    canonical_sign = -1.0
                if abs(ax[1]) > 0.99 and ax[1] < 0:
                    canonical_sign = -1.0
                lo_canon = canonical_sign * (lo + origin_proj)
                hi_canon = canonical_sign * (hi + origin_proj)
                a, b = sorted([lo_canon, hi_canon])
                lo_world = a if lo_world is None else min(lo_world, a)
                hi_world = b if hi_world is None else max(hi_world, b)
        coverage = None
        if db is not None and face_idx is not None:
            coverage = _cylinder_angular_coverage_deg(db, grp, face_idx)
        out.append({
            'radius_mm': r,
            'axis_dir': dir_key,
            'center_uv': uv,
            'start': lo_world,
            'end': hi_world,
            'raw_count': len(grp),
            'arc_coverage_deg': coverage,
            # `narrow_arc`: the face spans < ~60° of the cylinder, which
            # means the STEP encodes a shallow surface patch rather than
            # a through-cut. Whether cadlang can faithfully emit it as a
            # `cut(Circle)` depends on how much of the body the infinite
            # cylinder would carve — that gate lives in the emitter
            # (`_emit_extrude_cadpy`), because it needs the body's
            # z-extent. A narrow-arc cylinder producing a shallow carve
            # (0-20% of body thickness) is emitted as-is: the infinite-
            # cylinder approximation is close enough. A narrow-arc
            # cylinder that would over-carve (e.g. sad1 on the dewshield
            # rail, 50% of body thickness) is the one we can't express
            # yet, so it's skipped with a warning.
            'narrow_arc': (coverage is not None and coverage < 60.0),
        })
    # largest-radius groups first (likely the main saddle)
    out.sort(key=lambda g: -g['radius_mm'])
    return out


def _axial_bore_groups(bcyls, db=None, face_idx=None):
    """Cluster axial (|axis_z|=1) small cylinders in a brep as axial bores."""
    ax = [c for c in bcyls if abs(c['axis'][2]) > 0.99 and 1.0 <= c['radius'] < 10]
    ax.sort(key=lambda c: c['radius'])
    groups = []
    for rr, grp in _cluster(ax, key='radius', tol=0.05):
        positions = [(round(c['origin'][0], 3), round(c['origin'][1], 3))
                     for c in grp]
        zs = sorted({round(c['origin'][2], 3) for c in grp})
        depth = None
        if db is not None and face_idx is not None:
            depths = [cylinder_depth(db, c, face_idx) for c in grp]
            depths = [d for d in depths if d is not None]
            depth = _median(depths) if depths else None
        groups.append({
            'radius_mm': rr,
            'positions_xy': positions,
            'z_positions': zs,
            'raw_count': len(grp),
            'depth_mm': depth,
        })
    groups.sort(key=lambda g: -g['raw_count'])
    return groups


def _infer_revolve_body(bcyls, bpls, body_name, brep_ref, db=None, face_idx=None):
    axial = [c for c in bcyls if abs(c['axis'][2]) > 0.99 and c['radius'] > 10]
    body_radii = sorted({round(c['radius'], 3) for c in axial})
    ring_id = 2 * min(body_radii)
    ring_od = 2 * max(body_radii)

    z_planes = sorted({round(p['origin'][2], 3) for p in bpls
                       if abs(p['normal'][2]) > 0.99})
    z_base = z_planes[0]
    # pick the Z-plane that covers all small radial-hole z-positions
    small_radial_z = sorted(c['origin'][2] for c in bcyls
                            if abs(c['axis'][2]) < 0.01 and c['radius'] < 10)
    candidates = [z for z in z_planes if z > z_base + 1.0]
    if small_radial_z and candidates:
        valid = [z for z in candidates if z >= max(small_radial_z) - 0.01]
        z_top = min(valid) if valid else candidates[0]
    else:
        z_top = candidates[0] if candidates else z_planes[-1]
    return {
        'kind': 'revolve',
        'name': body_name,
        'brep_ref': brep_ref,
        'ring_id': ring_id,
        'ring_od': ring_od,
        'ring_h': z_top - z_base,
        'z_base': z_base,
        'hole_groups': _radial_bore_groups(bcyls, db, face_idx),
    }


def _detect_z_levels(bpts, tol=0.5):
    """Cluster distinct Z values in the point cloud. Adjacent values within
    `tol` are merged into a single representative level."""
    zs = sorted(bpts[:, 2].tolist())
    levels = []
    for z in zs:
        if not levels or z - levels[-1] > tol:
            levels.append(float(z))
    return levels


def _bbox_xy_near(bpts, z_target, tol=0.5):
    """XY bbox of brep points whose z is within `tol` of z_target."""
    mask = np.abs(bpts[:, 2] - z_target) < tol
    band = bpts[mask]
    if len(band) == 0:
        return None
    return (float(band[:, 0].min()), float(band[:, 1].min()),
            float(band[:, 0].max()), float(band[:, 1].max()))


def _infer_extrude_body(bcyls, bpls, bpts, body_name, brep_ref, db=None, face_idx=None):
    """Build a stacked-extrude body. Points are clustered by Z; each slab between
    adjacent Z levels becomes one extrude layer, with the slab's XY footprint
    taken from the points near its top face.
    """
    if len(bpts) == 0:
        raise ValueError(f'no points found for extrude brep #{brep_ref}')
    levels = _detect_z_levels(bpts)
    if len(levels) < 2:
        raise ValueError(f'extrude brep #{brep_ref}: need >=2 z levels, got {levels}')

    layers = []
    for i in range(len(levels) - 1):
        z_low, z_high = levels[i], levels[i + 1]
        # For each slab use the top-face bbox — captures the "upper profile" of the
        # slab. Falls back to the bottom-face bbox (which is always defined) if
        # the top face hasn't got vertex points at that exact z.
        bbox = _bbox_xy_near(bpts, z_high) or _bbox_xy_near(bpts, z_low)
        if bbox is None:
            continue
        xmin, ymin, xmax, ymax = bbox
        layers.append({
            'z_base': z_low,
            'thickness': z_high - z_low,
            'profile': [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)],
        })

    return {
        'kind': 'extrude',
        'name': body_name,
        'brep_ref': brep_ref,
        'layers': layers,
        'hole_groups_axial': _axial_bore_groups(bcyls, db, face_idx),
        'lateral_cuts': _saddle_cut_groups(bcyls, db, face_idx),
    }


# Back-compat shim: old CLI callers used infer_design(db, name) for a single ring.
def infer_design(db, name: str):
    bodies = infer_bodies(db, name)
    revs = [b for b in bodies if b['kind'] == 'revolve']
    if not revs:
        raise ValueError('no revolve body found in STEP')
    return revs[0]


# =========================================================================
# Emitter: design dict -> cadlang .cad.py source
# =========================================================================

def emit_cadpy(body, source_step: str) -> str:
    """Render a body dict (revolve or extrude) as a cadlang .cad.py script."""
    if body['kind'] == 'revolve':
        return _emit_revolve_cadpy(body, source_step)
    if body['kind'] == 'extrude':
        return _emit_extrude_cadpy(body, source_step)
    raise ValueError(f'unknown body kind {body["kind"]!r}')


def _emit_header(name: str, slug: str, source_step: str) -> list[str]:
    L = []
    L.append('"""')
    L.append(f'{name} — imported from {source_step} by cadlang.stepimport.')
    L.append('')
    L.append('Review params + feature list, then re-run this script to regenerate')
    L.append(f'{slug}.g.stl, {slug}.g_preview.png, and {slug}.g_fusion.py alongside it.')
    L.append('(The `.g.` infix marks these outputs as coming from an auto-generated')
    L.append('.g.cad.py source, distinct from hand-written parts.)')
    L.append('"""')
    L.append('import sys, pathlib')
    L.append('HERE = pathlib.Path(__file__).resolve().parent')
    L.append('# Walk up to find cadlang.py — supports nested project layouts.')
    L.append('for _p in [HERE, *HERE.parents]:')
    L.append('    if (_p / "cadlang.py").is_file():')
    L.append('        sys.path.insert(0, str(_p))')
    L.append('        break')
    L.append('')
    L.append('from cadlang import Design, Rect, Circle, OffsetPlane, Circular')
    L.append('')
    return L


def _emit_params_dict(name: str, params: list) -> list[str]:
    """params: list of (key, value_expr, trailing_comment)"""
    L = []
    L.append(f"d = Design(name={name!r}, units='mm', params={{")
    key_w = max((len(repr(k)) for k, _, _ in params), default=0)
    for k, v, comment in params:
        line = f"    {repr(k):<{key_w}}: {v},"
        if comment:
            line += f'  # {comment}'
        L.append(line)
    L.append('})')
    L.append('')
    return L


def _emit_footer(slug: str) -> list[str]:
    # Generated (.g.cad.py) sources carry the `.g.` infix through to their
    # outputs so they don't collide with hand-written parts of the same stem.
    return [
        "if __name__ == '__main__':",
        f"    d.emit_stl(str(HERE / '{slug}.g.stl'),",
        f"               render_png=str(HERE / '{slug}.g_preview.png'))",
        f"    d.emit_fusion(str(HERE / '{slug}.g_fusion.py'))",
    ]


def _emit_revolve_cadpy(body, source_step: str) -> str:
    name = body['name']; slug = _slug(name)
    params = [
        ('ring_id', f'{body["ring_id"]:.3f}', ''),
        ('ring_od', f'{body["ring_od"]:.3f}', ''),
        ('ring_h',  f'{body["ring_h"]:.3f}',  ''),
    ]
    hgs = body['hole_groups']
    group_prefix = []
    for i, hg in enumerate(hgs):
        prefix = 'hole' if i == 0 else f'hole{i + 1}'
        group_prefix.append(prefix)
        params.append((f'{prefix}_dia', f'{2 * hg["radius_mm"]:.3f}', ''))
        if hg.get('depth_mm') is not None:
            params.append((f'{prefix}_depth', f'{hg["depth_mm"]:.3f}',
                           'measured from STEP trim circles'))
        else:
            params.append((f'{prefix}_depth', '4.000',
                           'TODO: depth fallback — STEP trim curves were not circles'))
        for j, z in enumerate(hg['z_positions'], 1):
            params.append((f'{prefix}_z{j}', f'{z:.3f}', ''))

    L = _emit_header(name, slug, source_step)
    L += _emit_params_dict(name, params)
    L.append('# Base revolve: uniform annulus (STEP showed one ID + one OD cylinder).')
    L.append("d.revolve(name='base', plane='XZ', axis='Z', profile=[")
    L.append("    ('ring_id/2', 0),")
    L.append("    ('ring_od/2', 0),")
    L.append("    ('ring_od/2', 'ring_h'),")
    L.append("    ('ring_id/2', 'ring_h'),")
    L.append('])')
    L.append('')
    for i, (hg, prefix) in enumerate(zip(hgs, group_prefix)):
        cut_name = 'heat_inserts' if i == 0 else f'bores_group_{i+1}'
        L.append(f'# Radial bores (group {i+1}): {hg["raw_count"]} surfaces, '
                 f'{len(hg["z_positions"])} axial positions, '
                 f'inferred pattern count={hg["pattern_count"]}.')
        L.append('d.cut(')
        L.append(f"    name={cut_name!r},")
        L.append("    on=OffsetPlane(base='YZ', distance='ring_od/2'),")
        L.append('    sketch=[')
        for j in range(len(hg['z_positions'])):
            L.append(f"        Circle(center=(0, {prefix + f'_z{j+1}'!r}), "
                     f"radius={prefix + '_dia/2'!r}),")
        L.append('    ],')
        L.append(f"    depth={'-' + prefix + '_depth'!r},")
        L.append(f"    pattern=Circular(axis='Z', count={hg['pattern_count']}),")
        L.append(')')
        L.append('')
    L += _emit_footer(slug)
    return '\n'.join(L) + '\n'


def _emit_extrude_cadpy(body, source_step: str) -> str:
    """Emit a layered extrude with saddle-shaped tops.

    Each layer is extruded TALL and then its matching lateral cylinder
    (matched by x-extent) is subtracted — that carves the layer's top
    into the concave shape the STEP's cylindrical face prescribes.
    Doing this per-layer (instead of all-layers-then-all-cuts) is the
    key: an infinite-cylinder `cut(Circle)` applied to a short slab
    merely grazes its top; applied to a tall slab it carves the
    intended concave surface without reaching into the layers above.
    """
    name = body['name']; slug = _slug(name)
    layers = body['layers']
    lats = body.get('lateral_cuts', [])
    hgs = body.get('hole_groups_axial', [])

    # Match each saddle to the layer whose x-extent it matches (within 0.5 mm).
    saddle_to_layer: list[int | None] = [None] * len(lats)
    for si, lc in enumerate(lats):
        if lc.get('start') is None or lc.get('end') is None:
            continue
        for li, layer in enumerate(layers):
            (x0, _), (x1, _), _, _ = layer['profile']
            if abs(lc['start'] - x0) < 0.5 and abs(lc['end'] - x1) < 0.5:
                saddle_to_layer[si] = li
                break

    # Extrude height for each layer. Must be:
    #   - ABOVE every saddle cylinder's lower arc at the body's y-edges,
    #     so the cylinder cut reaches across the full layer top, and
    #   - BELOW every saddle cylinder's upper arc, so the cut doesn't
    #     leave a "shelf" of material above the cylinder.
    # The first constraint gives a small number (typ. 6-12 mm for rail-
    # sized parts); the second gives a very large number (z_c + r, ~256
    # for the SWQ8 rail). Any value in between works. We pick
    # body_z_top + 5 mm — comfortably above any real layer top but far
    # below the cylinder tops, so the resulting shapes are what the
    # STEP intended: concave-topped slabs, no stray top shelves.
    body_z_top = 0.0
    for layer in layers:
        body_z_top = max(body_z_top, layer['z_base'] + layer['thickness'])
    tall_height = body_z_top + 5.0

    # --- Params ---
    params = []
    for i, layer in enumerate(layers, 1):
        (x0, y0), (x1, _), (_, y1), _ = layer['profile']
        params += [
            (f'L{i}_x0', f'{x0:.3f}', ''),
            (f'L{i}_x1', f'{x1:.3f}', ''),
            (f'L{i}_y0', f'{y0:.3f}', ''),
            (f'L{i}_y1', f'{y1:.3f}', ''),
            (f'L{i}_t',  f'{layer["thickness"]:.3f}', 'original slab thickness (pre-carve)'),
            (f'L{i}_z0', f'{layer["z_base"]:.3f}', ''),
        ]
    params.append(('tall', f'{tall_height:.3f}',
                   'extrude height for each layer — top is carved to final '
                   'z by the saddle cuts; this just needs to be big enough'))

    for i, hg in enumerate(hgs):
        prefix = 'hole' if i == 0 else f'hole{i + 1}'
        params.append((f'{prefix}_dia', f'{2 * hg["radius_mm"]:.3f}', ''))
        if hg.get('depth_mm') is not None:
            params.append((f'{prefix}_depth', f'{hg["depth_mm"]:.3f}',
                           'measured from STEP trim edges'))
        else:
            params.append((f'{prefix}_depth', '4.000',
                           'TODO: depth fallback — STEP trim curves were not circles'))
        for j, (hx, hy) in enumerate(hg['positions_xy'], 1):
            params.append((f'{prefix}_x{j}', f'{hx:.3f}', ''))
            params.append((f'{prefix}_y{j}', f'{hy:.3f}', ''))

    for si, lc in enumerate(lats, 1):
        if saddle_to_layer[si - 1] is None:
            continue
        u, v = lc['center_uv']
        params += [
            (f'sad{si}_r', f'{lc["radius_mm"]:.3f}', f'{lc["axis_dir"]}-axis lateral cut radius'),
            (f'sad{si}_u', f'{u:.3f}', 'center in sketch plane (axis-perp coord 1)'),
            (f'sad{si}_v', f'{v:.3f}', 'center in sketch plane (axis-perp coord 2)'),
            (f'sad{si}_a', f'{lc["start"]:.3f}', 'start along axis'),
            (f'sad{si}_b', f'{lc["end"]:.3f}', 'end along axis'),
        ]

    # --- Body text ---
    L = _emit_header(name, slug, source_step)
    L += _emit_params_dict(name, params)

    L.append(f'# {len(layers)} stacked layer(s). Each extrudes TALL (to z={tall_height:.0f}),')
    L.append(f'# then its matching saddle cut carves the concave top. L1 gets the')
    L.append(f'# full-length saddle (r=125.2 on the dewshield rail), L2 gets the')
    L.append(f'# middle-only saddle (r=120), etc. Step height comes from the')
    L.append(f'# L{{i}}_z0 base offsets; concave curvature comes from sad{{i}}_r.')
    L.append('')
    for i, layer in enumerate(layers, 1):
        z_top_orig = layer['z_base'] + layer['thickness']
        L.append(f'# Layer {i}: z_base = {layer["z_base"]:.3f}, '
                 f'original slab top was {z_top_orig:.3f} (pre-carve)')
        layer_name = 'base' if i == 1 else f'layer_{i}'
        plane_arg = "'XY'" if i == 1 and layer['z_base'] == 0 \
            else f"OffsetPlane(base='XY', distance='L{i}_z0')"
        L.append(f"d.extrude(name={layer_name!r}, on={plane_arg}, profile=[")
        L.append(f"    ('L{i}_x0', 'L{i}_y0'),")
        L.append(f"    ('L{i}_x1', 'L{i}_y0'),")
        L.append(f"    ('L{i}_x1', 'L{i}_y1'),")
        L.append(f"    ('L{i}_x0', 'L{i}_y1'),")
        L.append(f"], height='tall')")
        L.append('')

        # Saddles matched to this layer — emitted immediately so the
        # cylinder carves THIS layer's top, not the next layer's body.
        for si, lc in enumerate(lats, 1):
            if saddle_to_layer[si - 1] != i - 1:
                continue
            base = 'YZ' if lc['axis_dir'] == 'X' else 'XZ'
            dist_expr = f'sad{si}_a'
            depth_expr = f'sad{si}_b - sad{si}_a'
            cov = lc.get('arc_coverage_deg')
            cov_txt = f' arc={cov:.1f}°' if cov is not None else ''
            L.append(f'# Lateral cut {si}: r={lc["radius_mm"]:.3f} cylinder carves '
                     f'layer {i}\'s top concave.{cov_txt}')
            L.append('d.cut(')
            L.append(f"    name={'saddle' if si == 1 else f'saddle_{si}'!r},")
            L.append(f"    on=OffsetPlane(base={base!r}, distance={dist_expr!r}),")
            L.append('    sketch=[')
            L.append(f"        Circle(center=({f'sad{si}_u'!r}, {f'sad{si}_v'!r}), "
                     f"radius={f'sad{si}_r'!r}),")
            L.append('    ],')
            L.append(f"    depth={depth_expr!r},")
            L.append(')')
            L.append('')

    unmatched = [si for si, m in enumerate(saddle_to_layer, 1) if m is None and lats[si - 1].get('start') is not None]
    if unmatched:
        L.append(f'# NOTE: {len(unmatched)} lateral cylindrical face(s) had no')
        L.append('# x-extent match to any layer and were skipped:')
        for si in unmatched:
            lc = lats[si - 1]
            L.append(f'#   #{si}: {lc["axis_dir"]}-axis r={lc["radius_mm"]:.3f}, '
                     f'x=[{lc.get("start"):.3f}, {lc.get("end"):.3f}]')
        L.append('')

    # Axial bores (bolt holes) — anchor at L1's original top, z = L1_z0 + L1_t.
    for i, hg in enumerate(hgs):
        prefix = 'hole' if i == 0 else f'hole{i + 1}'
        cut_name = 'bolt_holes' if i == 0 else f'{prefix}_holes'
        rd_expr = f'{prefix}_dia/2'
        depth_expr = f'-{prefix}_depth'
        L.append(f'# Axial bores (group {i+1}): {hg["raw_count"]} holes.')
        L.append('d.cut(')
        L.append(f"    name={cut_name!r},")
        L.append("    on=OffsetPlane(base='XY', distance='L1_z0 + L1_t'),")
        L.append('    sketch=[')
        for j in range(len(hg['positions_xy'])):
            cx = f'{prefix}_x{j+1}'
            cy = f'{prefix}_y{j+1}'
            L.append(f"        Circle(center=({cx!r}, {cy!r}), radius={rd_expr!r}),")
        L.append('    ],')
        L.append(f"    depth={depth_expr!r},")
        L.append(')')
        L.append('')

    L += _emit_footer(slug)
    return '\n'.join(L) + '\n'


def _slug(name: str) -> str:
    return re.sub(r'[^A-Za-z0-9_]+', '_', name).strip('_') or 'part'


# =========================================================================
# CLI
# =========================================================================

def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description='STEP -> cadlang')
    ap.add_argument('step_file')
    ap.add_argument('-o', '--out',
                    help='output target: a .cad.py filename for single-body STEPs, '
                         'or a directory to write one .cad.py per body')
    ap.add_argument('--name', help='base design name (default: STEP filename stem)')
    args = ap.parse_args(argv[1:])

    path = Path(args.step_file)
    report(path)
    if args.out:
        text = path.read_text(encoding='utf-8', errors='ignore')
        db = parse(text)
        base_name = args.name or re.sub(r'[^A-Za-z0-9]+', '', path.stem)
        bodies = infer_bodies(db, base_name)
        out = Path(args.out)
        if len(bodies) == 1 and out.suffix == '.py':
            out.write_text(emit_cadpy(bodies[0], path.name), encoding='utf-8')
            print(f'\n[stepimport] wrote {out}')
        else:
            out.mkdir(parents=True, exist_ok=True)
            # Imported parts get `.g.cad.py` — the `.g.` infix marks them as
            # auto-generated (vs hand-written `.cad.py`).
            for i, body in enumerate(bodies, 1):
                imported_name = f'{base_name}_part{i:03d}'
                body['name'] = imported_name
                slug = _slug(imported_name)
                dst = out / f'{slug}.g.cad.py'
                dst.write_text(emit_cadpy(body, path.name), encoding='utf-8')
                print(f'[stepimport] wrote {dst}  ({body["kind"]})')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
