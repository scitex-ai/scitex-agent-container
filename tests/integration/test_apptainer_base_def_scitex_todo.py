"""Static contract: ``apptainer-base.def`` must install scitex-todo[mcp].

P3a-1.5 (operator directive ``feedback_scitex_todo_single_shared_store``,
lead a2a ``5c0c1fe32a9a43888e01151d6fc0fb9e``): every sac-launched
agent must be able to attach the scitex-todo MCP and write to the
shared store. The MCP wiring (P3a) lands a ``.mcp.json`` entry that
invokes ``scitex-todo mcp start`` — but that only works when
``scitex-todo`` is on PATH inside the SIF. The ``[mcp]`` extra pulls
in fastmcp>=2.0 (the FastMCP server backing
``scitex_todo._mcp_server:mcp``) without which ``mcp start`` exits
with a missing-extra hint.

This test pins both requirements as code: drop scitex-todo or its
``[mcp]`` extra from the .def and CI yells before a SIF rebuild
ships an agent that can't reach its shared todo store.

The sibling ``test_apptainer_scitex_def_libxcb.py`` sets the same
contract pattern at the :scitex layer; this file enforces the
:base layer.

STX-TQ002 AAA + STX-TQ007 one-assert per test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BASE_DEF = (
    _REPO_ROOT / "src" / "scitex_agent_container" / "containers" / "apptainer-base.def"
)

# The capability floor the fleet actually depends on: the WIP-gate fix
# (scitex-todo #356, first released in 0.8.0) — the gate counts ``in_progress``
# ONLY. Below it, every agent's WIP gate counts DEFERRED + CANCELLED cards as
# open and refuses legitimate card creation. The .def's section-6c gate asserts
# that capability BY SYMBOL at build time
# (``scitex_todo._throughput.WIP_STATUSES``); this guard keeps the DECLARED
# floor at-or-above it, so the declared floor and the asserted symbol cannot
# drift apart.
#
# This is a CAPABILITY floor, NOT a snapshot of today's pin: the .def may name
# ANY version at-or-above it (>=0.9.4 today) and this guard stays green. That
# is the whole point. The guard this replaced substring-matched the literal
# string "0.3"::
#
#     has_version_pin = ">=0.3.0" in block or "==0.3.0" in block or ">=0.3" in block
#
# — so it recognised ONLY the stale floor and turned every attempt to RAISE the
# floor into a CI failure, while its own message promised "(>=0.3 or stricter)".
# It read like a pin check and behaved like a pin FREEZE, and it is what held
# the .def at an ancient floor while the live SIFs silently shipped scitex-todo
# 0.7.50 (incident-fleet-drift-stale-scitex-todo, 2026-07-12). A guard that
# cannot express "or stricter" is not a guard; it is a ratchet welded shut.
_WIP_GATE_MIN_VERSION = (0, 8, 0)

# The quoted requirement token naming scitex-todo, e.g. "scitex-todo[mcp]>=0.9.4".
_SCITEX_TODO_REQ_RE = re.compile(r'"(?P<req>[^"]*\bscitex-todo\b[^"]*)"')
# That requirement's lower bound (the first of >=, == or ~=).
_FLOOR_RE = re.compile(r"(?:>=|==|~=)\s*(?P<version>\d+(?:\.\d+)*)")


@pytest.fixture(scope="module")
def base_def_text() -> str:
    # Arrange
    return _BASE_DEF.read_text()


def _uv_pip_install_block(text: str) -> str:
    """Return the first ``uv pip install ...`` continuation chunk joined."""
    lines = text.splitlines()
    out: list[str] = []
    in_block = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("uv pip install"):
            in_block = True
        if in_block:
            out.append(stripped.rstrip("\\").strip())
            if not stripped.endswith("\\"):
                break
    return " ".join(out)


def _scitex_todo_requirement(block: str) -> str:
    """Return the scitex-todo requirement token, or ``""`` when absent."""
    match = _SCITEX_TODO_REQ_RE.search(block)
    return match.group("req") if match else ""


def _requirement_floor(requirement: str) -> tuple[int, ...] | None:
    """Return the requirement's lower-bound version, or ``None`` when unpinned."""
    match = _FLOOR_RE.search(requirement)
    if match is None:
        return None
    parts = [int(piece) for piece in match.group("version").split(".")]
    # Pad so ">=0.8" compares EQUAL to (0, 8, 0) instead of LESS than it — a
    # shorter tuple sorts before a longer one sharing its prefix.
    parts += [0] * (3 - len(parts))
    return tuple(parts)


