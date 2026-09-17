"""Twin-agent derivation + host-side context inheritance.

Real behaviour, no mocks of the code under test: pure spec-doc transforms
tested against real dicts, and ``seed_twin_from_parent`` tested against a
real on-disk parent spec (resolved via the real config resolver), real
per-agent state dirs, a real transcript file, and the real
``read_session_id`` / ``write_session_id`` marker helpers. The only stub is
an honest runtime collaborator exposing ``_state_dir`` (mirrors
ApptainerContainerRuntime's resolver API), same as the session-seed suite.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from scitex_agent_container._lifecycle._twin import (
    CARDS_AGENT_ENV,
    RETIRED_AGENT_ENV,
    TWIN_PARENT_ENV,
    TwinSeedError,
    _atomic_write_hermes_seed,
    _ensure_twin_worktree,
    _materialize_hermes_fork_seed,
    _reject_symlink_components,
    _resolve_host_repo_from_binds,
    _visible_history_digest,
    build_twin_boot_kick,
    derive_twin_spec,
    prepare_twin_spawn,
    resolve_twin_name,
    seed_twin_from_parent,
)
from scitex_agent_container._runners._session_state import (
    read_session_id,
    write_session_id,
)
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import ClaudeSpec
from scitex_agent_container.config._validation import validate_raw
from tests.scitex_agent_container._helpers.explicit_spec import (
    explicit_doc,
    explicitize_yaml,
)

_UUID = "123e4567-e89b-12d3-a456-426614174000"


def _parent_doc() -> dict:
    """A representative parent spec document (the raw v3 shape on disk)."""
    return {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "metadata": {"labels": {"role": "worker"}},
        "spec": {
            "runtime": "apptainer",
            "host": "local",
            "workdir": "/home/agent/proj/x",
            "apptainer": {
                "image": "/x.sif",
                "binds": ["~/proj:/home/agent/proj:rw"],
            },
            "claude": {
                "model": "opus",
                "session": "continue",
                "channels": ["server:sac", "server:claude-code-telegrammer"],
            },
            "env": {
                "SCITEX_CARDS_AGENT_ID": "parent",
                "SAC_NAME": "parent",
                "FOO": "bar",
            },
            "restart": {"policy": "always"},
            "a2a": {"port": 7901},
        },
    }


def _current_v3_parent_doc() -> dict:
    doc = explicit_doc(
        {
            "runtime": "tui",
            "harness": "claude-code",
            "workdir": "/scratch/ywatanabe/parent-workdir",
            "apptainer": {
                "image": "sac-base",
                "overlay": "/scratch/sac/agents/parent/overlay",
                "env": {CARDS_AGENT_ENV: "parent", "KEEP": "yes"},
            },
            "available_harnesses": {
                "claude-code": {
                    "session": {"mode": "continue", "max_age_minutes": None},
                    "approval_policy": "never",
                    "watchdog": {
                        "enabled": False,
                        "interval": 1.5,
                        "responses": {"y_n": "1", "y_y_n": "2", "waiting": "wait"},
                    },
                }
            },
        },
        metadata={"labels": {"role": "worker"}},
    )
    # Current independent-harness specs declare one surface, not the legacy
    # Claude/Docker/watchdog compatibility blocks in the generic test defaults.
    for legacy in ("claude", "container", "watchdog", "context_management"):
        doc["spec"].pop(legacy, None)
    doc["spec"]["comms"]["channels"] = ["server:sac", "server:scitex-cards"]
    return doc


# ─── resolve_twin_name ────────────────────────────────────────────────────


def test_resolve_twin_name_defaults_to_parent_twin():
    # Arrange
    parent = "neurovista"
    # Act
    name = resolve_twin_name(parent, None, [])
    # Assert
    assert name == "neurovista-fork"


def test_resolve_twin_name_bumps_when_default_taken():
    # Arrange
    existing = ["neurovista-fork"]
    # Act
    name = resolve_twin_name("neurovista", None, existing)
    # Assert
    assert name == "neurovista-fork-2"


def test_resolve_twin_name_honours_explicit_request():
    # Arrange
    requested = "neurovista-writer"
    # Act
    name = resolve_twin_name("neurovista", requested, ["neurovista-writer"])
    # Assert
    assert name == "neurovista-writer"


@pytest.mark.parametrize("name", ["../escape", "a/b", ".", "..", "/absolute", "UPPER"])
def test_resolve_twin_name_rejects_non_component_parent(name: str) -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(TwinSeedError, match="invalid parent agent name"):
        resolve_twin_name(name, None, [])


@pytest.mark.parametrize("name", ["../escape", "a/b", ".", "..", "/absolute", "UPPER"])
def test_resolve_twin_name_rejects_non_component_child(name: str) -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(TwinSeedError, match="invalid fork agent name"):
        resolve_twin_name("parent", name, [])


def test_resolve_twin_name_rejects_parent_as_child() -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(TwinSeedError, match="must differ"):
        resolve_twin_name("parent", "parent", [])


# ─── derive_twin_spec: current-v3 isolation contract ──────────────────────


def test_current_v3_twin_validates_and_uses_apptainer_env() -> None:
    # Arrange
    parent = _current_v3_parent_doc()
    # Act
    twin = derive_twin_spec(
        parent, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert (
        validate_raw(twin, "<twin>"),
        "env" in twin["spec"],
        twin["spec"]["apptainer"]["env"][CARDS_AGENT_ENV],
        twin["spec"]["apptainer"]["env"][TWIN_PARENT_ENV],
    ) == ([], False, "parent-twin", "parent")


def test_current_v3_twin_keeps_container_workdir_and_isolates_overlay() -> None:
    # Arrange
    parent = _current_v3_parent_doc()
    # Act
    twin = derive_twin_spec(
        parent, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert (
        twin["spec"]["workdir"] == parent["spec"]["workdir"],
        twin["spec"]["apptainer"]["overlay"] != parent["spec"]["apptainer"]["overlay"],
        "parent-twin" not in twin["spec"]["workdir"],
        "parent-twin" in twin["spec"]["apptainer"]["overlay"],
    ) == (True, True, True, True)


def test_derived_fork_drops_broad_and_parent_writable_binds() -> None:
    # Arrange
    parent = _current_v3_parent_doc()
    parent["spec"]["apptainer"]["binds"] = [
        "/scratch:/scratch:rw",
        "/home/ywatanabe:/home/ywatanabe:rw",
        "/srv/parent:/work/repo:rw",
        "/etc/ssl:/etc/ssl:ro",
    ]
    parent["spec"]["apptainer"]["relaxed"] = True
    parent["spec"]["apptainer"]["raw_args"] = [
        "--bind",
        "/home/ywatanabe:/host-home:rw",
    ]

    # Act
    fork = derive_twin_spec(
        parent, twin_name="parent-fork", parent_name="parent", persist=False
    )

    # Assert
    assert (
        fork["spec"]["apptainer"]["binds"],
        fork["spec"]["apptainer"]["relaxed"],
        fork["spec"]["apptainer"]["raw_args"],
    ) == (["/etc/ssl:/etc/ssl:ro"], False, [])


def test_current_v3_claude_twin_sets_selected_session_continue() -> None:
    # Arrange
    parent = _current_v3_parent_doc()
    # Act
    twin = derive_twin_spec(
        parent, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert (
        twin["spec"].get("claude"),
        twin["spec"]["available_harnesses"]["claude-code"]["session"]["mode"],
    ) == (parent["spec"].get("claude"), "continue")


def test_hermes_fork_uses_native_continue_session() -> None:
    # Arrange
    parent = _current_v3_parent_doc()
    parent["spec"]["harness"] = "hermes"
    parent["spec"]["available_harnesses"] = {
        "hermes": {"session": {"mode": "continue", "max_age_minutes": None}}
    }
    # Act
    fork = derive_twin_spec(
        parent, twin_name="parent-fork", parent_name="parent", persist=False
    )
    # Assert
    assert (
        fork["spec"]["harness"],
        tuple(fork["spec"]["available_harnesses"]),
        fork["spec"]["available_harnesses"]["hermes"]["session"],
    ) == (
        "hermes",
        ("hermes",),
        {"mode": "continue", "max_age_minutes": None},
    )


def test_scitex_hub_gui_shape_normalizes_to_selected_harness_authority() -> None:
    # Arrange — concrete failing parent shape: mixed harness library, legacy
    # claude compatibility block, and legacy top-level env.
    parent = _current_v3_parent_doc()
    parent["spec"]["engine"] = "qwen"
    parent["spec"]["available_engines"] = {
        "qwen": {"provider": "anthropic", "model": "qwen3-coder"}
    }
    parent["spec"]["available_harnesses"]["hermes"] = {
        "session": {"mode": "continue", "max_age_minutes": None}
    }
    parent["spec"]["claude"] = {
        "model": "",
        "channels": ["server:sac", "server:scitex-cards"],
        "flags": [],
        "raw_options": {},
        "session": "continue",
        "continue_max_age_minutes": None,
        "resume_id": "",
        "auto_accept": True,
        "account": "",
        "credentials_file": "",
        "credentials_files": [],
        "provider": None,
    }
    parent["spec"]["env"] = {"HUB_MODE": "gui"}
    # Act
    twin = derive_twin_spec(
        parent,
        twin_name="scitex-hub-auth-gui",
        parent_name="scitex-hub-gui",
        persist=False,
    )
    # Assert
    assert (
        tuple(twin["spec"]["available_harnesses"]),
        "claude" in twin["spec"],
        "env" in twin["spec"],
        twin["spec"]["engine"],
        twin["spec"]["available_engines"]["qwen"]["model"],
        twin["spec"]["workdir"],
        twin["spec"]["apptainer"]["binds"],
        twin["spec"]["available_harnesses"]["claude-code"]["session"]["mode"],
        twin["spec"]["apptainer"]["env"]["HUB_MODE"],
        twin["spec"]["apptainer"]["env"][CARDS_AGENT_ENV],
        twin["spec"]["apptainer"]["env"][TWIN_PARENT_ENV],
    ) == (
        ("claude-code",),
        False,
        False,
        "qwen",
        "qwen3-coder",
        "/scratch/ywatanabe/parent-workdir",
        parent["spec"]["apptainer"]["binds"],
        "continue",
        "gui",
        "scitex-hub-auth-gui",
        "scitex-hub-gui",
    )


def test_actual_scitex_hub_gui_hermes_parent_preserves_references_not_secrets(
    env_save_restore,
) -> None:
    # Arrange — sanitized compute authority shape; the provider key is an env
    # NAME in the spec and its VALUE must never be resolved into the fork.
    parent = _current_v3_parent_doc()
    parent["spec"]["harness"] = "hermes"
    parent["spec"]["engine"] = "opencode-go-deepseek-v4.1-flash"
    parent["spec"]["workdir"] = "/home/ywatanabe/proj/scitex-hub"
    parent["spec"]["apptainer"]["binds"] = [
        "/scratch:/scratch:rw",
        "/home/ywatanabe:/home/ywatanabe:rw",
        "/home/ywatanabe/.ssh:/home/agent/.ssh:ro",
        "/home/ywatanabe/.config/gh:/home/agent/.config/gh:ro",
    ]
    parent["spec"]["available_harnesses"] = {
        "claude-code": parent["spec"]["available_harnesses"]["claude-code"],
        "codex": {
            "session": {"mode": "continue", "max_age_minutes": None},
            "approval_policy": "never",
            "sandbox_mode": "danger-full-access",
        },
        "hermes": {"session": {"mode": "continue", "max_age_minutes": None}},
    }
    parent["spec"]["available_engines"] = {
        "opencode-go-deepseek-v4.1-flash": {
            "model": "deepseek-v4.1-flash",
            "provider": {
                "base_url": "http://provider.invalid/v1",
                "auth_token_env": "SAC_PROVIDER_KEY",
            },
        }
    }
    parent["spec"]["env"] = {"NON_SECRET_IDENTITY": "scitex-hub-gui"}
    env_save_restore.set("SAC_PROVIDER_KEY", "must-not-enter-fork-artifacts")
    # Act
    fork = derive_twin_spec(
        parent,
        twin_name="scitex-hub-disposable-fork",
        parent_name="scitex-hub-gui",
        persist=False,
    )
    encoded = yaml.safe_dump(fork)
    # Assert
    assert (
        fork["spec"]["harness"],
        tuple(fork["spec"]["available_harnesses"]),
        "env" in fork["spec"],
        fork["spec"]["engine"],
        fork["spec"]["available_engines"]["opencode-go-deepseek-v4.1-flash"]["model"],
        fork["spec"]["available_engines"]["opencode-go-deepseek-v4.1-flash"][
            "provider"
        ]["auth_token_env"],
        fork["spec"]["workdir"],
        fork["spec"]["apptainer"]["binds"],
        fork["spec"]["available_harnesses"]["hermes"]["session"]["mode"],
        fork["spec"]["apptainer"]["env"]["NON_SECRET_IDENTITY"],
        "must-not-enter-fork-artifacts" in encoded,
    ) == (
        "hermes",
        ("hermes",),
        False,
        "opencode-go-deepseek-v4.1-flash",
        "deepseek-v4.1-flash",
        "SAC_PROVIDER_KEY",
        "/home/ywatanabe/proj/scitex-hub",
        [
            "/home/ywatanabe/.ssh:/home/agent/.ssh:ro",
            "/home/ywatanabe/.config/gh:/home/agent/.config/gh:ro",
        ],
        "continue",
        "scitex-hub-gui",
        False,
    )


def test_hermes_fork_seed_uses_engine_scoped_keys_and_owner_only_file(
    tmp_path: Path,
) -> None:
    # Arrange
    parent = _current_v3_parent_doc()["spec"]
    parent["harness"] = "hermes"
    parent["available_harnesses"] = {
        "hermes": {"session": {"mode": "continue", "max_age_minutes": None}}
    }
    parent["engine"] = "engine-a"
    child = derive_twin_spec(
        {"apiVersion": "scitex-agent-container/v3", "kind": "Agent", "spec": parent},
        twin_name="disposable-fork",
        parent_name="scitex-hub-gui",
        persist=False,
    )["spec"]
    calls = []
    visible = [
        {"role": "user", "text": "known-parent-nonce"},
        {"role": "assistant", "text": "acknowledged"},
    ]

    def branch(state_dir, **kwargs):
        calls.append((state_dir, kwargs))
        return {
            "version": 1,
            "title": kwargs["child_session_key"],
            "parent_session_id": "parent-stored",
            "cwd": kwargs["child_cwd"],
            "messages": visible,
        }

    # Act
    path = _materialize_hermes_fork_seed(
        parent_name="scitex-hub-gui",
        child_name="disposable-fork",
        parent_spec=parent,
        child_spec=child,
        state_root=tmp_path,
        branch_fn=branch,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    # Assert
    assert (
        len(calls),
        calls[0][0],
        calls[0][1],
        payload,
        path.stat().st_mode & 0o777,
    ) == (
        1,
        tmp_path / "scitex-hub-gui",
        {
            "parent_session_key": "sac:scitex-hub-gui:engine-a",
            "child_session_key": "sac:disposable-fork:engine-a",
            "child_cwd": parent["workdir"],
        },
        {
            "version": 1,
            "title": "sac:disposable-fork:engine-a",
            "parent_session_id": "parent-stored",
            "cwd": parent["workdir"],
            "messages": visible,
            "parent_name": "scitex-hub-gui",
            "parent_engine": "engine-a",
            "visible_history_sha256": _visible_history_digest(visible),
        },
        0o600,
    )


def test_hermes_seed_atomic_writer_completes_partial_writes(tmp_path: Path) -> None:
    # Arrange
    target = tmp_path / "runtime" / "child" / "hermes-fork-seed.json"
    payload = b'{"version":1,"messages":[{"role":"user","text":"x"}]}'

    def partial_write(fd: int, remaining: bytes) -> int:
        return os.write(fd, remaining[:3])

    # Act
    _atomic_write_hermes_seed(target, payload, write_fn=partial_write)

    # Assert
    assert (target.read_bytes(), target.stat().st_mode & 0o777) == (payload, 0o600)


def test_hermes_seed_reuse_rejects_stale_parent_binding(tmp_path: Path) -> None:
    # Arrange
    parent = _current_v3_parent_doc()["spec"]
    parent["harness"] = "hermes"
    parent["available_harnesses"] = {
        "hermes": {"session": {"mode": "continue", "max_age_minutes": None}}
    }
    parent["engine"] = "engine-a"
    child = derive_twin_spec(
        {"apiVersion": "scitex-agent-container/v3", "kind": "Agent", "spec": parent},
        twin_name="child",
        parent_name="parent",
        persist=False,
    )["spec"]
    visible = [{"role": "user", "text": "bound history"}]

    def branch(_state_dir, **kwargs):
        return {
            "version": 1,
            "title": kwargs["child_session_key"],
            "parent_session_id": "stored-parent",
            "cwd": kwargs["child_cwd"],
            "messages": visible,
        }

    path = _materialize_hermes_fork_seed(
        parent_name="parent",
        child_name="child",
        parent_spec=parent,
        child_spec=child,
        state_root=tmp_path,
        branch_fn=branch,
    )
    stale = json.loads(path.read_text(encoding="utf-8"))
    stale["parent_name"] = "attacker"
    path.write_text(json.dumps(stale), encoding="utf-8")
    path.chmod(0o600)

    def must_not_rebranch(*_args, **_kwargs):
        raise AssertionError("must not rebranch")

    # Act
    action = pytest.raises(TwinSeedError, match="does not match")
    # Assert
    with action:
        _materialize_hermes_fork_seed(
            parent_name="parent",
            child_name="child",
            parent_spec=parent,
            child_spec=child,
            state_root=tmp_path,
            branch_fn=must_not_rebranch,
        )


def test_parent_container_workdir_maps_to_host_bind_source(tmp_path: Path) -> None:
    # Arrange
    repo = tmp_path / "host-repo"
    repo.mkdir()
    binds = [f"{tmp_path}:/home/agent/proj:rw"]
    # Act
    mapped = _resolve_host_repo_from_binds("/home/agent/proj/host-repo", binds)
    # Assert
    assert mapped == repo


def test_parent_container_workdir_requires_matching_bind(tmp_path: Path) -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(TwinSeedError, match="no parent apptainer bind maps"):
        _resolve_host_repo_from_binds("/home/agent/proj/repo", [f"{tmp_path}:/other"])


def test_current_v3_twin_does_not_mutate_parent() -> None:
    # Arrange
    import copy

    parent = _current_v3_parent_doc()
    before = copy.deepcopy(parent)
    # Act
    derive_twin_spec(
        parent, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert parent == before


def test_ensure_twin_worktree_creates_detached_checkout(tmp_path: Path) -> None:
    # Arrange
    parent = tmp_path / "parent"
    twin = tmp_path / "twins" / "child" / "workdir"
    parent.mkdir()
    subprocess.run(["git", "init", "-q", str(parent)], check=True)
    subprocess.run(
        ["git", "-C", str(parent), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(parent), "config", "user.name", "Twin Test"],
        check=True,
    )
    (parent / "tracked.txt").write_text("parent\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(parent), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(parent), "commit", "-qm", "seed"], check=True)

    # Act
    _ensure_twin_worktree(str(parent), str(twin))

    # Assert
    assert (
        (twin / "tracked.txt").read_text(encoding="utf-8"),
        subprocess.run(
            ["git", "-C", str(twin), "rev-parse", "--is-inside-work-tree"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
        subprocess.run(
            ["git", "-C", str(twin), "symbolic-ref", "-q", "HEAD"],
            check=False,
        ).returncode,
    ) == ("parent\n", "true", 1)


def test_ensure_twin_worktree_rejects_existing_non_worktree(tmp_path: Path) -> None:
    # Arrange
    parent = tmp_path / "parent"
    twin = tmp_path / "occupied"
    parent.mkdir()
    subprocess.run(["git", "init", "-q", str(parent)], check=True)
    twin.mkdir()

    # Act
    raises_ctx = pytest.raises(TwinSeedError, match="not a git worktree")

    # Assert
    with raises_ctx:
        _ensure_twin_worktree(str(parent), str(twin))


def test_ensure_twin_worktree_rejects_unrelated_git_checkout(tmp_path: Path) -> None:
    # Arrange
    parent = tmp_path / "parent"
    occupied = tmp_path / "occupied"
    subprocess.run(["git", "init", "-q", str(parent)], check=True)
    subprocess.run(["git", "init", "-q", str(occupied)], check=True)

    # Act
    raises_ctx = pytest.raises(TwinSeedError, match="different git repository")

    # Assert
    with raises_ctx:
        _ensure_twin_worktree(str(parent), str(occupied))


def test_ensure_twin_worktree_rejects_attached_existing_worktree(
    tmp_path: Path,
) -> None:
    # Arrange
    parent = tmp_path / "parent"
    twin = tmp_path / "twin"
    parent.mkdir()
    subprocess.run(["git", "init", "-q", str(parent)], check=True)
    subprocess.run(
        ["git", "-C", str(parent), "config", "user.email", "t@invalid"], check=True
    )
    subprocess.run(["git", "-C", str(parent), "config", "user.name", "T"], check=True)
    (parent / "f").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(parent), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(parent), "commit", "-qm", "seed"], check=True)
    subprocess.run(
        ["git", "-C", str(parent), "worktree", "add", "-q", "-b", "child", str(twin)],
        check=True,
    )
    # Act
    # Assert
    with pytest.raises(TwinSeedError, match="detached HEAD"):
        _ensure_twin_worktree(str(parent), str(twin))


def test_ensure_twin_worktree_rejects_dirty_existing_worktree(tmp_path: Path) -> None:
    # Arrange
    parent = tmp_path / "parent"
    twin = tmp_path / "twin"
    parent.mkdir()
    subprocess.run(["git", "init", "-q", str(parent)], check=True)
    subprocess.run(
        ["git", "-C", str(parent), "config", "user.email", "t@invalid"], check=True
    )
    subprocess.run(["git", "-C", str(parent), "config", "user.name", "T"], check=True)
    (parent / "f").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(parent), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(parent), "commit", "-qm", "seed"], check=True)
    subprocess.run(
        ["git", "-C", str(parent), "worktree", "add", "-q", "--detach", str(twin)],
        check=True,
    )
    (twin / "f").write_text("dirty", encoding="utf-8")
    # Act
    # Assert
    with pytest.raises(TwinSeedError, match="not clean"):
        _ensure_twin_worktree(str(parent), str(twin))


def test_ensure_twin_worktree_rejects_stale_parent_head(tmp_path: Path) -> None:
    # Arrange
    parent = tmp_path / "parent"
    twin = tmp_path / "twin"
    parent.mkdir()
    subprocess.run(["git", "init", "-q", str(parent)], check=True)
    subprocess.run(
        ["git", "-C", str(parent), "config", "user.email", "t@invalid"], check=True
    )
    subprocess.run(["git", "-C", str(parent), "config", "user.name", "T"], check=True)
    (parent / "f").write_text("first", encoding="utf-8")
    subprocess.run(["git", "-C", str(parent), "add", "f"], check=True)
    subprocess.run(["git", "-C", str(parent), "commit", "-qm", "first"], check=True)
    subprocess.run(
        ["git", "-C", str(parent), "worktree", "add", "-q", "--detach", str(twin)],
        check=True,
    )
    (parent / "f").write_text("second", encoding="utf-8")
    subprocess.run(["git", "-C", str(parent), "commit", "-qam", "second"], check=True)

    # Act
    action = pytest.raises(TwinSeedError, match="parent HEAD")
    # Assert
    with action:
        _ensure_twin_worktree(str(parent), str(twin))


def test_ensure_twin_worktree_rejects_symlinked_parent_component(
    tmp_path: Path,
) -> None:
    # Arrange
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    # Act
    # Assert
    with pytest.raises(TwinSeedError, match="symlinked path component"):
        _ensure_twin_worktree(str(alias), str(tmp_path / "twin"))


def test_twin_overlay_rejects_symlinked_component(tmp_path: Path) -> None:
    # Arrange
    real = tmp_path / "real-overlay-root"
    real.mkdir()
    alias = tmp_path / "overlay-alias"
    alias.symlink_to(real, target_is_directory=True)
    # Act
    # Assert
    with pytest.raises(TwinSeedError, match="symlinked path component"):
        _reject_symlink_components(alias / "child" / "overlay", label="twin overlay")


def test_prepare_current_v3_twin_validates_without_mutating_parent_checkout(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    parent = tmp_path / "parent-repo"
    parent.mkdir()
    subprocess.run(["git", "init", "-q", str(parent)], check=True)
    subprocess.run(
        ["git", "-C", str(parent), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(parent), "config", "user.name", "Twin Test"],
        check=True,
    )
    (parent / "tracked.txt").write_text("parent\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(parent), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(parent), "commit", "-qm", "seed"], check=True)
    agents = tmp_path / "agents"
    spec_dir = agents / "parent"
    spec_dir.mkdir(parents=True)
    doc = _current_v3_parent_doc()
    doc["spec"]["workdir"] = str(parent)
    (spec_dir / "spec.yaml").write_text(
        yaml.safe_dump(doc, sort_keys=False), encoding="utf-8"
    )
    env_save_restore.set("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(agents))
    env_save_restore.set("SCITEX_DIR", str(tmp_path / "state"))

    # Act
    name, twin = prepare_twin_spawn("parent", twin_name="parent-twin")

    # Assert
    assert (
        name,
        validate_raw(twin, "<twin>"),
        twin["spec"]["workdir"],
        subprocess.run(
            ["git", "-C", str(parent), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout,
    ) == ("parent-twin", [], str(parent), "")


# ─── derive_twin_spec: identity split (safety-critical) ───────────────────


def test_derive_sets_cards_author_to_twin():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert — the CANONICAL board-identity key, never the retired one.
    assert out["spec"]["apptainer"]["env"][CARDS_AGENT_ENV] == "parent-twin"


def test_derive_never_writes_the_retired_author_key():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert — a generated spec must not re-declare the retired name.
    assert RETIRED_AGENT_ENV not in out["spec"]["apptainer"]["env"]


def test_derive_drops_an_inherited_retired_author_key():
    # Arrange — a parent still launched from an old-name spec. Left in place
    # the key would carry the PARENT's name into the twin.
    doc = _parent_doc()
    doc["spec"]["env"][RETIRED_AGENT_ENV] = "parent"
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert RETIRED_AGENT_ENV not in out["spec"]["apptainer"]["env"]


def test_derive_sets_twin_parent_env_to_parent():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert out["spec"]["apptainer"]["env"][TWIN_PARENT_ENV] == "parent"


def test_derive_drops_inherited_sac_name_env():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert "SAC_NAME" not in out["spec"]["apptainer"]["env"]


def test_derive_inherits_other_env_verbatim():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert out["spec"]["apptainer"]["env"]["FOO"] == "bar"


# ─── derive_twin_spec: session / lifetime / port / channels ───────────────


def test_derive_sets_session_continue():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert out["spec"]["claude"]["session"] == "continue"


def test_derive_clears_resume_id_for_host_resolution():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert out["spec"]["claude"]["resume_id"] == ""


def test_derive_ephemeral_sets_restart_never():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert out["spec"]["restart"]["policy"] == "never"


def test_derive_persist_sets_restart_always():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=True
    )
    # Assert
    assert out["spec"]["restart"]["policy"] == "always"


def test_derive_sets_fresh_a2a_port_auto():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert out["spec"]["a2a"]["port"] == "auto"


def test_derive_drops_telegrammer_channel():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert out["spec"]["claude"]["channels"] == ["server:sac"]


def test_derive_drops_telegrammer_from_neutral_channels():
    # Arrange
    doc = _parent_doc()
    doc["spec"]["comms"] = {
        "channels": ["server:sac", "server:claude-code-telegrammer"]
    }
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert out["spec"]["comms"]["channels"] == ["server:sac"]


# ─── derive_twin_spec: inheritance / role / to_home / boot-kick ───────────


def test_derive_preserves_container_workdir_for_host_bind_handoff():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert out["spec"]["workdir"] == "/home/agent/proj/x"


def test_derive_inherits_image_verbatim():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert out["spec"]["apptainer"]["image"] == "/x.sif"


def test_derive_sets_role_label_when_given():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="t", parent_name="parent", persist=False, role="writer"
    )
    # Assert
    assert out["metadata"]["labels"]["role"] == "writer"


def test_derive_sets_to_home_when_given():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="t", parent_name="parent", persist=False, to_home="/abs/th"
    )
    # Assert
    assert out["spec"]["to_home"] == "/abs/th"


def test_derive_startup_prompt_carries_ownership_rule():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(
        doc, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert "assignee=parent" in out["spec"]["startup_prompts"][0]


def test_derive_does_not_mutate_parent_doc():
    # Arrange
    doc = _parent_doc()
    # Act
    derive_twin_spec(doc, twin_name="parent-twin", parent_name="parent", persist=False)
    # Assert
    assert doc["spec"]["env"][CARDS_AGENT_ENV] == "parent"


# ─── build_twin_boot_kick ─────────────────────────────────────────────────


def test_boot_kick_states_owner_stays_parent():
    # Arrange
    parent = "neurovista"
    # Act
    kick = build_twin_boot_kick("neurovista-twin", parent, None)
    # Assert
    assert "assignee=neurovista" in kick


def test_boot_kick_includes_task_when_given():
    # Arrange
    task = "audit the failing figures"
    # Act
    kick = build_twin_boot_kick("t", "p", task)
    # Assert
    assert task in kick


# ─── seed_twin_from_parent (real on-disk parent + state dirs) ─────────────


class _RuntimeStub:
    """Honest runtime collaborator — only the ``_state_dir`` resolver."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _state_dir(self, config: AgentConfig) -> Path:
        return self._root / config.name


