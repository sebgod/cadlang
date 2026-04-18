"""
cadlang/assembly.py — load a YAML assembly description, emit a combined STL.

v2 scope:
  - Interface-based mates (a rail's bolt pair → a ring's hole pair), with one
    part flagged as `anchor`.  Transforms are solved in topological order so
    most of the hand-computed pose math disappears from YAML.
  - Legacy explicit `pose` / `circular` remains supported for parts that
    don't have interfaces yet.

Run as a CLI:
    python assembly.py <file.yml>                 # emit combined STL + preview
    python assembly.py <file.yml> --regenerate    # re-run each part first

YAML schema (v2)
----------------

    name: <assembly-name>
    units: mm

    # Declarations of the mating features each part type exposes.  Key = the
    # part's name (derived from the source filename stem, dropping `.g` and
    # `.cad`/`.py`), value = named interfaces.
    interfaces:
      <part_type>:
        <interface_name>:
          kind: bolt_pair              # two parallel bolt holes at known points
          positions:                   # part-local coordinates
            - [x1, y1, z1]
            - [x2, y2, z2]
          axis: [ax, ay, az]           # bolt-axis direction in part-local frame

        <interface_name>:
          kind: tabbed_hole_stack      # 4 tabs × 4 holes on a ring OD (say)
          radius: 125.2                # where the holes open on the OD
          tab_count: 4                 # circular pattern around `pattern_axis`
          tab_phase_deg: 0             # first tab's angular position
          pattern_axis: Z              # Z only for now
          hole_z: [5, 11.667, 18.333, 25]   # intrinsic axial positions

    # Part instances.
    parts:
      <slot_name>:
        source: ../parts/<name>.cad.py
        anchor: true                   # optional — this part sits at origin
        count: 4                       # optional — multi-instance (default 1)
        pose:                          # optional — explicit override (legacy)
          translate: [x, y, z]
          rotate_deg: [rx, ry, rz]
        circular: {...}                # legacy explicit pattern
        regenerate: false              # optional per-part override

    # Mate declarations.  Each mate places one part (or one instance of a
    # multi-instance part) relative to another that is already positioned.
    mates:
      - part: <slot_name>              # which part to place
        # Optional when the part has count>1: per-instance substitutions.
        # `i` is automatically bound to the instance index (0..count-1).
        from: <interface_name>         # or {interface: ..., tab: ..., holes: [...]}
        to:
          part: <other_slot>
          instance: 0                  # optional; default 0
          interface: <interface_name>
          # When the target interface is a tabbed_hole_stack, select which
          # tab + holes this mate aligns to:
          tab: "{i}"                   # string is a template; i / j / etc. substituted
          holes: [2, 3]                # indices into hole_z (inclusive both ends)

Notes
-----
- `anchor: true` on exactly one part is required unless every part has a pose.
- Interfaces look up their geometry in the `interfaces:` block by the part's
  NAME-STEM (file stem without `.g`/`.cad`/`.py`). Multiple slots reusing the
  same source share one interface definition.
- The solver applies a rigid 4x4 transform per instance; it does NOT boolean-
  union parts.  Overlap is fine for visualisation; STL is concatenated.
"""
from __future__ import annotations
import argparse
import math
import re
import subprocess
import sys
from pathlib import Path
import numpy as np
import trimesh
import yaml


# =========================================================================
# Loader
# =========================================================================

def load(path: Path):
    with open(path, encoding='utf-8') as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict):
        raise ValueError(f'{path}: expected a YAML mapping at top level')
    if 'parts' not in doc:
        raise ValueError(f'{path}: missing required `parts:` block')
    return doc


# =========================================================================
# Pose math (legacy explicit-pose path)
# =========================================================================

