"""
cadlang — minimal declarative CAD description, Python-as-DSL.

A Design = name + params + ordered features. Features reference parameters
by name via expression strings ('ring_od/2 + chamfer_flare').

Backends:
  emit_stl(path, render_png=...)  -> STL via manifold3d CSG + trimesh export
  emit_fusion(path)               -> Fusion 360 Python script

Supported feature set:
  - revolve (plane='XZ', axis='Z'): closed 2D profile -> body of revolution
  - cut on top_face(body) with Rect sketch: rectangular slot from the top face
  - cut on OffsetPlane(YZ, distance) with Circle sketch: radial cylindrical hole
  - Circular pattern around Z for any cut
"""
from __future__ import annotations
import json
import math
import numpy as np


# =========================================================================
# DSL classes
# =========================================================================

class Design:
    def __init__(self, name: str, units: str = 'mm', params: dict | None = None):
        self.name = name
        self.units = units
        self.params = dict(params or {})
        self.features: list[dict] = []
        # Optional labelled measurements, shown in the GUI viewer next to
        # the STL. Populated by `measurements(...)`; serialized as a
        # `<stl_stem>.measurements.json` sidecar by `emit_stl`.
        self._measurements: list[tuple[str, dict]] = []

    # ------ feature constructors ------
    def revolve(self, name, plane, axis, profile):
        self.features.append({'op': 'revolve', 'name': name, 'plane': plane,
                              'axis': axis, 'profile': list(profile)})
        return self

    def extrude(self, name, on, height, profile=None, sketch=None):
        """Extrude a closed 2D polygon into a prismatic body.

        ``on`` is ``'XY'`` (at z=0) or ``OffsetPlane(base='XY', distance=expr)``.
        ``height`` is signed; positive extrudes in +plane-normal direction.

        Provide exactly one of:

        * ``profile=[(u, v), ...]`` — explicit point list in plane-local
          coords. Each entry is a number or expression string. Same
          legacy API as before.
        * ``sketch=Sketch(...)`` — a constraint-based sketch whose
          ``solve()`` produces the closed loop at emit time.
        """
        if (profile is None) == (sketch is None):
            raise ValueError('extrude() needs exactly one of profile= or sketch=')
        feat = {'op': 'extrude', 'name': name, 'on': on, 'height': height,
                'profile': list(profile) if profile is not None else None,
                'sketch': sketch}
        self.features.append(feat)
        return self

    def cut(self, name, on, sketch, depth, pattern=None):
        self.features.append({'op': 'cut', 'name': name, 'on': on,
                              'sketch': list(sketch), 'depth': depth,
                              'pattern': pattern})
        return self

    def top_face(self, body_name):
        return {'ref': 'top_face', 'body': body_name}

    def measurements(self, *sections):
        """Declare labelled measurements to surface in the GUI.

        Each section is a ``(title, rows_dict)`` pair. ``rows_dict`` maps a
        label to a numeric value or an expression string evaluated against
        ``params``. Example::

            d.measurements(
                ('Overall', {'ID': 'ring_id', 'OD': 'ring_od',
                             'height': 'ring_h'}),
                ('Heat inserts', {'count': 4, 'Ø': 'hole_dia',
                                  'depth': 'hole_depth'}),
            )
        """
        for sec in sections:
            title, rows = sec
            self._measurements.append((str(title), dict(rows)))
        return self

    # ------ expression eval (mm) ------
    def E(self, expr):
        if isinstance(expr, (int, float)):
            return float(expr)
        if isinstance(expr, str):
            ns = {'math': math, 'pi': math.pi, 'sin': math.sin,
                  'cos': math.cos, **self.params}
            return float(eval(expr, {'__builtins__': {}}, ns))
        raise TypeError(f'cannot eval {expr!r}')

    # ------ backends ------
    def emit_stl(self, path, render_png=None, n_seg=360):
        mesh = _build_mesh(self, n_seg)
        mesh.export(path)
        print(f'cadlang[stl] wrote {path}  verts={len(mesh.vertices)} '
              f'faces={len(mesh.faces)} watertight={mesh.is_watertight} '
              f'vol={mesh.volume/1000:.1f} cm^3')
        if render_png:
            _render(mesh, render_png, title=self.name)
            print(f'cadlang[render] wrote {render_png}')
        if self._measurements:
            meas_path = _measurements_path_for(path)
            doc = self._measurements_doc(mesh)
            with open(meas_path, 'w', encoding='utf-8') as f:
                json.dump(doc, f, indent=2)
            print(f'cadlang[measurements] wrote {meas_path}')

    def _measurements_doc(self, mesh=None):
        ns = {'math': math, 'pi': math.pi, 'sin': math.sin,
              'cos': math.cos, **self.params}

        def ev(expr):
            if isinstance(expr, str):
                try:
                    return eval(expr, {'__builtins__': {}}, ns)
                except Exception:
                    return expr
            return expr

        def ev_point(pt):
            return [round(float(ev(c)), 4) for c in pt]

        sections = []
        for title, rows in self._measurements:
            out_rows = []
            for label, value in rows.items():
                # Row value may be (a) a number, (b) an expr string, or
                # (c) a dict {'value': expr, 'anchor': {...}} attaching a
                # 3D dimension line.
                anchor = None
                if isinstance(value, dict) and 'value' in value:
                    anchor_raw = value.get('anchor')
                    value = value['value']
                    if anchor_raw:
                        anchor = {
                            'kind': anchor_raw.get('kind', 'linear'),
                            'from': ev_point(anchor_raw['from']),
                            'to':   ev_point(anchor_raw['to']),
                        }
                v = ev(value) if isinstance(value, str) else value
                if isinstance(v, float):
                    v = round(v, 4)
                row = {'label': str(label), 'value': v}
                if anchor is not None:
                    row['anchor'] = anchor
                out_rows.append(row)
            sections.append({'title': title, 'rows': out_rows})
        # Always-included derived stats from the built mesh, when we have one.
        if mesh is not None:
            try:
                bbox = mesh.bounds  # [[xmin, ymin, zmin], [xmax, ymax, zmax]]
                size = bbox[1] - bbox[0]
                sections.append({
                    'title': 'Mesh',
                    'rows': [
                        {'label': 'bbox x', 'value': round(float(size[0]), 3)},
                        {'label': 'bbox y', 'value': round(float(size[1]), 3)},
                        {'label': 'bbox z', 'value': round(float(size[2]), 3)},
                        {'label': 'volume', 'value': round(float(mesh.volume) / 1000.0, 3),
                         'unit': 'cm³'},
                    ],
                })
            except Exception:
                pass
        return {'name': self.name, 'units': self.units, 'sections': sections}

    def emit_fusion(self, path):
        code = _emit_fusion(self)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(code)
        print(f'cadlang[fusion] wrote {path}')