def _write_parent_spec(agents_dir: Path, name: str) -> None:
    spec_dir = agents_dir / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "spec.yaml").write_text(
        explicitize_yaml(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n"
            "  runtime: apptainer\n"
            # ${HOSTNAME} is the validator-documented portable-fixture form:
            # 'host: local' is BANNED (operator directive 2026-07-10) and a
            # hardcoded hostname would break on any other machine (incl. CI).
            "  host: ${HOSTNAME}\n"
            "  workdir: /home/agent/proj/x\n"
            "  apptainer:\n"
            "    image: /x.sif\n"
            "    binds: []\n"
            "  claude:\n"
            "    model: haiku\n"
            "  health:\n"
            "    enabled: true\n"
            "    interval: 30\n"
            "    method: sdk-alive\n"
            "  restart:\n"
            "    policy: never\n"
            "    max_retries: 0\n"
        ),
        encoding="utf-8",
    )


def _seed_parent_session(state_root: Path, name: str, uuid: str) -> Path:
    """Write the parent's session_id marker + a transcript; return the jsonl."""
    write_session_id(state_root / name, uuid)
    proj = state_root / name / "home" / ".claude" / "projects" / "-home-agent-proj-x"
    proj.mkdir(parents=True, exist_ok=True)
    jsonl = proj / f"{uuid}.jsonl"
    jsonl.write_text('{"type":"user","message":{"content":"hi"}}\n', encoding="utf-8")
    return jsonl


