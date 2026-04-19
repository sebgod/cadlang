"""Shared pytest fixtures for cadlang tests.

Ground truth for the SWQ8 dewshield STEP comes from physical measurements
captured in `example-project/SWQ8-Dewshield-Holder/STATE.md §2`, not from
cadlang itself. If any of these numbers look wrong, measure the physical
part again (or re-measure in the STEP via an independent tool like
FreeCAD) — don't trust what our own importer says.
"""
from __future__ import annotations
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

SWQ8_STEP = (REPO_ROOT / 'example-project' / 'SWQ8-Dewshield-Holder'
             / 'SWQ8DewShieldHolder.step')


@pytest.fixture(scope='session')
def swq8_step_path():
    assert SWQ8_STEP.is_file(), f'fixture missing: {SWQ8_STEP}'
    return SWQ8_STEP


@pytest.fixture(scope='session')
def swq8_db(swq8_step_path):
    """Parsed STEP record dict — shared across all importer tests."""
    import stepimport
    text = swq8_step_path.read_text(encoding='utf-8', errors='ignore')
    return stepimport.parse(text)


@pytest.fixture(scope='session')
def swq8_bodies(swq8_db):
    """List of body dicts inferred from the SWQ8 STEP."""
    import stepimport
    return stepimport.infer_bodies(swq8_db, 'SWQ8')


@pytest.fixture(scope='session')
def swq8_ring(swq8_bodies):
    revs = [b for b in swq8_bodies if b['kind'] == 'revolve']
    assert len(revs) == 1, f'expected 1 revolve body, got {len(revs)}'
    return revs[0]


@pytest.fixture(scope='session')
def swq8_rail(swq8_bodies):
    exts = [b for b in swq8_bodies if b['kind'] == 'extrude']
    assert len(exts) == 1, f'expected 1 extrude body, got {len(exts)}'
    return exts[0]
