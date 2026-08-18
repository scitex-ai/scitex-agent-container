"""The `mcp` upper bound is the gate — an unpinned one makes CI a coin flip.

2026-08-18. Three pytest-matrix runs of the SAME workflow on the same py3.11,
minutes apart, with nothing in the repo changed between them::

    run 32184153812   fastmcp==3.4.7     mcp==1.29.0   ->   17094 passed
    run 32185071483   fastmcp==2.14.1    mcp==2.0.0    ->      29 failed
    run 32185515xxx   fastmcp==4.0.0b3   mcp==2.0.0    ->      26 failed

EVERY failure is ``AttributeError: 'Server' object has no attribute
'list_tools'`` (plus ``TypeError: 'types.UnionType' object is not callable``).
Neither message names a package, let alone a version, so each run reads as a
code regression on whichever branch happened to draw it.

READ THE TABLE BY WHAT VARIES WITH THE OUTCOME. ``fastmcp`` moves DOWN then UP
across those rows and the result does not follow it. ``mcp`` does: 1.29.0
passes, 2.0.0 fails, twice. ``Server`` is an ``mcp`` python-sdk class and
``list_tools`` is its API — mcp 2.0.0 (released 2026-07-28) removed it, and
fastmcp only pulls mcp transitively.

THE FIRST VERSION OF THIS FILE PINNED THE WRONG PACKAGE. It floored
``fastmcp>=3.0`` on the theory that 2.x was too old. The very next CI run
resolved ``fastmcp==4.0.0b3`` — satisfying that floor, reaching for a
PRE-RELEASE — and failed identically, because mcp was still 2.0.0. A bound
that admits the failure is not a gate; it is a comment.

So the pins are now: ``mcp>=1.29,<2`` (the bound the code is written against)
and ``fastmcp>=3.0,<4`` in [dev] / ``fastmcp>=2.0,<4`` in [mcp] (keeping the
observed pre-release out). The [dev] fastmcp FLOOR is higher than [mcp]'s
because the TESTS call ``server.list_tools()`` outright while the SOURCE
tolerates more — ``cli_pkg/mcp_group._list_tools`` branches on ``hasattr``.

There is no lockfile, so ``uv pip install -e .[dev]`` re-resolves every run;
these bounds are the only thing standing between the suite and the index.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

#: The lowest fastmcp the _mcp suite has ever been observed to pass on.
#: Not a guess: 3.4.7 is what run 32184153812 resolved and passed with.
MINIMUM_TESTED_MAJOR = 3

#: The first mcp major that REMOVES ``Server.list_tools``. Every red run
#: above carried it; the one green run did not.
FIRST_BREAKING_MCP_MAJOR = 2


def _pyproject() -> dict:
    root = Path(__file__).resolve().parents[2]
    path = root / "pyproject.toml"
    assert path.is_file(), f"pyproject.toml not found at {path}"
    return tomllib.loads(path.read_text())


def _specs(extra: str, dist: str) -> list[str]:
    extras = _pyproject()["project"]["optional-dependencies"]
    assert extra in extras, f"[{extra}] extra is gone; this gate is now blind"
    out = []
    for d in extras[extra]:
        name = d.split(">")[0].split("<")[0].split("=")[0].split("[")[0].strip()
        if name == dist:
            out.append(d)
    return out


def _fastmcp_specs(extra: str) -> list[str]:
    return _specs(extra, "fastmcp")


def _mcp_specs(extra: str) -> list[str]:
    return _specs(extra, "mcp")


def _sole_mcp_spec(extra: str) -> str:
    """The ONE mcp requirement in ``extra``.

    The exactly-once check lives here rather than in the test body because
    STX-TQ007 allows a test one assertion, and because a second mcp
    requirement would make the bound below ambiguous for EVERY caller — a
    gate that reads whichever spec it happened to see first is not a gate.
    """
    specs = _mcp_specs(extra)
    assert len(specs) == 1, f"[{extra}] must pin mcp exactly once, got {specs}"
    return specs[0]


def _ceiling_major(spec: str) -> int:
    """The major version from a ``pkg<X`` upper bound."""
    _, _, rest = spec.partition("<")
    assert rest, f"expected a < ceiling, got {spec!r}"
    return int(rest.split(".")[0])


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


@pytest.mark.parametrize("extra", ["dev", "mcp"])
def test_the_mcp_bound_excludes_the_release_that_removed_list_tools(extra: str):
    # Arrange — THE GATE, and the one the first version of this file
    # missed. mcp 2.0.0 removed `Server.list_tools`; every red run above
    # carried it and the one green run did not.
    spec = _sole_mcp_spec(extra)
    # Act
    ceiling = _ceiling_major(spec)
    # Assert
    assert ceiling <= FIRST_BREAKING_MCP_MAJOR


@pytest.mark.parametrize("extra", ["dev", "mcp"])
def test_fastmcp_has_a_ceiling_so_a_prerelease_cannot_be_drawn(extra: str):
    # Arrange — run 32185515xxx resolved fastmcp==4.0.0b3 under a
    # floor-only spec. A bound that admits the failure is not a gate.
    spec = _fastmcp_specs(extra)[0]
    # Act
    has_ceiling = "<" in spec
    # Assert
    assert has_ceiling is True