def _twin_cfg(parent: str, twin: str) -> AgentConfig:
    return AgentConfig(
        name=twin,
        runtime="apptainer",
        claude=ClaudeSpec(model="haiku", session="resume"),
        env={TWIN_PARENT_ENV: parent},
    )


@pytest.fixture()
def _set_yaml_dirs():
    """Yield a setter for ``SCITEX_AGENT_CONTAINER_YAML_DIRS`` (restored on teardown).

    Real env manipulation (no ``monkeypatch``, forbidden ecosystem-wide):
    saves the prior value, hands the test a setter, and restores on teardown
    so the real config resolver finds the tmp parent spec during the test.
    """
    key = "SCITEX_AGENT_CONTAINER_YAML_DIRS"
    prev = os.environ.get(key)

    def _set(path) -> None:
        os.environ[key] = str(path)

    yield _set
    if prev is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = prev


@pytest.fixture()
def _twin_env(tmp_path, _set_yaml_dirs):
    """Real parent spec on disk + a parent live session; returns the pieces."""
    agents_dir = tmp_path / "agents"
    state_root = tmp_path / "state"
    _write_parent_spec(agents_dir, "twinp")
    jsonl = _seed_parent_session(state_root, "twinp", _UUID)
    _set_yaml_dirs(agents_dir)
    return state_root, jsonl


