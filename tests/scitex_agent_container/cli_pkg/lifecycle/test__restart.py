"""Tests for ``cli_pkg.lifecycle._restart.restart`` (Click command).

The command has four behavioural branches: ``--dry-run`` prints a
"would restart" line without invoking the restart collaborator; absence
of ``-y``/``--yes`` refuses with exit code ``2``; the happy path
delegates to ``agent_restart(name)`` and reports success; a YAML path
argument is resolved through ``resolve_with_prefix`` / ``load_config``
so the resolved ``config.name`` is forwarded to ``agent_restart`` rather
than the raw path; any exception from ``agent_restart`` surfaces as
exit code ``1`` with the message preserved on stderr/stdout.

PA-306: no ``unittest.mock`` / ``monkeypatch``. Production collaborators
(``agent_restart``, ``resolve_with_prefix``, ``load_config``) are
swapped at the module's namespace via a small ``_swap`` context
manager with explicit save/restore.

TQ cleanup: module docstring summarises intent (TQ001); every test
carries AAA markers (TQ002); descriptive names spell out the verified
behaviour (TQ003); each test asserts exactly one fact (TQ007).
Same-shape invariants over a single arrange/act collapse into
``pytest.parametrize``.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Callable, Iterator

import pytest
from click.testing import CliRunner

import scitex_agent_container.cli_pkg.lifecycle._restart as restart_mod
import scitex_agent_container.cli_pkg.lifecycle._restart_local as restart_local_mod
from scitex_agent_container.cli_pkg.lifecycle._restart import restart
from scitex_agent_container.cli_pkg.lifecycle._restart_verify import SessionObservation


@pytest.fixture(autouse=True)
def _instances_store(pg_schema: str):
    """A throwaway ``instances`` store for every test in this file.

    ``instances`` moved to the shared PostgreSQL store on 2026-08-28 and the
    verbs driven here read ``list_active_instances`` on every path, so the
    dependency belongs to the VERB rather than to any one case. Autouse
    rather than per-signature for that reason, and for one more: it keeps a
    NEW test in this file from silently resolving whatever store the process
    happens to point at.
    """
    yield


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path):
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


@pytest.fixture(autouse=True)
def _isolate_runtime_root(tmp_path):
    """Pin the runtime root at a real, EMPTY, per-test directory.

    ``restart`` now reads the target's identity-of-run from
    ``<runtime-root>/<agent>/instance_id`` to verify its own postcondition,
    so the runtime root is live input to every test in this file — and
    ``runtime_base_dir()`` resolves it from
    ``SCITEX_AGENT_CONTAINER_RUNTIME_DIR`` first, falling back to ``$HOME``.
    Isolating ``$HOME`` alone is therefore NOT enough: a sibling test that
    sets the env var and does not restore it (or a production value left in
    the ambient env) silently redirects these tests at the REAL fleet's
    runtime dir. That is not hypothetical — running this file as part of the
    whole ``cli_pkg/lifecycle`` directory made ``test_happy_path_exits_zero``
    read agent ``alpha``'s genuine instance_id and correctly report that a
    no-op restart cycled nothing, while the same test passed in isolation.

    Pinning it here makes the ambient environment irrelevant in BOTH
    directions: the legacy happy-path tests get a guaranteed-empty root (no
    marker either side → the verdict abstains → their historical success is
    preserved), and no test can ever touch the operator's real runtime dir.

    RESTORING THE ENV VAR IS NOT ENOUGH, and the teardown reload below is not
    decoration. ``_runners._session_state.DEFAULT_STATE_ROOT`` is a MODULE
    CONSTANT evaluated once, at import — and that import happens lazily, from
    inside the first test that reads a run marker, i.e. while this fixture
    has the env var pointed at a pytest tmp dir. Dropping the env var
    afterwards therefore un-pinned nothing: the constant stayed at the first
    test's ``tmp_path`` for the rest of the worker, and the suite's own state
    floor flagged it on the teardown of EVERY subsequent test in this file
    (69 such errors, all of them this one cause). Re-importing AFTER the
    restore is what actually puts the constant back.
    """
    import importlib

    from scitex_agent_container._runtime_paths import RUNTIME_DIR_ENV

    root = tmp_path / "runtime-root"
    root.mkdir(parents=True, exist_ok=True)
    saved = os.environ.get(RUNTIME_DIR_ENV)
    os.environ[RUNTIME_DIR_ENV] = str(root)
    try:
        yield root
    finally:
        if saved is None:
            os.environ.pop(RUNTIME_DIR_ENV, None)
        else:
            os.environ[RUNTIME_DIR_ENV] = saved
        # Order matters: the env var is restored FIRST, then the module is
        # re-executed, so the constant is re-derived from the real value.
        import scitex_agent_container._runners._session_state as _session_state

        importlib.reload(_session_state)


@contextmanager
def _swap(name: str, fn: Callable) -> Iterator[None]:
    """Swap a collaborator in BOTH restart modules (v4 step 5 split).

    The local leg (``_restart_locally`` / ``_restart_via_broker``) moved
    into ``_restart_local`` and reads its collaborators from ITS OWN
    module globals, while the command orchestration stays in
    ``_restart``. Swapping on whichever of the two carries the name
    keeps every existing test meaningful across the split.
    """
    targets = [m for m in (restart_mod, restart_local_mod) if hasattr(m, name)]
    saved = [(m, getattr(m, name)) for m in targets]
    for m in targets:
        setattr(m, name, fn)
    try:
        yield
    finally:
        for m, value in saved:
            setattr(m, name, value)


class _FakeCfg:
    def __init__(self, name: str) -> None:
        self.name = name


# ---------------------------------------------------------------------------
# --dry-run branch: no collaborator invocation, prints "would restart"
# ---------------------------------------------------------------------------


def test_dry_run_exits_zero():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["alpha", "--dry-run"])
    # Assert
    assert result.exit_code == 0


def test_dry_run_announces_target_agent():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["alpha", "--dry-run"])
    # Assert
    assert "would restart agent 'alpha'" in result.output


def test_dry_run_does_not_invoke_agent_restart():
    # Arrange
    called: list[str] = []
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda name: called.append(name)):
        runner.invoke(restart, ["alpha", "--dry-run"])
    # Assert
    assert called == []


# ---------------------------------------------------------------------------
# Confirmation guard: missing --yes refuses with exit code 2
# ---------------------------------------------------------------------------


def test_refuse_without_yes_exits_two():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["alpha"])
    # Assert
    assert result.exit_code == 2


def test_refuse_without_yes_emits_refusal_message():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["alpha"])
    # Assert
    assert "Refusing to restart" in result.output


def test_refuse_without_yes_does_not_invoke_agent_restart():
    # Arrange
    called: list[str] = []
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda name: called.append(name)):
        runner.invoke(restart, ["alpha"])
    # Assert
    assert called == []


# ---------------------------------------------------------------------------
# Happy path: bare name is forwarded to agent_restart verbatim
# ---------------------------------------------------------------------------


def test_happy_path_exits_zero():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda _name: None):
        result = runner.invoke(restart, ["alpha", "-y"])
    # Assert
    assert result.exit_code == 0, result.output


def test_happy_path_forwards_name_to_agent_restart():
    # Arrange
    called: list[str] = []
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda name: called.append(name)):
        runner.invoke(restart, ["alpha", "-y"])
    # Assert
    assert called == ["alpha"]


def test_happy_path_reports_success():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda _name: None):
        result = runner.invoke(restart, ["alpha", "-y"])
    # Assert
    assert "restarted" in result.output


# ---------------------------------------------------------------------------
# YAML path argument: resolved through resolve_with_prefix/load_config,
# resolved config.name (not the raw path) is forwarded to agent_restart.
# ---------------------------------------------------------------------------


@pytest.fixture
def _yaml_path(tmp_path):
    path = tmp_path / "foo.yaml"
    path.write_text("name: foo\n")
    return path


def test_yaml_path_exits_zero(_yaml_path):
    # Arrange
    runner = CliRunner()
    # Act
    with (
        _swap("resolve_with_prefix", lambda *_a, **_kw: str(_yaml_path)),
        _swap("load_config", lambda *_a, **_kw: _FakeCfg("resolved")),
        _swap("agent_restart", lambda _name: None),
    ):
        result = runner.invoke(restart, [str(_yaml_path), "-y"])
    # Assert
    assert result.exit_code == 0, result.output


def test_yaml_path_forwards_resolved_name(_yaml_path):
    # Arrange
    called: list[str] = []
    runner = CliRunner()
    # Act
    with (
        _swap("resolve_with_prefix", lambda *_a, **_kw: str(_yaml_path)),
        _swap("load_config", lambda *_a, **_kw: _FakeCfg("resolved")),
        _swap("agent_restart", lambda name: called.append(name)),
    ):
        runner.invoke(restart, [str(_yaml_path), "-y"])
    # Assert
    assert called == ["resolved"]


# ---------------------------------------------------------------------------
# Failure path: any exception from agent_restart surfaces as exit code 1
# and the message is reported back to the user.
# ---------------------------------------------------------------------------


def _boom(_name: Any) -> None:
    raise RuntimeError("boom")


def test_failure_exits_one():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("agent_restart", _boom):
        result = runner.invoke(restart, ["alpha", "-y"])
    # Assert
    assert result.exit_code == 1


def test_failure_reports_exception_message():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("agent_restart", _boom):
        result = runner.invoke(restart, ["alpha", "-y"])
    # Assert
    assert "boom" in result.output


# ---------------------------------------------------------------------------
# Cross-host dispatch: an active state.db row on a PEER node makes restart
# ssh into that peer and run `sac agents restart --yes --json` there — the
# node-aware automation of the manual recipe. Real on-disk state.db + a
# PATH-prepended fake ssh shim (no mocks; mirrors test__stop.py).
# ---------------------------------------------------------------------------


@pytest.fixture
def cross_host_state_db(tmp_path):
    """Per-test state.db at tmp_path; SCITEX_AGENT_CONTAINER_STATE_DB +
    module reload so the env override actually takes effect. The current
    host resolves to ``lead-host`` and one peer ``peer-x`` is declared.
    """
    import importlib

    db = tmp_path / "state.db"
    saved_db_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_host_env = os.environ.get("SAC_HOST")
    saved_cfg_env = os.environ.get("SCITEX_AGENT_CONTAINER_CONFIG")
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    os.environ["SAC_HOST"] = "lead-host"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "host:\n  fallback: hostname-short\npeers:\n  peer-x:\n    ssh: peer-x\n"
    )
    os.environ["SCITEX_AGENT_CONTAINER_CONFIG"] = str(cfg)
    import scitex_agent_container._state.state_db as _state_db_mod

    importlib.reload(_state_db_mod)
    try:
        yield tmp_path
    finally:
        for k, v in (
            ("SCITEX_AGENT_CONTAINER_STATE_DB", saved_db_env),
            ("SAC_HOST", saved_host_env),
            ("SCITEX_AGENT_CONTAINER_CONFIG", saved_cfg_env),
        ):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(_state_db_mod)


@pytest.fixture
def remote_row_for_zeta(cross_host_state_db):
    """Seed an active row for agent ``zeta`` on peer ``peer-x``."""
    from scitex_agent_container._state.state_db import record_instance_start

    # Port reads as a whole (carve-out, see _skills .../14_numeric-literals.md).
    port = 18_888
    iid = record_instance_start(name="zeta", host="peer-x", a2a_port=port)
    return iid


@pytest.fixture
def ssh_shim(tmp_path):
    """PATH-prepended fake ssh that emits a restart JSON envelope and rc=0,
    recording each invocation's argv so tests can assert the right node +
    verb + flags were dispatched.
    """
    import json
    import sys

    bin_dir = tmp_path / "_shim_bin"
    bin_dir.mkdir(exist_ok=True)
    log = bin_dir / "ssh.argv.jsonl"
    payload = '{"name":"zeta","restarted":true,"a2a_port":18888,"dispatched":false}'
    script = bin_dir / "ssh"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"with open({json.dumps(str(log))}, 'a') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"sys.stdout.write({json.dumps(payload)})\n"
        "sys.exit(0)\n"
    )
    script.chmod(0o755)
    saved_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{saved_path}"
    try:
        yield bin_dir
    finally:
        os.environ["PATH"] = saved_path


def _ssh_invocations(bin_dir):
    import json as _json

    log = bin_dir / "ssh.argv.jsonl"
    if not log.exists():
        return []
    return [_json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]


def test_cross_host_restart_dispatches_via_ssh(remote_row_for_zeta, ssh_shim):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["zeta", "-y"])
    # Assert
    assert result.exit_code == 0, result.output


def test_cross_host_restart_ssh_argv_targets_peer_node(remote_row_for_zeta, ssh_shim):
    # Arrange
    runner = CliRunner()
    # Act
    runner.invoke(restart, ["zeta", "-y"])
    argv = _ssh_invocations(ssh_shim)[-1]
    # Assert — the recorded host (peer-x), not the local node, is targeted.
    assert "peer-x" in argv


def test_cross_host_restart_ssh_argv_carries_restart_verb(
    remote_row_for_zeta, ssh_shim
):
    # Arrange
    runner = CliRunner()
    # Act
    runner.invoke(restart, ["zeta", "-y"])
    argv = " ".join(_ssh_invocations(ssh_shim)[-1])
    # Assert
    assert "sac agents restart zeta" in argv


def test_cross_host_restart_ssh_argv_includes_json_flag(remote_row_for_zeta, ssh_shim):
    # Arrange
    runner = CliRunner()
    # Act
    runner.invoke(restart, ["zeta", "-y"])
    argv = _ssh_invocations(ssh_shim)[-1]
    # Assert
    assert "--json" in argv


def test_cross_host_restart_does_not_call_local_agent_restart(
    remote_row_for_zeta, ssh_shim
):
    # Arrange — when the row is remote, the local restart MUST NOT run.
    called: list[str] = []
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda name: called.append(name)):
        runner.invoke(restart, ["zeta", "-y"])
    # Assert
    assert called == []


def test_cross_host_restart_json_envelope_marks_dispatched(
    remote_row_for_zeta, ssh_shim
):
    import json as _json

    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["zeta", "-y", "--json"])
    envelope = _json.loads(result.stdout)
    # Assert
    assert envelope.get("dispatched") is True


def test_cross_host_restart_reopens_fresh_remote_row(remote_row_for_zeta, ssh_shim):
    # Arrange
    from scitex_agent_container._state.state_db import list_active_instances

    runner = CliRunner()
    # Act
    runner.invoke(restart, ["zeta", "-y"])
    rows = [r for r in list_active_instances() if r["name"] == "zeta"]
    # Assert — exactly one active row, still on the peer (old closed, new opened).
    assert len(rows) == 1 and rows[0]["host"] == "peer-x"


# ---------------------------------------------------------------------------
# No-registry-row fallback at the CLI level: an agent with NO active state.db
# row (ad-hoc / pre-autorecord launch) restarts locally via the spec, instead
# of attempting any cross-host ssh.
# ---------------------------------------------------------------------------


def test_no_row_agent_restarts_locally_without_ssh(cross_host_state_db, ssh_shim):
    # Arrange — no row seeded for ``solo``; agent_restart swapped to a recorder.
    called: list[str] = []
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda name: called.append(name)):
        runner.invoke(restart, ["solo", "-y"])
    # Assert — local path taken (agent_restart called), no ssh dispatched.
    assert called == ["solo"] and _ssh_invocations(ssh_shim) == []


def test_local_restart_json_envelope_marks_not_dispatched(
    cross_host_state_db, ssh_shim
):
    import json as _json

    # Arrange — no row; local restart with --json. agent_restart no-op.
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda _name: None):
        result = runner.invoke(restart, ["solo", "-y", "--json"])
    envelope = _json.loads(result.stdout)
    # Assert — JSON envelope reports the local (non-dispatched) restart.
    assert envelope.get("dispatched") is False and envelope.get("restarted") is True


def test_local_restart_failure_json_envelope_carries_error(
    cross_host_state_db, ssh_shim
):
    import json as _json

    # Arrange — local restart raises; --json must surface the error.
    runner = CliRunner()
    # Act
    with _swap("agent_restart", _boom):
        result = runner.invoke(restart, ["solo", "-y", "--json"])
    envelope = _json.loads(result.stdout)
    # Assert
    assert "boom" in envelope.get("error", "")


# ---------------------------------------------------------------------------
# Cross-host dispatch error branches: a remote node that exits non-zero, or
# returns non-JSON stdout, must surface a clear RuntimeError (exit 1) — no
# silent fallback. Real fake-ssh shims drive each branch.
# ---------------------------------------------------------------------------


def _install_ssh_shim(bin_dir, *, rc: int, stdout: str):
    """Write a PATH-prepended fake ssh emitting ``stdout`` and exiting ``rc``."""
    import json
    import sys

    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "ssh"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.stdout.write({json.dumps(stdout)})\n"
        f"sys.exit({rc})\n"
    )
    script.chmod(0o755)
    saved_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{saved_path}"
    return saved_path


@pytest.fixture
def ssh_shim_rc1(tmp_path):
    """Fake ssh that exits non-zero (remote restart failed)."""
    bin_dir = tmp_path / "_shim_rc1"
    saved_path = _install_ssh_shim(bin_dir, rc=1, stdout="remote boom")
    try:
        yield bin_dir
    finally:
        os.environ["PATH"] = saved_path


@pytest.fixture
def ssh_shim_nonjson(tmp_path):
    """Fake ssh that exits zero but emits non-JSON stdout (peer too old)."""
    bin_dir = tmp_path / "_shim_nonjson"
    saved_path = _install_ssh_shim(bin_dir, rc=0, stdout="not json at all")
    try:
        yield bin_dir
    finally:
        os.environ["PATH"] = saved_path


def test_cross_host_restart_remote_failure_exits_one(remote_row_for_zeta, ssh_shim_rc1):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["zeta", "-y"])
    # Assert — remote rc=1 surfaces as a local exit-1, not a silent pass.
    assert result.exit_code == 1


def test_cross_host_restart_remote_failure_reports_peer(
    remote_row_for_zeta, ssh_shim_rc1
):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["zeta", "-y"])
    # Assert — the failure message names the peer node.
    assert "peer-x" in result.output


def test_cross_host_restart_nonjson_stdout_exits_one(
    remote_row_for_zeta, ssh_shim_nonjson
):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["zeta", "-y"])
    # Assert — non-JSON peer stdout is a hard error, not a silent fallback.
    assert result.exit_code == 1


def test_cross_host_restart_nonjson_stdout_reports_non_json(
    remote_row_for_zeta, ssh_shim_nonjson
):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["zeta", "-y"])
    # Assert
    assert "non-JSON" in result.output


# ---------------------------------------------------------------------------
# --fresh branch: a fresh (no-resume) restart is broker-only. Inside a SIF it
# brokers ``start --force --fresh`` (fresh=True) and never touches the local
# restart; on a bare host there is nothing to broker to, so it fails LOUD with
# the direct command rather than silently doing a resuming restart.
# ---------------------------------------------------------------------------


def _broker_ok(name, *, fresh=False):
    """Stand-in for ``brokered_restart`` — always succeeds."""
    return {"name": name, "restarted": True, "via": "host-listen"}, True


def test_fresh_on_bare_host_exits_one():
    # Arrange — not inside a SIF, so there is no host listen to broker to.
    runner = CliRunner()
    # Act
    with _swap("must_broker_to_host", lambda: False):
        result = runner.invoke(restart, ["alpha", "-y", "--fresh"])
    # Assert
    assert result.exit_code == 1


def test_fresh_on_bare_host_reports_direct_start_command():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("must_broker_to_host", lambda: False):
        result = runner.invoke(restart, ["alpha", "-y", "--fresh"])
    # Assert — fail loud with the deterministic bare-host command.
    assert "start alpha --force --fresh" in result.output


def test_fresh_in_sif_brokers_fresh_true():
    # Arrange — in a SIF; record the brokered (name, fresh).
    calls: list[tuple[str, bool]] = []

    def _rec(name, *, fresh=False):
        calls.append((name, fresh))
        return {"name": name, "restarted": True}, True

    runner = CliRunner()
    # Act
    with (
        _swap("must_broker_to_host", lambda: True),
        _swap("brokered_restart", _rec),
    ):
        runner.invoke(restart, ["alpha", "-y", "--fresh"])
    # Assert
    assert calls == [("alpha", True)]


def test_fresh_does_not_call_local_agent_restart():
    # Arrange — fresh is broker-only; the local restart must NOT run.
    called: list[str] = []
    runner = CliRunner()
    # Act
    with (
        _swap("must_broker_to_host", lambda: True),
        _swap("brokered_restart", _broker_ok),
        _swap("agent_restart", lambda name: called.append(name)),
    ):
        runner.invoke(restart, ["alpha", "-y", "--fresh"])
    # Assert
    assert called == []


def test_brokered_restart_threads_fresh_to_the_host_client():
    # Arrange — ``brokered_restart`` is the only caller of the host client, so
    # the fresh flag must survive that hop too (the CLI test above stops at
    # ``brokered_restart``'s own signature).
    import scitex_agent_container.cli_pkg.lifecycle._restart_remote as remote_mod

    seen: list[tuple[str, bool]] = []

    def _client(name, fresh=False):
        seen.append((name, fresh))
        return {"returncode": 0, "stdout": ""}

    saved = remote_mod._restart_via_host_bypass
    remote_mod._restart_via_host_bypass = _client
    # Act
    try:
        remote_mod.brokered_restart("alpha", fresh=True)
    finally:
        remote_mod._restart_via_host_bypass = saved
    # Assert
    assert seen == [("alpha", True)]


# ---------------------------------------------------------------------------
# Variadic NAME... + --all (operator TODO 2026-07-04): restart accepts
# multiple names in one call and an --all flag that enumerates the same
# fleet ``sac agents list`` shows. The per-agent restart is exercised via
# ``_restart_one``; the loop-level behaviour (fan-out, mutual exclusion,
# fail-loud aggregate, JSON aggregation) is asserted by swapping that seam.
# ---------------------------------------------------------------------------


def _ok_restart_one(name, *, as_json, fresh):
    """Recorder stand-in for ``_restart_one`` — always succeeds."""
    return {"name": name, "restarted": True, "dispatched": False}, True


def test_multiple_names_exit_zero():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("_restart_one", _ok_restart_one):
        result = runner.invoke(restart, ["alpha", "beta", "-y"])
    # Assert
    assert result.exit_code == 0, result.output


def test_multiple_names_restart_each_once():
    # Arrange
    seen: list[str] = []

    def _rec(name, *, as_json, fresh):
        seen.append(name)
        return {"name": name, "restarted": True}, True

    runner = CliRunner()
    # Act
    with _swap("_restart_one", _rec):
        runner.invoke(restart, ["alpha", "beta", "-y"])
    # Assert — each name restarted exactly once, in order.
    assert seen == ["alpha", "beta"]


def test_multiple_names_json_emits_array():
    import json as _json

    # Arrange
    runner = CliRunner()
    # Act
    with _swap("_restart_one", _ok_restart_one):
        result = runner.invoke(restart, ["alpha", "beta", "-y", "--json"])
    payload = _json.loads(result.stdout)
    # Assert — multiple names aggregate into a JSON array.
    assert isinstance(payload, list) and len(payload) == 2


def test_all_flag_restarts_every_enumerated_agent():
    # Arrange
    seen: list[str] = []

    def _rec(name, *, as_json, fresh):
        seen.append(name)
        return {"name": name, "restarted": True}, True

    runner = CliRunner()
    # Act
    with (
        _swap("_enumerate_fleet", lambda: ["a1", "a2", "a3"]),
        _swap("_restart_one", _rec),
    ):
        result = runner.invoke(restart, ["--all", "-y"])
    # Assert — every enumerated agent restarted.
    assert result.exit_code == 0 and seen == ["a1", "a2", "a3"]


def test_all_flag_json_emits_array():
    import json as _json

    # Arrange
    runner = CliRunner()
    # Act
    with (
        _swap("_enumerate_fleet", lambda: ["only-one"]),
        _swap("_restart_one", _ok_restart_one),
    ):
        result = runner.invoke(restart, ["--all", "-y", "--json"])
    payload = _json.loads(result.stdout)
    # Assert — --all always emits an array, even for a single enumerated agent.
    assert isinstance(payload, list) and len(payload) == 1


def test_all_with_explicit_names_is_usage_error():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["--all", "alpha", "-y"])
    # Assert — --all and explicit NAMEs are mutually exclusive (exit 2).
    assert result.exit_code == 2 and "cannot be combined" in result.output


def test_all_without_yes_refuses_exit_two():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("_enumerate_fleet", lambda: ["a1", "a2"]):
        result = runner.invoke(restart, ["--all"])
    # Assert — a batch restart still requires -y/--yes.
    assert result.exit_code == 2 and "Refusing to restart" in result.output


def test_no_names_and_no_all_is_usage_error():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["-y"])
    # Assert — nothing to restart is a usage error.
    assert result.exit_code == 2


def test_one_failure_still_attempts_rest():
    # Arrange
    seen: list[str] = []

    def _rec(name, *, as_json, fresh):
        seen.append(name)
        if name == "bad":
            return {"name": name, "error": "boom"}, False
        return {"name": name, "restarted": True}, True

    runner = CliRunner()
    # Act
    with _swap("_restart_one", _rec):
        runner.invoke(restart, ["ok1", "bad", "ok2", "-y"])
    # Assert — a mid-batch failure does not abort the remaining agents.
    assert seen == ["ok1", "bad", "ok2"]


def test_one_failure_exits_nonzero():
    # Arrange
    def _rec(name, *, as_json, fresh):
        if name == "bad":
            return {"name": name, "error": "boom"}, False
        return {"name": name, "restarted": True}, True

    runner = CliRunner()
    # Act
    with _swap("_restart_one", _rec):
        result = runner.invoke(restart, ["ok1", "bad", "-y"])
    # Assert — any failure surfaces as an overall non-zero exit.
    assert result.exit_code == 1


def test_dry_run_multiple_names_lists_each():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["alpha", "beta", "--dry-run"])
    # Assert — dry-run announces every target and invokes no restart.
    assert (
        "would restart agent 'alpha'" in result.output
        and "would restart agent 'beta'" in result.output
    )


def test_single_name_json_stays_bare_object():
    import json as _json

    # Arrange — single explicit name must keep the historical bare-object shape.
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda _name: None):
        result = runner.invoke(restart, ["alpha", "-y", "--json"])
    payload = _json.loads(result.stdout)
    # Assert
    assert isinstance(payload, dict) and payload.get("name") == "alpha"


# ---------------------------------------------------------------------------
# Split selection flags (operator request 2026-07-09): --all was surprising
# because it restarted stopped agents too. It is now split into
# --all-running (only currently-running agents) and --all-registry (every
# registered agent == the old --all behaviour). --all stays a backward-compat
# alias for --all-registry. The two modes are mutually exclusive; more than
# one selection flag fails loud.
# ---------------------------------------------------------------------------


def test_all_running_restarts_only_running_agents():
    # Arrange — --all-running must enumerate via the RUNNING-only seam.
    seen: list[str] = []

    def _rec(name, *, as_json, fresh):
        seen.append(name)
        return {"name": name, "restarted": True}, True

    runner = CliRunner()
    # Act
    with (
        _swap("_enumerate_running", lambda: ["live-1", "live-2"]),
        _swap("_enumerate_fleet", lambda: ["live-1", "live-2", "stopped-3"]),
        _swap("_restart_one", _rec),
    ):
        result = runner.invoke(restart, ["--all-running", "-y"])
    # Assert — only the running agents restarted (stopped-3 excluded).
    assert result.exit_code == 0 and seen == ["live-1", "live-2"]


def test_all_registry_restarts_every_agent():
    # Arrange — --all-registry must enumerate via the full-fleet seam.
    seen: list[str] = []

    def _rec(name, *, as_json, fresh):
        seen.append(name)
        return {"name": name, "restarted": True}, True

    runner = CliRunner()
    # Act
    with (
        _swap("_enumerate_running", lambda: ["live-1"]),
        _swap("_enumerate_fleet", lambda: ["live-1", "stopped-2", "stopped-3"]),
        _swap("_restart_one", _rec),
    ):
        result = runner.invoke(restart, ["--all-registry", "-y"])
    # Assert — every registered agent restarted, stopped ones included.
    assert result.exit_code == 0 and seen == ["live-1", "stopped-2", "stopped-3"]


def test_all_alias_matches_all_registry_behaviour():
    # Arrange — --all is a backward-compat alias for --all-registry: it must
    # enumerate the FULL fleet, not the running-only subset.
    seen: list[str] = []

    def _rec(name, *, as_json, fresh):
        seen.append(name)
        return {"name": name, "restarted": True}, True

    runner = CliRunner()
    # Act
    with (
        _swap("_enumerate_running", lambda: ["live-1"]),
        _swap("_enumerate_fleet", lambda: ["live-1", "stopped-2"]),
        _swap("_restart_one", _rec),
    ):
        result = runner.invoke(restart, ["--all", "-y"])
    # Assert — --all == --all-registry (uses the full-fleet enumeration).
    assert result.exit_code == 0 and seen == ["live-1", "stopped-2"]


def test_all_running_and_all_registry_together_is_usage_error():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["--all-running", "--all-registry", "-y"])
    # Assert — the two selection modes are mutually exclusive (exit 2).
    assert result.exit_code == 2 and "mutually exclusive" in result.output


def test_all_running_and_all_alias_together_is_usage_error():
    # Arrange — --all is the alias for --all-registry, so it conflicts with
    # --all-running just the same.
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["--all-running", "--all", "-y"])
    # Assert
    assert result.exit_code == 2 and "mutually exclusive" in result.output


def test_all_running_with_explicit_names_is_usage_error():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("_enumerate_running", lambda: ["live-1"]):
        result = runner.invoke(restart, ["--all-running", "alpha", "-y"])
    # Assert — a selection flag cannot combine with explicit NAMEs (exit 2).
    assert result.exit_code == 2 and "cannot" in result.output


def test_all_running_json_emits_array():
    import json as _json

    # Arrange
    runner = CliRunner()
    # Act
    with (
        _swap("_enumerate_running", lambda: ["only-live"]),
        _swap("_restart_one", _ok_restart_one),
    ):
        result = runner.invoke(restart, ["--all-running", "-y", "--json"])
    payload = _json.loads(result.stdout)
    # Assert — a batch selection flag always emits an array.
    assert isinstance(payload, list) and len(payload) == 1


def test_enumerate_running_keeps_only_running_status_rows():
    # Arrange — _enumerate_running reuses the SAME data (and liveness) as the
    # list command; it must keep only rows whose probed status is "running".
    import scitex_agent_container.cli_pkg._helpers as _helpers_mod

    rows = [
        {"name": "run-a", "status": "running"},
        {"name": "stop-b", "status": "stopped"},
        {"name": "unk-c", "status": "unknown"},
        {"name": "def-d", "status": "defined"},
        {"name": "run-e", "status": "running"},
    ]
    saved = getattr(_helpers_mod, "get_agent_list_data", None)
    _helpers_mod.get_agent_list_data = lambda *_a, **_kw: rows
    # Act
    try:
        got = restart_mod._enumerate_running()
    finally:
        if saved is None:
            delattr(_helpers_mod, "get_agent_list_data")
        else:
            _helpers_mod.get_agent_list_data = saved
    # Assert — only the two running agents survive, in order.
    assert got == ["run-a", "run-e"]


# ---------------------------------------------------------------------------
# WHERE the restart runs (P0, 2026-07-20). An in-SIF ``sac`` cannot touch a
# host agent's tmux session, so the restart MUST be brokered to the host
# listen. The old plain path decided this by EXCEPTION — it brokered only when
# the local restart raised an error whose text contained "not found in
# registry" — which never happened for an agent whose spec is bind-mounted
# into the container (i.e. all of them). The restart then ran locally, touched
# nothing, and printed green. These tests arm exactly that condition: local
# resolution SUCCEEDS, so a predicate that waits for a failure cannot fire.
#
# The package conftest clears APPTAINER_CONTAINER / SINGULARITY_CONTAINER for
# every test (the suite itself runs inside a SIF), so "not in a SIF" is the
# default and ``in_sif_env`` opts a test back in with a real env var.
# ---------------------------------------------------------------------------


_LISTEN_URL_KEYS = (
    "SAC_LISTEN_BASE_URL",
    "SCITEX_AGENT_CONTAINER_LISTEN_BASE_URL",
)


@pytest.fixture
def in_sif_env():
    """Run the CLI as if inside an apptainer SIF (real env var, restored).

    ARMS THE CONDITION AND PROVES IT: the fixture asserts the production
    detector actually reports in-SIF before yielding, so a test using it
    can never quietly exercise the bare-host branch instead. (The arming
    check lives here rather than in each test because the suite's TQ007
    rule allows exactly one assertion per test function.)
    """
    from scitex_agent_container._lifecycle._in_sif_broker import is_in_sif

    saved = os.environ.get("APPTAINER_CONTAINER")
    os.environ["APPTAINER_CONTAINER"] = "/path/to/agent.sif"
    try:
        assert is_in_sif() is True, "fixture failed to arm the in-SIF branch"
        yield
    finally:
        if saved is None:
            os.environ.pop("APPTAINER_CONTAINER", None)
        else:
            os.environ["APPTAINER_CONTAINER"] = saved


@pytest.fixture
def bare_host_env():
    """Assert the conftest really left us OUTSIDE a SIF (arming check)."""
    from scitex_agent_container._lifecycle._in_sif_broker import is_in_sif

    assert is_in_sif() is False, "fixture failed to arm the bare-host branch"
    yield


@pytest.fixture
def no_listen_url():
    """Clear both spellings of the listen base URL; restore on teardown."""
    saved = {k: os.environ.get(k) for k in _LISTEN_URL_KEYS}
    for key in _LISTEN_URL_KEYS:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        for key, prev in saved.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev


def _record_broker(sink: list[str]):
    """Build a ``brokered_restart`` stand-in that records the brokered name."""

    def _broker(name, *, fresh=False):
        sink.append(name)
        return {"name": name, "restarted": True, "via": "host-listen"}, True

    return _broker


def test_in_sif_restart_of_a_resolvable_agent_is_brokered(in_sif_env):
    # Arrange — local resolution SUCCEEDS here (``agent_restart`` returns
    # instead of raising), which is precisely what made the old
    # exception-gated predicate unreachable. ``in_sif_env`` proves the
    # branch is armed before the test body runs.
    brokered: list[str] = []
    runner = CliRunner()
    # Act
    with (
        _swap("brokered_restart", _record_broker(brokered)),
        _swap("agent_restart", lambda _name: True),
    ):
        runner.invoke(restart, ["broker-me", "-y"])
    # Assert
    assert brokered == ["broker-me"]


def test_in_sif_restart_never_runs_the_local_restart(in_sif_env):
    # Arrange — the local leg cannot reach the host's tmux session, so it must
    # not run at all; running it is the silent no-op this fix removes.
    local: list[str] = []
    runner = CliRunner()
    # Act
    with (
        _swap("brokered_restart", _record_broker([])),
        _swap("agent_restart", lambda name: local.append(name)),
    ):
        runner.invoke(restart, ["broker-me", "-y"])
    # Assert
    assert local == []


def test_outside_a_sif_restart_runs_locally(bare_host_env):
    # Arrange — ``bare_host_env`` proves the SIF markers really are clear.
    local: list[str] = []
    brokered: list[str] = []
    runner = CliRunner()
    # Act
    with (
        _swap("brokered_restart", _record_broker(brokered)),
        _swap("agent_restart", lambda name: local.append(name)),
    ):
        runner.invoke(restart, ["local-me", "-y"])
    # Assert
    assert local == ["local-me"] and brokered == []


def test_in_sif_without_a_listen_url_exits_one(in_sif_env, no_listen_url):
    # Arrange — in a SIF with nothing to broker to. The restart must FAIL
    # LOUD; there is deliberately no fall-through to the local path.
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["broker-me", "-y"])
    # Assert
    assert result.exit_code == 1, result.output


def test_in_sif_without_a_listen_url_does_not_fall_back_to_local(
    in_sif_env, no_listen_url
):
    # Arrange — the silent local fallback IS the bug; assert it is gone.
    local: list[str] = []
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda name: local.append(name)):
        runner.invoke(restart, ["broker-me", "-y"])
    # Assert
    assert local == []


def test_in_sif_without_a_listen_url_names_the_missing_env_var(
    in_sif_env, no_listen_url
):
    # Arrange
    import json

    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["broker-me", "-y", "--json"])
    payload = json.loads(result.stdout)
    # Assert — the operator is told WHICH knob is missing.
    assert "SAC_LISTEN_BASE_URL" in payload.get("error", "")


# ---------------------------------------------------------------------------
# Postcondition: a restart that changed NOTHING must not report success.
# ``<runtime-dir>/<agent>/instance_id`` is the agent's identity-of-run (a uuid7
# minted at launch, deleted at stop), so a cycled agent has a NEW one. The
# runtime root is relocated to a real tmp dir via the production env var, and
# the restart collaborator is a REAL function that either rewrites the marker
# (a genuine restart), leaves it alone (the P0's no-op) or deletes it (a stop
# whose start leg never came back).
#
# THE MARKER IS NECESSARY, NEVER SUFFICIENT (P0 2026-08-14). It is written by
# the same start path these tests are checking, so a NEW marker on its own is
# an echo, not evidence — measured on scitex-compute-04, "verified: ... is a
# NEW run" printed over a tmux session alive and untouched since the previous
# day. A pass now also needs the OS to agree, read through the session name
# ``instances.screen`` records; with the registry isolated and EMPTY here,
# that second witness is absent by construction, so these tests pin the
# ABSTENTION ("cannot verify") rather than the old unearned pass. The
# two-witness table itself is ``test_verify_cycled_is_a_ternary`` below, and
# the end-to-end proof against a real tmux server lives in
# ``test__restart_verify_session.py``.
# ---------------------------------------------------------------------------


@pytest.fixture
def runtime_root(_isolate_runtime_root):
    """The per-test runtime root, named for tests that write markers into it."""
    return _isolate_runtime_root


@pytest.fixture
def isolated_state_db(tmp_path, pg_schema: str):
    """Pin the ``instances`` registry at a real, EMPTY, per-test state.db.

    The postcondition now reads a SECOND witness — ``instances.screen``, the
    tmux session the start path recorded — so the registry is live input to
    every test below, exactly as the runtime root already was. Left ambient,
    these tests would ask the OPERATOR'S real fleet database whether
    ``verify-me`` names a session, and would answer differently on a host
    that happens to hold such a row. Isolating it makes "no row names a
    session for this agent" a FACT of the test rather than a property of the
    machine it runs on.
    """
    import importlib

    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    os.environ[key] = str(tmp_path / "state.db")
    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    try:
        yield tmp_path / "state.db"
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        importlib.reload(mod)


def _write_run_marker(root, name: str, value: str) -> None:
    """Write a real ``instance_id`` marker for ``name`` under ``root``."""
    state_dir = root / name
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "instance_id").write_text(value, encoding="utf-8")


def _current_run(name: str):
    """Read the marker back through the production resolver."""
    from scitex_agent_container.cli_pkg.lifecycle._restart_verify import (
        read_run_identity,
    )

    return read_run_identity(name)


def _envelope(result):
    """Parse the CLI's ``--json`` envelope off the last stdout line."""
    import json

    return json.loads(result.stdout)


