"""Tests for the declared hook floor and the refusal it produces.

The four states the brief demands, plus the one the coordinator added after
``scitex-cards health`` showed it working: UNKNOWN must not collapse into
either pass or fail.

PA-306 no-mocks: real hook trees under ``tmp_path``, real ``AgentConfig``
objects built by the real loader, real ``caplog`` for the bypass record.
STX-TQ002/TQ007: AAA markers, one fact per test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._claude_hooks._errors import MissingRequiredHooks
from scitex_agent_container._claude_hooks._floor import (
    evaluate_floor,
    measurement_site,
    unknown_event_dirs,
)
from scitex_agent_container._claude_hooks._gate import (
    ALLOW_ENV,
    check_required_hooks,
)
from scitex_agent_container._claude_hooks._report import hooks_health

from ._trees import effective_home, layer_only_home, write_hooks

_AGENT = "floor-fixture"
_MISSED = "log_post_tool_use.sh"
_FLOOR = {"post-tool-use": [_MISSED]}


class _Spec:
    """A minimal real config-shaped object (no mock: a plain value holder).

    The gate reads exactly three attributes off a config, and the loader-built
    ``AgentConfig`` is covered separately in ``config/test__declarations.py``.
    Using a value holder here keeps each test about the FLOOR rather than about
    spec parsing.
    """

    def __init__(self, floor, name: str = _AGENT, config_path: str = "/spec.yaml"):
        self.required_claude_hooks = floor
        self.name = name
        self.config_path = config_path


@pytest.fixture
def as_the_agent(env_save_restore):
    """Make this process look like ``_AGENT`` so the measurement site is valid."""
    env_save_restore.set("SAC_NAME", _AGENT)
    env_save_restore.delete(ALLOW_ENV)
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_ALLOW_MISSING_HOOKS")
    return _AGENT


def _report(home: Path, floor, agent: str = _AGENT) -> dict:
    return hooks_health(_Spec(floor), agent_name=agent, home=str(home))


def _refusal_message(report: dict, **kwargs) -> str:
    """The refusal text, or ``""`` when the gate let the start proceed.

    Collapsing "did it refuse" and "what did it say" into one returned string
    keeps each test to a single assertion (STX-TQ007) without hiding the
    exception type: only :class:`MissingRequiredHooks` is caught, so any other
    error still propagates and fails the test loudly.
    """
    try:
        check_required_hooks(report, **kwargs)
    except MissingRequiredHooks as exc:
        return str(exc)
    return ""


class TestTheFourOutcomes:
    def test_undeclared_floor_does_not_refuse(self, tmp_path: Path, as_the_agent):
        # Arrange — the fleet's position today: 101 of 102 specs declare nothing.
        report = _report(layer_only_home(tmp_path), None)
        # Act
        proceeded = check_required_hooks(report)
        # Assert
        assert proceeded is True

    def test_undeclared_floor_emits_no_refusal_log(
        self, tmp_path: Path, as_the_agent, caplog
    ):
        # Arrange — "no refusal, no warning spam": an undeclared spec must not
        # put a single ERROR line in front of the operator.
        report = _report(layer_only_home(tmp_path), None)
        # Act
        with caplog.at_level("ERROR"):
            check_required_hooks(report)
        # Assert
        assert caplog.records == []

    def test_satisfied_floor_starts(self, tmp_path: Path, as_the_agent):
        # Arrange — the EFFECTIVE view has the hook the layer view lacks.
        report = _report(effective_home(tmp_path), _FLOOR)
        # Act
        proceeded = check_required_hooks(report)
        # Assert
        assert proceeded is True

    def test_unsatisfied_floor_refuses(self, tmp_path: Path, as_the_agent):
        # Arrange
        report = _report(layer_only_home(tmp_path), _FLOOR)
        # Act
        raised = _refusal_message(report)
        # Assert
        assert raised != ""

    def test_refusal_names_the_missing_hook(self, tmp_path: Path, as_the_agent):
        # Arrange — a refusal that does not say WHICH hook is a refusal nobody
        # can act on.
        report = _report(layer_only_home(tmp_path), _FLOOR)
        # Act
        message = _refusal_message(report)
        # Assert
        assert _MISSED in message

    def test_refusal_says_where_the_hook_should_have_come_from(
        self, tmp_path: Path, as_the_agent
    ):
        # Arrange — the coordinator's second ask: every failing check carries an
        # actionable hint, not just a name.
        report = _report(layer_only_home(tmp_path), _FLOOR)
        # Act
        message = _refusal_message(report)
        # Assert
        assert ".claude/hooks/post-tool-use/" in message


class TestTheOverride:
    def test_override_starts_despite_an_unsatisfied_floor(
        self, tmp_path: Path, as_the_agent
    ):
        # Arrange
        report = _report(layer_only_home(tmp_path), _FLOOR)
        # Act
        proceeded = check_required_hooks(report, allow_missing=True)
        # Assert
        assert proceeded is True

    def test_override_records_that_it_overrode(
        self, tmp_path: Path, as_the_agent, caplog
    ):
        # Arrange — "a silent override is just a slower version of the warning
        # nobody read" (PR #949). The bypass logs at ERROR, same as the refusal.
        report = _report(layer_only_home(tmp_path), _FLOOR)
        # Act
        with caplog.at_level("ERROR"):
            check_required_hooks(report, allow_missing=True)
        # Assert
        assert "BYPASSED" in caplog.text

    def test_override_still_names_the_missing_hook(
        self, tmp_path: Path, as_the_agent, caplog
    ):
        # Arrange
        report = _report(layer_only_home(tmp_path), _FLOOR)
        # Act
        with caplog.at_level("ERROR"):
            check_required_hooks(report, allow_missing=True)
        # Assert
        assert _MISSED in caplog.text

    def test_env_override_is_honoured(
        self, tmp_path: Path, as_the_agent, env_save_restore
    ):
        # Arrange — the transport that survives a subprocess boundary: the
        # container's boot step is its own process, so `spec.env` is how an
        # operator grants the bypass.
        env_save_restore.set(ALLOW_ENV, "1")
        report = _report(layer_only_home(tmp_path), _FLOOR)
        # Act
        proceeded = check_required_hooks(report)
        # Assert
        assert proceeded is True

    def test_absent_instruction_is_not_leniency(self, tmp_path: Path, as_the_agent):
        # Arrange — allow_missing=None means "no instruction", NOT "be lenient".
        # This is the shape of the defect PR #949 shipped at its click seam.
        report = _report(layer_only_home(tmp_path), _FLOOR)
        # Act
        message = _refusal_message(report, allow_missing=None)
        # Assert
        assert message != ""


class TestUnknownIsItsOwnAnswer:
    """Copied from ``scitex-cards health``'s ``delivery_confirmed``: a check
    that cannot distinguish unknown from pass is how the two wrong conclusions
    of 2026-08-10 happened."""

    def test_measuring_a_different_agent_is_unknown(self, env_save_restore):
        # Arrange
        env_save_restore.set("SAC_NAME", "somebody-else")
        # Act
        site = measurement_site(_AGENT)
        # Assert
        assert site["ok"] is None

    def test_off_agent_measurement_never_reports_a_failure(self, env_save_restore):
        # Arrange — reading the wrong $HOME is not a finding ABOUT the agent, so
        # it must never put a red mark on one that may be perfectly configured.
        env_save_restore.set("SAC_NAME", "somebody-else")
        # Act
        site = measurement_site(_AGENT)
        # Assert
        assert site["ok"] is not False

    def test_off_agent_measurement_forces_the_floor_unknown(
        self, tmp_path: Path, env_save_restore
    ):
        # Arrange — the hooks are RIGHT THERE and readable; the only thing wrong
        # is that they belong to somebody else. A confident True here is exactly
        # the host-side answer this feature exists to stop.
        env_save_restore.set("SAC_NAME", "somebody-else")
        report = _report(effective_home(tmp_path), _FLOOR)
        # Act
        floor = report["floor"]
        # Assert
        assert floor["satisfied"] is None

    def test_unknown_floor_does_not_refuse(self, tmp_path: Path, env_save_restore):
        # Arrange — refusing on "I could not check" would ground the fleet.
        env_save_restore.set("SAC_NAME", "somebody-else")
        env_save_restore.delete(ALLOW_ENV)
        report = _report(layer_only_home(tmp_path), _FLOOR)
        # Act
        proceeded = check_required_hooks(report)
        # Assert
        assert proceeded is True

    def test_unknown_floor_is_said_out_loud(
        self, tmp_path: Path, env_save_restore, caplog
    ):
        # Arrange — not refusing is not the same as staying quiet.
        env_save_restore.set("SAC_NAME", "somebody-else")
        env_save_restore.delete(ALLOW_ENV)
        report = _report(layer_only_home(tmp_path), _FLOOR)
        # Act
        with caplog.at_level("ERROR"):
            check_required_hooks(report)
        # Assert
        assert "UNKNOWN" in caplog.text

    def test_unreadable_hooks_root_is_unknown_not_unmet(
        self, tmp_path: Path, as_the_agent
    ):
        # Arrange — an agent whose home is not mounted yet must not be reported
        # as one whose guards were removed.
        empty = tmp_path / "no-home"
        empty.mkdir()
        # Act
        report = _report(empty, _FLOOR)
        # Assert
        assert report["floor"]["satisfied"] is None


class TestTheReportShape:
    """The cross-package standard shape, shared with ``scitex-cards health``."""

    def test_report_names_the_package(self, tmp_path: Path, as_the_agent):
        # Arrange
        report = _report(effective_home(tmp_path), _FLOOR)
        # Act
        package = report["package"]
        # Assert
        assert package == "scitex-agent-container"

    def test_every_check_carries_the_four_standard_fields(
        self, tmp_path: Path, as_the_agent
    ):
        # Arrange
        report = _report(effective_home(tmp_path), _FLOOR)
        # Act
        keys = {frozenset(c) for c in report["checks"]}
        # Assert
        assert keys == {frozenset({"name", "ok", "detail", "hint"})}

    def test_every_failing_check_carries_a_hint(self, tmp_path: Path, as_the_agent):
        # Arrange
        report = _report(layer_only_home(tmp_path), _FLOOR)
        # Act
        hintless = [
            c["name"] for c in report["checks"] if not c["ok"] and not c["hint"]
        ]
        # Assert
        assert hintless == []

    def test_unknown_checks_are_named_in_the_summary(
        self, tmp_path: Path, as_the_agent
    ):
        # Arrange — an UNKNOWN that is not named reads as a silent pass.
        report = _report(effective_home(tmp_path), None)
        # Act
        summary = report["summary"]
        # Assert
        assert "unknown:" in summary

    def test_an_unknown_alone_does_not_fail_the_run(self, tmp_path: Path, as_the_agent):
        # Arrange — `ok` counts only FALSE, exactly as scitex-cards does.
        report = _report(effective_home(tmp_path), None)
        # Act
        ok = report["ok"]
        # Assert
        assert ok is True

    def test_a_missing_hook_fails_the_run(self, tmp_path: Path, as_the_agent):
        # Arrange
        report = _report(layer_only_home(tmp_path), _FLOOR)
        # Act
        ok = report["ok"]
        # Assert
        assert ok is False

    def test_report_lists_what_is_actually_armed(self, tmp_path: Path, as_the_agent):
        # Arrange — "what does this container enforce" needs the LIST, not only
        # a verdict about it.
        report = _report(effective_home(tmp_path), _FLOOR)
        # Act
        armed = report["inventory"]["dirs"]["post-tool-use"]
        # Assert
        assert _MISSED in armed


class TestATypoCannotSilentlySatisfyNothing:
    def test_misspelt_event_dir_is_reported(self):
        # Arrange — `pre_tool_use` matches no directory, so a floor declaring it
        # could never be satisfied AND never be meaningfully missing.
        floor = {"pre_tool_use": ["x.sh"]}
        # Act
        bogus = unknown_event_dirs(floor)
        # Assert
        assert bogus == ["pre_tool_use"]

    def test_misspelt_event_dir_fails_the_declaration_check(
        self, tmp_path: Path, as_the_agent
    ):
        # Arrange
        report = _report(effective_home(tmp_path), {"pre_tool_use": ["x.sh"]})
        # Act
        declared = next(
            c for c in report["checks"] if c["name"] == "required_hooks_declared"
        )
        # Assert
        assert declared["ok"] is False

    def test_the_refusal_calls_a_typo_a_typo(self, tmp_path: Path, as_the_agent):
        # Arrange — a typo also reads as "hook not armed" (nothing can be armed
        # under a directory Claude Code never loads). A refusal that showed only
        # the missing-hook fix would send the reader off to CREATE the bogus
        # directory, entrenching the mistake instead of naming it.
        report = _report(effective_home(tmp_path), {"pre_tool_use": ["x.sh"]})
        # Act
        message = _refusal_message(report)
        # Assert
        assert "not Claude Code hook directories" in message


class TestTheFloorIsEvaluatedAgainstTheEffectiveView:
    """The regression the whole card is about, at the FLOOR level rather than
    the inventory level: the same declaration must pass against the effective
    view and fail against a single layer."""

    def test_same_floor_fails_against_the_layer_only_view(
        self, tmp_path: Path, as_the_agent
    ):
        # Arrange
        verdict = evaluate_floor(
            _Spec(_FLOOR), home=str(layer_only_home(tmp_path)), site_ok=True
        )
        # Act
        satisfied = verdict.satisfied
        # Assert
        assert satisfied is False

    def test_same_floor_passes_against_the_effective_view(
        self, tmp_path: Path, as_the_agent
    ):
        # Arrange
        verdict = evaluate_floor(
            _Spec(_FLOOR), home=str(effective_home(tmp_path)), site_ok=True
        )
        # Act
        satisfied = verdict.satisfied
        # Assert
        assert satisfied is True

    def test_an_empty_declaration_is_trivially_satisfied(
        self, tmp_path: Path, as_the_agent
    ):
        # Arrange — `{}` is a spec deliberately requiring no hooks. It is a
        # STATEMENT, and must not read as the absence of one.
        home = write_hooks(tmp_path / "h", {"pre-tool-use": ["real.sh"]})
        verdict = evaluate_floor(_Spec({}), home=str(home), site_ok=True)
        # Act
        satisfied = verdict.satisfied
        # Assert
        assert satisfied is True

    def test_an_empty_declaration_still_counts_as_declared(
        self, tmp_path: Path, as_the_agent
    ):
        # Arrange
        home = write_hooks(tmp_path / "h", {"pre-tool-use": ["real.sh"]})
        verdict = evaluate_floor(_Spec({}), home=str(home), site_ok=True)
        # Act
        declared = verdict.declared
        # Assert
        assert declared is True
