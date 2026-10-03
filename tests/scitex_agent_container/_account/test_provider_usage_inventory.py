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


def recording_fetch(target, deadline):
    """Make any network-producer invocation visible in a private fixture."""
    target.auth_path.write_text("provider-invoked")
    return known_fetch(target, deadline)


def cache_path(home, account):
    return home / ".scitex/cache/account-usage" / (account.provider + "-" + account.name + ".json")


def blocked_fifo_writer(path, snapshot):
    """An actual blocking filesystem boundary, without a FIFO reader."""
    with path.open("w") as stream:
        stream.write(json.dumps(snapshot))


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


def test_passive_fifo_cache_is_bounded(tmp_path):
    # Arrange
    account = targets()[1]
    path = cache_path(tmp_path, account)
    path.parent.mkdir(parents=True)
    os.mkfifo(path, 0o600)
    started = time.monotonic()
    # Act
    collect([account], home=tmp_path, passive=True, budget=1)
    elapsed = time.monotonic() - started
    # Assert
    assert elapsed < 1.5


def test_passive_fifo_cache_retains_its_alias(tmp_path):
    # Arrange
    account = targets()[1]
    path = cache_path(tmp_path, account)
    path.parent.mkdir(parents=True)
    os.mkfifo(path, 0o600)
    # Act
    rows = collect([account], home=tmp_path, passive=True, budget=2)
    # Assert
    assert rows[0]["aliases"] == account.aliases


def test_passive_fifo_cache_performs_no_provider_request(tmp_path):
    # Arrange
    account = targets()[1]
    account.auth_path = tmp_path / "provider-invocation"
    path = cache_path(tmp_path, account)
    path.parent.mkdir(parents=True)
    os.mkfifo(path, 0o600)
    # Act
    collect([account], home=tmp_path, passive=True, budget=2, fetcher=recording_fetch)
    # Assert
    assert not account.auth_path.exists()


def test_fifo_does_not_hide_another_current_cache(tmp_path):
    # Arrange
    accounts = targets()
    path = cache_path(tmp_path, accounts[0])
    path.parent.mkdir(parents=True)
    os.mkfifo(path, 0o600)
    cache_path(tmp_path, accounts[1]).write_text(json.dumps(known_fetch(accounts[1], 0)))
    # Act
    rows = collect(accounts, home=tmp_path, passive=True, budget=2)
    # Assert
    assert rows[1]["usage_state"] == "known"


def test_valid_oversized_cache_is_not_trusted(tmp_path):
    # Arrange
    account = targets()[1]
    path = cache_path(tmp_path, account)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(known_fetch(account, 0)) + " " * 65536)
    # Act
    rows = collect([account], home=tmp_path, passive=True, budget=2)
    # Assert
    assert rows[0]["usage_state"] == "unknown"


def test_malformed_oversized_cache_stays_row_local(tmp_path):
    # Arrange
    accounts = targets()
    path = cache_path(tmp_path, accounts[0])
    path.parent.mkdir(parents=True)
    path.write_text("{" + "x" * 131072)
    cache_path(tmp_path, accounts[1]).write_text(json.dumps(known_fetch(accounts[1], 0)))
    # Act
    rows = collect(accounts, home=tmp_path, passive=True, budget=2)
    # Assert
    assert rows[1]["usage_state"] == "known"


def test_cache_symlink_is_not_followed(tmp_path):
    # Arrange
    account = targets()[1]
    sentinel = tmp_path / "outside-cache"
    sentinel.write_text(json.dumps(known_fetch(account, 0)))
    path = cache_path(tmp_path, account)
    path.parent.mkdir(parents=True)
    path.symlink_to(sentinel)
    # Act
    rows = collect([account], home=tmp_path, passive=True, budget=2)
    # Assert
    assert rows[0]["usage_state"] == "unknown"


def test_zero_budget_retains_every_unstarted_account(tmp_path):
    # Arrange
    accounts = targets()
    # Act
    rows = collect(accounts, home=tmp_path, budget=0)
    # Assert
    assert [row["qualified_id"] for row in rows] == ["opencode-go:" + account.name for account in accounts]


def test_blocking_cache_write_is_bounded(tmp_path):
    # Arrange
    account = targets()[1]
    path = cache_path(tmp_path, account)
    path.parent.mkdir(parents=True)
    os.mkfifo(path, 0o600)
    started = time.monotonic()
    # Act
    collect([account], home=tmp_path, budget=1, fetcher=known_fetch, cache_writer=blocked_fifo_writer)
    elapsed = time.monotonic() - started
    # Assert
    assert elapsed < 1.5


def test_blocking_cache_write_does_not_hide_observed_metrics(tmp_path):
    # Arrange
    account = targets()[1]
    path = cache_path(tmp_path, account)
    path.parent.mkdir(parents=True)
    os.mkfifo(path, 0o600)
    # Act
    rows = collect([account], home=tmp_path, budget=1, fetcher=known_fetch, cache_writer=blocked_fifo_writer)
    # Assert
    assert rows[0]["usage_state"] == "known"


def test_blocking_cache_write_leaves_no_owned_child(tmp_path):
    # Arrange
    account = targets()[1]
    path = cache_path(tmp_path, account)
    path.parent.mkdir(parents=True)
    os.mkfifo(path, 0o600)
    before = {process.pid for process in multiprocessing.active_children()}
    # Act
    collect([account], home=tmp_path, budget=1, fetcher=known_fetch, cache_writer=blocked_fifo_writer)
    new_pids = {process.pid for process in multiprocessing.active_children()} - before
    # Assert
    assert new_pids == set()
