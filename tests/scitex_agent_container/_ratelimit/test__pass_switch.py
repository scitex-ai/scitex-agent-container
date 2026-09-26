"""Tests for the MODEL-CAP branch of ``_ratelimit._pass`` — armed and unarmed.

Real on-disk v3 specs, a real temp switch ledger, real captured banner text,
and a real recorder standing in for the mutation. Nothing is patched and
nothing is monkeypatched: every collaborator is a production keyword argument
with a real default.

THE CONTROL THIS FILE IS BUILT AROUND
    :func:`test_the_flag_off_changes_nothing` and
    :func:`test_the_flag_on_switches_the_capped_agent` are ONE experiment run
    twice. Same fleet, same spec, same captured panes, same clock — the ONLY
    variable is ``switch_model``. A green result in the second without the
    first would not show that the default is safe, and "the default is safe"
    is the promise that lets this ship before the operator has decided to arm
    it.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml as _yaml

from scitex_agent_container._events import read_events
from scitex_agent_container._ratelimit._pass import resume_pass
from scitex_agent_container._ratelimit._rule import Verdict
from tests.scitex_agent_container._helpers.explicit_spec import explicit_spec

#: The Fable cap exactly as the harness answered the operator on 2026-09-06,
#: in the pane position it really occupied.
FABLE_PANE = "\n".join(
    [
        "● Reading the spec...",
        "  ⎿ You've reached your Fable limit. Run /usage-credits to continue "
        "or switch models with /model.",
        "────────────────────────────────────────────",
        "❯ ",
    ]
)

NOW = 1_800_000_000.0


class SwitchRecorder:
    """A real switch callable that records instead of typing into a pane.

    Not a mock: a plain object with the production signature
    ``(agent, target) -> bool | None``. ``calls`` is the evidence a test reads
    to prove a switch did — or, more importantly, did NOT — happen.
    """

    def __init__(self, result: bool | None = True) -> None:
        self.calls: list[tuple[str, str]] = []
        self._result = result

    def __call__(self, name: str, target: str) -> bool | None:
        self.calls.append((name, target))
        return self._result


def write_fable_spec(registry: Path, name: str = "alpha") -> Path:
    """A real dir-as-SSoT v3 ``<name>/spec.yaml`` running a Fable model."""
    agent_dir = registry / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    body = explicit_spec(
        {
            "host": "${HOSTNAME}",
            "runtime": "apptainer",
            "claude": {"model": "claude-fable-5"},
            "apptainer": {"image": "/opt/sac/scitex.sif", "binds": []},
            "health": {"enabled": True, "interval": 60},
        }
    )
    body["workdir"] = f"~/.scitex/agent-container/runtime/agents/{name}"
    body["restart"].update({"policy": "on-failure", "max_retries": 3})
    (agent_dir / "spec.yaml").write_text(
        _yaml.safe_dump(
            {
                "apiVersion": "scitex-agent-container/v3",
                "kind": "Agent",
                "metadata": {"labels": {}},
                "spec": body,
            }
        )
    )
    return agent_dir / "spec.yaml"


@pytest.fixture()
def fleet(tmp_path):
    """One managed Fable agent whose live pane shows tonight's cap."""
    registry = tmp_path / "agents"
    write_fable_spec(registry)
    return {
        "specs_dir": registry,
        "history_file": tmp_path / "resume-history.json",
        "switch_history_file": tmp_path / "switch-history.json",
        "events_path": tmp_path / "sac-events.jsonl",
    }


def _run(fleet, *, switcher, apply=True, switch_model=True, now=NOW, **overrides):
    kwargs = dict(fleet)
    kwargs.update(
        {
            "apply": apply,
            "now": now,
            "switch_model": switch_model,
            "switch_fn": switcher,
            "resume_fn": lambda name: True,
            "capture_fn": lambda: {"alpha": (FABLE_PANE, FABLE_PANE)},
        }
    )
    kwargs.update(overrides)
    return resume_pass(**kwargs)


def _verdict_of(outcome, name: str) -> Verdict:
    return next(r.verdict for r in outcome.reports if r.name == name)


# --- THE CONTROL: one experiment, the flag as the only variable -------------


def test_the_flag_off_changes_nothing(fleet) -> None:
    # Arrange — the RED half. With the switcher unarmed this pass must be the
    # pass it was before the branch existed: the Fable banner carries no
    # reset clause, so the rate-wall rule sees no wall and does nothing.
    switcher = SwitchRecorder()
    # Act
    _run(fleet, switcher=switcher, switch_model=False)
    # Assert
    assert switcher.calls == []


def test_the_flag_off_reports_not_limited(fleet) -> None:
    # Arrange — the same unarmed pass. This is the GAP the branch exists to
    # close, stated as a test: today sac looks at a visibly capped agent and
    # reports that there is nothing here.
    switcher = SwitchRecorder()
    # Act
    outcome = _run(fleet, switcher=switcher, switch_model=False)
    # Assert
    assert _verdict_of(outcome, "alpha") is Verdict.NOT_LIMITED