class Rect:
    def __init__(self, x, y):
        self.x = tuple(x); self.y = tuple(y)

class Circle:
    def __init__(self, center, radius):
        self.center = tuple(center); self.radius = radius

class OffsetPlane:
    def __init__(self, base, distance):
        self.base = base
        self.distance = distance

class Circular:
    def __init__(self, axis, count, total_angle='360 deg'):
        self.axis = axis; self.count = int(count); self.total_angle = total_angle


# =========================================================================
# STL backend
# =========================================================================

def _resolve_profile(d: Design, feat: dict):
    """Resolve a feature's profile into a list of numeric (u, v) pairs.

    Supports both the legacy ``profile=[...]`` path (each entry is an
    expression string or number) and the new ``sketch=Sketch(...)`` path
    (solvespace-backed constraint solver). Solved sketch results are
    cached on the feature dict so the Fusion backend can re-use them
    without solving twice."""
    sk = feat.get('sketch')
    if sk is not None:
        cached = feat.get('_solved')
        if cached is None:
            cached = sk.solve(d.params)
            feat['_solved'] = cached
        if not cached.profile:
            raise ValueError(
                f'extrude {feat["name"]!r}: sketch has no closed line '
                f'profile (circle-only profiles not yet supported in '
                f'extrude — use cut() with a Circle sketch)')
        return cached.profile
    return [(d.E(u), d.E(v)) for (u, v) in feat['profile']]


def _measurements_path_for(stl_path: str) -> str:
    """Return the sidecar JSON path for a given STL path. Strips a trailing
    ``.stl`` so `foo.g.stl` → `foo.g.measurements.json`."""
    base = str(stl_path)
    if base.lower().endswith('.stl'):
        base = base[:-4]
    return base + '.measurements.json'


def _build_mesh(d: Design, n_seg: int):
    """Build a watertight mesh by applying features in order via manifold3d CSG."""
    import trimesh
    import manifold3d as m3d

    body = None
    v_top = None  # axial extent of the base revolve, used by top_face cuts

    for feat in d.features:
        if feat['op'] == 'revolve':
            if feat['plane'] != 'XZ' or feat['axis'] != 'Z':
                raise NotImplementedError('STL: only plane=XZ axis=Z revolve')
            profile = [(d.E(u), d.E(v)) for (u, v) in feat['profile']]
            cs = m3d.CrossSection([profile], fillrule=m3d.FillRule.Positive)
            m = m3d.Manifold.revolve(cs, circular_segments=n_seg)
            body = m if body is None else body + m
            v_top = max(v for (_, v) in profile) if v_top is None \
                else max(v_top, max(v for (_, v) in profile))
        elif feat['op'] == 'extrude':
            m = _build_extrude(d, feat)
            body = m if body is None else body + m
        elif feat['op'] == 'cut':
            if body is None:
                raise ValueError('STL: cut before any body feature')
            tool = _build_cut_tool(d, feat, n_seg, v_top)
            if tool is None:
                print(f'cadlang[stl] skipping cut {feat["name"]!r} (unsupported)')
                continue
            body = body - tool
        else:
            print(f'cadlang[stl] skipping feature {feat["name"]!r} (unknown op {feat["op"]!r})')

    if body is None:
        raise ValueError('STL: no body built')

    md = body.to_mesh()
    verts = np.asarray(md.vert_properties, dtype=float)[:, :3]
    faces = np.asarray(md.tri_verts, dtype=np.int64)
    mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=True)
    mesh.merge_vertices()
    return mesh


