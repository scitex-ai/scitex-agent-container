"""The birth certificate — compiled spec at launch, secrets by reference.

Operator requirement (2026-08-14): record the COMPILED final spec as
"this agent was born like this", keyed by incarnation id, in the DB —
and reference credentials by slot/source NAME, never by value.

Real AgentConfigs, a real on-disk SQLite via explicit ``db_path``, and a
REAL git repo for the sha tests (git invoked as a subprocess with -C,
signing disabled per-invocation). No mocks.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scitex_agent_container._lifecycle._birth_certificate import (
    SPEC_SHA_UNRESOLVABLE,
    compiled_spec_snapshot,
    spec_git_sha,
    write_birth_certificate,
)
from scitex_agent_container._state.state_db_incarnations import get_incarnation
from scitex_agent_container.config import AgentConfig


# ---------------------------------------------------------------------------
# compiled_spec_snapshot — redaction by key shape
# ---------------------------------------------------------------------------


def test_snapshot_redacts_secret_shaped_env_values() -> None:
    # Arrange: a spec-injected credential VALUE.
    cfg = AgentConfig(name="alpha", env={"GITHUB_TOKEN": "ghp_livesecret"})
    # Act
    snap = compiled_spec_snapshot(cfg)
    # Assert: the value never survives.
    assert snap["env"]["GITHUB_TOKEN"] == "<redacted:GITHUB_TOKEN>"


def test_snapshot_keeps_the_secret_slot_name(tmp_path: Path) -> None:
    # Arrange
    cfg = AgentConfig(name="alpha", env={"CCT_BOT_TOKEN": "123:abc"})
    # Act
    snap = compiled_spec_snapshot(cfg)
    # Assert: the KEY (the slot name) is exactly what the record keeps.
    assert "CCT_BOT_TOKEN" in snap["env"]


def test_snapshot_keeps_non_secret_env_values(tmp_path: Path) -> None:
    # Arrange
    cfg = AgentConfig(name="alpha", env={"SCITEX_LANG": "ja"})
    # Act
    snap = compiled_spec_snapshot(cfg)
    # Assert
    assert snap["env"]["SCITEX_LANG"] == "ja"


def test_snapshot_keeps_credentials_file_paths(tmp_path: Path) -> None:
    # Arrange: the credentials FILE PATH is the slot/source reference the
    # operator approved recording — it names where, not what.
    cfg = AgentConfig(name="alpha")
    cfg.claude.credentials_file = "/accounts/slug-a/.credentials.json"
    # Act
    snap = compiled_spec_snapshot(cfg)
    # Assert
    assert snap["claude"]["credentials_file"] == "/accounts/slug-a/.credentials.json"


def test_snapshot_is_json_serializable(tmp_path: Path) -> None:
    # Arrange: the full default config tree, nothing hand-pruned.
    cfg = AgentConfig(name="alpha")
    # Act
    text = json.dumps(compiled_spec_snapshot(cfg), default=str)
    # Assert
    assert json.loads(text)["name"] == "alpha"


# ---------------------------------------------------------------------------
# spec_git_sha — honest resolution
# ---------------------------------------------------------------------------


def test_sha_is_unresolvable_outside_a_git_repo(tmp_path: Path) -> None:
    # Arrange: a spec dir that is NOT a git repo (the common host shape).
    spec = tmp_path / "agents" / "alpha" / "spec.yaml"
    spec.parent.mkdir(parents=True)
    spec.write_text("apiVersion: v3\n", encoding="utf-8")
    # Act
    sha = spec_git_sha(str(spec))
    # Assert
    assert sha == SPEC_SHA_UNRESOLVABLE


def test_sha_is_unresolvable_for_no_path(tmp_path: Path) -> None:
    # Arrange
    path = None
    # Act
    sha = spec_git_sha(path)
    # Assert
    assert sha == SPEC_SHA_UNRESOLVABLE


def _git_repo_with_commit(root: Path) -> str:
    """Create a REAL git repo with one committed spec; return HEAD sha."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "spec.yaml").write_text("apiVersion: v3\n", encoding="utf-8")
    for argv in (
        ["git", "-C", str(root), "init", "-q"],
        ["git", "-C", str(root), "add", "spec.yaml"],
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.email=t@test",
            "-c",
            "user.name=t",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "spec",
        ],
    ):
        subprocess.run(argv, check=True, capture_output=True, text=True)
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return head.stdout.strip()