# ---------------------------------------------------------------------------
# scitex-todo install line is present + carries the [mcp] extra
# ---------------------------------------------------------------------------


def test_uv_pip_install_block_mentions_scitex_todo(base_def_text: str) -> None:
    # Arrange
    block = _uv_pip_install_block(base_def_text)
    # Act
    present = "scitex-todo" in block
    # Assert
    assert present, (
        f"scitex-todo missing from uv pip install in apptainer-base.def:\n{block}"
    )


def test_uv_pip_install_block_carries_mcp_extra(base_def_text: str) -> None:
    # Arrange
    block = _uv_pip_install_block(base_def_text)
    # Act
    present = "scitex-todo[mcp]" in block
    # Assert
    assert present, (
        "scitex-todo install must carry the [mcp] extra (pulls fastmcp>=2.0)"
        f" in apptainer-base.def:\n{block}"
    )


# ---------------------------------------------------------------------------
# ... and a version floor at-or-above the capability the fleet depends on
# ---------------------------------------------------------------------------


def test_uv_pip_install_block_pins_scitex_todo_minimum_version(
    base_def_text: str,
) -> None:
    # Arrange — an UNVERSIONED requirement takes whatever happens to be newest
    # on the bake day and then freezes it into every downstream layer.
    block = _uv_pip_install_block(base_def_text)
    requirement = _scitex_todo_requirement(block)
    # Act
    floor = _requirement_floor(requirement)
    # Assert
    assert floor is not None, (
        "scitex-todo install must carry a version specifier (>=, == or ~=)"
        f" in apptainer-base.def; got {requirement!r} in:\n{block}"
    )


def test_scitex_todo_floor_is_at_least_the_wip_gate_capability(
    base_def_text: str,
) -> None:
    # Arrange — below the WIP-gate fix (scitex-todo #356, first released in
    # 0.8.0) every agent's WIP gate counts DEFERRED + CANCELLED cards as open.
    block = _uv_pip_install_block(base_def_text)
    floor = _requirement_floor(_scitex_todo_requirement(block))
    # Act
    meets_capability = floor is not None and floor >= _WIP_GATE_MIN_VERSION
    # Assert
    assert meets_capability, (
        "scitex-todo floor must be at-or-above the WIP-gate capability "
        f"{'.'.join(str(p) for p in _WIP_GATE_MIN_VERSION)} (scitex-todo #356);"
        f" apptainer-base.def declares {floor}:\n{block}"
    )


# ---------------------------------------------------------------------------
# The guard's own parser — so it cannot silently rot back into a substring
# match that recognises exactly one hardcoded floor and rejects every bump.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "requirement",
    [
        "scitex-todo[mcp]>=0.8.0",  # exactly the capability floor
        "scitex-todo[mcp]>=0.8",  # the same floor, written short
        "scitex-todo[mcp]>=0.9.4",  # today's floor
        "scitex-todo[mcp]>=0.9.4,<1.0",  # a floor with an upper bound
        "scitex-todo[mcp]==0.9.8",  # an exact pin
        "scitex-todo[mcp]>=1.0",  # a future major
    ],
)
def test_requirement_floor_accepts_the_capability_floor_or_stricter(
    requirement: str,
) -> None:
    # Arrange
    minimum = _WIP_GATE_MIN_VERSION
    # Act
    floor = _requirement_floor(requirement)
    # Assert
    assert floor is not None and floor >= minimum, requirement


@pytest.mark.parametrize(
    "requirement",
    [
        "scitex-todo[mcp]",  # unpinned — the drift that started this
        "scitex-todo[mcp]>=0.3.0",  # the stale floor the old guard froze
        "scitex-todo[mcp]>=0.7.50",  # the version the live SIFs shipped
    ],
)
def test_requirement_floor_rejects_unpinned_or_pre_wip_gate(requirement: str) -> None:
    # Arrange
    minimum = _WIP_GATE_MIN_VERSION
    # Act
    floor = _requirement_floor(requirement)
    # Assert
    assert floor is None or floor < minimum, requirement
