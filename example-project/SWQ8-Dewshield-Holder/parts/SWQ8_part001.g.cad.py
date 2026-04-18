"""
SWQ8_part001 — imported from SWQ8DewShieldHolder.step by cadlang.stepimport.

Review params + feature list, then re-run this script to regenerate
SWQ8_part001.g.stl, SWQ8_part001.g_preview.png, and SWQ8_part001.g_fusion.py alongside it.
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

d = Design(name='SWQ8_part001', units='mm', params={
    'ring_id'   : 240.000,
    'ring_od'   : 250.400,
    'ring_h'    : 30.000,
    'hole_dia'  : 4.300,
    'hole_depth': 5.219,  # measured from STEP trim circles
    'hole_z1'   : 5.000,
    'hole_z2'   : 11.667,
    'hole_z3'   : 18.333,
    'hole_z4'   : 25.000,
})

# Base revolve: uniform annulus (STEP showed one ID + one OD cylinder).
d.revolve(name='base', plane='XZ', axis='Z', profile=[
    ('ring_id/2', 0),
    ('ring_od/2', 0),
    ('ring_od/2', 'ring_h'),
    ('ring_id/2', 'ring_h'),
])

# Radial bores (group 1): 16 surfaces, 4 axial positions, inferred pattern count=4.
d.cut(
    name='heat_inserts',
    on=OffsetPlane(base='YZ', distance='ring_od/2'),
    sketch=[
        Circle(center=(0, 'hole_z1'), radius='hole_dia/2'),
        Circle(center=(0, 'hole_z2'), radius='hole_dia/2'),
        Circle(center=(0, 'hole_z3'), radius='hole_dia/2'),
        Circle(center=(0, 'hole_z4'), radius='hole_dia/2'),
    ],
    depth='-hole_depth',
    pattern=Circular(axis='Z', count=4),
)

if __name__ == '__main__':
    d.emit_stl(str(HERE / 'SWQ8_part001.g.stl'),
               render_png=str(HERE / 'SWQ8_part001.g_preview.png'))
    d.emit_fusion(str(HERE / 'SWQ8_part001.g_fusion.py'))
