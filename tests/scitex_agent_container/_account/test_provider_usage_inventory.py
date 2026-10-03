"""Real spawned worker/cache tests: hard deadline, failure isolation and aliases."""

import json
import multiprocessing
import os
import time
from datetime import datetime, timedelta, timezone

from scitex_agent_container._account.provider_usage_inventory import (
    UsageTarget,
    collect,
    discover,
)
from scitex_agent_container._account.provider_usage_projection import project


def known_fetch(target, deadline):
    """A pure metadata producer executing in a real owned child process."""
    now = datetime.now(timezone.utc)
    return project("opencode-go", {"quota": {"usage": {"rolling": {
        "percent": 10, "resetsAt": (now + timedelta(hours=5)).isoformat(),
    }}}}, now)


def mixed_fetch(target, deadline):
    if target.name == "OPENCODE_GO_API_KEY_hung":
        time.sleep(10)
    return known_fetch(target, deadline)


def refusing_fetch(target, deadline):
    return {"error": "http-403"}


def parallel_fetch(target, deadline):
    """Require another worker to publish before returning, proving concurrency."""
    directory = target.auth_path
    (directory / target.name).write_text(str(os.getpid()))
    while len(list(directory.iterdir())) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    if len(list(directory.iterdir())) < 2:
        return {"error": "metadata-deadline"}
    return known_fetch(target, deadline)


def targets():
    return [
        UsageTarget("opencode-go", "OPENCODE_GO_API_KEY_hung", ["OPENCODE_GO_API_KEY_hung"], secret="fixture-one"),
        UsageTarget("opencode-go", "OPENCODE_GO_API_KEY_good", ["OPENCODE_GO_API_KEY_good"], secret="fixture-two"),
    ]


def test_same_credential_aliases_do_not_duplicate_rows():
    # Arrange
    env = {"COMMANDCODE_API_KEY_01": "synthetic-same", "COMMANDCODE_API_KEY_ywatanabe": "synthetic-same"}
    # Act
    result = discover([], env=env)
    # Assert
    assert len(result) == 1


def test_shared_credential_retains_both_configured_alias_names():
    # Arrange
    env = {"COMMANDCODE_API_KEY_01": "synthetic-same", "COMMANDCODE_API_KEY_ywatanabe": "synthetic-same"}
    # Act
    result = discover([], env=env)
    # Assert
    assert result[0].aliases == ["COMMANDCODE_API_KEY_01", "COMMANDCODE_API_KEY_ywatanabe"]


def test_unrelated_env_is_not_a_configured_provider():
    # Arrange
    env = {"SECRET": "private", "OPENAI_API_KEY": "private", "COMMANDCODE_API_KEY_bad/path": "private"}
    # Act
    result = discover([], env=env)
    # Assert
    assert result == []


def test_target_representation_excludes_credential_value():
    # Arrange
    target = UsageTarget("commandcode", "fixture", ["fixture"], secret="synthetic-private")
    # Act
    text = repr(target)
    # Assert
    assert "synthetic-private" not in text


def test_passive_inventory_never_starts_a_probe(tmp_path):
    # Arrange
    accounts = targets()
    # Act
    rows = collect(accounts, home=tmp_path, passive=True, fetcher=mixed_fetch)
    # Assert
    assert [row["error"] for row in rows] == ["metadata-not-observed"] * 2


def test_one_hung_alias_does_not_hide_successful_other_alias(tmp_path):
    # Arrange
    accounts = targets()
    # Act
    rows = collect(accounts, home=tmp_path, budget=2, fetcher=mixed_fetch)
    # Assert
    assert rows[1]["usage_state"] == "known"


def test_hung_worker_is_stopped_within_overall_budget(tmp_path):
    # Arrange
    accounts = targets()
    started = time.monotonic()
    # Act
    collect(accounts, home=tmp_path, budget=1, fetcher=mixed_fetch)
    elapsed = time.monotonic() - started
    # Assert
    assert elapsed < 1.5


def test_timed_out_collection_leaves_no_owned_worker(tmp_path):
    # Arrange
    before = {process.pid for process in multiprocessing.active_children()}
    # Act
    collect(targets(), home=tmp_path, budget=1, fetcher=mixed_fetch)
    new_pids = {process.pid for process in multiprocessing.active_children()} - before
    # Assert
    assert new_pids == set()


def test_actual_workers_overlap_instead_of_serial_wait(tmp_path):
    # Arrange
    witnesses = tmp_path / "witnesses"
    witnesses.mkdir()
    accounts = [UsageTarget("opencode-go", name, [name], secret="synthetic", auth_path=witnesses)
                for name in ("OPENCODE_GO_API_KEY_1", "OPENCODE_GO_API_KEY_2")]
    # Act
    rows = collect(accounts, home=tmp_path, budget=2, fetcher=parallel_fetch)
    # Assert
    assert [row["usage_state"] for row in rows] == ["known", "known"]


def test_success_cache_contains_metrics_and_not_credentials(tmp_path):
    # Arrange
    account = targets()[1]
    # Act
    collect([account], home=tmp_path, budget=2, fetcher=known_fetch)
    text = next((tmp_path / ".scitex/cache/account-usage").glob("*.json")).read_text()
    # Assert
    assert "fixture-two" not in text


def test_usage_cache_is_private(tmp_path):
    # Arrange
    account = targets()[1]
    # Act
    collect([account], home=tmp_path, budget=2, fetcher=known_fetch)
    path = next((tmp_path / ".scitex/cache/account-usage").glob("*.json"))
    # Assert
    assert path.stat().st_mode & 0o777 == 0o600


def test_failed_refresh_keeps_old_measurement_explicitly_stale(tmp_path):
    # Arrange
    account = targets()[1]
    collect([account], home=tmp_path, budget=2, fetcher=known_fetch)
    # Act
    rows = collect([account], home=tmp_path, refresh=True, budget=2, fetcher=refusing_fetch)
    # Assert
    assert rows[0]["usage_state"] == "stale"


def test_unavailable_alias_stays_represented(tmp_path):
    # Arrange
    account = targets()[1]
    # Act
    rows = collect([account], home=tmp_path, budget=2, fetcher=refusing_fetch)
    # Assert
    assert rows[0]["qualified_id"] == "opencode-go:OPENCODE_GO_API_KEY_good"


def test_cache_reader_whitelists_private_injected_fields(tmp_path):
    # Arrange
    account = targets()[1]
    cache = tmp_path / ".scitex/cache/account-usage/opencode-go-OPENCODE_GO_API_KEY_good.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"fetchedAt": datetime.now(timezone.utc).isoformat(), "secret": "private"}))
    # Act
    rows = collect([account], home=tmp_path, passive=True)
    # Assert
    assert "private" not in json.dumps(rows)
