"""Static contract: both .def layers must bake the ``openai-agents`` SDK.

openai-compat-3: one image serves EVERY agent, so ``spec.provider:
openai`` cannot be a build-time conditional — the honest design bakes
the (small) ``openai-agents`` dependency unconditionally and keeps the
per-spec conditionality at RUNTIME (OPENAI_* env injection +
``openai_session`` executor routing, gated on the resolved family).

Layer contracts pinned here:

* ``apptainer-base.def`` installs sac WITH its ``[openai]`` extra
  (``/opt/scitex-agent-container-src[openai]``) — the floor stays in
  pyproject.toml (SSoT).
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

from pathlib import Path

import pytest

_CONTAINERS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "scitex_agent_container"
    / "containers"
)


@pytest.fixture(scope="module")
def base_def_text() -> str:
    # Arrange
    return (_CONTAINERS / "apptainer-base.def").read_text()


@pytest.fixture(scope="module")
def scitex_def_text() -> str:
    # Arrange
    return (_CONTAINERS / "apptainer-scitex.def").read_text()


# ---------------------------------------------------------------------------
# base layer — sac's own [openai] extra rides the bundled-source install
# ---------------------------------------------------------------------------


def test_base_def_installs_sac_with_openai_extra(base_def_text: str) -> None:
    # Arrange
    needle = "/opt/scitex-agent-container-src[openai]"
    # Act
    present = needle in base_def_text
    # Assert
    assert present, (
        "apptainer-base.def must install the bundled sac source WITH the "
        "[openai] extra (openai-agents for spec.provider: openai agents); "
        f"expected {needle!r} in the uv pip install block."
    )


# ---------------------------------------------------------------------------
# scitex layer — explicit floor in BOTH resolver branches (sac install
# there is --no-deps, so the extra cannot ride it)
# ---------------------------------------------------------------------------


def test_scitex_def_lists_openai_agents_floor(scitex_def_text: str) -> None:
    # Arrange
    needle = "openai-agents>=0.17.4"
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
    # Arrange — the .def floor mirrors pyproject's [openai] extra; a bump
    # in one without the other is drift this test refuses.
    pyproject = (
        Path(__file__).resolve().parents[2] / "pyproject.toml"
    ).read_text()
    # Act
    in_both = "openai-agents>=0.17.4" in pyproject and (
        "openai-agents>=0.17.4" in scitex_def_text
    )
    # Assert
    assert in_both, (
        "the openai-agents floor must be identical in pyproject.toml's "
        "[openai] extra and apptainer-scitex.def (keep in lockstep)."
    )