def _build_extrude(d: Design, feat: dict):
    """Build a prismatic Manifold from a closed 2D profile on an XY-parallel plane."""
    import manifold3d as m3d

    on = feat['on']
    profile = _resolve_profile(d, feat)
    h = d.E(feat['height'])
    if h == 0:
        raise ValueError(f'extrude height must be non-zero (got {h})')

    if on == 'XY':
        z0 = 0.0
    elif isinstance(on, OffsetPlane) and on.base == 'XY':
        z0 = d.E(on.distance)
    else:
        raise NotImplementedError(f'extrude on plane {on!r} not supported; use "XY" or OffsetPlane("XY", ...)')

    cs = m3d.CrossSection([profile], fillrule=m3d.FillRule.Positive)
    # CrossSection.extrude needs positive height; build in +Z then translate.
    m = m3d.Manifold.extrude(cs, abs(h))
    if h < 0:
        m = m.translate([0, 0, -abs(h)])
    if z0 != 0:
        m = m.translate([0, 0, z0])
    return m


def _build_cut_tool(d: Design, feat: dict, n_seg: int, v_top):
    """Return a Manifold representing the volume to subtract, or None if unsupported."""
    import manifold3d as m3d

    on = feat['on']
    depth = d.E(feat['depth'])  # signed mm; negative = into the body
    eps = 1e-3                   # slight overshoot to avoid coplanar boolean glitches

    parts: list = []

    if isinstance(on, dict) and on.get('ref') == 'top_face':
        if v_top is None:
            return None
        for s in feat['sketch']:
            if isinstance(s, Rect):
                x0, x1 = sorted([d.E(s.x[0]), d.E(s.x[1])])
                y0, y1 = sorted([d.E(s.y[0]), d.E(s.y[1])])
                dx, dy = x1 - x0, y1 - y0
                dz = abs(depth) + 2 * eps
                box = m3d.Manifold.cube([dx, dy, dz], center=False)
                # top face is at Z=v_top; depth<0 goes down into the body
                z0 = v_top - abs(depth) - eps if depth < 0 else v_top - eps
                box = box.translate([x0, y0, z0])
                parts.append(box)
            else:
                print(f'cadlang[stl] cut {feat["name"]!r}: unsupported top_face sketch {type(s).__name__}')
                return None

    elif isinstance(on, OffsetPlane):
        dist = d.E(on.distance)
        if on.base == 'YZ':
            for s in feat['sketch']:
                if isinstance(s, Circle):
                    cu, cv = d.E(s.center[0]), d.E(s.center[1])
                    r = d.E(s.radius)
                    # Sketch on YZ at X=dist; cylinder axis along X.
                    h = abs(depth) + 2 * eps
                    cyl = m3d.Manifold.cylinder(h, r, r, n_seg)
                    cyl = cyl.rotate([0, 90, 0])  # +Z cylinder -> +X cylinder
                    x_start = dist - abs(depth) - eps if depth < 0 else dist - eps
                    cyl = cyl.translate([x_start, cu, cv])
                    parts.append(cyl)
                else:
                    print(f'cadlang[stl] cut {feat["name"]!r}: unsupported YZ-plane sketch {type(s).__name__}')
                    return None
        elif on.base == 'XY':
            for s in feat['sketch']:
                if isinstance(s, Circle):
                    cu, cv = d.E(s.center[0]), d.E(s.center[1])
                    r = d.E(s.radius)
                    # Sketch on XY at Z=dist; cylinder axis along Z (manifold default).
                    h = abs(depth) + 2 * eps
                    cyl = m3d.Manifold.cylinder(h, r, r, n_seg)
                    z_start = dist - abs(depth) - eps if depth < 0 else dist - eps
                    cyl = cyl.translate([cu, cv, z_start])
                    parts.append(cyl)
                else:
                    print(f'cadlang[stl] cut {feat["name"]!r}: unsupported XY-plane sketch {type(s).__name__}')
                    return None
        else:
            print(f'cadlang[stl] cut {feat["name"]!r}: OffsetPlane base {on.base!r} not supported')
            return None
    else:
        return None

    tool = _union(parts)
    pattern = feat['pattern']
    if pattern is not None:
        tool = _apply_circular(tool, pattern)
    return tool


def _union(parts):
    if not parts:
        return None
    out = parts[0]
    for p in parts[1:]:
        out = out + p
    return out


def _apply_circular(tool, pattern):
    """Replicate `tool` N times around the pattern axis, spaced evenly over total_angle."""
    if tool is None or pattern.count <= 1:
        return tool
    total = _parse_angle_deg(pattern.total_angle)
    step = total / pattern.count  # matches Fusion's isSymmetric=False full-circle convention
    result = tool
    for k in range(1, pattern.count):
        ang = step * k
        if pattern.axis == 'Z':
            rv = [0, 0, ang]
        elif pattern.axis == 'Y':
            rv = [0, ang, 0]
        elif pattern.axis == 'X':
            rv = [ang, 0, 0]
        else:
            raise NotImplementedError(f'pattern axis {pattern.axis!r}')
        result = result + tool.rotate(rv)
    return result