def test_seed_noop_for_non_twin(tmp_path):
    # Arrange — a config with no SAC_TWIN_PARENT is not a twin.
    cfg = AgentConfig(name="plain", runtime="apptainer")
    # Act
    seeded = seed_twin_from_parent(cfg, _RuntimeStub(tmp_path))
    # Assert
    assert seeded is False


def test_seed_defers_hermes_fork_to_native_owner_handoff(tmp_path: Path) -> None:
    # Arrange
    cfg = AgentConfig(
        name="hermes-fork",
        runtime="tui",
        harness="hermes",
        env={TWIN_PARENT_ENV: "parent"},
    )
    # Act
    seeded = seed_twin_from_parent(cfg, _RuntimeStub(tmp_path))
    # Assert — no Claude marker/JSONL is fabricated for Hermes.
    assert (seeded, list(tmp_path.rglob("*"))) == (False, [])


def test_seed_returns_true_for_twin(_twin_env):
    # Arrange
    state_root, _ = _twin_env
    # Act
    seeded = seed_twin_from_parent(
        _twin_cfg("twinp", "twinp-twin"), _RuntimeStub(state_root)
    )
    # Assert
    assert seeded is True


def test_seed_copies_transcript_into_twin_home(_twin_env):
    # Arrange
    state_root, _ = _twin_env
    # Act
    seed_twin_from_parent(_twin_cfg("twinp", "twinp-twin"), _RuntimeStub(state_root))
    # Assert
    assert (
        state_root
        / "twinp-twin"
        / "home"
        / ".claude"
        / "projects"
        / "-home-agent-proj-x"
        / f"{_UUID}.jsonl"
    ).is_file()


