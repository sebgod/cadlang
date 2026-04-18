---
name: new-part
description: Scaffold a new hand-written cadlang part at parts/<name>.cad.py with the standard boilerplate (imports, Design, features, emit_stl/emit_fusion entry point). Use when the user asks to create a new part from scratch — NOT from a STEP import (that's `import-step`).
---

# Scaffold a hand-written cadlang part

Create `parts/<name>.cad.py` using this template. Replace `<Name>` (display name, PascalCase) and `<name>` (slug, snake_case) and fill in `params` + features.

```python
import sys, pathlib
HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from cadlang import Design, Rect, Circle, OffsetPlane, Circular

d = Design('<Name>', units='mm', params={
    # dimensions and design parameters — all numeric, in mm
    # 'outer_r': 25,
    # 'height': 20,
})

# Features run in order. Later features union/subtract against earlier ones.
# Common building blocks:
#   d.revolve(name=..., plane='XZ', axis='Z', profile=[(u, v), ...])
#   d.extrude(name=..., on='XY', height=..., profile=[(x, y), ...])
#   d.extrude(name=..., on=OffsetPlane(base='XY', distance=expr), ...)
#   d.cut(name=..., on=d.top_face('body'), sketch=[Rect|Circle], depth=...)
#   d.cut(name=..., on=OffsetPlane(base='YZ', distance=...),
#         sketch=[Circle(center=(u, v), radius=r)],
#         depth=..., pattern=Circular(axis='Z', count=N))

if __name__ == '__main__':
    slug = '<name>'
    d.emit_stl(str(HERE / f'{slug}.stl'),
               render_png=str(HERE / f'{slug}_preview.png'))
    d.emit_fusion(str(HERE / f'{slug}_fusion.py'))
```

## After writing the file

1. `python parts/<name>.cad.py` — generates STL / preview / Fusion script next to the source.
2. Sanity-check the preview PNG.
3. If the part is part of an assembly, update `assembly.yaml` to declare its interfaces and add it to the `parts:` + `mates:` sections.

## Notes

- Use `.cad.py` (no `.g.` infix) for hand-written parts. `.g.cad.py` is reserved for STEP-imported files, which are gitignored and regenerated.
- Every expression string (e.g. `'outer_r/2 + 1'`) is evaluated against `params` + `math.*` only — no Python builtins. Keep expressions pure.
- See CLAUDE.md "Feature coverage" for which op/plane combinations the STL backend supports today.