def _parse_angle_deg(expr):
    if isinstance(expr, (int, float)):
        return float(expr)
    s = str(expr).strip()
    if s.endswith('deg'):
        return float(s[:-3].strip())
    if s.endswith('rad'):
        return math.degrees(float(s[:-3].strip()))
    return float(s)


def _render(mesh, path, title=''):
    """3-pane matplotlib preview. View angles adapt to bbox aspect.

    Note: matplotlib's Poly3DCollection uses painter's-algorithm depth sorting
    and struggles with thin prismatic parts (walls can occlude face cutouts).
    For geometry verification, trust the STL and the Fusion script; the PNG is
    a quick sanity check only.
    """
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    b = mesh.bounds
    dx, dy, dz = b[1] - b[0]
    # If one XY dimension dominates (thin bar): use three iso angles rather than
    # straight-down top/bottom (which matplotlib renders poorly for flat parts
    # with through-holes).
    flat = dz < 0.3 * max(dx, dy)
    if flat:
        views = [(20, -55, 'iso'), (20, 35, 'iso alt'), (60, -90, 'plan')]
    else:
        views = [(22, -55, 'iso'), (90, -90, 'top'), (-90, -90, 'bottom')]

    fig = plt.figure(figsize=(14, 6))

    def draw(ax, elev, azim, sub):
        tri = mesh.vertices[mesh.faces]
        nrm = mesh.face_normals
        light = np.array([0.35, -0.55, 0.85]); light /= np.linalg.norm(light)
        shade = np.clip(nrm @ light, 0.2, 1.0)
        col = np.stack([shade*0.82, shade*0.55, shade*0.35, np.ones_like(shade)], axis=1)
        ax.add_collection3d(Poly3DCollection(tri, facecolors=col,
                                             edgecolors='none'))
        ax.set_xlim(b[0,0], b[1,0]); ax.set_ylim(b[0,1], b[1,1]); ax.set_zlim(b[0,2], b[1,2])
        try: ax.set_box_aspect(b[1] - b[0])
        except Exception: pass
        ax.view_init(elev=elev, azim=azim); ax.set_axis_off()
        ax.set_title(sub, fontsize=10)

    for i, (elev, azim, sub) in enumerate(views):
        draw(fig.add_subplot(1, 3, i + 1, projection='3d'), elev, azim, sub)
    fig.suptitle(title, fontsize=12); fig.tight_layout()
    fig.savefig(path, dpi=140, bbox_inches='tight', facecolor='white')


# =========================================================================
# Fusion 360 backend
# =========================================================================

_PLANE_ATTR = {'XY': 'xYConstructionPlane', 'XZ': 'xZConstructionPlane',
               'YZ': 'yZConstructionPlane'}
_AXIS_ATTR  = {'X': 'xConstructionAxis',  'Y': 'yConstructionAxis',
               'Z': 'zConstructionAxis'}


def _expr_str(e, units='mm'):
    """Render an expression for createByString."""
    if isinstance(e, (int, float)):
        return f'{e} {units}'
    return str(e)


def _emit_fusion(d: Design) -> str:
    E = d.E
    L = []
    w = L.append
    w('# Auto-generated by cadlang. Regenerate from the .cad.py description')
    w('# Usage: Utilities -> Scripts and Add-Ins -> Scripts -> Create -> From Existing -> this file.')
    w('')
    w('import adsk.core, adsk.fusion, adsk.cam, traceback, math')
    w('')
    w('def run(context):')
    w('    ui = None')
    w('    try:')
    w('        app = adsk.core.Application.get(); ui = app.userInterface')
    w('        design = adsk.fusion.Design.cast(app.activeProduct)')
    w('        if not design: ui.messageBox("Open a Fusion design first"); return')
    w('        design.designType = adsk.fusion.DesignTypes.ParametricDesignType')
    w('        root = design.rootComponent; params = design.userParameters')
    w('')
    w('        def P(n, expr, units="mm", comment=""):')
    w('            it = params.itemByName(n)')
    w('            vi = adsk.core.ValueInput.createByString(f"{expr} {units}")')
    w('            if it: it.expression = f"{expr} {units}"')
    w('            else: params.add(n, vi, units, comment)')
    w('')
    for k, v in d.params.items():
        w(f'        P({k!r}, {v!r}, {d.units!r})')
    w('')

    body_name = None
    for f in d.features:
        if f['op'] == 'revolve':
            body_name = f['name']
            _emit_revolve(w, d, f)
        elif f['op'] == 'extrude':
            body_name = f['name']
            _emit_extrude(w, d, f)
        elif f['op'] == 'cut':
            _emit_cut(w, d, f, body_name)

    w('')
    w(f'        ui.messageBox("cadlang: {d.name} created")')
    w('    except:')
    w('        if ui: ui.messageBox(traceback.format_exc())')
    return '\n'.join(L)