@pytest.fixture
def armed_run_marker(runtime_root, isolated_state_db):
    """Give ``verify-me`` a real run marker and PROVE the CLI can read it.

    Arming is checked HERE, not in each test: a fixture that silently
    fails to arm turns every postcondition test into a test of nothing
    (before and after would both be ``None``, which the ternary correctly
    refuses to call a failure — so the suite would go green over a broken
    check). Asserting in the fixture also keeps each test at the single
    assertion the suite's TQ007 rule requires.
    """
    _write_run_marker(runtime_root, "verify-me", "run-1")
    assert _current_run("verify-me") == "run-1", (
        "fixture failed to arm the run marker — the postcondition tests "
        "below would be vacuous"
    )
    return runtime_root


@pytest.fixture
def no_run_marker(runtime_root, isolated_state_db):
    """Prove ``ghost-agent`` has NO run marker (the abstention case)."""
    assert _current_run("ghost-agent") is None, (
        "fixture failed to arm the no-evidence case — a stray marker would "
        "make this test assert the wrong branch"
    )
    return runtime_root


def _noop_restart(_name):
    """A restart that returns happily and changes nothing — the P0's shape."""
    return True


def test_restart_that_leaves_the_run_unchanged_exits_one(armed_run_marker):
    # Arrange
    runner = CliRunner()
    # Act — the restart returns happily and touches nothing.
    with _swap("agent_restart", _noop_restart):
        result = runner.invoke(restart, ["verify-me", "-y"])
    # Assert
    assert result.exit_code == 1, result.output


