---
name: invoke-local
description: Run cadlang locally — regenerate a single hand-written part with `python parts/<name>.cad.py`, or execute the full project pipeline with `cadlang build` (STEP imports → per-part STL/Fusion → assembly). Use when the user asks to rebuild a part, refresh outputs, or check that the pipeline still works end-to-end.
---

# Invoke cadlang locally

## Single part (regenerate one `.cad.py`'s outputs)

```bash
python parts/<name>.cad.py
```

Rewrites `parts/<name>.stl`, `parts/<name>_preview.png`, `parts/<name>_fusion.py` next to the source. For thin / flat parts the preview PNG may look off; trust the STL and open it in Windows 3D Viewer or Fusion for a cleaner render.

## Full project (STEP imports + all parts + assembly)

```bash
cd <project-dir>          # directory containing project.cadlang
cadlang build
```

Or from anywhere:

```bash
cadlang build <project-dir>
```

This runs every step in `project.cadlang` in order — `import:` (STEP → `.g.cad.py` starters), per-part `.cad.py` execution, and `assemble:` (mate solver + combined STL + preview).

## Fallback when `cadlang` isn't on PATH

If the user hasn't `pip install`ed the package yet, invoke the module directly:

```bash
python cadlang.py build <project-dir>
```

Both entry points route through `main()` in `cadlang.py`.

## After running

- Generated `.g.stl` / `.g_preview.png` / `.g_fusion.py` files are gitignored — don't try to commit them.
- Hand-written part outputs (`<name>.stl` without `.g.`) and assembly outputs ARE tracked.
- If a feature emits a `cadlang[stl] skipping …` warning, it means the STL backend doesn't support that op yet; the Fusion script still includes it.
