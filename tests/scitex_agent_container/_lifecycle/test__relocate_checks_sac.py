"""The hint that told an operator to set a thing that was already set.

Two measurements, and the check has to satisfy both:

    2026-08-11, scitex-compute-04: sac is at ~/.env-sac/bin/sac and absent from
    the non-interactive ssh PATH, so `ssh compute-04 sac …` says "No such file
    or directory" — the same words a machine with no sac produces. INSTALLED and
    FINDABLE must stay separable, because they need opposite fixes.

    2026-08-12, ywata-note-win: the check FAILED and said "set the peer's
    env_preamble". The peer declares `export PATH="$HOME/.env-3.11/bin:$PATH"`,
    it works, and Shell prepends it to every command a relocation sends. So the
    check was failing on a PATH nothing in this feature uses, and answering with
    a step already taken.

The property under test is therefore WHICH PATH THE ANSWER IS ABOUT, and that
the hint branches on whether a preamble already exists rather than recommending
one unconditionally — the same narrowness `_state._remote_sac_hint` documents.

Pure predicates over observed facts. No I/O, no mocks.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._relocate_checks_sac import (
    CHECK_SAC_PRESENT,
    check_sac_present,
)
from scitex_agent_container._lifecycle._relocate_preflight_facts import TargetFacts

HOST = "ywata-note-win"
VENV_SAC = "/home/ywatanabe/.env-3.11/bin/sac"


@pytest.fixture
def preamble_host() -> TargetFacts:
    """ywata-note-win as it actually is: not on the bare PATH, reachable anyway."""
    return TargetFacts(
        sac_on_path=False,
        sac_resolved_path=VENV_SAC,
        sac_usable_path=VENV_SAC,
        preamble_declared=True,
    )


def test_a_host_reachable_only_via_its_preamble_passes(
    preamble_host: TargetFacts,
) -> None:
    # Arrange: the ywata-note-win measurement. The old check FAILED here.
    facts = preamble_host
    # Act
    check = check_sac_present(facts, HOST)
    # Assert
    assert check.ok is True


def test_the_pass_says_the_preamble_is_what_makes_it_reachable(
    preamble_host: TargetFacts,
) -> None:
    # Arrange: a pass that hides its reason cannot be audited later.
    facts = preamble_host
    # Act
    check = check_sac_present(facts, HOST)
    # Assert
    assert "env_preamble" in check.detail


def test_sac_on_the_bare_path_still_passes_on_its_own() -> None:
    # Arrange: an older probe supplies only this fact, and it is a real answer.
    facts = TargetFacts(sac_on_path=True, sac_resolved_path="/usr/local/bin/sac")
    # Act
    check = check_sac_present(facts, HOST)
    # Assert
    assert check.ok is True


def test_nothing_observed_at_all_is_unknown() -> None:
    # Arrange
    facts = TargetFacts()
    # Act
    check = check_sac_present(facts, HOST)
    # Assert
    assert check.ok is None


def test_an_unmeasured_preamble_effect_is_unknown_not_a_failure() -> None:
    # Arrange: the bare PATH says no, a preamble is declared, and nobody measured
    # what it does. That is a question, not a verdict.
    facts = TargetFacts(
        sac_on_path=False, sac_resolved_path=VENV_SAC, preamble_declared=True
    )
    # Act
    check = check_sac_present(facts, HOST)
    # Assert
    assert check.ok is None


def test_sac_installed_nowhere_fails_as_not_installed() -> None:
    # Arrange: looked-and-found-nothing is the empty string, not None.
    facts = TargetFacts(
        sac_on_path=False, sac_usable_path="", sac_resolved_path="", preamble_declared=False
    )
    # Act
    check = check_sac_present(facts, HOST)
    # Assert
    assert "NOT INSTALLED" in check.detail


def test_not_installed_is_told_to_install_it() -> None:
    # Arrange
    facts = TargetFacts(
        sac_on_path=False, sac_usable_path="", sac_resolved_path="", preamble_declared=False
    )
    # Act
    check = check_sac_present(facts, HOST)
    # Assert
    assert "install sac" in check.hint


def test_installed_but_unreachable_is_reported_as_installed_not_missing() -> None:
    # Arrange: the two failures produce the same shell error and need opposite
    # fixes, so the DETAIL has to say which one this is.
    facts = TargetFacts(
        sac_on_path=False,
        sac_usable_path="",
        sac_resolved_path="/home/ywatanabe/.env-sac/bin/sac",
        preamble_declared=False,
    )
    # Act
    check = check_sac_present(facts, "scitex-compute-04")
    # Assert
    assert "IS INSTALLED" in check.detail


def test_installed_but_unreachable_names_where_it_actually_is() -> None:
    # Arrange
    facts = TargetFacts(
        sac_on_path=False,
        sac_usable_path="",
        sac_resolved_path="/home/ywatanabe/.env-sac/bin/sac",
        preamble_declared=False,
    )
    # Act
    check = check_sac_present(facts, "scitex-compute-04")
    # Assert
    assert "/home/ywatanabe/.env-sac/bin/sac" in check.detail


def test_installed_but_unreachable_with_no_preamble_is_told_to_add_one() -> None:
    # Arrange: the 2026-08-11 scitex-compute-04 shape, on a peer with no preamble.
    facts = TargetFacts(
        sac_on_path=False,
        sac_usable_path="",
        sac_resolved_path="/home/ywatanabe/.env-sac/bin/sac",
        preamble_declared=False,
    )
    # Act
    check = check_sac_present(facts, "scitex-compute-04")
    # Assert
    assert "declares NO env_preamble" in check.hint


def test_installed_but_unreachable_WITH_a_preamble_is_not_told_to_add_one() -> None:
    # Arrange: THE 2026-08-12 DEFECT. A peer that already declares one has a
    # different problem, and naming env_preamble as the fix is a wrong answer.
    facts = TargetFacts(
        sac_on_path=False,
        sac_usable_path="",
        sac_resolved_path=VENV_SAC,
        preamble_declared=True,
    )
    # Act
    check = check_sac_present(facts, HOST)
    # Assert
    assert "do NOT add an env_preamble" in check.hint


def test_that_hint_points_at_the_preamble_itself_as_the_thing_to_fix() -> None:
    # Arrange: a refusal must name the next step, and here the next step is
    # comparing what the preamble exports against where sac actually is.
    facts = TargetFacts(
        sac_on_path=False,
        sac_usable_path="",
        sac_resolved_path=VENV_SAC,
        preamble_declared=True,
    )
    # Act
    check = check_sac_present(facts, HOST)
    # Assert
    assert "the preamble itself is what to fix" in check.hint


def test_an_unknown_preamble_state_asks_before_recommending() -> None:
    # Arrange: nobody recorded whether a preamble exists, so neither branch of
    # the advice is safe to give outright.
    facts = TargetFacts(
        sac_on_path=False, sac_usable_path="", sac_resolved_path=VENV_SAC
    )
    # Act
    check = check_sac_present(facts, HOST)
    # Assert
    assert "Check whether" in check.hint


def test_an_uninspected_install_location_is_unknown_not_a_failure() -> None:
    # Arrange: not reachable, and nobody looked for it anywhere else.
    facts = TargetFacts(sac_on_path=False, sac_usable_path="", preamble_declared=False)
    # Act
    check = check_sac_present(facts, HOST)
    # Assert
    assert check.ok is None


def test_the_check_is_named_for_the_report(preamble_host: TargetFacts) -> None:
    # Arrange
    facts = preamble_host
    # Act
    check = check_sac_present(facts, HOST)
    # Assert
    assert check.name == CHECK_SAC_PRESENT
