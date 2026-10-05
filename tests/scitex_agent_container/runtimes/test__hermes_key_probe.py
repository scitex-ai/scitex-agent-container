import json

import pytest

from scitex_agent_container.runtimes import _hermes_key_probe as probe
from scitex_agent_container.runtimes._hermes_failover import DeclaredPool


def _pool(name, count=2):
    return DeclaredPool(
        [
            {
                "id": f"{name}-{i}",
                "label": f"KEY_{i}",
                "source": f"manual:sac:KEY_{i}",
                "auth_type": "api_key",
                "priority": i,
                "access_token": f"fake-{name}-{i}",
            }
            for i in range(count)
        ],
        [],
        {
            "model": "model-" + name,
            "model_config": {"provider": name, "default": "model-" + name},
        },
    )


def test_every_candidate_is_probed_and_rejected_key_is_preserved(tmp_path, monkeypatch):
    calls = []

    def check(spec, token):
        calls.append(token)
        return (
            {"last_status": "dead", "last_error_code": 401, "last_status_at": 100}
            if token.endswith("-0")
            else {"last_status": "ok", "last_status_at": 100}
        )

    monkeypatch.setattr(probe, "probe_key", check)
    pools = {"go": _pool("go")}
    rendered = {
        "model": {"provider": "go", "default": "model-go"},
        "fallback_providers": [],
    }
    probe.preflight_pools(rendered, pools, [tmp_path])
    assert calls == ["fake-go-0", "fake-go-1"]
    rows = json.loads((tmp_path / "auth.json").read_text())["credential_pool"]["go"]
    assert len(rows) == 2 and rows[0]["last_status"] == "dead"
    calls.clear()
    probe.preflight_pools(rendered, pools, [tmp_path])
    assert calls == ["fake-go-1"]


def test_unavailable_primary_launches_on_verified_backup(tmp_path, monkeypatch):
    def check(spec, token):
        if token.startswith("fake-go"):
            return {
                "last_status": "dead",
                "last_error_code": 401,
                "last_status_at": 100,
            }
        return {"last_status": "ok", "last_status_at": 100}

    monkeypatch.setattr(probe, "probe_key", check)
    pools = {"go": _pool("go"), "backup": _pool("backup", 1)}
    rendered = {
        "model": {"provider": "go", "default": "model-go"},
        "fallback_providers": [{"provider": "backup", "model": "model-backup"}],
    }
    probe.preflight_pools(rendered, pools, [tmp_path / "home", tmp_path / "overlay"])
    assert rendered["model"] == {"provider": "backup", "default": "model-backup"}
    assert rendered["fallback_providers"] == [{"provider": "go", "model": "model-go"}]
    assert (tmp_path / "home/auth.json").read_bytes() == (
        tmp_path / "overlay/auth.json"
    ).read_bytes()


def test_active_quota_reset_skipped_then_retried_after_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(probe.time, "time", lambda: 1000)
    calls = []

    def check(spec, token):
        calls.append(token)
        return {"last_status": "ok", "last_status_at": 1000}

    monkeypatch.setattr(probe, "probe_key", check)
    pool = _pool("go")
    pool.credentials[0].update(
        last_status="exhausted",
        last_error_code=429,
        last_status_at=900,
        last_error_reset_at=1100,
    )
    pools = {"go": pool}
    rendered = {
        "model": {"provider": "go", "default": "model-go"},
        "fallback_providers": [],
    }
    probe.preflight_pools(rendered, pools, [tmp_path])
    assert calls == ["fake-go-1"]
    calls.clear()
    monkeypatch.setattr(probe.time, "time", lambda: 1200)
    probe.preflight_pools(rendered, pools, [tmp_path])
    assert calls == ["fake-go-0", "fake-go-1"]


def test_all_failed_refuses_launch_and_keeps_declared_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(
        probe,
        "probe_key",
        lambda *args: {
            "last_status": "dead",
            "last_error_code": 401,
            "last_status_at": 100,
        },
    )
    pools = {"go": _pool("go")}
    rendered = {
        "model": {"provider": "go", "default": "model-go"},
        "fallback_providers": [],
    }
    with pytest.raises(RuntimeError, match="No declared Hermes account passed"):
        probe.preflight_pools(rendered, pools, [tmp_path])
    assert (
        len(json.loads((tmp_path / "auth.json").read_text())["credential_pool"]["go"])
        == 2
    )