def _emit_revolve(w, d, f):
    w(f'        # ---- revolve {f["name"]!r} ----')
    w(f'        sk = root.sketches.add(root.{_PLANE_ATTR[f["plane"]]})')
    w(f'        sk.name = {f["name"]+"_profile"!r}')
    w('        _L = sk.sketchCurves.sketchLines')
    pts_cm = []
    for u, v in f['profile']:
        uu, vv = d.E(u) / 10.0, d.E(v) / 10.0
        pts_cm.append(f'adsk.core.Point3D.create({uu:.6f}, 0, {vv:.6f})')
    w('        _P = [' + ', '.join(pts_cm) + ']')
    w('        for i in range(len(_P)): _L.addByTwoPoints(_P[i], _P[(i+1) % len(_P)])')
    w('        prof = sk.profiles.item(0)')
    w(f'        _rI = root.features.revolveFeatures.createInput(prof, root.{_AXIS_ATTR[f["axis"]]}, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)')
    w('        _rI.setAngleExtent(False, adsk.core.ValueInput.createByString("360 deg"))')
    w('        body = root.features.revolveFeatures.add(_rI).bodies.item(0)')
    w(f'        body.name = {f["name"]!r}')


def _emit_extrude(w, d, f):
    w(f'        # ---- extrude {f["name"]!r} ----')
    on = f['on']
    if on == 'XY':
        w('        sk = root.sketches.add(root.xYConstructionPlane)')
    elif isinstance(on, OffsetPlane) and on.base == 'XY':
        dist_cm = d.E(on.distance) / 10.0
        w('        _cpI = root.constructionPlanes.createInput()')
        w(f'        _cpI.setByOffset(root.xYConstructionPlane, adsk.core.ValueInput.createByReal({dist_cm:.6f}))')
        w(f'        _pl = root.constructionPlanes.add(_cpI); _pl.name = {f["name"]+"_plane"!r}')
        w('        sk = root.sketches.add(_pl)')
    else:
        w(f'        # UNSUPPORTED extrude plane {on!r}')
        return
    w(f'        sk.name = {f["name"]+"_profile"!r}')

    if f.get('sketch') is not None:
        _emit_sketch_geometry(w, d, f)
    else:
        w('        _L = sk.sketchCurves.sketchLines')
        pts_cm = []
        for u, v in f['profile']:
            uu, vv = d.E(u) / 10.0, d.E(v) / 10.0
            pts_cm.append(f'adsk.core.Point3D.create({uu:.6f}, {vv:.6f}, 0)')
        w('        _P = [' + ', '.join(pts_cm) + ']')
        w('        for i in range(len(_P)): _L.addByTwoPoints(_P[i], _P[(i+1) % len(_P)])')

    w('        prof = sk.profiles.item(0)')
    w(f'        _eI = root.features.extrudeFeatures.createInput(prof, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)')
    w(f'        _eI.setDistanceExtent(False, adsk.core.ValueInput.createByString({_expr_str(f["height"], d.units)!r}))')
    w('        body = root.features.extrudeFeatures.add(_eI).bodies.item(0)')
    w(f'        body.name = {f["name"]!r}')


