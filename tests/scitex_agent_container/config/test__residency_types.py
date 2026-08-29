"""Tests for the residency axis — ``spec.residency`` (v4 step 6).

The axis declares whether the session daemon OUTLIVES its work:
``resident`` (default — the fleet posture) parks awaiting more turns
after a conversation completes; ``one-shot`` exits cleanly when the
work is done. A NEW axis with no legacy spelling, so it is DEFAULTED
(absence loads — the live corpus must not red-start en masse) while an
ILLEGAL value fails loud naming the closed set.

Every load goes through the REAL ``load_config`` against a REAL
spec.yaml on disk, same as the harness-axis suite. STX-TQ002 AAA,
STX-TQ007 one assert. No mocks.
"""

from __future__ import annotations

import pytest
import yaml

from scitex_agent_container.config import load_config
from scitex_agent_container.config._explicit_validation import (
    explicit_spec_defaults,
)
from scitex_agent_container.config._residency_types import (
    AGENT_RESIDENCIES,
    DEFAULT_AGENT_RESIDENCY,
    ONE_SHOT,
    RESIDENT,
    declared_residency,
    is_known_residency,
    list_residencies,
    residency_coupling_error,
    residency_value_error,
    resolve_spec_residency,
)

# ---------------------------------------------------------------------------
# Helpers — write a REAL, fully-explicit spec.yaml and load it
# ---------------------------------------------------------------------------


def _write_spec(tmp_path, name: str, overrides: dict, *, kind: str = "Agent"):
    """Write ``<tmp_path>/<name>/spec.yaml`` and return its path.

    Starts from the production paste-defaults map so the spec satisfies
    the red-start explicit-fields gate, then applies ``overrides`` —
    NOTE the defaults map deliberately does NOT contain ``residency``
    (the axis is defaulted, not required), so the no-override case is
    exactly a live fleet spec that predates the axis.
    """
    spec = explicit_spec_defaults(kind)
    spec.update(overrides)
    spec["host"] = "${HOSTNAME}"
    agent_dir = tmp_path / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "spec.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "scitex-agent-container/v3",
                "kind": kind,
                "spec": spec,
            },
            sort_keys=False,
        )
    )
    return path


def _load_error(path):
    """The validation error ``load_config(path)`` raises, or ``None``."""
    try:
        load_config(path)
    except ValueError as exc:
        return exc
    return None


# ---------------------------------------------------------------------------
# The resolver — value-level semantics
# ---------------------------------------------------------------------------


def test_absent_key_resolves_to_resident():
    # Arrange
    spec: dict = {}
    # Act
    residency = resolve_spec_residency(spec)
    # Assert
    assert residency == RESIDENT


def test_null_key_states_no_opinion():
    # Arrange: a written-but-null key declares nothing (harness idiom).
    spec = {"residency": None}
    # Act
    declared = declared_residency(spec)
    # Assert
    assert declared is None


def test_one_shot_resolves_to_one_shot():
    # Arrange
    spec = {"residency": "one-shot"}
    # Act
    residency = resolve_spec_residency(spec)
    # Assert
    assert residency == ONE_SHOT


def test_resolution_normalises_case_and_whitespace():
    # Arrange: identity is normalised; the VALUE check owns casing.
    spec = {"residency": "  One-Shot  "}
    # Act
    residency = resolve_spec_residency(spec)
    # Assert
    assert residency == ONE_SHOT


def test_default_is_resident():
    # Arrange: the fleet posture is the module's declared default.
    expected = RESIDENT
    # Act
    default = DEFAULT_AGENT_RESIDENCY
    # Assert
    assert default == expected


def test_the_enum_is_closed_at_two():
    # Arrange
    expected = ["one-shot", "resident"]
    # Act
    members = sorted(AGENT_RESIDENCIES)
    # Assert
    assert members == expected


def test_unknown_value_is_not_known():
    # Arrange: the hyphen matters — "oneshot" is a typo, not a member.
    candidate = "oneshot"
    # Act
    known = is_known_residency(candidate)
    # Assert
    assert known is False


# ---------------------------------------------------------------------------
# The validator — value legality fails LOUD naming the closed set
# ---------------------------------------------------------------------------


def test_value_error_names_the_valid_set():
    # Arrange
    spec = {"residency": "oneshot"}
    # Act
    errors = residency_value_error(spec)
    # Assert: the message carries the exact set the operator can pick from.
    assert len(errors) == 1 and str(list_residencies()) in errors[0]


def test_value_error_names_the_written_value():
    # Arrange
    spec = {"residency": "daemon"}
    # Act
    errors = residency_value_error(spec)
    # Assert
    assert "daemon" in errors[0]


def test_legal_value_produces_no_error():
    # Arrange
    spec = {"residency": "one-shot"}
    # Act
    errors = residency_value_error(spec)
    # Assert
    assert errors == []


def test_load_config_refuses_a_typo_loudly(tmp_path):
    # Arrange: a REAL spec on disk carrying the typo.
    path = _write_spec(
        tmp_path,
        "ag-typo",
        {"residency": "oneshot", "runtime": "claude-agent-sdk"},
    )
    # Act
    exc = _load_error(path)
    # Assert
    assert exc is not None and "residency" in str(exc)


# ---------------------------------------------------------------------------
# The loader — end to end through a real spec.yaml
# ---------------------------------------------------------------------------