def test_seed_marks_twin_session_id_to_parent_uuid(_twin_env):
    # Arrange
    state_root, _ = _twin_env
    # Act
    seed_twin_from_parent(_twin_cfg("twinp", "twinp-twin"), _RuntimeStub(state_root))
    # Assert
    assert read_session_id(state_root / "twinp-twin") == _UUID


def test_seed_noop_when_twin_already_booted(_twin_env):
    # Arrange — the twin has its OWN (diverged) session marker already.
    state_root, _ = _twin_env
    own = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    write_session_id(state_root / "twinp-twin", own)
    # Act
    seeded = seed_twin_from_parent(
        _twin_cfg("twinp", "twinp-twin"), _RuntimeStub(state_root)
    )
    # Assert
    assert seeded is False


def test_seed_preserves_diverged_twin_marker_on_restart(_twin_env):
    # Arrange — a restart must never re-fork the twin back to the parent uuid.
    state_root, _ = _twin_env
    own = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    write_session_id(state_root / "twinp-twin", own)
    # Act
    seed_twin_from_parent(_twin_cfg("twinp", "twinp-twin"), _RuntimeStub(state_root))
    # Assert
    assert read_session_id(state_root / "twinp-twin") == own


def test_seed_fails_loud_when_parent_has_no_session(tmp_path, _set_yaml_dirs):
    # Arrange — parent spec exists but no session_id marker was written.
    agents_dir = tmp_path / "agents"
    _write_parent_spec(agents_dir, "twinp")
    _set_yaml_dirs(agents_dir)
    cfg = _twin_cfg("twinp", "twinp-twin")
    runtime = _RuntimeStub(tmp_path / "state")

    def _run() -> None:
        seed_twin_from_parent(cfg, runtime)

    # Act
    raised = pytest.raises(TwinSeedError)
    # Assert
    with raised:
        _run()


def test_seed_fails_loud_when_transcript_missing(tmp_path, _set_yaml_dirs):
    # Arrange — parent has a session id but no transcript file on disk.
    agents_dir = tmp_path / "agents"
    state_root = tmp_path / "state"
    _write_parent_spec(agents_dir, "twinp")
    write_session_id(state_root / "twinp", _UUID)
    _set_yaml_dirs(agents_dir)
    cfg = _twin_cfg("twinp", "twinp-twin")
    runtime = _RuntimeStub(state_root)

    def _run() -> None:
        seed_twin_from_parent(cfg, runtime)

    # Act
    raised = pytest.raises(TwinSeedError)
    # Assert
    with raised:
        _run()
