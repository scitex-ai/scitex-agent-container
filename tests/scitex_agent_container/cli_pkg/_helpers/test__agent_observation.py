"""Real file/process controls for the canonical observation boundary."""

from __future__ import annotations

import base64
import json
import multiprocessing
import os
import subprocess
import sys
import time

import pytest

from scitex_agent_container.cli_pkg._helpers._agent_observation import (
    assert_instance,
    enrich_rows,
    observe_instance,
)
from scitex_agent_container.cli_pkg._helpers._agent_observation_io import (
    mount_source,
    read_bytes,
)
from scitex_agent_container.cli_pkg._helpers._agent_observation_render import (
    detail_lines,
)
from scitex_agent_container.cli_pkg._helpers._agent_observation_selection import (
    cached_usage,
    codex_auth_selection,
    selected_account,
    subscription_metadata,
)
from scitex_agent_container.cli_pkg._helpers._agent_observation_versions import (
    distribution_versions,
    observe_versions,
    sac_build,
)


def _record(pid, name="owned"):
    from scitex_agent_container._runners._tmux._process_group import _identity

    identity = _identity(pid)
    return {
        "id": "instance-owned",
        "name": name,
        "host": "owned-host",
        "pid": pid,
        "process_start_time": identity.start_time,
        "process_uid": identity.uid,
        "control_group": identity.control_group,
        "remote": 0,
        "ended_at": None,
    }


@pytest.fixture
def child():
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", "import time; time.sleep(20)"],
        start_new_session=True,
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)


def _drained_workers(timeout=5.0):
    """Poll owned-worker exit; immediate reads flake under xdist load."""
    deadline = time.monotonic() + timeout
    remaining = multiprocessing.active_children()
    while remaining and time.monotonic() < deadline:
        time.sleep(0.05)
        remaining = multiprocessing.active_children()
    return remaining


def _delayed_observer(record, snapshot, detail, salt):
    if record["name"] == "slow":
        time.sleep(10)
    return observe_instance(record, snapshot, detail, salt)


def _immediate_observer(record, snapshot, detail, salt):
    return observe_instance(record, snapshot, detail, salt)


def _jwt(account="private-person-id", plan="pro"):
    claims = {
        "email": "private@example.test",
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account,
            "chatgpt_plan_type": plan,
        },
    }
    middle = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return {"tokens": {"id_token": "header." + middle + ".signature"}}


@pytest.mark.parametrize(
    "field,expected",
    [("process", "alive"), ("work", "unknown"), ("selection", {"state": "unknown"})],
)
def test_live_canonical_process_preserves_independent_evidence(child, field, expected):
    # Arrange
    record = _record(child.pid)
    # Act
    observation = observe_instance(record, {}, 0, b"salt")
    # Assert
    assert observation[field] == expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("process_start_time", 1),
        ("process_uid", -1),
        ("control_group", "/foreign"),
        ("remote", 1),
        ("ended_at", "yesterday"),
    ],
)
def test_changed_canonical_incarnation_refuses(child, field, value):
    # Arrange
    record = _record(child.pid)
    record[field] = value
    # Act
    # Assert
    with pytest.raises(ValueError, match="canonical-"):
        assert_instance(record)


def test_genuinely_exited_process_refuses(child):
    # Arrange
    record = _record(child.pid)
    child.terminate()
    child.wait(timeout=10)
    # Act
    # Assert
    with pytest.raises(ValueError, match="canonical-process-unavailable"):
        assert_instance(record)


@pytest.fixture
def hung_observation(child):
    fast = _record(child.pid, "fast")
    other = subprocess.Popen(
        [sys.executable, "-I", "-B", "-c", "import time; time.sleep(20)"],
        start_new_session=True,
    )
    try:
        slow = dict(_record(other.pid, "slow"), id="slow-instance")
        started = time.monotonic()
        # A real sleeping worker and separate fast process exercise the budget.
        rows = enrich_rows(
            [{"name": "slow"}, {"name": "fast"}],
            [slow, fast],
            "owned-host",
            budget=2.0,
            observer=_delayed_observer,
        )
    finally:
        other.terminate()
        other.wait(timeout=10)
    return rows, time.monotonic() - started


def test_hung_observation_returns_within_parent_budget(hung_observation):
    # Arrange
    _, elapsed = hung_observation
    # Act
    within_budget = elapsed < 10
    # Assert
    assert within_budget


