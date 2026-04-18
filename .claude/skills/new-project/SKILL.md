---
name: new-project
description: Scaffold a new cadlang project directory with project.cadlang manifest, STATE.md notes file, and parts/ subdir. Use when the user is starting a new CAD project and wants the standard layout that `cadlang build` expects.
---

# Scaffold a new cadlang project

A cadlang project is a directory containing a `project.cadlang` manifest plus its source files (STEP imports, hand-written `.cad.py` files, assembly YAML).

## Directory layout

```
<project-dir>/
├── project.cadlang        # manifest — `cadlang build` entry point
├── STATE.md               # per-project notes (measurements, decisions, TODOs)
├── <source>.step          # (optional) STEP imports at project root
├── assembly.yaml          # (optional) multi-part assembly description
└── parts/                 # hand-written and STEP-imported .cad.py files
```

## project.cadlang (minimal)

```yaml
name: <project-name>
description: <one-line summary>
steps: []
```

## project.cadlang (typical — STEP import + assembly)

```yaml
name: <project-name>
description: <one-line summary>
steps:
  - import:
      source: <source>.step
      name: <BaseName>        # → parts/<BaseName>_partNNN.g.cad.py
      out: parts/
  - assemble: assembly.yaml
```

Steps run strictly in order. Additional actions can be added by extending `_STEP_ACTIONS` in `cadlang.py`.

## STATE.md (starter)

```markdown
# <project-name>

## Goal
<what this project is building and why>

## Measurements / reference data
<physical dimensions, STEP source provenance, etc.>

## Open questions
<anything that blocks forward progress>

## TODO
<specific next steps>
```

Don't duplicate cadlang language docs here — those live in the repo's root `CLAUDE.md`.

## After scaffolding

- Add parts via the `new-part` skill (hand-written) or `import-step` skill (from a STEP).
- For assemblies, document interfaces + mates in `assembly.yaml` (see `assembly.py` for schema).
- Run `cadlang build` to exercise the pipeline end-to-end.
