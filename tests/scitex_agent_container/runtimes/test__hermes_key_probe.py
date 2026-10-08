"""Native probe diagnostics expose status metadata, never echoed credentials."""

from types import SimpleNamespace

from scitex_agent_container.runtimes import _hermes_key_probe as probe


def test_upstream_error_detail_is_not_logged_or_persisted():
    # Arrange
    from scitex_agent_container.runtimes import _availability as availability

    token = "synthetic-private-sentinel"
    result = SimpleNamespace(
        available=False,
        status=SimpleNamespace(kind="http", code=401),
        check=SimpleNamespace(detail="upstream echoed " + token),
        reset_at=None,
    )
    messages = []
    real_probe, real_info = availability.probe_provider_key, probe.logger.info
    availability.probe_provider_key = lambda *_args, **_kwargs: result
    probe.logger.info = lambda msg, *args: messages.append(msg % args)
    spec = {
        "provider": "custom:sac-muse",
        "model": "muse-spark-1.3-contributor",
        "url": "https://opencode.ai/zen/go/v1/responses",
        "protocol": "openai-responses",
        "headers": {},
        "session_id": "sac:synthetic:muse",
    }
    # Act
    try:
        state = probe.probe_key(spec, token)
    finally:
        availability.probe_provider_key = real_probe
        probe.logger.info = real_info
    # Assert
    assert (
        state["last_status"],
        state["last_error_code"],
        token not in repr(state),
        token not in " ".join(messages),
        "status=http code=401" in " ".join(messages),
    ) == ("dead", 401, True, True, True)