def test_hung_observation_retains_every_row(hung_observation):
    # Arrange
    rows, _ = hung_observation
    # Act
    observed_count = len(rows)
    # Assert
    assert observed_count == 2


def test_hung_observation_marks_slow_row_unknown(hung_observation):
    # Arrange
    rows, _ = hung_observation
    # Act
    reason = rows[0]["observation"]["work_reason"]
    # Assert
    assert reason == "observation-deadline"


def test_hung_observation_preserves_fast_process_evidence(hung_observation):
    # Arrange
    rows, _ = hung_observation
    # Act
    process = rows[1]["observation"]["process"]
    # Assert
    assert process == "alive"


def test_hung_observation_reaps_owned_workers(hung_observation):
    # Arrange
    _ = hung_observation
    # Act
    remaining = _drained_workers()
    # Assert
    assert remaining == []


def test_observed_live_row_overrides_false_adapter_stopped(child):
    # Arrange
    record = _record(child.pid)
    # Act
    rows = enrich_rows(
        [{"name": "owned", "status": "stopped"}],
        [record],
        "owned-host",
        observer=_immediate_observer,
    )
    # Assert
    assert rows[0]["status"] == "running"


def test_duplicate_instances_never_choose_newest(child):
    # Arrange
    record = _record(child.pid)
    # Act
    rows = enrich_rows(
        [{"name": "owned"}], [record, dict(record, id="another")], "owned-host"
    )
    # Assert
    assert rows[0]["observation"]["work_reason"] == "canonical-instance-ambiguous"


def test_one_pid_assigned_to_different_canonical_agents_refuses(child):
    # Arrange
    record = _record(child.pid)
    other = dict(record, name="other-agent", id="other-instance")
    # Act
    rows = enrich_rows([{"name": "owned"}], [record, other], "owned-host", detail=3)
    # Assert
    assert (
        rows[0]["observation"]["work_reason"] == "canonical-process-shared-by-instances"
    )


def test_foreign_host_cannot_supply_local_account_or_work(child):
    # Arrange
    record = _record(child.pid)
    # Act
    rows = enrich_rows([{"name": "owned"}], [record], "another-host", detail=3)
    # Assert
    assert rows[0]["observation"]["selection"] == {"state": "unknown"}


def test_foreign_row_never_borrows_same_named_local_instance(child):
    # Arrange
    record = _record(child.pid)
    # Act
    rows = enrich_rows(
        [{"name": "owned", "host": "foreign"}],
        [record],
        "owned-host",
        detail=3,
    )
    # Assert
    assert rows[0]["observation"]["work_reason"] == "owning-host-observation-required"


@pytest.fixture(params=["fifo", "symlink", "oversized"])
def invalid_source(tmp_path, request):
    path = tmp_path / "source"
    if request.param == "fifo":
        os.mkfifo(path)
    elif request.param == "symlink":
        target = tmp_path / "target"
        target.write_bytes(b"secret")
        path.symlink_to(target)
    else:
        path.write_bytes(b"x" * 33)
    return path


def test_nonregular_or_oversized_source_refuses(invalid_source):
    # Arrange
    path = invalid_source
    # Act
    # Assert
    with pytest.raises((OSError, ValueError)):
        read_bytes(path, 32)


def test_private_credential_file_requires_owner_only_mode(tmp_path):
    # Arrange
    path = tmp_path / "auth.json"
    path.write_bytes(b"{}")
    path.chmod(0o644)
    # Act
    # Assert
    with pytest.raises(ValueError, match="private-owner-required"):
        read_bytes(path, 1_024, private=True)


def test_private_credential_file_accepts_owner_only_mode(tmp_path):
    # Arrange
    path = tmp_path / "auth.json"
    path.write_bytes(b"{}")
    path.chmod(0o600)
    # Act
    content = read_bytes(path, 1_024, private=True)
    # Assert
    assert content == b"{}"


def test_atime_change_is_not_a_false_content_race(tmp_path):
    # Arrange
    path = tmp_path / "metadata"
    path.write_bytes(b"owned bytes")
    os.utime(path, (1, path.stat().st_mtime))
    # Act
    content = read_bytes(path, 1_024)
    # Assert
    assert content == b"owned bytes"