def test_restart_that_leaves_the_run_unchanged_reports_verified_false(
    armed_run_marker,
):
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("agent_restart", _noop_restart):
        result = runner.invoke(restart, ["verify-me", "-y", "--json"])
    # Assert
    assert _envelope(result).get("verified") is False


def test_restart_that_leaves_the_run_unchanged_reports_the_same_run_both_sides(
    armed_run_marker,
):
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("agent_restart", _noop_restart):
        result = runner.invoke(restart, ["verify-me", "-y", "--json"])
    envelope = _envelope(result)
    # Assert — the evidence travels with the verdict.
    assert (envelope.get("run_before"), envelope.get("run_after")) == (
        "run-1",
        "run-1",
    )


def _cycling_restart_for(root):
    """Build a REAL restart that replaces the marker, as a launch would."""

    def _restart(_name):
        _write_run_marker(root, "verify-me", "run-2")
        return True

    return _restart


def test_restart_that_cycles_the_run_exits_zero(armed_run_marker):
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("agent_restart", _cycling_restart_for(armed_run_marker)):
        result = runner.invoke(restart, ["verify-me", "-y"])
    # Assert
    assert result.exit_code == 0, result.output


def test_restart_that_cycles_only_the_ledger_cannot_be_verified(armed_run_marker):
    # Arrange — the marker cycles, and NOTHING else does. This used to report
    # ``verified: true``, which is the P0 of 2026-08-14: the marker is written
    # by the very start path being checked, so on its own it can only ever
    # agree with itself. With no ``instances`` row naming a tmux session there
    # is no second witness, and the honest answer is "I could not check".
    runner = CliRunner()
    # Act
    with _swap("agent_restart", _cycling_restart_for(armed_run_marker)):
        result = runner.invoke(restart, ["verify-me", "-y", "--json"])
    # Assert — an abstention, not the old unearned pass.
    assert _envelope(result).get("verified") is None


