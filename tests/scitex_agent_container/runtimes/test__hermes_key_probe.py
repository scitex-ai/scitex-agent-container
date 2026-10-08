"""Preflight keeps declared pools when the availability surface is missing."""

from scitex_agent_container.runtimes import _hermes_key_probe as key_probe


def test_preflight_keeps_declared_pools_without_availability_surface(monkeypatch):
    # Arrange
    monkeypatch.setattr(key_probe, "probe_provider_key", None)
    monkeypatch.setattr(key_probe, "provider_route", None)
    sentinel = object()
    pools = {"muse": sentinel}

    # Act
    key_probe.preflight_pools(
        {"model": {"provider": "muse"}, "fallback_providers": []}, pools, []
    )

    # Assert
    assert pools["muse"] is sentinel