def test_bound_mount_translation_uses_kernel_device_and_root(tmp_path):
    # Arrange
    selected = tmp_path / "selected"
    selected.mkdir()
    mounts = "1 0 8:1 /selected /uvwork rw - ext4 disk rw\n"
    host = f"2 0 8:1 / {tmp_path} rw - ext4 disk rw\n"
    # Act
    result = mount_source(mounts, "/uvwork", host_mountinfo=host)
    # Assert
    assert result == selected


def test_missing_selected_mount_refuses_without_guessing(tmp_path):
    # Arrange
    mounts = "1 0 8:1 /selected /uvwork rw - ext4 disk rw\n"
    host = f"2 0 8:1 / {tmp_path} rw - ext4 disk rw\n"
    # Act
    # Assert
    with pytest.raises(ValueError, match="selected-bind-unknown"):
        mount_source(mounts, "/different", host_mountinfo=host)


def test_two_selected_mounts_refuse_ambiguous_authority(tmp_path):
    # Arrange
    mounts = "1 0 8:1 /one /uvwork rw - ext4 disk rw\n2 0 8:1 /two /uvwork rw - ext4 disk rw\n"
    # Act
    # Assert
    with pytest.raises(ValueError, match="selected-bind-unknown"):
        mount_source(mounts, "/uvwork", host_mountinfo="")


@pytest.mark.parametrize(
    "private", ["private-person-id", "private@example.test", "header."]
)
def test_subscription_projection_never_returns_private_identity(private):
    # Arrange
    payload = _jwt()
    # Act
    result = subscription_metadata(payload, b"one observation salt")
    text = json.dumps(result)
    # Assert
    assert private not in text


def test_subscription_projection_preserves_declared_plan():
    # Arrange
    payload = _jwt()
    # Act
    result = subscription_metadata(payload, b"salt")
    # Assert
    assert result["plan"] == "pro"


def test_same_selected_account_shares_one_observation_group():
    # Arrange
    payload = _jwt()
    # Act
    groups = [
        subscription_metadata(payload, b"salt")["account_group"] for _ in range(2)
    ]
    # Assert
    assert groups[0] == groups[1]


def test_account_group_is_not_linkable_across_observations():
    # Arrange
    payload = _jwt()
    # Act
    groups = [
        subscription_metadata(payload, salt)["account_group"]
        for salt in (b"one", b"two")
    ]
    # Assert
    assert groups[0] != groups[1]


def test_unknown_plan_does_not_become_paid_entitlement():
    # Arrange
    payload = _jwt(plan="invented")
    # Act
    result = subscription_metadata(payload, b"salt")
    # Assert
    assert result["plan"] == "unknown"


def test_selected_chatgpt_mode_does_not_become_ambient_api_key():
    # Arrange
    payload = dict(_jwt(), auth_mode="chatgpt")
    # Act
    result = codex_auth_selection(
        payload, {"OPENAI_API_KEY": "unused-private-key"}, b"salt"
    )
    # Assert
    assert result["authentication"] == "subscription"


def test_conflicting_selected_api_key_refuses():
    # Arrange
    payload = {"auth_mode": "apikey", "OPENAI_API_KEY": "selected-key"}
    environment = {"OPENAI_API_KEY": "another-key"}
    # Act
    # Assert
    with pytest.raises(ValueError, match="selected-auth-mode-ambiguous"):
        codex_auth_selection(payload, environment, b"salt")


@pytest.mark.parametrize(
    "alias,provider",
    [("OPENCODE_GO_API_KEY_02", "opencode-go"), ("COMMANDCODE_API_KEY", "commandcode")],
)
def test_hermes_uses_its_selected_provider_key_without_claude_inventory(
    tmp_path, alias, provider
):
    # Arrange
    snapshot = {
        "harness": "hermes",
        "provider": {"auth_token_env": alias},
        "subscription_provider": "openai",
        "claude": {"account": "unrelated"},
    }
    # Act
    result = selected_account(snapshot, tmp_path, {alias: "private-key-value"}, b"salt")
    # Assert
    assert result["provider"] == provider


@pytest.mark.parametrize("private", ["private-key-value", "COMMANDCODE_API_KEY"])
def test_hermes_projection_does_not_publish_key_or_alias(tmp_path, private):
    # Arrange
    alias = "COMMANDCODE_API_KEY"
    snapshot = {"harness": "hermes", "provider": {"auth_token_env": alias}}
    # Act
    result = selected_account(snapshot, tmp_path, {alias: "private-key-value"}, b"salt")
    # Assert
    assert private not in json.dumps(result)


