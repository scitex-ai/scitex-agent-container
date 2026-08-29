"""Static contract: both .def layers must bake the ``openai-agents`` SDK.

openai-compat-3: one image serves EVERY agent, so ``spec.provider:
openai`` cannot be a build-time conditional — the honest design bakes
the (small) ``openai-agents`` dependency unconditionally and keeps the
per-spec conditionality at RUNTIME (OPENAI_* env injection +
``openai_session`` executor routing, gated on the resolved family).

Layer contracts pinned here:

* ``apptainer-base.def`` installs sac with ``[all,dev]``
  (``/opt/scitex-agent-container-src[all,dev]``) — the floors stay in
  pyproject.toml (SSoT). The bracket was widened from ``[openai]`` on
  2026-08-11 so the image also ships ``[dev]``'s test tooling; because
  that makes openai-agents arrive INDIRECTLY through ``all``, the
  aggregate's membership is asserted here too. Without that second
  assert, deleting ``scitex-agent-container[openai]`` from ``all`` would
  silently stop baking the SDK while this file still passed.
* ``apptainer-scitex.def`` lists ``openai-agents`` EXPLICITLY in BOTH
  resolver branches (uv + pip fallback), because its sac install is
  ``--force-reinstall --no-deps`` (an extra can never ride that) and
  the transitive sac wheel from ``scitex[all]`` may predate the extra.

Drop either install and CI yells before a SIF rebuild ships an image
whose ``provider: openai`` agents crash on ``import agents``. Follows
the sibling ``test_apptainer_base_def_scitex_todo.py`` contract pattern.

STX-TQ002 AAA + STX-TQ007 one-assert per test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_CONTAINERS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "scitex_agent_container"
    / "containers"
)


_PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"

#: Matches the ``[openai]`` extra's requirement string in pyproject.toml.
_OPENAI_FLOOR_RE = re.compile(r'"(openai-agents>=[0-9][^"]*)"')


def _openai_floor() -> str:
    """The ``openai-agents`` floor as pyproject's ``[openai]`` extra declares it.

    DERIVED, NEVER SPELLED OUT. This file's whole job is to assert that two
    places agree on one version, so a test that hardcodes that version is a
    THIRD place to forget. It then fails on a LEGITIMATE bump, and its message
    ("the floor must be identical in both") misdescribes what it actually
    checked ("both files contain this literal").

    2026-08-30: exactly that happened. The floor moved 0.17.4 -> 0.19.0 in
    pyproject (the PostgreSQL-backed session needs the SDK's
    ``coerce_session_settings``, absent before 0.19.0) and this file failed
    while pointing at the .def — which was indeed stale, but raising it alone
    would have left the suite red with both files agreeing.
    """
    match = _OPENAI_FLOOR_RE.search(_PYPROJECT.read_text())
    if match is None:
        raise AssertionError(
            "pyproject.toml's [openai] extra no longer declares an "
            "openai-agents floor, so this file's contract has no subject."
        )
    return match.group(1)


@pytest.fixture(scope="module")
def base_def_text() -> str:
    # Arrange
    return (_CONTAINERS / "apptainer-base.def").read_text()


@pytest.fixture(scope="module")
def scitex_def_text() -> str:
    # Arrange
    return (_CONTAINERS / "apptainer-scitex.def").read_text()


# ---------------------------------------------------------------------------
# base layer — the [all] aggregate rides the bundled-source install, and
# [openai] + [dev] ride the aggregate
# ---------------------------------------------------------------------------


def test_base_def_installs_sac_with_all_and_dev_extras(base_def_text: str) -> None:
    # Arrange — `dev` is named explicitly even though `all` contains it, so
    # the requirement (the image must arrive able to run its own tests) is
    # legible in the line that implements it and survives a trimmed aggregate.
    needle = "/opt/scitex-agent-container-src[all,dev]"
    # Act
    present = needle in base_def_text
    # Assert
    assert present, (
        "apptainer-base.def must install the bundled sac source with the "
        "[all,dev] extras — they carry BOTH openai-agents (spec.provider: "
        "openai agents) and [dev]'s pytest tooling, without which a "
        "pristine container cannot run the suite; "
        f"expected {needle!r} in the uv pip install block."
    )


def test_all_extra_carries_openai_agents(base_def_text: str) -> None:
    # Arrange — base installs [all,dev], so the SDK now arrives INDIRECTLY
    # through `all`; dropping it from the aggregate would unbake it silently.
    pyproject = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text()
    # Act
    present = "scitex-agent-container[openai]" in pyproject
    # Assert
    assert present, (
        "pyproject's [all] aggregate must keep listing "
        "scitex-agent-container[openai]: apptainer-base.def reaches "
        "openai-agents THROUGH [all], so removing it there unbakes the SDK "
        "from every image while the .def line still looks correct."
    )


def test_all_extra_carries_dev_test_tooling(base_def_text: str) -> None:
    # Arrange — the reason the bracket was widened: [dev] is where pytest
    # and its plugins live, and [all] is how they reach the image.
    pyproject = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text()
    # Act
    present = "scitex-agent-container[dev]" in pyproject
    # Assert
    assert present, (
        "pyproject's [all] aggregate must keep listing "
        "scitex-agent-container[dev]: pytest / pytest-asyncio / "
        "pytest-xdist reach the base image through the extras, and a "
        "pristine container that loses them answers NO_PYTEST_ON_PATH "
        "again while every agent hand-installs a different pytest major. "
        "apptainer-base.def also names [dev] directly, so this assert is "
        "the belt to that braces — keep BOTH."
    )


# ---------------------------------------------------------------------------
# scitex layer — explicit floor in BOTH resolver branches (sac install
# there is --no-deps, so the extra cannot ride it)
# ---------------------------------------------------------------------------


def test_scitex_def_lists_openai_agents_floor(scitex_def_text: str) -> None:
    # Arrange
    needle = _openai_floor()
    # Act
    present = needle in scitex_def_text
    # Assert
    assert present, (
        "apptainer-scitex.def must list an explicit openai-agents floor "
        "(its sac install is --no-deps, so pyproject's [openai] extra "
        "cannot ride it)."
    )


def test_scitex_def_floors_openai_agents_in_both_branches(
    scitex_def_text: str,
) -> None:
    # Arrange — uv branch AND the pip fallback must stay in lockstep.
    occurrences = scitex_def_text.count("openai-agents>=")
    # Act
    both = occurrences >= 2
    # Assert
    assert both, (
        "openai-agents floor must appear in BOTH the uv and the pip-"
        f"fallback install branches of apptainer-scitex.def (found "
        f"{occurrences})."
    )


def test_scitex_def_floor_matches_pyproject_extra(scitex_def_text: str) -> None:
    # Arrange — pyproject's [openai] extra is the SSoT; the .def must echo
    # whatever it currently says, so a bump is ONE edit and this test follows.
    floor = _openai_floor()
    # Act
    echoed = floor in scitex_def_text
    # Assert
    assert echoed, (
        f"apptainer-scitex.def must carry pyproject's openai-agents floor "
        f"({floor!r}); a bump in one without the other is drift this test "
        "refuses. Both resolver branches need it — see the sibling test that "
        "counts occurrences."
    )