def test_unverifiable_restart_is_not_reported_as_a_failure(armed_run_marker):
    # Arrange — the mirror-image lie must not be invented either: "I could not
    # check" is not "it failed", so the restart's own outcome stands.
    runner = CliRunner()
    # Act
    with _swap("agent_restart", _cycling_restart_for(armed_run_marker)):
        result = runner.invoke(restart, ["verify-me", "-y", "--json"])
    # Assert
    assert _envelope(result).get("restarted") is True


def test_unverifiable_restart_is_not_printed_under_the_word_verified(armed_run_marker):
    # Arrange — the console line is what the operator actually reads, and
    # printing an ABSTENTION under the word "verified" is how an unchecked
    # restart came to look like a checked one. v4 step 5 fixed the label
    # too: "NOT verified" was a BINARY word on a TERNARY verdict — it
    # accused a restart nobody could observe. An abstention now renders
    # in its own words.
    runner = CliRunner()
    # Act
    with _swap("agent_restart", _cycling_restart_for(armed_run_marker)):
        result = runner.invoke(restart, ["verify-me", "-y"])
    # Assert
    assert "CANNOT VERIFY" in result.output


def test_restart_that_leaves_no_run_at_all_exits_one(armed_run_marker):
    # Arrange — the stop leg ran, the start leg never came back.
    def _stop_only(_name):
        (armed_run_marker / "verify-me" / "instance_id").unlink()
        return True

    runner = CliRunner()
    # Act
    with _swap("agent_restart", _stop_only):
        result = runner.invoke(restart, ["verify-me", "-y"])
    # Assert
    assert result.exit_code == 1, result.output


