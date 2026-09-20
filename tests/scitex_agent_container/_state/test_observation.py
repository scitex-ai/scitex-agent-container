from __future__ import annotations

from scitex_agent_container._state.observation import (
    DefinitionState,
    build_agent_observation,
)


def _status(
    *, pane: str = "", verdict: str = "alive", process_verdict: str | None = "alive"
) -> dict:
    evidence = [
        {
            "source": "delivery",
            "verdict": "alive",
            "detail": "one live subscriber",
        }
    ]
    if process_verdict is not None:
        evidence.append(
            {
                "source": "process",
                "verdict": process_verdict,
                "detail": f"process observation: {process_verdict}",
            }
        )
    return {
        "status": "running",
        "pane_text": pane,
        "pane_state": "running",
        "liveness": {
            "verdict": verdict,
            "evidence": evidence,
        },
    }


def test_ready_process_is_alive_but_turn_is_idle() -> None:
    # Arrange
    status = _status(pane="─ ready │ qwen38 27b low │ 204k/1M")

    # Act
    observed = build_agent_observation(
        status,
        definition_state=DefinitionState.VALID,
    )

    # Assert
    assert {
        key: observed[key]["state"]
        for key in ("definition", "process", "turn", "communication", "progress")
    } == {
        "definition": "valid",
        "process": "alive",
        "turn": "idle",
        "communication": "reachable",
        "progress": "unknown",
    }


def test_reasoning_status_line_is_not_reported_as_idle() -> None:
    # Arrange
    status = _status(pane="─ ruminating… · 15m 28s │ qwen38 27b low")

    # Act
    observed = build_agent_observation(
        status,
        definition_state=DefinitionState.VALID,
    )

    # Assert
    assert (
        observed["turn"]["state"],
        observed["turn"]["evidence"][0]["source"],
    ) == ("reasoning", "terminal.status_line")


def test_unknown_progress_is_explicit_not_invented() -> None:
    # Arrange
    status = _status()

    # Act
    observed = build_agent_observation(
        status, definition_state=DefinitionState.INVALID
    )

    # Assert
    assert (
        observed["definition"]["state"],
        observed["turn"]["state"],
        "successive observations"
        in observed["progress"]["evidence"][0]["detail"],
    ) == ("invalid", "unknown", True)


def test_runtime_phase_is_preferred_over_terminal_heuristic() -> None:
    # Arrange
    status = _status(pane="─ ready │ qwen38")
    status["runtime_control"] = {"current_phase": "prefilling"}

    # Act
    observed = build_agent_observation(
        status, definition_state=DefinitionState.VALID
    )

    # Assert
    assert (
        observed["turn"]["state"],
        observed["turn"]["evidence"][0]["source"],
    ) == ("prefilling", "runtime_control.current_phase")


def test_delivery_only_does_not_claim_the_process_is_alive() -> None:
    # Arrange
    status = _status(process_verdict=None)

    # Act
    observed = build_agent_observation(
        status, definition_state=DefinitionState.VALID
    )

    # Assert
    assert (
        observed["process"]["state"],
        observed["communication"]["state"],
    ) == ("unknown", "reachable")


def test_dead_process_is_not_overwritten_by_a_live_delivery_subscription() -> None:
    # Arrange
    status = _status(verdict="alive", process_verdict="dead")

    # Act
    observed = build_agent_observation(
        status, definition_state=DefinitionState.VALID
    )

    # Assert
    assert (
        observed["process"]["state"],
        observed["communication"]["state"],
    ) == ("exited", "reachable")
