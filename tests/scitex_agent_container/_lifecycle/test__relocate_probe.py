"""A probe that fails must say "I could not tell", never "no".

`_relocate_preflight` is three-valued so that a failed check cannot masquerade
as a decided one. That guarantee is trivially destroyed one layer down: a single
`except: return False` in a prober turns "I could not reach the host" into "the
host says no", and the report then refuses — or worse, passes — for a reason
nobody can trace back.

The two directions, since only one of them is safe:

    probe raises -> False   a missing image reads as present, a busy port as
                            free; the relocation proceeds on fiction
    probe raises -> None    preflight reports UNKNOWN, refuses, and names the
                            check to re-run

These tests pin the second. They also pin that the failure TEXT survives, so a
report can say why a fact is missing rather than only that it is.

Real callables, including ones that raise. No mocks.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._relocate_preflight import preflight
from scitex_agent_container._lifecycle._relocate_probe import (
    TargetProbes,
    gather_target_facts,
    probe,
)


class Boom(RuntimeError):
    """A prober failing the way a network does."""


def _raises() -> bool:
    raise Boom("ssh: connect to host nas-03 port 22: Connection timed out")


# ---------------------------------------------------------------------------
# a failed probe is unobserved, not false
# ---------------------------------------------------------------------------


def test_a_successful_probe_is_observed() -> None:
    # Arrange
    outcome = probe(lambda: True)
    # Act
    observed = outcome.observed
    # Assert
    assert observed is True


def test_a_raising_probe_is_not_observed() -> None:
    # Arrange
    outcome = probe(_raises)
    # Act
    observed = outcome.observed
    # Assert
    assert observed is False


def test_a_raising_probe_yields_no_value() -> None:
    # Arrange: the value must be None, NOT False — False is an answer.
    outcome = probe(_raises)
    # Act
    value = outcome.value
    # Assert
    assert value is None


def test_a_raising_probe_keeps_the_failure_text() -> None:
    # Arrange: nothing is swallowed; it just stops being fatal.
    outcome = probe(_raises)
    # Act
    error = outcome.error
    # Assert
    assert "Connection timed out" in (error or "")


def test_a_probe_returning_false_is_still_an_answer() -> None:
    # Arrange: the distinction only works if an observed negative survives it.
    outcome = probe(lambda: False)
    # Act
    observed = outcome.observed
    # Assert
    assert observed is True


# ---------------------------------------------------------------------------
# gathering
# ---------------------------------------------------------------------------


def test_supplied_probes_populate_their_facts() -> None:
    # Arrange
    probes = TargetProbes(reachable=lambda: True, image_present=lambda: True)
    # Act
    result = gather_target_facts(probes)
    # Assert
    assert result.facts.reachable is True


def test_an_omitted_probe_leaves_its_fact_unobserved() -> None:
    # Arrange: "nobody asked" and "asked and could not tell" are the same thing
    # from the decision's point of view, and both differ from an answer.
    probes = TargetProbes(reachable=lambda: True)
    # Act
    result = gather_target_facts(probes)
    # Assert
    assert result.facts.image_present is None


def test_a_failing_probe_leaves_its_fact_unobserved() -> None:
    # Arrange: the failure this module exists to prevent is this becoming False.
    probes = TargetProbes(image_present=_raises)
    # Act
    result = gather_target_facts(probes)
    # Assert
    assert result.facts.image_present is None


def test_a_failing_probe_is_reported_by_name() -> None:
    # Arrange
    probes = TargetProbes(image_present=_raises)
    # Act
    result = gather_target_facts(probes)
    # Assert
    assert "image_present" in result.errors


def test_the_failure_reason_is_available_to_the_report() -> None:
    # Arrange: so a reader sees "UNKNOWN (ssh timed out)", not bare "UNKNOWN".
    probes = TargetProbes(image_present=_raises)
    # Act
    result = gather_target_facts(probes)
    # Assert
    assert "Connection timed out" in result.errors["image_present"]


def test_one_failing_probe_does_not_stop_the_others() -> None:
    # Arrange: a dry run that stops early makes the operator run it N times to
    # find N problems — the same reasoning as preflight returning every check.
    probes = TargetProbes(reachable=_raises, image_present=lambda: True)
    # Act
    result = gather_target_facts(probes)
    # Assert
    assert result.facts.image_present is True


def test_all_observed_is_true_when_nothing_failed() -> None:
    # Arrange
    probes = TargetProbes(reachable=lambda: True)
    # Act
    result = gather_target_facts(probes)
    # Assert
    assert result.all_observed is True


def test_all_observed_is_false_when_something_failed() -> None:
    # Arrange
    probes = TargetProbes(reachable=_raises)
    # Act
    result = gather_target_facts(probes)
    # Assert
    assert result.all_observed is False


def test_no_probes_at_all_yields_entirely_unobserved_facts() -> None:
    # Arrange: a caller that ran nothing must not produce a go.
    probes = TargetProbes()
    # Act
    result = gather_target_facts(probes)
    # Assert
    assert result.facts.reachable is None


# ---------------------------------------------------------------------------
# the two layers agree: a failed probe reaches preflight as UNKNOWN
# ---------------------------------------------------------------------------


def test_a_failed_probe_makes_preflight_refuse_rather_than_pass() -> None:
    # Arrange: everything healthy EXCEPT one probe that cannot answer. If the
    # adapter degraded that to False the verdict would be a decided refusal; if
    # it degraded to True it would be a go. It must be neither.
    probes = TargetProbes(
        reachable=lambda: True,
        image_present=lambda: True,
        missing_bind_sources=lambda: (),
        card_store_reachable=lambda: True,
        credential_expires_in_s=lambda: 3600.0,
        credential_refresh_token_present=_raises,  # <- cannot answer
        supported_runtimes=lambda: ("apptainer",),
        rejected_spec_keys=lambda: (),
        ports_in_use=lambda: (),
        hub_reachable_from_target=lambda: True,
    )
    gathered = gather_target_facts(probes)
    # Act
    report = preflight(
        agent="a", to_host="nas-03", facts=gathered.facts, runtime="apptainer"
    )
    # Assert
    assert report.ok is None


def test_a_fully_healthy_probe_set_passes_preflight() -> None:
    # Arrange: the positive control — without it, the test above could pass
    # because the probe set is wrong rather than because unknown propagates.
    probes = TargetProbes(
        reachable=lambda: True,
        image_present=lambda: True,
        missing_bind_sources=lambda: (),
        card_store_reachable=lambda: True,
        credential_expires_in_s=lambda: 3600.0,
        credential_refresh_token_present=lambda: True,
        supported_runtimes=lambda: ("apptainer",),
        rejected_spec_keys=lambda: (),
        ports_in_use=lambda: (),
        hub_reachable_from_target=lambda: True,
    )
    gathered = gather_target_facts(probes)
    # Act
    report = preflight(
        agent="a", to_host="nas-03", facts=gathered.facts, runtime="apptainer"
    )
    # Assert
    assert report.ok is True


def test_an_observed_negative_still_reaches_preflight_as_a_refusal() -> None:
    # Arrange: the adapter must not blunt real answers into unknowns either.
    probes = TargetProbes(
        reachable=lambda: True,
        image_present=lambda: False,  # <- a real, observed "no"
        missing_bind_sources=lambda: (),
        card_store_reachable=lambda: True,
        credential_expires_in_s=lambda: 3600.0,
        credential_refresh_token_present=lambda: True,
        supported_runtimes=lambda: ("apptainer",),
        rejected_spec_keys=lambda: (),
        ports_in_use=lambda: (),
        hub_reachable_from_target=lambda: True,
    )
    gathered = gather_target_facts(probes)
    # Act
    report = preflight(
        agent="a", to_host="nas-03", facts=gathered.facts, runtime="apptainer"
    )
    # Assert
    assert report.ok is False


def test_a_keyboard_interrupt_is_not_swallowed_as_a_probe_failure() -> None:
    # Arrange: broad catching is deliberate for probe errors, but an operator
    # pressing Ctrl-C must still stop the run rather than become "unobserved".
    def interrupted() -> bool:
        raise KeyboardInterrupt

    probes = TargetProbes(reachable=interrupted)

    # Act
    def run() -> object:
        return gather_target_facts(probes)

    # Assert
    with pytest.raises(KeyboardInterrupt):
        run()