def test_unselected_alias_never_substitutes_for_missing_selected_key(tmp_path):
    # Arrange
    snapshot = {
        "harness": "hermes",
        "provider": {"auth_token_env": "OPENCODE_GO_API_KEY_02"},
    }
    # Act
    # Assert
    with pytest.raises(ValueError, match="selected-key-unknown"):
        selected_account(snapshot, tmp_path, {"COMMANDCODE_API_KEY": "other"}, b"salt")


@pytest.fixture
def image_observation(tmp_path):
    process = tmp_path / "process"
    image_site = process / "root/opt/venv-sac/lib/python3.12/site-packages"
    metadata = image_site / "scitex_agent_container-0.29.4.dist-info/METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text("Name: scitex-agent-container\nVersion: 0.29.4\n")
    (process / "mountinfo").write_text("")
    return observe_versions(process, {})


def test_empty_private_environment_is_unknown(image_observation):
    # Arrange
    observation = image_observation
    # Act
    state = observation["private_environment"]["state"]
    # Assert
    assert state == "unknown"


def test_image_metadata_does_not_depend_on_private_install(image_observation):
    # Arrange
    observation = image_observation
    # Act
    version = observation["image_environment"]["packages"]["scitex-agent-container"]
    # Assert
    assert version == "0.29.4"


def test_installed_metadata_does_not_invent_loaded_module_origin(image_observation):
    # Arrange
    observation = image_observation
    # Act
    state = observation["loaded_process"]["state"]
    # Assert
    assert state == "unknown"


def test_missing_private_metadata_does_not_assert_equal_versions(image_observation):
    # Arrange
    observation = image_observation
    # Act
    skew = observation["skew"]
    # Assert
    assert skew == "unknown"


def test_duplicate_distribution_metadata_refuses(tmp_path):
    # Arrange
    for dirname in ("one.dist-info", "two.dist-info"):
        path = tmp_path / dirname / "METADATA"
        path.parent.mkdir()
        path.write_text("Name: scitex-dev\nVersion: 0.62.2\n")
    # Act
    # Assert
    with pytest.raises(ValueError, match="distribution-metadata-ambiguous"):
        distribution_versions(tmp_path)


def test_passive_unknown_cache_never_claims_capacity(tmp_path):
    # Arrange
    snapshot = {"provider": {"auth_token_env": "COMMANDCODE_API_KEY"}}
    # Act
    result = cached_usage(snapshot, home=tmp_path)
    # Assert
    assert result == {"state": "unknown"}


@pytest.fixture
def render_row():
    row = {
        "name": "owned",
        "observation": {
            "process": "alive",
            "work": "unknown",
            "selection": {
                "provider": "commandcode",
                "plan": "unknown",
                "account_group": "opaque",
            },
            "versions": {
                "image_environment": {
                    "state": "observed",
                    "packages": {"scitex-dev": "0.62.2"},
                }
            },
        },
    }
    return row


@pytest.mark.parametrize("level,present", [(1, False), (2, True)])
def test_environment_detail_appears_only_from_second_verbose_level(
    render_row, level, present
):
    # Arrange
    row = render_row
    # Act
    output = "\n".join(detail_lines(row, level))
    # Assert
    assert ("image environment" in output) is present


@pytest.mark.parametrize("level,present", [(2, False), (3, True)])
def test_provenance_detail_appears_only_at_third_verbose_level(
    render_row, level, present
):
    # Arrange
    row = render_row
    # Act
    output = "\n".join(detail_lines(row, level))
    # Assert
    assert ("evidence_source" in output) is present


def test_first_verbose_level_keeps_unknown_work_visible(render_row):
    # Arrange
    row = render_row
    # Act
    output = "\n".join(detail_lines(row, 1))
    # Assert
    assert "work: unknown" in output


@pytest.fixture
def generic_heartbeat(tmp_path):
    from scitex_agent_container._lifecycle._tui_heartbeat_loop import _beat_one
    from scitex_agent_container._listen._activity_projection import activity_projection
    from scitex_agent_container._runners._session_state import (
        read_heartbeat,
        write_heartbeat,
    )

    written = _beat_one(
        {"name": "owned", "state_dir": tmp_path},
        snapshot={"tui-owned": time.time()},
        write_fn=write_heartbeat,
    )
    heartbeat = read_heartbeat(tmp_path)
    projected = activity_projection(tmp_path)
    return written, heartbeat, projected


