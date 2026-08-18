#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The [dev] fastmcp floor is a GATE — an unbounded one makes CI a coin flip.

2026-08-18 incident. Two pytest-matrix runs on the SAME workflow and the SAME
py3.11, five minutes apart, with nothing in the repo changed between them::

    run 32184153812   fastmcp==3.4.7  mcp==1.29.0   ->  17094 passed
    run 32185071483   fastmcp==2.14.1               ->      29 failed

Twenty-six of those failures were the ``_mcp`` channel suite, reporting
``'Server' object has no attribute 'list_tools'`` and ``TypeError:
'types.UnionType' object is not callable``. Neither message names a version,
so the run reads as a code regression — and an agent on an unrelated branch
spent the next twenty minutes proving it was not theirs.

WHY IT CAN HAPPEN AT ALL: there is no lockfile, so ``uv pip install -e .[dev]``
re-resolves on every run, and ``fastmcp>=2.0`` admits both API lines. The
SOURCE genuinely tolerates both — ``cli_pkg/mcp_group._list_tools`` branches on
``hasattr(server, "list_tools")`` — but the TESTS call ``server.list_tools()``
outright, so a 2.x resolution cannot run them.

The floor is therefore ASYMMETRIC ON PURPOSE: ``[dev]`` (the test surface)
requires >=3.0; ``[mcp]`` (the runtime server) stays at >=2.0 because the shim
really does handle both and the scitex-* convention expects it. These tests
hold that asymmetry in place, since the whole failure mode is that nothing
noticed the floor was too low until CI flipped.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

#: The lowest fastmcp the _mcp suite has ever been observed to pass on.
#: Not a guess: 3.4.7 is what run 32184153812 resolved and passed with.
MINIMUM_TESTED_MAJOR = 3


def _pyproject() -> dict:
    root = Path(__file__).resolve().parents[2]
    path = root / "pyproject.toml"
    assert path.is_file(), f"pyproject.toml not found at {path}"
    return tomllib.loads(path.read_text())


def _fastmcp_specs(extra: str) -> list[str]:
    extras = _pyproject()["project"]["optional-dependencies"]
    assert extra in extras, f"[{extra}] extra is gone; this gate is now blind"
    return [d for d in extras[extra] if d.replace("-", "_").startswith("fastmcp")]


def _floor_major(spec: str) -> int:
    """The major version from a ``fastmcp>=X.Y`` spec."""
    _, _, rest = spec.partition(">=")
    assert rest, f"expected a >= floor, got {spec!r}"
    return int(rest.split(".")[0])


def test_the_dev_extra_declares_exactly_one_fastmcp_requirement():
    # Arrange — two specs would make the floor below ambiguous, and this
    # gate would then pass while reading the permissive one.
    specs = _fastmcp_specs("dev")
    # Act
    count = len(specs)
    # Assert
    assert count == 1


def test_the_dev_fastmcp_floor_excludes_the_untestable_api_line():
    # Arrange — THE GATE. Below 3.0 the _mcp suite cannot run at all.
    spec = _fastmcp_specs("dev")[0]
    # Act
    major = _floor_major(spec)
    # Assert
    assert major >= MINIMUM_TESTED_MAJOR


def test_the_runtime_extra_keeps_the_wider_floor():
    # Arrange — POSITIVE CONTROL for the asymmetry. If someone "fixes the
    # inconsistency" by raising [mcp] too, the 2.x support the shim exists
    # for is dropped from consumers who never asked for the test surface.
    spec = _fastmcp_specs("mcp")[0]
    # Act
    major = _floor_major(spec)
    # Assert
    assert major == 2


@pytest.mark.parametrize("extra", ["dev", "mcp"])
def test_neither_floor_is_removed_entirely(extra: str):
    # Arrange — an absent requirement is not a permissive one; it is a
    # suite that silently collection-skips. Pair the bound checks above
    # with the presence check they both assume.
    specs = _fastmcp_specs(extra)
    # Act
    present = bool(specs)
    # Assert
    assert present is True
