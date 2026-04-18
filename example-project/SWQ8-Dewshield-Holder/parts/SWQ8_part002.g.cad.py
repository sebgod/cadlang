"""
SWQ8_part002 — imported from SWQ8DewShieldHolder.step by cadlang.stepimport.

Review params + feature list, then re-run this script to regenerate
SWQ8_part002.g.stl, SWQ8_part002.g_preview.png, and SWQ8_part002.g_fusion.py alongside it.
(The `.g.` infix marks these outputs as coming from an auto-generated
.g.cad.py source, distinct from hand-written parts.)
"""
import sys, pathlib
HERE = pathlib.Path(__file__).resolve().parent
# Walk up to find cadlang.py — supports nested project layouts.
for _p in [HERE, *HERE.parents]:
    if (_p / "cadlang.py").is_file():
        sys.path.insert(0, str(_p))
        break

from cadlang import Design, Rect, Circle, OffsetPlane, Circular

d = Design(name='SWQ8_part002', units='mm', params={
    'L1_x0'     : -60.250,
    'L1_x1'     : 60.250,
    'L1_y0'     : -165.200,
    'L1_y1'     : -145.200,
    'L1_t'      : 5.600,
    'L1_z0'     : 0.000,
    'L2_x0'     : -45.250,
    'L2_x1'     : 45.250,
    'L2_y0'     : -165.200,
    'L2_y1'     : -145.200,
    'L2_t'      : 5.600,
    'L2_z0'     : 5.600,
    'hole_dia'  : 4.300,
    'hole_depth': 5.618,  # measured from STEP trim edges
    'hole_x1'   : -56.917,
    'hole_y1'   : -155.200,
    'hole_x2'   : 50.250,
    'hole_y2'   : -155.200,
    'hole_x3'   : 56.917,
    'hole_y3'   : -155.200,
    'hole_x4'   : -50.250,
    'hole_y4'   : -155.200,
    'sad1_r'    : 125.200,  # X-axis lateral cut radius
    'sad1_u'    : -155.200,  # center in sketch plane (axis-perp coord 1)
    'sad1_v'    : 130.800,  # center in sketch plane (axis-perp coord 2)
    'sad1_a'    : -60.250,  # start along axis
    'sad1_b'    : 60.250,  # end along axis
    'sad2_r'    : 120.000,  # X-axis lateral cut radius
    'sad2_u'    : -155.200,  # center in sketch plane (axis-perp coord 1)
    'sad2_v'    : 130.800,  # center in sketch plane (axis-perp coord 2)
    'sad2_a'    : -45.250,  # start along axis
    'sad2_b'    : 45.250,  # end along axis
})

# Layered extrude: 2 stacked rectangular slab(s).
# Each layer's outline is the XY bbox of points at that Z level.
# Non-rectangular layer outlines (other than the lateral cuts below) are NOT captured yet.
# Layer 1: z ∈ [0.000, 5.600]
d.extrude(name='base', on='XY', profile=[
    ('L1_x0', 'L1_y0'),
    ('L1_x1', 'L1_y0'),
    ('L1_x1', 'L1_y1'),
    ('L1_x0', 'L1_y1'),
], height='L1_t')

# Layer 2: z ∈ [5.600, 11.200]
d.extrude(name='layer_2', on=OffsetPlane(base='XY', distance='L2_z0'), profile=[
    ('L2_x0', 'L2_y0'),
    ('L2_x1', 'L2_y0'),
    ('L2_x1', 'L2_y1'),
    ('L2_x0', 'L2_y1'),
], height='L2_t')

# Axial bores (group 1): 4 holes.
d.cut(
    name='bolt_holes',
    on=OffsetPlane(base='XY', distance='L1_z0 + L1_t'),
    sketch=[
        Circle(center=('hole_x1', 'hole_y1'), radius='hole_dia/2'),
        Circle(center=('hole_x2', 'hole_y2'), radius='hole_dia/2'),
        Circle(center=('hole_x3', 'hole_y3'), radius='hole_dia/2'),
        Circle(center=('hole_x4', 'hole_y4'), radius='hole_dia/2'),
    ],
    depth='-hole_depth',
)

# Lateral cut 1: X-axis cylinder, r=125.200, from X=-60.25 to 60.25.
d.cut(
    name='saddle',
    on=OffsetPlane(base='YZ', distance='sad1_a'),
    sketch=[
        Circle(center=('sad1_u', 'sad1_v'), radius='sad1_r'),
    ],
    depth='sad1_b - sad1_a',
)

# Lateral cut 2: X-axis cylinder, r=120.000, from X=-45.25 to 45.25.
d.cut(
    name='saddle_2',
    on=OffsetPlane(base='YZ', distance='sad2_a'),
    sketch=[
        Circle(center=('sad2_u', 'sad2_v'), radius='sad2_r'),
    ],
    depth='sad2_b - sad2_a',
)

if __name__ == '__main__':
    d.emit_stl(str(HERE / 'SWQ8_part002.g.stl'),
               render_png=str(HERE / 'SWQ8_part002.g_preview.png'))
    d.emit_fusion(str(HERE / 'SWQ8_part002.g_fusion.py'))