def _transform_from_pose(pose: dict | None) -> np.ndarray:
    T = np.eye(4)
    if not pose:
        return T
    if 'rotate_deg' in pose:
        rx, ry, rz = pose['rotate_deg']
        tm = trimesh.transformations
        T = tm.rotation_matrix(math.radians(rx), [1, 0, 0]) @ T
        T = tm.rotation_matrix(math.radians(ry), [0, 1, 0]) @ T
        T = tm.rotation_matrix(math.radians(rz), [0, 0, 1]) @ T
    if 'translate' in pose:
        T = trimesh.transformations.translation_matrix(pose['translate']) @ T
    return T


def _circular_instances(spec: dict) -> list[dict]:
    axis = spec.get('axis', 'Z')
    count = int(spec['count'])
    total_deg = float(spec.get('total_deg', 360))
    radius = float(spec.get('radius', 0))
    local = spec.get('local_pose') or {}
    step = total_deg / count if count > 0 else 0
    if axis != 'Z':
        raise NotImplementedError(f'circular pattern on axis {axis!r} not supported yet')
    out = []
    for k in range(count):
        angle = step * k
        pose = {
            'rotate_deg': [0, 0, angle],
            'translate': [
                radius * math.cos(math.radians(angle)),
                radius * math.sin(math.radians(angle)),
                0,
            ],
        }
        out.append({'local_pose': local, 'pose': pose})
    return out


# =========================================================================
# Mate solver (v2)
# =========================================================================

def _part_type_from_source(source_path: str) -> str:
    """`../parts/SWQ8_part001.g.cad.py` -> `SWQ8_part001`."""
    name = Path(source_path).name
    for suffix in ('.g.cad.py', '.cad.py'):
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return Path(source_path).stem


def _unit(v):
    v = np.asarray(v, dtype=float)
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def _frame_from_bolt_pair(positions, axis) -> dict:
    """Build an orthonormal frame from a bolt pair.

    x = pair direction (from position 0 to position 1)
    z = bolt axis (normal of the plate)
    y = z × x
    origin = midpoint of the pair.
    """
    p0 = np.asarray(positions[0], dtype=float)
    p1 = np.asarray(positions[1], dtype=float)
    x = _unit(p1 - p0)
    z = _unit(axis)
    y = _unit(np.cross(z, x))
    # re-orthogonalise x so that (x, y, z) is exactly orthonormal
    x = _unit(np.cross(y, z))
    return {
        'origin': (p0 + p1) / 2,
        'x': x,
        'y': y,
        'z': z,
    }


def _evaluate_interface(iface_def: dict, selector: dict) -> dict:
    """Return a bolt_pair frame (dict with origin, x, y, z) in part-local coords.

    `selector` is what came from the mate's `from`/`to` block after substitution
    (e.g. {tab: 1, holes: [2, 3]} for a tabbed_hole_stack).
    """
    kind = iface_def['kind']
    if kind == 'bolt_pair':
        return _frame_from_bolt_pair(iface_def['positions'], iface_def['axis'])

    if kind == 'tabbed_hole_stack':
        radius = float(iface_def['radius'])
        tab_count = int(iface_def['tab_count'])
        tab_phase = math.radians(float(iface_def.get('tab_phase_deg', 0)))
        hole_z = list(iface_def['hole_z'])
        tab = int(selector.get('tab', 0))
        hole_indices = selector.get('holes')
        if hole_indices is None or len(hole_indices) != 2:
            raise ValueError(f'tabbed_hole_stack selector requires `holes: [i, j]`; got {selector}')
        phi = tab_phase + 2 * math.pi * tab / tab_count
        cx, cy = radius * math.cos(phi), radius * math.sin(phi)
        z0, z1 = float(hole_z[hole_indices[0]]), float(hole_z[hole_indices[1]])
        positions = [[cx, cy, z0], [cx, cy, z1]]
        axis = [math.cos(phi), math.sin(phi), 0]  # radial outward
        return _frame_from_bolt_pair(positions, axis)

    raise NotImplementedError(f'interface kind {kind!r} not supported yet')


