import json

from scitex_agent_container.runtimes._hermes_failover import (
    DeclaredPool,
    materialize_pools,
)


def test_rejected_key_stays_declared_but_is_skipped_until_replaced(tmp_path):
    row = {
        "id": "sac-old",
        "label": "KEY_A",
        "source": "manual:sac:KEY_A",
        "auth_type": "api_key",
        "access_token": "fake-old",
    }
    pools = {"provider": DeclaredPool([row], [])}
    materialize_pools(tmp_path, pools)
    path = tmp_path / "auth.json"
    store = json.loads(path.read_text())
    store["credential_pool"]["provider"][0].update(
        last_status="exhausted", last_error_code=401, last_status_at=1
    )
    path.write_text(json.dumps(store))
    materialize_pools(tmp_path, pools)
    stored = json.loads(path.read_text())["credential_pool"]["provider"]
    assert len(stored) == 1 and stored[0]["last_status"] == "dead"
    pools["provider"].credentials[0] = {
        **row,
        "id": "sac-new",
        "access_token": "fake-new",
    }
    materialize_pools(tmp_path, pools)
    changed = json.loads(path.read_text())["credential_pool"]["provider"]
    assert "last_status" not in changed[0]