def test_no_marker_on_either_side_does_not_invent_a_failure(no_run_marker):
    # Arrange — no evidence at all. Reporting FAILURE here would be the exact
    # mirror of the false SUCCESS being fixed, so the verdict must abstain.
    runner = CliRunner()
    # Act
    with _swap("agent_restart", _noop_restart):
        result = runner.invoke(restart, ["ghost-agent", "-y", "--json"])
    # Assert
    assert result.exit_code == 0, result.output


def test_no_marker_on_either_side_reports_verified_null(no_run_marker):
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("agent_restart", _noop_restart):
        result = runner.invoke(restart, ["ghost-agent", "-y", "--json"])
    # Assert — abstention is reported as such, never as a pass or a fail.
    assert _envelope(result).get("verified") is None


_BLIND = SessionObservation()
_OLD_SESSION = SessionObservation(True, "tui-some-agent@1000")
_NEW_SESSION = SessionObservation(True, "tui-some-agent@2000")
_NO_SESSION = SessionObservation(True, None)


@pytest.mark.parametrize(
    "before,after,seen_before,seen_after,expected",
    [
        # BOTH witnesses agree the agent cycled — the only path to a pass.
        ("run-1", "run-2", _OLD_SESSION, _NEW_SESSION, True),
        ("run-1", "run-2", _NO_SESSION, _NEW_SESSION, True),
        (None, "run-2", _NO_SESSION, _NEW_SESSION, True),
        # The ledger says NEW RUN and nobody asked the OS. This is the P0's
        # own input, and it used to be the pass above: the marker is written
        # by the start path under test, so alone it is an echo, not evidence.
        ("run-1", "run-2", _BLIND, _BLIND, None),
        (None, "run-2", _BLIND, _BLIND, None),
        # A new run id minted over a session the OS says never moved.
        ("run-1", "run-2", _OLD_SESSION, _OLD_SESSION, False),
        # The ledger says it came back up; the OS says nothing is running.
        ("run-1", "run-2", _OLD_SESSION, _NO_SESSION, False),
        # Ledger-definitive NOs, unchanged: still the same run, or no run at
        # all afterwards. These need no second witness to be conclusive.
        ("run-1", "run-1", _BLIND, _BLIND, False),
        ("run-1", None, _BLIND, _BLIND, False),
        # No evidence from either witness.
        (None, None, _BLIND, _BLIND, None),
    ],
)
def test_verify_cycled_is_a_ternary(before, after, seen_before, seen_after, expected):
    # Arrange
    from scitex_agent_container.cli_pkg.lifecycle._restart_verify import verify_cycled

    # Act
    verdict = verify_cycled(
        "some-agent",
        before,
        after,
        session_before=seen_before,
        session_after=seen_after,
    )
    # Assert — True / False / None, never a two-valued collapse.
    assert verdict.verified is expected