def _emit_sketch_geometry(w, d, f):
    """Emit Fusion native sketch entities + geometric constraints +
    sketch dimensions for a feature whose ``sketch=`` is a
    ``sketch.Sketch``. Assumes a Fusion ``sk`` variable already exists
    in the emitted code, holding an ``adsk.fusion.Sketch`` on the right
    plane.

    Produces parametric sketches: dimension expressions reference
    ``userParameters`` by name when the cadlang expression does.
    """
    from sketch import Circle as _SkCircle
    from sketch import Line as _SkLine
    from sketch import Point as _SkPoint

    sketch = f['sketch']
    cached = f.get('_solved')
    if cached is None:
        cached = sketch.solve(d.params)
        f['_solved'] = cached
    pts_uv = cached.points              # eid → (u, v) in mm

    w('        _L  = sk.sketchCurves.sketchLines')
    w('        _C  = sk.sketchCurves.sketchCircles')
    w('        _GC = sk.geometricConstraints')
    w('        _SD = sk.sketchDimensions')

    # eid → emitted-variable expression. SketchPoints are referenced as
    # ``_lnN.startSketchPoint`` / ``_ciN.centerSketchPoint`` etc.
    sp_var: dict[int, str] = {}
    ln_var: dict[int, str] = {}
    cir_var: dict[int, str] = {}

    # ---- Lines (re-using already-emitted SketchPoints when an endpoint
    # is shared with a previously emitted line). Sharing the SketchPoint
    # at construction time saves an explicit coincident constraint. ----
    for i, ln in enumerate(sketch._lines):
        a, b = ln.start.eid, ln.end.eid
        ax, ay = pts_uv[a]; bx, by = pts_uv[b]
        ax_cm, ay_cm = ax / 10.0, ay / 10.0
        bx_cm, by_cm = bx / 10.0, by / 10.0
        v = f'_ln{i}'
        ln_var[ln.eid] = v
        a_have = a in sp_var; b_have = b in sp_var
        if a_have and b_have:
            w(f'        {v} = _L.addByTwoPoints({sp_var[a]}, {sp_var[b]})')
        elif a_have:
            w(f'        {v} = _L.addByTwoPoints({sp_var[a]}, '
              f'adsk.core.Point3D.create({bx_cm:.6f}, {by_cm:.6f}, 0))')
            sp_var[b] = f'{v}.endSketchPoint'
        elif b_have:
            w(f'        {v} = _L.addByTwoPoints('
              f'adsk.core.Point3D.create({ax_cm:.6f}, {ay_cm:.6f}, 0), '
              f'{sp_var[b]})')
            sp_var[a] = f'{v}.startSketchPoint'
        else:
            w(f'        {v} = _L.addByTwoPoints('
              f'adsk.core.Point3D.create({ax_cm:.6f}, {ay_cm:.6f}, 0), '
              f'adsk.core.Point3D.create({bx_cm:.6f}, {by_cm:.6f}, 0))')
            sp_var[a] = f'{v}.startSketchPoint'
            sp_var[b] = f'{v}.endSketchPoint'

    # ---- Circles. Take initial center+radius from the cadlang solve.
    # Order of cached.circles matches sketch._circles. ----
    for i, c in enumerate(sketch._circles):
        sc = cached.circles[i]
        cx_cm, cy_cm = sc.cx / 10.0, sc.cy / 10.0
        r_cm = sc.r / 10.0
        v = f'_ci{i}'; cir_var[c.eid] = v
        w(f'        {v} = _C.addByCenterRadius('
          f'adsk.core.Point3D.create({cx_cm:.6f}, {cy_cm:.6f}, 0), {r_cm:.6f})')
        if c.center.eid not in sp_var:
            sp_var[c.center.eid] = f'{v}.centerSketchPoint'

    # ---- Standalone Points that aren't endpoints of any line/circle:
    # add as sketch points so later constraints have something to refer
    # to. ----
    for pt in sketch._points:
        if pt.eid not in sp_var:
            ux_cm, uy_cm = pts_uv[pt.eid][0] / 10.0, pts_uv[pt.eid][1] / 10.0
            v = f'_p{pt.eid}'
            w(f'        {v} = sk.sketchPoints.add('
              f'adsk.core.Point3D.create({ux_cm:.6f}, {uy_cm:.6f}, 0))')
            sp_var[pt.eid] = v

    # ---- Fix points (Fusion: SketchPoint.isFixed = True) ----
    for pt in sketch._points:
        if pt.fix:
            w(f'        {sp_var[pt.eid]}.isFixed = True')

    # ---- Helper: resolve a sketch entity to its emitted Fusion var ----
    def ent_var(ent):
        if isinstance(ent, _SkPoint):  return sp_var[ent.eid]
        if isinstance(ent, _SkLine):   return ln_var[ent.eid]
        if isinstance(ent, _SkCircle): return cir_var[ent.eid]
        raise ValueError(f'unknown sketch entity {ent!r}')

    # ---- Geometric constraints ----
    for gc in sketch._geom_cons:
        kind = gc['kind']; args = gc['args']
        if kind == 'horizontal':
            w(f'        _GC.addHorizontal({ent_var(args[0])})')
        elif kind == 'vertical':
            w(f'        _GC.addVertical({ent_var(args[0])})')
        elif kind == 'parallel':
            w(f'        _GC.addParallel({ent_var(args[0])}, {ent_var(args[1])})')
        elif kind == 'perpendicular':
            w(f'        _GC.addPerpendicular({ent_var(args[0])}, {ent_var(args[1])})')
        elif kind == 'coincident':
            w(f'        _GC.addCoincident({ent_var(args[0])}, {ent_var(args[1])})')
        elif kind == 'equal':
            w(f'        _GC.addEqual({ent_var(args[0])}, {ent_var(args[1])})')
        elif kind == 'tangent':
            w(f'        _GC.addTangent({ent_var(args[0])}, {ent_var(args[1])})')
        else:
            w(f'        # UNSUPPORTED geometric constraint {kind!r}')

    # ---- Dimensional constraints. Set dim.parameter.expression so the
    # dimension references a userParameter when cadlang did. ----
    for j, dc in enumerate(sketch._dim_cons):
        kind = dc['kind']; args = dc['args']
        ex_str = _fusion_dim_expr(dc['expr'], dc.get('factor', 1.0), d.units)
        dvar = f'_d{j}'
        if kind == 'distance':
            ax, ay = pts_uv[args[0].eid]; bx, by = pts_uv[args[1].eid]
            tx_cm = ((ax + bx) / 2 + 5.0) / 10.0
            ty_cm = ((ay + by) / 2 + 5.0) / 10.0
            w(f'        {dvar} = _SD.addDistanceDimension({ent_var(args[0])}, '
              f'{ent_var(args[1])}, '
              f'adsk.fusion.DimensionOrientations.AlignedDimensionOrientation, '
              f'adsk.core.Point3D.create({tx_cm:.6f}, {ty_cm:.6f}, 0))')
            w(f'        {dvar}.parameter.expression = {ex_str!r}')
        elif kind == 'angle':
            w(f'        {dvar} = _SD.addAngularDimension({ent_var(args[0])}, '
              f'{ent_var(args[1])}, adsk.core.Point3D.create(0, 0, 0))')
            w(f'        {dvar}.parameter.expression = {ex_str!r}')
        elif kind == 'diameter':
            sc = next(sc for sc, c in zip(cached.circles, sketch._circles)
                      if c.eid == args[0].eid)
            tx_cm = (sc.cx + sc.r) / 10.0
            ty_cm = sc.cy / 10.0
            w(f'        {dvar} = _SD.addDiameterDimension({ent_var(args[0])}, '
              f'adsk.core.Point3D.create({tx_cm:.6f}, {ty_cm:.6f}, 0))')
            w(f'        {dvar}.parameter.expression = {ex_str!r}')
        else:
            w(f'        # UNSUPPORTED dimensional constraint {kind!r}')


