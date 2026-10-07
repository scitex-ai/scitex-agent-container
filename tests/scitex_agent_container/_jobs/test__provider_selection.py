"""The real federated provider honors persistent host selection on every read."""

import pytest

from scitex_agent_container._jobs._jobs_plugin import provide_jobs

RECOVERY = {
    "scitex-agent-container-fleet-reconcile",
    "scitex-agent-container-restart-login-expired-agents",
    "scitex-agent-container-resume-rate-limited-agents",
}


def _jobs(root, *, env=None):
    return provide_jobs(root=root, env={} if env is None else env)


def _write(root, content):
    root.mkdir(exist_ok=True)
    path = root / "jobs-enabled.txt"
    path.write_text(content)
    return path


def test_absent_selection_keeps_existing_provider_payload(tmp_path):
    names = {j.name for j in _jobs(tmp_path)}
    assert RECOVERY <= names
    assert "scitex-agent-container-accounts-refresh" in names


def test_empty_selection_intentionally_disarms_only_sac_provider(tmp_path):
    _write(tmp_path, "# operator selected no SAC periodic jobs\n")
    assert _jobs(tmp_path) == []


@pytest.mark.parametrize(
    "token",
    [
        "fleet-reconcile",
        "scitex-agent-container-fleet-reconcile",
        "sac.fleet-reconcile",
    ],
)
def test_short_canonical_and_historical_alias_select_one_actual_job(tmp_path, token):
    _write(tmp_path, token + "\n")
    assert [j.name for j in _jobs(tmp_path)] == [
        "scitex-agent-container-fleet-reconcile"
    ]


def test_wildcard_preserves_every_declared_job(tmp_path):
    baseline = {j.name for j in _jobs(tmp_path)}
    _write(tmp_path, "*\n")
    assert {j.name for j in _jobs(tmp_path)} == baseline


def test_env_selection_precedence_retains_existing_contract(tmp_path):
    _write(tmp_path, "*\n")
    names = [j.name for j in _jobs(tmp_path, env={"SAC_JOBS_ENABLED": "worktree-gc"})]
    assert names == ["scitex-agent-container-worktree-gc"]


def test_blank_env_keeps_persistent_pause(tmp_path):
    _write(tmp_path, "")
    assert _jobs(tmp_path, env={"SAC_JOBS_ENABLED": " "}) == []


@pytest.mark.parametrize("content", ["unknown-job\n", "*\nunknown-job\n"])
def test_unknown_selection_is_visible_refusal_even_with_wildcard(tmp_path, content):
    _write(tmp_path, content)
    with pytest.raises(ValueError, match="unknown job names"):
        _jobs(tmp_path)


def test_unreadable_selection_does_not_rearm_jobs(tmp_path):
    # An actual directory where the file should be raises an actual I/O error.
    (tmp_path / "jobs-enabled.txt").mkdir()
    with pytest.raises(RuntimeError, match="refusing job discovery"):
        _jobs(tmp_path)


def test_broken_authored_selection_symlink_does_not_rearm_jobs(tmp_path):
    (tmp_path / "jobs-enabled.txt").symlink_to(tmp_path / "missing-pause-config")
    with pytest.raises(RuntimeError, match="broken symlink"):
        _jobs(tmp_path)


def test_invalid_utf8_selection_is_not_treated_as_absent(tmp_path):
    (tmp_path / "jobs-enabled.txt").write_bytes(b"\xff\xfe")
    with pytest.raises(UnicodeError):
        _jobs(tmp_path)


def test_changed_file_is_applied_on_each_provider_discovery(tmp_path):
    from scitex_dev._supervisor._runtime import Supervisor

    supervisor = Supervisor(
        discover=lambda: _jobs(tmp_path),
        discover_placement_fn=lambda: [],
        state_path=tmp_path / "state.json",
        log_dir=tmp_path / "logs",
    )
    before = {j.name for j in supervisor.discover_periodic_jobs()}
    assert RECOVERY <= before
    keep = before - RECOVERY
    path = _write(tmp_path, "\n".join(sorted(keep)) + "\n")
    assert {j.name for j in supervisor.discover_periodic_jobs()} == keep
    path.write_text("*\n")
    assert {j.name for j in supervisor.discover_periodic_jobs()} == before


def test_canonical_scitex_dir_override_controls_provider_path(tmp_path, monkeypatch):
    ecosystem_root = tmp_path / "ecosystem"
    own_root = ecosystem_root / "agent-container"
    own_root.mkdir(parents=True)
    _write(own_root, "worktree-gc\n")
    monkeypatch.setenv("SCITEX_DIR", str(ecosystem_root))
    assert [j.name for j in provide_jobs(env={})] == [
        "scitex-agent-container-worktree-gc"
    ]