def test_generic_pane_observer_still_writes_heartbeat(generic_heartbeat):
    # Arrange
    written, _, _ = generic_heartbeat
    # Act
    result = bool(written)
    # Assert
    assert result


def test_generic_pane_observer_cannot_publish_work(generic_heartbeat):
    # Arrange
    _, heartbeat, _ = generic_heartbeat
    # Act
    work = heartbeat["work_state"]
    # Assert
    assert work == "unknown"


def test_generic_pane_observer_cannot_invent_account_capacity(generic_heartbeat):
    # Arrange
    _, heartbeat, _ = generic_heartbeat
    # Act
    capped = heartbeat["capped"]
    # Assert
    assert capped is None


@pytest.mark.parametrize("field", ["last_progress", "operation"])
def test_generic_pane_projection_preserves_unknown_progress(generic_heartbeat, field):
    # Arrange
    _, _, projected = generic_heartbeat
    # Act
    state = projected[field]["state"]
    # Assert
    assert state == "unknown"


@pytest.mark.parametrize("flag,level", [("-v", 1), ("-vv", 2), ("-vvv", 3)])
def test_cli_accepts_incremental_detail_without_invoking_status(flag, level):
    # Arrange
    from scitex_agent_container.cli_pkg.status_cmds import status

    # Act
    context = status.make_context("status", [flag, "owned"])
    # Assert
    assert context.params["verbose"] == level


def test_remote_detail_travels_with_required_no_fanout_guard():
    # Arrange
    from scitex_agent_container.cli_pkg._helpers._agent_list_fleet_probe import (
        _remote_argv,
    )

    # Act
    argv = _remote_argv(
        capability=None, machine=None, group=None, guard=True, detail_level=2
    )
    # Assert
    assert "--no-fanout" in argv and "-vv" in argv and "--json" in argv


@pytest.fixture
def stamped_site(tmp_path):
    path = tmp_path / "scitex_agent_container/_provenance/_build_info.py"
    path.parent.mkdir(parents=True)
    stamp = {"version": "0.29.4", "commit": "a" * 40, "code_hash": "b" * 32}
    path.write_text("STAMP = " + repr(stamp) + "\n")
    return tmp_path, path, stamp


def test_build_stamp_exposes_public_source_identity_without_import(stamped_site):
    # Arrange
    site, _, stamp = stamped_site
    # Act
    result = sac_build(site, stamp["version"])
    # Assert
    assert result == {
        "state": "observed",
        "commit": stamp["commit"],
        "code_hash": stamp["code_hash"],
    }


@pytest.mark.parametrize(
    "field,value", [("commit", "branch"), ("code_hash", "bad"), ("version", "0.28.1")]
)
def test_unqualified_build_stamp_remains_unknown(stamped_site, field, value):
    # Arrange
    site, path, stamp = stamped_site
    stamp[field] = value
    path.write_text("STAMP = " + repr(stamp) + "\n")
    # Act
    result = sac_build(site, "0.29.4")
    # Assert
    assert result == {"state": "unknown"}


def test_build_stamp_call_expression_is_not_executed(stamped_site):
    # Arrange
    site, path, _ = stamped_site
    sentinel = site / "must-not-exist"
    path.write_text(
        "STAMP = __import__('pathlib').Path(" + repr(str(sentinel)) + ").touch()\n"
    )
    # Act
    sac_build(site, "0.29.4")
    # Assert
    assert not sentinel.exists()