def _fusion_dim_expr(expr, factor, units):
    """Render a sketch-dim expression suitable for
    ``SketchDimension.parameter.expression``. References to user
    parameters pass through verbatim; numeric literals get a unit
    suffix so Fusion knows how to interpret them."""
    if factor == 1.0:
        if isinstance(expr, str):
            return expr
        return f'{float(expr)} {units}'
    # factor ≠ 1: wrap the expression. Fusion's expression parser handles
    # arithmetic on userParameters cleanly.
    if isinstance(expr, str):
        return f'({expr}) * {factor}'
    return f'{float(expr) * factor} {units}'


def _emit_cut(w, d, f, body_name):
    w(f'        # ---- cut {f["name"]!r} ----')
    on = f['on']
    if isinstance(on, dict) and on.get('ref') == 'top_face':
        w('        _face = None; _zmax = -1e9')
        w('        for _fc in body.faces:')
        w('            try:')
        w('                if abs(_fc.geometry.normal.z) > 0.9 and _fc.centroid.z > _zmax:')
        w('                    _zmax = _fc.centroid.z; _face = _fc')
        w('            except: pass')
        w('        sk = root.sketches.add(_face)')
        w(f'        sk.name = {f["name"]+"_sketch"!r}')
    elif isinstance(on, OffsetPlane):
        dist_cm = d.E(on.distance) / 10.0
        w('        _cpI = root.constructionPlanes.createInput()')
        w(f'        _cpI.setByOffset(root.{_PLANE_ATTR[on.base]}, adsk.core.ValueInput.createByReal({dist_cm:.6f}))')
        w(f'        _pl = root.constructionPlanes.add(_cpI); _pl.name = {f["name"]+"_plane"!r}')
        w('        sk = root.sketches.add(_pl)')
        w(f'        sk.name = {f["name"]+"_sketch"!r}')
    else:
        w(f'        # UNSUPPORTED plane {on!r}')
        return

    # sketch primitives
    for s in f['sketch']:
        if isinstance(s, Rect):
            x0, x1 = d.E(s.x[0]) / 10.0, d.E(s.x[1]) / 10.0
            y0, y1 = d.E(s.y[0]) / 10.0, d.E(s.y[1]) / 10.0
            w('        _Ln = sk.sketchCurves.sketchLines')
            w('        _z  = _face.centroid.z if "_face" in dir() and _face else 0.0')
            w(f'        _rp = [adsk.core.Point3D.create({x0:.6f},{y0:.6f},_z),'
              f' adsk.core.Point3D.create({x1:.6f},{y0:.6f},_z),'
              f' adsk.core.Point3D.create({x1:.6f},{y1:.6f},_z),'
              f' adsk.core.Point3D.create({x0:.6f},{y1:.6f},_z)]')
            w('        for _i in range(4): _Ln.addByTwoPoints(_rp[_i], _rp[(_i+1) % 4])')
        elif isinstance(s, Circle):
            cx, cy = d.E(s.center[0]) / 10.0, d.E(s.center[1]) / 10.0
            r  = d.E(s.radius) / 10.0
            w(f'        sk.sketchCurves.sketchCircles.addByCenterRadius('
              f'adsk.core.Point3D.create({cx:.6f},{cy:.6f},0), {r:.6f})')

    # Pick small-area profiles (holes or notches) — exclude the full-sketch-plane surround.
    w('        _profs = adsk.core.ObjectCollection.create()')
    w('        for _p in sk.profiles:')
    w('            try: _a = _p.areaProperties().area')
    w('            except: continue')
    w('            if _a < 1.0:  # cm^2  ->  < 100 mm^2')
    w('                _profs.add(_p)')
    w('        if _profs.count == 0:')
    w('            _profs.add(sk.profiles.item(0))')
    w(f'        _eI = root.features.extrudeFeatures.createInput(_profs, adsk.fusion.FeatureOperations.CutFeatureOperation)')
    w(f'        _eI.setDistanceExtent(False, adsk.core.ValueInput.createByString({_expr_str(f["depth"], d.units)!r}))')
    w('        _eI.participantBodies = [body]')
    w(f'        _cut = root.features.extrudeFeatures.add(_eI); _cut.name = {f["name"]!r}')

    if f['pattern'] is not None:
        p = f['pattern']
        w('        _ents = adsk.core.ObjectCollection.create(); _ents.add(_cut)')
        w(f'        _pI = root.features.circularPatternFeatures.createInput(_ents, root.{_AXIS_ATTR[p.axis]})')
        w(f'        _pI.quantity = adsk.core.ValueInput.createByString({str(p.count)!r})')
        w(f'        _pI.totalAngle = adsk.core.ValueInput.createByString({p.total_angle!r})')
        w('        _pI.isSymmetric = False')
        w('        root.features.circularPatternFeatures.add(_pI)')


