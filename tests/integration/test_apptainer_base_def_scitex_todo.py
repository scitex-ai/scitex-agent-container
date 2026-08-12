"""Static contract: ``apptainer-base.def``'s scitex-cards install must PROVIDE
fastmcp and psycopg — by an extra, or by core at a high enough floor.

The pin is BARE: ``scitex-cards>=0.32.0``. Both deps are core from 0.32.0
(psycopg from 0.31.8), so there is no extras subset left to pick wrong, and the
floor is what carries the guarantee.

This docstring has been WRONG THREE TIMES: it said "must install
scitex-cards[mcp]", then "the pin is ``scitex-cards[all]``", each time
outliving the assertions below by hours. Once, a peer read the stale line and
concluded the suite still demanded a deleted extra. A stale summary is as
misleading as a stale assertion and cheaper to overlook, because nothing
executes it — so state the PROPERTY (provides fastmcp + psycopg) and mention
today's spelling only as an example.

Package renamed scitex-todo -> scitex-cards (2026-07-16, migration S1-S3).
The wheel ships BOTH console scripts (``scitex-todo`` and ``scitex-cards``)
and the ``scitex_todo`` import shim, so the runtime claims below — the
``.mcp.json`` entry invoking ``scitex-todo mcp start`` — stay true with the
new dist installed. What this file pins is the DIST the .def installs.

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

# The quoted requirement token naming the board package (renamed
# scitex-cards 2026-07-16), e.g. "scitex-cards[mcp]==0.16.0".
_SCITEX_TODO_REQ_RE = re.compile(r'"(?P<req>[^"]*\bscitex-cards\b[^"]*)"')
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
    present = "scitex-cards" in block
    # Assert
    assert present, (
        f"scitex-cards missing from uv pip install in apptainer-base.def:\n{block}"
    )


def _requirement_extras(requirement: str) -> set[str]:
    """The extras NAMED in a requirement — ``[mcp,postgres]`` -> {mcp, postgres}.

    Parsed rather than substring-matched. The old check was
    ``"scitex-cards[mcp]" in block``, which asserts the extras list is EXACTLY
    ``[mcp]`` while reading as "carries the mcp extra": adding a second,
    equally required extra made the substring vanish and turned a correct
    change red. A contract test should pin what it claims to pin.
    """
    if "[" not in requirement or "]" not in requirement:
        return set()
    inner = requirement.split("[", 1)[1].split("]", 1)[0]
    return {part.strip() for part in inner.split(",") if part.strip()}


# A capability can be provided TWO ways: by an extra, or by CORE at a
# sufficient floor. Asserting only the extras spelling has now failed this file
# THREE times in one day:
#   1. `"scitex-cards[mcp]" in block`  -- froze the extras list to exactly [mcp];
#      adding `postgres` turned a correct change red
#   2. `"postgres" in extras`          -- my replacement; then called `[all]`
#      a regression for the same reason
#   3. `extras & {"mcp","all"}`        -- correct for extras, and blind to the
#      pin going BARE when 0.32.0 moved the deps to core
# So the fourth version asks the real question: WILL THIS INSTALL PROVIDE THE
# CAPABILITY -- from an extra, or from core because the floor is high enough.
_EXTRAS_PROVIDING_FASTMCP = {"mcp", "all"}
_EXTRAS_PROVIDING_PSYCOPG = {"postgres", "all"}

#: scitex-cards versions at which each dep became a CORE requirement, verified
#: against published PyPI metadata rather than the changelog. psycopg went core
#: in 0.31.8 -- NOT 0.31.6, which is what a peer told me and what would have
#: made a bare pin admit two driverless versions.
_CORE_SINCE_FASTMCP = (0, 32, 0)
_CORE_SINCE_PSYCOPG = (0, 31, 8)


def _provides(requirement: str, extras_providing: set[str], core_since) -> bool:
    """Does this requirement deliver the capability, by EITHER route?

    Reuses ``_requirement_floor`` deliberately. A second, local floor parser is
    how ">=0.32" would compare LESS than (0, 32, 0) — the padding bug that
    function already carries a comment about. One parser, one behaviour.
    """
    if _requirement_extras(requirement) & extras_providing:
        return True
    floor = _requirement_floor(requirement)
    return floor is not None and floor >= core_since


def test_uv_pip_install_block_can_provide_fastmcp(base_def_text: str) -> None:
    # Arrange
    block = _uv_pip_install_block(base_def_text)
    requirement = _scitex_todo_requirement(block)
    # Act
    provided = _provides(requirement, _EXTRAS_PROVIDING_FASTMCP, _CORE_SINCE_FASTMCP)
    # Assert
    assert provided, (
        "scitex-cards install must PROVIDE fastmcp — via an extra "
        f"{sorted(_EXTRAS_PROVIDING_FASTMCP)} or a floor >= "
        f"{'.'.join(map(str, _CORE_SINCE_FASTMCP))} where it is core. "
        f"Got {requirement!r} in:\n{block}"
    )


def test_uv_pip_install_block_can_provide_psycopg(base_def_text: str) -> None:
    # Arrange — the cards store is PostgreSQL. Without a driver EVERY agent's
    # card writes fail, reported as "canonical store ... does not exist" —
    # naming the database, which is fine. Measured 2026-08-02: the pin was
    # [mcp] only, and site-packages held a bare psycopg/ directory with no
    # __init__.py, which imports as a NAMESPACE PACKAGE and exposes no
    # .connect, so an import-guarded check passed while the board was down.
    #
    # The floor matters as much as the extra here: psycopg became core in
    # 0.31.8, so a BARE pin of >=0.31.6 would resolve a driverless version and
    # succeed. That is why this asserts the floor and not merely bareness.
    block = _uv_pip_install_block(base_def_text)
    requirement = _scitex_todo_requirement(block)
    # Act
    provided = _provides(requirement, _EXTRAS_PROVIDING_PSYCOPG, _CORE_SINCE_PSYCOPG)
    # Assert
    assert provided, (
        "scitex-cards install must PROVIDE psycopg — via an extra "
        f"{sorted(_EXTRAS_PROVIDING_PSYCOPG)} or a floor >= "
        f"{'.'.join(map(str, _CORE_SINCE_PSYCOPG))} where it is core. "
        f"Got {requirement!r} in:\n{block}"
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
        "scitex-cards install must carry a version specifier (>=, == or ~=)"
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
        "scitex-cards[mcp]>=0.8.0",  # exactly the capability floor
        "scitex-cards[mcp]>=0.8",  # the same floor, written short
        "scitex-cards[mcp]>=0.9.4",  # today's floor
        "scitex-cards[mcp]>=0.9.4,<1.0",  # a floor with an upper bound
        "scitex-cards[mcp]==0.9.8",  # an exact pin
        "scitex-cards[mcp]>=1.0",  # a future major
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
        "scitex-cards[mcp]",  # unpinned — the drift that started this
        "scitex-cards[mcp]>=0.3.0",  # the stale floor the old guard froze
        "scitex-cards[mcp]>=0.7.50",  # the version the live SIFs shipped
    ],
)
def test_requirement_floor_rejects_unpinned_or_pre_wip_gate(requirement: str) -> None:
    # Arrange
    minimum = _WIP_GATE_MIN_VERSION
    # Act
    floor = _requirement_floor(requirement)
    # Assert
    assert floor is None or floor < minimum, requirement


# ---------------------------------------------------------------------------
# ...and the CAPABILITY predicate itself. Without these, "provides psycopg"
# passes for the same reason the fixture-that-cannot-fail passes: nothing
# demonstrates the predicate can say NO. The rejection cases below are the ones
# that actually shipped the outage.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "requirement",
    [
        "scitex-cards>=0.32.0",  # today's bare pin — psycopg is core
        "scitex-cards>=0.31.8",  # the release psycopg became core in
        "scitex-cards[postgres]>=0.20.0",  # the old extras route
        "scitex-cards[all]>=0.31.6",  # the interim [all] pin
    ],
)
def test_provides_psycopg_accepts_extras_or_a_core_floor(requirement: str) -> None:
    # Arrange
    providing = _EXTRAS_PROVIDING_PSYCOPG
    # Act
    provided = _provides(requirement, providing, _CORE_SINCE_PSYCOPG)
    # Assert
    assert provided, requirement


@pytest.mark.parametrize(
    "requirement",
    [
        "scitex-cards[mcp]>=0.19.0",  # THE OUTAGE: mcp only, no driver
        "scitex-cards>=0.31.6",  # bare but BELOW core — driverless
        "scitex-cards>=0.31.7",  # ditto, the release right before
        "scitex-cards",  # bare and unpinned — resolves to anything
    ],
)
def test_provides_psycopg_rejects_a_driverless_install(requirement: str) -> None:
    # Arrange
    providing = _EXTRAS_PROVIDING_PSYCOPG
    # Act
    provided = _provides(requirement, providing, _CORE_SINCE_PSYCOPG)
    # Assert
    assert not provided, requirement