def _apply_transform_to_frame(T: np.ndarray, frame: dict) -> dict:
    """Apply a 4x4 to a frame — rotate axes, transform origin."""
    R = T[:3, :3]
    t = T[:3, 3]
    return {
        'origin': R @ frame['origin'] + t,
        'x': R @ frame['x'],
        'y': R @ frame['y'],
        'z': R @ frame['z'],
    }


def _align_frames(from_frame_local: dict, to_frame_world: dict) -> np.ndarray:
    """Compute a 4x4 transform that maps the local frame onto the world frame.

    Frames are orthonormal (columns x, y, z).  Transform = to_basis @ from_basis^T
    for rotation; translation chases the origin shift."""
    R_from = np.column_stack([from_frame_local['x'], from_frame_local['y'], from_frame_local['z']])
    R_to   = np.column_stack([to_frame_world['x'],   to_frame_world['y'],   to_frame_world['z']])
    R = R_to @ R_from.T
    t = np.asarray(to_frame_world['origin']) - R @ np.asarray(from_frame_local['origin'])
    T = np.eye(4)
    T[:3, :3] = R
    T[:3, 3] = t
    return T


def _substitute(spec, context: dict):
    """Recursively walk a dict/list; replace any '{key}' strings with context[key]."""
    if isinstance(spec, str):
        # Treat `"{name}"` as a placeholder; try to resolve to an int/float if the
        # substituted text looks numeric.
        m = re.fullmatch(r'\{(\w+)\}', spec)
        if m and m.group(1) in context:
            return context[m.group(1)]
        # Substitute `{name}` inside a longer string.
        def repl(mm): return str(context.get(mm.group(1), mm.group(0)))
        return re.sub(r'\{(\w+)\}', repl, spec)
    if isinstance(spec, list):
        return [_substitute(x, context) for x in spec]
    if isinstance(spec, dict):
        return {k: _substitute(v, context) for k, v in spec.items()}
    return spec


def _resolve_side(side, *, my_part_type, interfaces, context,
                  known_transforms, parts):
    """Return ((part_slot, instance_idx) or None, frame_in_world_coords).

    `side` is either:
      - a plain string: names an interface on the current part — returns
        (None, frame_in_local_coords)   [i.e. world_coords == local_coords since
        the current part isn't placed yet]
      - a dict: references another part's interface.
    """
    side = _substitute(side, context)
    if isinstance(side, str):
        # Local interface on the current part.
        if my_part_type not in interfaces or side not in interfaces[my_part_type]:
            raise KeyError(f'no interface {side!r} on part type {my_part_type!r}')
        iface = interfaces[my_part_type][side]
        return None, _evaluate_interface(iface, {})
    if isinstance(side, dict):
        if 'part' in side:
            target_slot = side['part']
            target_instance = int(side.get('instance', 0))
            target_spec = parts[target_slot]
            target_type = _part_type_from_source(target_spec['source'])
            iface_name = side['interface']
            iface = interfaces[target_type][iface_name]
            frame_local = _evaluate_interface(iface, side)
            T = known_transforms.get((target_slot, target_instance))
            if T is None:
                return (target_slot, target_instance), None  # not yet resolved
            return (target_slot, target_instance), _apply_transform_to_frame(T, frame_local)
        # Dict with `interface` but no `part` → local interface with selector.
        if 'interface' in side:
            iface_name = side['interface']
            if my_part_type not in interfaces or iface_name not in interfaces[my_part_type]:
                raise KeyError(f'no interface {iface_name!r} on part type {my_part_type!r}')
            iface = interfaces[my_part_type][iface_name]
            return None, _evaluate_interface(iface, side)
    raise ValueError(f'cannot understand mate side {side!r}')