# =========================================================================
# CLI — `cadlang build` + subcommand dispatch
# =========================================================================

def _find_project(hint=None):
    """Locate a `project.cadlang` file. If `hint` is a file, use it. If a
    directory, look for `project.cadlang` inside. Otherwise search upward
    from the current working directory."""
    import pathlib
    if hint:
        p = pathlib.Path(hint)
        if p.is_file():
            return p
        if p.is_dir() and (p / 'project.cadlang').is_file():
            return p / 'project.cadlang'
        raise FileNotFoundError(f'no project.cadlang at {hint}')
    cur = pathlib.Path.cwd()
    for anc in [cur, *cur.parents]:
        if (anc / 'project.cadlang').is_file():
            return anc / 'project.cadlang'
    raise FileNotFoundError('no project.cadlang in cwd or any parent directory')


def _do_import_step(project_dir, params):
    """Run `stepimport` for an `import:` step.

    `params` is either a string (the .step path) or a dict with `source`,
    optional `name`, and optional `out` (relative to project_dir)."""
    import stepimport
    if isinstance(params, str):
        params = {'source': params}
    source = (project_dir / params['source']).resolve()
    out_dir = (project_dir / params.get('out', 'parts/')).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    argv = ['stepimport', str(source), '-o', str(out_dir)]
    if 'name' in params:
        argv += ['--name', str(params['name'])]
    print(f'[cadlang build] import {source.name} → {out_dir}')
    rc = stepimport.main(argv)
    if rc:
        raise SystemExit(rc)


def _do_assemble(project_dir, params):
    """Run `assembly` for an `assemble:` step.

    `params` is a string (the .yaml path) or a dict with `source`."""
    import assembly
    if isinstance(params, str):
        source = (project_dir / params).resolve()
    else:
        source = (project_dir / params['source']).resolve()
    print(f'[cadlang build] assemble {source.name}')
    rc = assembly.main(['assembly', str(source)])
    if rc:
        raise SystemExit(rc)


_STEP_ACTIONS = {
    'import': _do_import_step,
    'assemble': _do_assemble,
}


def build_project(project_path=None):
    """Execute every step in a `project.cadlang` file, in order."""
    import yaml
    proj = _find_project(project_path)
    project_dir = proj.parent
    config = yaml.safe_load(proj.read_text(encoding='utf-8')) or {}
    name = config.get('name', project_dir.name)
    print(f"[cadlang build] project {name!r} at {project_dir}")
    for i, step in enumerate(config.get('steps') or [], 1):
        if not isinstance(step, dict) or len(step) != 1:
            raise ValueError(f'step #{i} must be a single-key mapping: {step!r}')
        (action, params), = step.items()
        if action not in _STEP_ACTIONS:
            raise ValueError(f'step #{i}: unknown action {action!r}; known: {sorted(_STEP_ACTIONS)}')
        _STEP_ACTIONS[action](project_dir, params)
    print(f"[cadlang build] done: {name!r}")
    return 0


def main(argv=None):
    import argparse, sys
    argv = sys.argv if argv is None else argv
    p = argparse.ArgumentParser(prog='cadlang', description='cadlang DSL + project CLI')
    sub = p.add_subparsers(dest='cmd', required=True)
    b = sub.add_parser('build', help='build the cadlang project in cwd (or given path)')
    b.add_argument('project', nargs='?', default=None,
                   help='path to a project.cadlang file or its directory; '
                        'defaults to searching upward from cwd')
    g = sub.add_parser('gui', help='serve a local web UI to browse and rebuild parts')
    g.add_argument('project', nargs='?', default=None,
                   help='project path (same resolution as `build`)')
    g.add_argument('--host', default='127.0.0.1')
    g.add_argument('--port', type=int, default=8765,
                   help='port to bind (default: 8765; pass 0 for ephemeral)')
    g.add_argument('--no-browser', action='store_true',
                   help="don't auto-open the browser")
    g.add_argument('--no-reload', action='store_true',
                   help="don't auto-restart on cadlang source edits")
    args = p.parse_args(argv[1:])
    if args.cmd == 'build':
        return build_project(args.project)
    if args.cmd == 'gui':
        import gui
        return gui.serve(args.project, host=args.host, port=args.port,
                         open_browser=not args.no_browser,
                         reload=not args.no_reload)
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main(sys.argv))