# ---------------------------------------------------------------------------
# Decision log: whether a restart was handled locally or brokered — and why —
# is recorded before any work happens. The listen log can only ever show what
# ARRIVED, so a request that was never sent used to leave no trace anywhere.
# ---------------------------------------------------------------------------


def _decision_entries(runtime_root):
    """Read the JSONL decision log written under the relocated runtime root."""
    import json

    path = runtime_root / "logs" / "restart_decision.log"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def test_decision_log_records_a_local_restart(runtime_root):
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda _name: True):
        runner.invoke(restart, ["local-me", "-y"])
    entries = _decision_entries(runtime_root)
    # Assert
    assert entries[0]["site"] == "local", entries


def test_decision_log_records_a_brokered_restart(runtime_root, in_sif_env):
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("brokered_restart", _record_broker([])):
        runner.invoke(restart, ["broker-me", "-y"])
    entries = _decision_entries(runtime_root)
    # Assert
    assert entries[0]["site"] == "host-listen", entries


def test_decision_log_records_the_reason_for_the_route(runtime_root, in_sif_env):
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("brokered_restart", _record_broker([])):
        runner.invoke(restart, ["broker-me", "-y"])
    entries = _decision_entries(runtime_root)
    # Assert — the WHY is written down, not just the WHAT.
    assert "apptainer SIF" in entries[0]["why"]


def test_decision_log_records_the_outcome_after_the_work(runtime_root):
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda _name: True):
        runner.invoke(restart, ["local-me", "-y"])
    entries = _decision_entries(runtime_root)
    # Assert — one line for the decision, one for what came of it.
    assert [e["event"] for e in entries] == ["decided", "completed"]