def solve_poses(assembly: dict) -> dict:
    """Resolve per-instance 4x4 transforms for every part in the assembly.

    Returns a dict keyed by (slot_name, instance_index)."""
    parts = assembly['parts']
    interfaces = assembly.get('interfaces') or {}
    mates = assembly.get('mates') or []
    transforms: dict = {}

    # Seed: anchors at identity + legacy explicit-pose parts at their pose.
    for slot, spec in parts.items():
        count = int(spec.get('count', 1))
        if spec.get('anchor'):
            transforms[(slot, 0)] = np.eye(4)
        elif 'pose' in spec or 'circular' in spec or 'instances' in spec:
            instances = _enumerate_legacy_instances(spec)
            for i, inst in enumerate(instances):
                T = np.eye(4)
                if inst.get('local_pose'):
                    T = _transform_from_pose(inst['local_pose']) @ T
                if inst.get('pose'):
                    T = _transform_from_pose(inst['pose']) @ T
                transforms[(slot, i)] = T

    # Expand mate specs per-instance (binding {i} to the instance index).
    expanded: list = []
    for m in mates:
        slot = m['part']
        if slot not in parts:
            raise KeyError(f'mate references unknown part {slot!r}')
        count = int(parts[slot].get('count', 1))
        for i in range(count):
            expanded.append({
                'slot': slot,
                'instance': i,
                'from': m['from'],
                'to': m['to'],
                'context': {'i': i},
            })

    # Topological iteration: solve mates whose `to` side is fully resolved.
    progress = True
    remaining = expanded
    while remaining and progress:
        progress = False
        still_pending = []
        for mate in remaining:
            slot = mate['slot']
            inst = mate['instance']
            if (slot, inst) in transforms:
                continue  # already seeded by legacy pose
            p_type = _part_type_from_source(parts[slot]['source'])
            # Resolve target side first — requires its part to be placed.
            to_ref, to_frame = _resolve_side(
                mate['to'], my_part_type=p_type, interfaces=interfaces,
                context=mate['context'], known_transforms=transforms, parts=parts,
            )
            if to_frame is None:
                still_pending.append(mate)
                continue
            from_ref, from_frame_local = _resolve_side(
                mate['from'], my_part_type=p_type, interfaces=interfaces,
                context=mate['context'], known_transforms=transforms, parts=parts,
            )
            # from_frame_local is in *this part's* local coords — we're solving
            # for the transform that places this part.  (If from_ref is not
            # None, the YAML is referencing a DIFFERENT part's interface as the
            # source, which isn't what we typically want.)
            if from_ref is not None:
                raise ValueError(
                    f'mate on {slot!r}: `from` should name an interface on the '
                    f'part being placed, not on another part'
                )
            T = _align_frames(from_frame_local, to_frame)
            transforms[(slot, inst)] = T
            progress = True
        remaining = still_pending

    if remaining:
        stuck = [(m['slot'], m['instance']) for m in remaining]
        raise ValueError(
            f'unresolvable mates (circular dependency or missing anchor?): {stuck}'
        )

    # Fill missing instances with identity (for parts without any pose info).
    for slot, spec in parts.items():
        count = int(spec.get('count', 1))
        for i in range(count):
            transforms.setdefault((slot, i), np.eye(4))
    return transforms


def _enumerate_legacy_instances(spec: dict) -> list[dict]:
    if 'circular' in spec:
        return _circular_instances(spec['circular'])
    if 'instances' in spec:
        return [{'local_pose': None, 'pose': i.get('pose')} for i in spec['instances']]
    return [{'local_pose': None, 'pose': spec.get('pose')}]


# =========================================================================
# STL resolution
# =========================================================================

def _stl_path_for(cad_py: Path) -> Path:
    """Resolve the STL sibling of a `.cad.py` or `.g.cad.py` source.

    `.cad.py`   → `<stem>.stl`
    `.g.cad.py` → `<stem>.g.stl`   (the `.g.` marker propagates to outputs so
                                    generated and hand-written parts with the
                                    same stem don't collide)
    """
    stem = cad_py.name
    if stem.endswith('.g.cad.py'):
        return cad_py.with_name(stem[:-len('.g.cad.py')] + '.g.stl')
    if stem.endswith('.cad.py'):
        return cad_py.with_name(stem[:-len('.cad.py')] + '.stl')
    return cad_py.with_name(cad_py.stem + '.stl')