@pytest.fixture
def two_environment_observation(tmp_path):
    process = tmp_path / "process"
    private = tmp_path / "private"
    image_site = process / "root/opt/venv-sac/lib/python3.12/site-packages"
    private_site = private / "venv-agent/lib/python3.12/site-packages"
    for site, code_hash in ((image_site, "a" * 32), (private_site, "b" * 32)):
        for name in ("scitex-agent-container", "scitex-cards", "scitex-dev"):
            metadata = site / (name + ".dist-info") / "METADATA"
            metadata.parent.mkdir(parents=True)
            metadata.write_text("Name: " + name + "\nVersion: 0.29.4\n")
        stamp_path = site / "scitex_agent_container/_provenance/_build_info.py"
        stamp_path.parent.mkdir(parents=True)
        stamp_path.write_text(
            "STAMP = "
            + repr({"version": "0.29.4", "commit": "a" * 40, "code_hash": code_hash})
            + "\n"
        )
    device = private.stat().st_dev
    device_name = f"{os.major(device)}:{os.minor(device)}"
    from pathlib import Path

    host_mounts = Path("/proc/self/mountinfo").read_text().splitlines()
    candidates = []
    for line in host_mounts:
        fields = line.split()
        if fields[2] == device_name and private.is_relative_to(fields[4]):
            candidates.append((Path(fields[4]), Path(fields[3])))
    destination, root = max(candidates, key=lambda pair: len(str(pair[0])))
    kernel_root = root / private.relative_to(destination)
    mount = f"1 0 {device_name} {kernel_root} /uvwork rw - ext4 disk rw\n"
    (process / "mountinfo").write_text(mount)
    return observe_versions(process, {})


def test_equal_semver_does_not_hide_different_source_build(two_environment_observation):
    # Arrange
    result = two_environment_observation
    # Act
    skew = result["skew"]
    # Assert
    assert skew == "different"


def test_source_build_observation_does_not_claim_loaded_mcp_origin(
    two_environment_observation,
):
    # Arrange
    result = two_environment_observation
    # Act
    state = result["loaded_process"]["state"]
    # Assert
    assert state == "unknown"


def test_source_addressed_image_is_declared_without_hash_verification(tmp_path):
    # Arrange
    process = tmp_path / "process"
    process.mkdir()
    (process / "mountinfo").write_text("")
    declaration = "/retained/sac-base-sha256-" + "a" * 64 + ".sif"
    # Act
    result = observe_versions(process, {"APPTAINER_CONTAINER": declaration})
    # Assert
    assert result["image_declaration"] == {
        "state": "declared",
        "sha256": "a" * 64,
        "verification": "not-rehashed",
    }


def test_shared_canonical_pid_cannot_preserve_false_stopped_status(child):
    # Arrange
    record = _record(child.pid)
    other = dict(record, id="another", name="other")
    # Act
    rows = enrich_rows(
        [{"name": "owned", "status": "stopped"}], [record, other], "owned-host"
    )
    # Assert
    assert rows[0]["status"] == "unknown"


def test_ambiguous_current_instance_remains_visible_in_compact_output(child):
    # Arrange
    from scitex_agent_container.cli_pkg._helpers._agent_list_render import (
        _compact_visible,
    )

    record = _record(child.pid)
    # Act
    rows = enrich_rows(
        [{"name": "owned", "status": "stopped"}],
        [record, dict(record, id="another")],
        "owned-host",
    )
    # Assert
    assert _compact_visible(rows[0])


def test_genuinely_alive_kernel_evidence_is_not_a_spec_missing_ghost(child):
    # Arrange
    from scitex_agent_container.cli_pkg._helpers._agent_list_render import _is_ghost_row

    rows = [{"name": "owned", "host": "local", "validation_errors": ["File not found"]}]
    # Act
    observed = enrich_rows(rows, [_record(child.pid)], "owned-host")
    # Assert
    assert not _is_ghost_row(observed[0])


def test_unverified_spec_missing_row_retains_ghost_filter():
    # Arrange
    from scitex_agent_container.cli_pkg._helpers._agent_list_render import _is_ghost_row

    row = {
        "name": "owned",
        "host": "local",
        "validation_errors": ["File not found"],
        "observation": {"process": "unknown"},
    }
    # Act
    ghost = _is_ghost_row(row)
    # Assert
    assert ghost


def test_declared_hermes_pool_does_not_claim_primary_alias_is_active(tmp_path):
    # Arrange
    alias = "OPENCODE_GO_API_KEY_01"
    snapshot = {
        "harness": "hermes",
        "provider": {"auth_token_env": alias},
        "hermes_failover": {"accounts": {"muse": [alias, "OPENCODE_GO_API_KEY_02"]}},
    }
    # Act
    # Assert
    with pytest.raises(ValueError, match="active-pool-selection-unavailable"):
        selected_account(snapshot, tmp_path, {alias: "primary-key"}, b"salt")
