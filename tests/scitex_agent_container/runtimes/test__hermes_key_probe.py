"""Native probe diagnostics expose status metadata, never echoed credentials."""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scitex_agent_container.runtimes import _hermes_key_probe as probe
from scitex_agent_container.runtimes._hermes_failover import DeclaredPool


def test_preflight_failover_moves_judge_to_verified_lane():
    # Operator rule (prompt-2026-1022): when the primary lane fails its
    # availability probe and the launch rewrites onto a verified provider,
    # the goal judge must move with the agent. A judge left on the dead
    # lane pauses every goal after 5 transport failures.
    # Arrange
    rendered = {
        "model": {
            "default": "muse-spark-1.3-contributor",
            "provider": "custom:sac-primary",
        },
        "fallback_providers": [
            {
                "provider": "custom:sac-verified",
                "model": "muse-spark-1.3-contributor",
            }
        ],
        "auxiliary": {
            "goal_judge": {
                "provider": "custom:sac-primary",
                "model": "muse-spark-1.3-contributor",
                "reasoning_effort": "xhigh",
                "timeout": 60,
                "max_tokens": 4096,
            }
        },
        "providers": {},
    }
    pools = {
        "custom:sac-primary": DeclaredPool(
            credentials=[{"id": "a", "label": "A", "access_token": "dead"}],
            suppressed_sources=[],
            probe={
                "model": "muse-spark-1.3-contributor",
                "model_config": {
                    "provider": "custom:sac-primary",
                    "default": "muse-spark-1.3-contributor",
                },
            },
        ),
        "custom:sac-verified": DeclaredPool(
            credentials=[{"id": "b", "label": "B", "access_token": "live"}],
            suppressed_sources=[],
            probe={
                "model": "muse-spark-1.3-contributor",
                "model_config": {
                    "provider": "custom:sac-verified",
                    "default": "muse-spark-1.3-contributor",
                },
            },
        ),
    }

    def fake_probe_key(pool_probe, token, **kwargs):
        return {
            "last_status": "ok" if token == "live" else "dead",
            "last_status_at": 1000.0,
        }

    # Act
    with tempfile.TemporaryDirectory() as raw:
        profile = Path(raw)
        with patch.object(probe, "probe_key", fake_probe_key):
            probe.preflight_pools(rendered, pools, [profile])
    # Assert
    assert rendered["model"]["provider"] == "custom:sac-verified"
    assert rendered["auxiliary"]["goal_judge"] == {
        "provider": "custom:sac-verified",
        "model": "muse-spark-1.3-contributor",
        "reasoning_effort": "xhigh",
        "timeout": 60,
        "max_tokens": 4096,
    }


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
