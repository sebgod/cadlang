"""
Dew-shield overlap ring — described in cadlang.

Run directly (`python overlap_ring.cad.py`) or via the project build
(`cadlang build` from the project root). Outputs land alongside this file.
"""
import sys, pathlib
HERE = pathlib.Path(__file__).resolve().parent
# Walk up to find cadlang.py — supports nested project layouts.
for _p in [HERE, *HERE.parents]:
    if (_p / 'cadlang.py').is_file():
        sys.path.insert(0, str(_p))
        break

from cadlang import Design, Rect, Circle, OffsetPlane, Circular

d = Design(
    name='DewShieldOverlapRing',
    units='mm',
    params={
        # ---- geometry (all mm) ----
        'ring_id':        240.5,   # slip fit over scope white band (OD ~240.27)
        'ring_od':        250.0,   # flush with rolled sheet / existing ring
        'ring_h':          46.0,   # total height (up over felt + down to Ring B)
        'notch_arc':       20.4,   # 20 mm rail + 0.2 mm clearance each side
        'notch_depth':     32.0,   # matches felt-to-support-end distance
        'chamfer_len':      2.0,   # scope-end chamfer axial length
        'chamfer_flare':    1.0,   # scope-end chamfer ID flare (push-fit lead-in)
        'hole_dia':         4.0,   # M3 heat-insert bore diameter
        'hole_depth':       4.0,   # heat-insert bore depth (radial)
        'hole_z1':          5.0,     # lower insert (exact z from existing-ring STEP)
        'hole_z2':         11.667,   # upper insert (exact z from existing-ring STEP)
    },
)

# Revolved cross-section on XZ plane. (u, v) = (radius, axial).
d.revolve(
    name='base',
    plane='XZ',
    axis='Z',
    profile=[
        ('ring_id/2 + chamfer_flare', 0),               # bottom-inner, flared
        ('ring_od/2',                 0),               # bottom-outer (scope face)
        ('ring_od/2',                 'ring_h'),        # top-outer
        ('ring_id/2',                 'ring_h'),        # top-inner (shield face)
        ('ring_id/2',                 'chamfer_len'),   # chamfer corner
    ],
)

# Four rail-clearance slots cut from the shield (top) face.
d.cut(
    name='rail_notches',
    on=d.top_face('base'),
    sketch=[Rect(x=('ring_id/2 - 1', 'ring_od/2 + 1'),
                 y=('-notch_arc/2',  'notch_arc/2'))],
    depth='-notch_depth',
    pattern=Circular(axis='Z', count=4),
)

# Two M3 heat-insert bores per angular position (×4 positions = 8 total),
# drilled radially into the OD in the solid base below the slots.
d.cut(
    name='heat_inserts',
    on=OffsetPlane(base='YZ', distance='ring_od/2'),
    sketch=[Circle(center=(0, 'hole_z1'), radius='hole_dia/2'),
            Circle(center=(0, 'hole_z2'), radius='hole_dia/2')],
    depth='-hole_depth',
    pattern=Circular(axis='Z', count=4),
)

d.measurements(
    ('Overall', {
        'ID': {'value': 'ring_id',
               'anchor': {'kind': 'diameter',
                          'from': ['-ring_id/2', 0, 0],
                          'to':   ['ring_id/2',  0, 0]}},
        'OD': {'value': 'ring_od',
               'anchor': {'kind': 'diameter',
                          'from': ['-ring_od/2', 0, 0],
                          'to':   ['ring_od/2',  0, 0]}},
        'wall':   '(ring_od - ring_id) / 2',
        'height': {'value': 'ring_h',
                   'anchor': {'kind': 'linear',
                              'from': ['ring_od/2', 0, 0],
                              'to':   ['ring_od/2', 0, 'ring_h']}},
    }),
    ('Scope-end chamfer', {
        'axial length': 'chamfer_len',
        'ID flare':     'chamfer_flare',
    }),
    ('Rail notches', {
        'count': 4,
        'arc':   'notch_arc',
        'depth': 'notch_depth',
    }),
    ('Heat inserts (radial)', {
        'count': 8,
        'Ø':     'hole_dia',
        'depth': 'hole_depth',
        'z1':    'hole_z1',
        'z2':    'hole_z2',
    }),
)

if __name__ == '__main__':
    slug = 'overlap_ring'
    d.emit_stl(str(HERE / f'{slug}.stl'),
               render_png=str(HERE / f'{slug}_preview.png'))
    d.emit_fusion(str(HERE / f'{slug}_fusion.py'))