def test_compiled_spec_carries_declared_one_shot(tmp_path):
    # Arrange
    path = _write_spec(
        tmp_path,
        "ag-oneshot",
        {"residency": "one-shot", "runtime": "claude-agent-sdk"},
    )
    # Act
    config = load_config(path)
    # Assert
    assert config.residency == ONE_SHOT


def test_spec_without_the_field_loads_as_resident(tmp_path):
    # Arrange: the live-fleet shape — a spec that predates the axis.
    # This is the no-en-masse-red-start pin: absence must LOAD.
    path = _write_spec(tmp_path, "ag-legacy", {})
    # Act
    config = load_config(path)
    # Assert
    assert config.residency == RESIDENT


def test_explicit_resident_loads_as_resident(tmp_path):
    # Arrange
    path = _write_spec(tmp_path, "ag-resident", {"residency": "resident"})
    # Act
    config = load_config(path)
    # Assert
    assert config.residency == RESIDENT


# ---------------------------------------------------------------------------
# Coupling — one-shot needs a session daemon to honour it
# ---------------------------------------------------------------------------


def test_one_shot_on_the_tui_harness_is_refused(tmp_path):
    # Arrange: runtime tui resolves to the externally hosted TUI entry,
    # which has no session daemon to end.
    path = _write_spec(
        tmp_path, "ag-tui", {"residency": "one-shot", "runtime": "tui"}
    )
    # Act
    exc = _load_error(path)
    # Assert
    assert exc is not None and "one-shot" in str(exc)


def test_the_tui_refusal_names_the_v4_card():
    # Arrange
    from scitex_agent_container.config._harness_types import (
        V4_HARNESS_DISPATCH_CARD,
    )

    spec = {"residency": "one-shot", "runtime": "tui", "harness": "anthropic"}
    # Act
    errors = residency_coupling_error(spec, "Agent")
    # Assert
    assert V4_HARNESS_DISPATCH_CARD in errors[0]


def test_one_shot_on_the_sdk_runner_is_accepted():
    # Arrange
    spec = {
        "residency": "one-shot",
        "runtime": "claude-agent-sdk",
        "harness": "anthropic",
    }
    # Act
    errors = residency_coupling_error(spec, "Agent")
    # Assert
    assert errors == []


def test_one_shot_on_an_agent_proxy_is_refused():
    # Arrange: the proxy forwarder is inherently resident.
    spec = {"residency": "one-shot"}
    # Act
    errors = residency_coupling_error(spec, "AgentProxy")
    # Assert
    assert len(errors) == 1 and "AgentProxy" in errors[0]


def test_coupling_declines_when_the_harness_axes_are_broken():
    # Arrange: an unmappable harness owns its OWN diagnostic; a second
    # residency error derived from it would only obscure the first.
    spec = {"residency": "one-shot", "harness": "gemini"}
    # Act
    errors = residency_coupling_error(spec, "Agent")
    # Assert
    assert errors == []


def test_resident_never_triggers_the_coupling_check():
    # Arrange: resident on the TUI is today's fleet, and must stay legal.
    spec = {"residency": "resident", "runtime": "tui", "harness": "anthropic"}
    # Act
    errors = residency_coupling_error(spec, "Agent")
    # Assert
    assert errors == []


# ---------------------------------------------------------------------------
# The compiled spec reaches the runner argv (the threading seam)
# ---------------------------------------------------------------------------


def test_declared_one_shot_reaches_the_runner_argv(tmp_path):
    # Arrange: a compiled spec declaring one-shot.
    from scitex_agent_container.runtimes._apptainer_inner_argv import (
        _agent_runner_argv,
    )

    config = load_config(
        _write_spec(
            tmp_path,
            "ag-argv",
            {"residency": "one-shot", "runtime": "claude-agent-sdk"},
        )
    )
    # Act
    argv = _agent_runner_argv(config, one_shot=False)
    # Assert: the daemon will receive the axis as an explicit parameter.
    assert argv[argv.index("--residency") + 1] == "one-shot"


def test_default_residency_keeps_the_argv_byte_identical(tmp_path):
    # Arrange: a spec declaring nothing must launch byte-identically —
    # containers running an older runner build must keep booting.
    from scitex_agent_container.runtimes._apptainer_inner_argv import (
        _agent_runner_argv,
    )

    config = load_config(_write_spec(tmp_path, "ag-noflag", {}))
    # Act
    argv = _agent_runner_argv(config, one_shot=False)
    # Assert
    assert "--residency" not in argv


def test_runner_cli_threads_residency_to_the_daemon_parameter(tmp_path):
    # Arrange: the exact argv tail the builder emits for one-shot.
    from scitex_agent_container._runners._session_cli import _parse_argv
    from scitex_agent_container.runtimes._apptainer_inner_argv import (
        _agent_runner_argv,
    )

    config = load_config(
        _write_spec(
            tmp_path,
            "ag-thread",
            {"residency": "one-shot", "runtime": "claude-agent-sdk"},
        )
    )
    argv = _agent_runner_argv(config, one_shot=False)
    # Act: the runner CLI parses the builder's own output.
    args = _parse_argv(argv)
    # Assert: compiled spec -> argv -> CLI arg, ready for run(residency=...).
    assert args.residency == "one-shot"


def test_runner_cli_refuses_an_unknown_residency():
    # Arrange
    from scitex_agent_container._runners._session_cli import _parse_argv

    argv = ["--name", "x", "--residency", "half-shot"]
    # Act
    parse = _parse_argv
    # Assert: argparse choices fail loud, naming the valid set on stderr.
    with pytest.raises(SystemExit):
        parse(argv)