def test_the_flag_on_switches_the_capped_agent(fleet) -> None:
    # Arrange — the GREEN half. Same fleet, same spec, same panes, same
    # clock; only the flag moved. This is the claim that the operator's two
    # unanswered messages would have been answered.
    switcher = SwitchRecorder()
    # Act
    _run(fleet, switcher=switcher)
    # Assert
    assert switcher.calls == [("alpha", "opus[1m]")]


def test_a_performed_switch_is_reported_switched(fleet) -> None:
    # Arrange — a proven switch. The verdict is what the timer log carries,
    # so it must say what happened rather than leaving a silent success.
    switcher = SwitchRecorder()
    # Act
    outcome = _run(fleet, switcher=switcher)
    # Assert
    assert _verdict_of(outcome, "alpha") is Verdict.SWITCHED


# --- read-only stays read-only ---------------------------------------------


def test_the_check_mode_types_nothing(fleet) -> None:
    # Arrange — --switch-model without --apply. Detection is read-only and
    # arming a remedy must not silently arm its mutation too.
    switcher = SwitchRecorder()
    # Act
    _run(fleet, switcher=switcher, apply=False)
    # Assert
    assert switcher.calls == []


def test_the_check_mode_reports_would_switch(fleet) -> None:
    # Arrange — read-only is not silent. An operator previewing the remedy
    # must see which agents it would touch.
    switcher = SwitchRecorder()
    # Act
    outcome = _run(fleet, switcher=switcher, apply=False)
    # Assert
    assert _verdict_of(outcome, "alpha") is Verdict.WOULD_SWITCH


# --- the ledger: never twice for one incarnation ----------------------------


def test_a_second_pass_does_not_switch_again(fleet) -> None:
    # Arrange — one pass switches, then the SAME ledger is read by a second
    # pass minutes later against the same frozen pane. Without the debounce
    # this is a /model typed into the agent every five minutes forever.
    switcher = SwitchRecorder()
    _run(fleet, switcher=switcher)
    # Act
    _run(fleet, switcher=switcher, now=NOW + 300.0)
    # Assert
    assert len(switcher.calls) == 1


def test_a_second_pass_reports_cooling_down(fleet) -> None:
    # Arrange — holding must be a STATED verdict, not an absence: an agent
    # nobody reports on is an agent nobody watches.
    switcher = SwitchRecorder()
    _run(fleet, switcher=switcher)
    # Act
    outcome = _run(fleet, switcher=switcher, now=NOW + 300.0)
    # Assert
    assert _verdict_of(outcome, "alpha") is Verdict.COOLING_DOWN


def test_the_switch_ledger_is_written(fleet) -> None:
    # Arrange — the debounce above is only real if the memory survives the
    # process. This pass is a short-lived cron job; RAM is not a ledger.
    switcher = SwitchRecorder()
    # Act
    _run(fleet, switcher=switcher)
    # Assert
    assert "alpha" in fleet["switch_history_file"].read_text()


def test_the_resume_ledger_is_not_spent(fleet) -> None:
    # Arrange — two remedies, two ledgers. A shared flat {agent: [epoch]}
    # file would let one switch disarm the resume debounce for the same
    # agent, and neither remedy could then be reasoned about alone.
    switcher = SwitchRecorder()
    # Act
    _run(fleet, switcher=switcher)
    # Assert
    assert fleet["history_file"].read_text().strip() in ("{}", '{\n}')


# --- an unprovable switch is an unresolved reading, not a success -----------


def test_an_unverified_switch_is_reported(fleet) -> None:
    # Arrange — the mutation could not prove the model changed. That must
    # never be logged as a healthy tick, because the operator would read it
    # as an agent that came back.
    switcher = SwitchRecorder(result=None)
    # Act
    outcome = _run(fleet, switcher=switcher)
    # Assert
    assert _verdict_of(outcome, "alpha") is Verdict.SWITCH_UNVERIFIED


def test_an_unverified_switch_is_recorded_degraded(fleet) -> None:
    # Arrange — an enforcer that gives up SILENTLY is the original bug with
    # extra steps. The record is written to a real temp event log and read
    # back with the production reader.
    switcher = SwitchRecorder(result=None)
    # Act
    _run(fleet, switcher=switcher)
    # Assert
    assert any(
        e.subject == "alpha" and e.event == "subject-degraded"
        for e in read_events(fleet["events_path"])
    )


def test_a_later_success_records_the_recovery(fleet) -> None:
    # Arrange — the other rail, and it is a TRANSITION rather than a
    # heartbeat: sac records a degraded subject once and its recovery when it
    # actually recovers. So this is one agent across two passes an hour
    # apart — failed first (degraded), switched second — which is the only
    # sequence that can produce a recovery record at all.
    _run(fleet, switcher=SwitchRecorder(result=False))
    # Act
    _run(fleet, switcher=SwitchRecorder(result=True), now=NOW + 3600.0)
    # Assert
    assert any(
        e.subject == "alpha" and e.event == "subject-recovered"
        for e in read_events(fleet["events_path"])
    )


def test_an_unverified_switch_exits_two(fleet) -> None:
    # Arrange — 2 is this command's "something could not be determined",
    # alongside an unreadable pane and an unreadable reset clause. Each needs
    # a human; none may pass as clean.
    switcher = SwitchRecorder(result=None)
    # Act
    outcome = _run(fleet, switcher=switcher)
    # Assert
    assert outcome.exit_code() == 2