def _ensure_stl(cad_py: Path, regenerate: bool) -> Path:
    stl = _stl_path_for(cad_py)
    if regenerate or not stl.exists() or cad_py.stat().st_mtime > stl.stat().st_mtime:
        print(f'[assembly] running {cad_py} to refresh {stl.name}')
        subprocess.check_call([sys.executable, str(cad_py)])
    return stl


# =========================================================================
# Emission
# =========================================================================

def emit(assembly: dict, yaml_dir: Path, regenerate: bool = False):
    name = assembly.get('name', 'assembly')
    slug = re.sub(r'[^A-Za-z0-9_]+', '_', name) or 'assembly'
    global_regen = bool(regenerate or assembly.get('regenerate', False))

    transforms = solve_poses(assembly)

    combined_meshes = []
    for slot, spec in assembly['parts'].items():
        src_path = (yaml_dir / spec['source']).resolve()
        if not src_path.exists():
            raise FileNotFoundError(f'part source for {slot!r}: {src_path}')
        stl_path = _ensure_stl(src_path, regenerate=global_regen or spec.get('regenerate', False))
        base_mesh = trimesh.load(str(stl_path))
        count = int(spec.get('count', 1))
        for i in range(count):
            T = transforms[(slot, i)]
            mesh = base_mesh.copy()
            mesh.apply_transform(T)
            combined_meshes.append(mesh)
            tag = f'[{i}]' if count > 1 else ''
            print(f'[assembly] placed {slot}{tag}')

    combined = trimesh.util.concatenate(combined_meshes)
    out_stl = yaml_dir / f'{slug}.stl'
    out_png = yaml_dir / f'{slug}_preview.png'
    combined.export(str(out_stl))
    print(f'cadlang[assembly] wrote {out_stl}  verts={len(combined.vertices)}  '
          f'faces={len(combined.faces)}')
    _render_preview(combined, out_png, title=name)
    print(f'cadlang[assembly] wrote {out_png}')
    return combined


def _render_preview(mesh, path: Path, title: str = ''):
    import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection
    b = mesh.bounds
    fig = plt.figure(figsize=(14, 6))

    def draw(ax, elev, azim, sub):
        tri = mesh.vertices[mesh.faces]
        nrm = mesh.face_normals
        light = np.array([0.35, -0.55, 0.85]); light /= np.linalg.norm(light)
        shade = np.clip(nrm @ light, 0.2, 1.0)
        col = np.stack([shade*0.82, shade*0.55, shade*0.35, np.ones_like(shade)], axis=1)
        ax.add_collection3d(Poly3DCollection(tri, facecolors=col, edgecolors='none'))
        ax.set_xlim(b[0,0], b[1,0]); ax.set_ylim(b[0,1], b[1,1]); ax.set_zlim(b[0,2], b[1,2])
        try: ax.set_box_aspect(b[1] - b[0])
        except Exception: pass
        ax.view_init(elev=elev, azim=azim); ax.set_axis_off()
        ax.set_title(sub, fontsize=10)

    for i, (elev, azim, sub) in enumerate([(20, -55, 'iso'), (20, 35, 'iso alt'), (60, -90, 'plan')]):
        draw(fig.add_subplot(1, 3, i + 1, projection='3d'), elev, azim, sub)
    fig.suptitle(title, fontsize=12); fig.tight_layout()
    fig.savefig(str(path), dpi=140, bbox_inches='tight', facecolor='white')


# =========================================================================
# CLI
# =========================================================================

def main(argv):
    ap = argparse.ArgumentParser(description='YAML assembly -> combined STL')
    ap.add_argument('assembly_file')
    ap.add_argument('--regenerate', action='store_true',
                    help='re-run every referenced .cad.py before loading its STL')
    args = ap.parse_args(argv[1:])
    path = Path(args.assembly_file).resolve()
    assembly = load(path)
    emit(assembly, path.parent, regenerate=args.regenerate)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
