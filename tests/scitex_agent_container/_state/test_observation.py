from __future__ import annotations

from scitex_agent_container._state.observation import (
    DefinitionState,
    build_agent_observation,
)


def _status(*, pane: str = "", verdict: str = "alive") -> dict:
    return {
        "status": "running",
        "pane_text": pane,
        "pane_state": "running",
        "liveness": {
            "verdict": verdict,
            "evidence": [
                {
                    "source": "delivery",
                    "verdict": "alive",
                    "detail": "one live subscriber",
                }
            ],
        },
    }


def test_ready_process_is_alive_but_turn_is_idle() -> None:
    observed = build_agent_observation(
        _status(pane="─ ready │ qwen38 27b low │ 204k/1M"),
        definition_state=DefinitionState.VALID,
    )

    assert observed["definition"]["state"] == "valid"
    assert observed["process"]["state"] == "alive"
    assert observed["turn"]["state"] == "idle"
    assert observed["communication"]["state"] == "reachable"
    assert observed["progress"]["state"] == "unknown"


def test_reasoning_status_line_is_not_reported_as_idle() -> None:
    observed = build_agent_observation(
        _status(pane="─ ruminating… · 15m 28s │ qwen38 27b low"),
        definition_state=DefinitionState.VALID,
    )

    assert observed["turn"]["state"] == "reasoning"
    assert observed["turn"]["evidence"][0]["source"] == "terminal.status_line"


def test_unknown_progress_is_explicit_not_invented() -> None:
    observed = build_agent_observation(
        _status(), definition_state=DefinitionState.INVALID
    )

    assert observed["definition"]["state"] == "invalid"
    assert observed["turn"]["state"] == "unknown"
    assert "successive observations" in observed["progress"]["evidence"][0]["detail"]


def test_runtime_phase_is_preferred_over_terminal_heuristic() -> None:
    status = _status(pane="─ ready │ qwen38")
    status["runtime_control"] = {"current_phase": "prefilling"}

    observed = build_agent_observation(
        status, definition_state=DefinitionState.VALID
    )

    assert observed["turn"]["state"] == "prefilling"
    assert observed["turn"]["evidence"][0]["source"] == "runtime_control.current_phase"