def test_sha_resolves_head_in_a_real_repo(tmp_path: Path) -> None:
    # Arrange
    repo = tmp_path / "specs"
    head = _git_repo_with_commit(repo)
    # Act
    sha = spec_git_sha(str(repo / "spec.yaml"))
    # Assert
    assert sha == head


# ---------------------------------------------------------------------------
# write_birth_certificate — the row, end to end
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


def test_certificate_row_lands_keyed_by_incarnation(db: Path) -> None:
    # Arrange
    cfg = AgentConfig(name="alpha")
    # Act
    ok = write_birth_certificate(cfg, "inc-b1", db_path=db)
    # Assert
    assert ok is True and get_incarnation("inc-b1", db_path=db) is not None


def test_certificate_names_the_agent_identity(db: Path) -> None:
    # Arrange
    cfg = AgentConfig(name="alpha")
    # Act
    write_birth_certificate(cfg, "inc-b2", db_path=db)
    # Assert
    assert get_incarnation("inc-b2", db_path=db)["agent_id"] == "alpha"


def test_certificate_records_unresolvable_sha_honestly(db: Path) -> None:
    # Arrange: no config_path at all — nothing to fake a sha from.
    cfg = AgentConfig(name="alpha")
    # Act
    write_birth_certificate(cfg, "inc-b3", db_path=db)
    # Assert
    assert get_incarnation("inc-b3", db_path=db)["spec_git_sha"] == (
        SPEC_SHA_UNRESOLVABLE
    )


def test_certificate_records_the_spec_repo_head(db: Path, tmp_path: Path) -> None:
    # Arrange: a spec tracked in a real git repo.
    repo = tmp_path / "specs"
    head = _git_repo_with_commit(repo)
    cfg = AgentConfig(name="alpha", config_path=str(repo / "spec.yaml"))
    # Act
    write_birth_certificate(cfg, "inc-b4", db_path=db)
    # Assert
    assert get_incarnation("inc-b4", db_path=db)["spec_git_sha"] == head


def test_certificate_compiled_spec_is_redacted_json(db: Path) -> None:
    # Arrange
    cfg = AgentConfig(name="alpha", env={"API_KEY": "sk-live"})
    # Act
    write_birth_certificate(cfg, "inc-b5", db_path=db)
    stored = json.loads(get_incarnation("inc-b5", db_path=db)["compiled_spec_json"])
    # Assert: no secret material in the record.
    assert stored["env"]["API_KEY"] == "<redacted:API_KEY>"


def test_certificate_compiled_spec_carries_residency(db: Path, tmp_path: Path) -> None:
    # Arrange: a compiled config declaring the v4 residency axis — the
    # birth certificate must record it (provenance for "why did this
    # incarnation end at oneshot-complete?").
    from scitex_agent_container.config._residency_types import ONE_SHOT

    cfg = AgentConfig(name="alpha", residency=ONE_SHOT)
    # Act
    write_birth_certificate(cfg, "inc-b7", db_path=db)
    stored = json.loads(get_incarnation("inc-b7", db_path=db)["compiled_spec_json"])
    # Assert
    assert stored["residency"] == ONE_SHOT


def test_certificate_failure_is_false_not_raise(db: Path) -> None:
    # Arrange: a config whose serialization cannot work (not a dataclass).
    broken = object()
    # Act
    ok = write_birth_certificate(broken, "inc-b6", db_path=db)
    # Assert: best-effort — the launch it documents must not die over it.
    assert ok is False
