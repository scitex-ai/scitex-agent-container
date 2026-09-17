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
    _ensure_twin_worktree,
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
            "harness": "hermes",
            "workdir": "/scratch/ywatanabe/parent-workdir",
            "apptainer": {
                "image": "sac-base",
                "overlay": "/scratch/sac/agents/parent/overlay",
                "env": {CARDS_AGENT_ENV: "parent", "KEEP": "yes"},
            },
            "available_harnesses": {
                "hermes": {"session": {"mode": "continue", "max_age_minutes": None}}
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
    assert name == "neurovista-twin"


def test_resolve_twin_name_bumps_when_default_taken():
    # Arrange
    existing = ["neurovista-twin"]
    # Act
    name = resolve_twin_name("neurovista", None, existing)
    # Assert
    assert name == "neurovista-twin-2"


def test_resolve_twin_name_honours_explicit_request():
    # Arrange
    requested = "neurovista-writer"
    # Act
    name = resolve_twin_name("neurovista", requested, ["neurovista-writer"])
    # Assert
    assert name == "neurovista-writer"


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


def test_current_v3_twin_has_unique_workdir_and_overlay() -> None:
    # Arrange
    parent = _current_v3_parent_doc()
    # Act
    twin = derive_twin_spec(
        parent, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert (
        twin["spec"]["workdir"] != parent["spec"]["workdir"],
        twin["spec"]["apptainer"]["overlay"]
        != parent["spec"]["apptainer"]["overlay"],
        "parent-twin" in twin["spec"]["workdir"],
        "parent-twin" in twin["spec"]["apptainer"]["overlay"],
    ) == (True, True, True, True)


def test_current_v3_hermes_twin_does_not_inject_legacy_claude_block() -> None:
    # Arrange
    parent = _current_v3_parent_doc()
    # Act
    twin = derive_twin_spec(
        parent, twin_name="parent-twin", parent_name="parent", persist=False
    )
    # Assert
    assert (
        twin["spec"].get("claude"),
        twin["spec"]["available_harnesses"]["hermes"]["session"]["mode"],
    ) == (parent["spec"].get("claude"), "continue")


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
    subprocess.run(
        ["git", "-C", str(parent), "commit", "-qm", "seed"], check=True
    )

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
    subprocess.run(
        ["git", "-C", str(parent), "commit", "-qm", "seed"], check=True
    )
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
    twin_workdir = Path(twin["spec"]["workdir"])
    assert (
        name,
        validate_raw(twin, "<twin>"),
        twin_workdir.exists(),
        subprocess.run(
            ["git", "-C", str(parent), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout,
    ) == ("parent-twin", [], False, "")


# ─── derive_twin_spec: identity split (safety-critical) ───────────────────


def test_derive_sets_cards_author_to_twin():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(doc, twin_name="parent-twin", parent_name="parent", persist=False)
    # Assert — the CANONICAL board-identity key, never the retired one.
    assert out["spec"]["apptainer"]["env"][CARDS_AGENT_ENV] == "parent-twin"


def test_derive_never_writes_the_retired_author_key():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(doc, twin_name="parent-twin", parent_name="parent", persist=False)
    # Assert — a generated spec must not re-declare the retired name.
    assert RETIRED_AGENT_ENV not in out["spec"]["apptainer"]["env"]


def test_derive_drops_an_inherited_retired_author_key():
    # Arrange — a parent still launched from an old-name spec. Left in place
    # the key would carry the PARENT's name into the twin.
    doc = _parent_doc()
    doc["spec"]["env"][RETIRED_AGENT_ENV] = "parent"
    # Act
    out = derive_twin_spec(doc, twin_name="parent-twin", parent_name="parent", persist=False)
    # Assert
    assert RETIRED_AGENT_ENV not in out["spec"]["apptainer"]["env"]


def test_derive_sets_twin_parent_env_to_parent():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(doc, twin_name="parent-twin", parent_name="parent", persist=False)
    # Assert
    assert out["spec"]["apptainer"]["env"][TWIN_PARENT_ENV] == "parent"


def test_derive_drops_inherited_sac_name_env():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(doc, twin_name="parent-twin", parent_name="parent", persist=False)
    # Assert
    assert "SAC_NAME" not in out["spec"]["apptainer"]["env"]


def test_derive_inherits_other_env_verbatim():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(doc, twin_name="parent-twin", parent_name="parent", persist=False)
    # Assert
    assert out["spec"]["apptainer"]["env"]["FOO"] == "bar"


# ─── derive_twin_spec: session / lifetime / port / channels ───────────────


def test_derive_sets_session_continue():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(doc, twin_name="parent-twin", parent_name="parent", persist=False)
    # Assert
    assert out["spec"]["claude"]["session"] == "continue"


def test_derive_clears_resume_id_for_host_resolution():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(doc, twin_name="parent-twin", parent_name="parent", persist=False)
    # Assert
    assert out["spec"]["claude"]["resume_id"] == ""


def test_derive_ephemeral_sets_restart_never():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(doc, twin_name="parent-twin", parent_name="parent", persist=False)
    # Assert
    assert out["spec"]["restart"]["policy"] == "never"


def test_derive_persist_sets_restart_always():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(doc, twin_name="parent-twin", parent_name="parent", persist=True)
    # Assert
    assert out["spec"]["restart"]["policy"] == "always"


def test_derive_sets_fresh_a2a_port_auto():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(doc, twin_name="parent-twin", parent_name="parent", persist=False)
    # Assert
    assert out["spec"]["a2a"]["port"] == "auto"


def test_derive_drops_telegrammer_channel():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(doc, twin_name="parent-twin", parent_name="parent", persist=False)
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


def test_derive_assigns_unique_twin_workdir():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(doc, twin_name="parent-twin", parent_name="parent", persist=False)
    # Assert
    assert out["spec"]["workdir"] == "/home/agent/proj/.sac-twins/parent-twin/workdir"


def test_derive_inherits_image_verbatim():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(doc, twin_name="parent-twin", parent_name="parent", persist=False)
    # Assert
    assert out["spec"]["apptainer"]["image"] == "/x.sif"


def test_derive_sets_role_label_when_given():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(doc, twin_name="t", parent_name="parent", persist=False, role="writer")
    # Assert
    assert out["metadata"]["labels"]["role"] == "writer"


def test_derive_sets_to_home_when_given():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(doc, twin_name="t", parent_name="parent", persist=False, to_home="/abs/th")
    # Assert
    assert out["spec"]["to_home"] == "/abs/th"


def test_derive_startup_prompt_carries_ownership_rule():
    # Arrange
    doc = _parent_doc()
    # Act
    out = derive_twin_spec(doc, twin_name="parent-twin", parent_name="parent", persist=False)
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
        explicitize_yaml("apiVersion: scitex-agent-container/v3\n"
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
        "    max_retries: 0\n"),
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


def test_seed_returns_true_for_twin(_twin_env):
    # Arrange
    state_root, _ = _twin_env
    # Act
    seeded = seed_twin_from_parent(_twin_cfg("twinp", "twinp-twin"), _RuntimeStub(state_root))
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
    seeded = seed_twin_from_parent(_twin_cfg("twinp", "twinp-twin"), _RuntimeStub(state_root))
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
