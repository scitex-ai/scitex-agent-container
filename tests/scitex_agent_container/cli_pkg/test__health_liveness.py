"""Health summary keeps delivery, process, and unknown evidence distinct."""

from __future__ import annotations

from scitex_agent_container.cli_pkg._health_liveness import (
    health_summary,
    heartbeat_health_state,
)


def _liveness(
    *,
    overall: str,
    process: str | None,
    delivery: str | None,
    heartbeat: str | None = None,
) -> dict:
    evidence = []
    if process is not None:
        evidence.append(
            {
                "source": "process",
                "verdict": process,
                "detail": f"process probe: {process}",
            }
        )
    if delivery is not None:
        evidence.append(
            {
                "source": "delivery",
                "verdict": delivery,
                "detail": f"delivery probe: {delivery}",
            }
        )
    if heartbeat is not None:
        evidence.append(
            {
                "source": "heartbeat",
                "verdict": heartbeat,
                "detail": f"heartbeat probe: {heartbeat}",
            }
        )
    return {
        "verdict": overall,
        "summary": f"{overall} summary",
        "evidence": evidence,
    }


def test_delivery_only_alive_is_not_reported_as_process_healthy() -> None:
    # Arrange
    liveness = _liveness(overall="alive", process="unknown", delivery="alive")

    # Act
    result = health_summary(False, "unhealthy: process not running", liveness)

    # Assert
    assert (result["state"], result["message"]) == (
        "alive-by-delivery-only",
        "delivery reachable; process liveness unknown (process probe: unknown)",
    )


def test_all_unknown_health_is_reported_unknown_not_dead() -> None:
    # Arrange
    liveness = _liveness(overall="unknown", process="unknown", delivery="unknown")

    # Act
    result = health_summary(False, "unhealthy: process not running", liveness)

    # Assert
    assert (result["state"], result["message"]) == (
        "unknown",
        "health unknown: process probe: unknown",
    )


def test_observed_dead_process_remains_unhealthy() -> None:
    # Arrange
    liveness = _liveness(overall="dead", process="dead", delivery="unknown")

    # Act
    result = health_summary(False, "unhealthy: tui process not running", liveness)

    # Assert
    assert result == {
        "state": "unhealthy",
        "message": "unhealthy: tui process not running",
    }


def test_observed_live_process_remains_healthy() -> None:
    # Arrange
    liveness = _liveness(overall="alive", process="alive", delivery="alive")

    # Act
    result = health_summary(True, "healthy", liveness)

    # Assert
    assert result == {"state": "healthy", "message": "healthy"}


def test_direct_dead_process_overrides_legacy_healthy_bool() -> None:
    # Arrange
    liveness = _liveness(overall="dead", process="dead", delivery="alive")
    # Act
    result = health_summary(True, "healthy", liveness)
    # Assert
    assert result == {
        "state": "unhealthy",
        "message": "unhealthy: process probe: dead",
    }


def test_fresh_alive_heartbeat_repairs_unknown_process_probe() -> None:
    # Arrange
    liveness = _liveness(
        overall="alive",
        process="unknown",
        delivery="unknown",
        heartbeat="alive",
    )
    # Act
    result = health_summary(False, "unhealthy: process not running", liveness)
    # Assert
    assert result == {
        "state": "healthy",
        "message": "healthy: fresh heartbeat proves process presence",
    }


def test_disconnected_heartbeat_projects_typed_unknown_health() -> None:
    # Arrange
    resident_state = "disconnected"
    # Act
    result = heartbeat_health_state(resident_state)
    # Assert
    assert result == "unknown"


def test_dead_heartbeat_projects_typed_unhealthy_health() -> None:
    # Arrange
    resident_state = "dead"
    # Act
    result = heartbeat_health_state(resident_state)
    # Assert
    assert result == "unhealthy"
