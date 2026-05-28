"""Tests for scitex_agent_container._state.spec_manifest (fleet sync).

Pure-function tests on real tmp_path agent dirs — no mocks (PA-306),
one assertion per test (TQ007), AAA layout (TQ002).

Manifest contract (locked here so it survives refactors):

    {
      "host": "<canonical name>",
      "agents_dir": "<abs path>",
      "agents": {
        "<agent>": {
          "present": True | False,
          "files": {
            "spec.yaml":              {"sha256": "...", "size": int, "mode": "0644"},
            "to_home/CLAUDE.md":      {"sha256": "...", "size": int, "mode": "0644"},
            "to_home/.claude/x.md":   {...},
          },
        }
      },
      "errors": []
    }

Diff contract (locked here):

    {
      "ok": bool,
      "fleet": ["host-a", "host-b", ...],
      "agents": {
        "<agent>": {
          "ok": bool,
          "conflicts": [
            {
              "file": "spec.yaml" | "to_home/..." | "<agent-dir>",
              "kind": "sha256_mismatch" | "missing_on_host"
                    | "agent_missing_on_host" | "mode_mismatch",
              "per_host": {host: {"present": ..., "sha256": ..., "size": ..., "mode": ...}},
              "diverged_hosts": [...]
            }
          ]
        }
      }
    }
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from scitex_agent_container._state.spec_manifest import (
    build_manifest,
    diff_manifests,
)


# ---------------------------------------------------------------------------
# Helpers (not tests).
# ---------------------------------------------------------------------------


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_agent(
    agents_dir: Path,
    name: str,
    *,
    spec: str = "kind: Agent\nmetadata:\n  name: x\n",
    to_home_files: dict[str, str] | None = None,
) -> None:
    """Lay down `<agents_dir>/<name>/{spec.yaml, to_home/...}`."""
    adir = agents_dir / name
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "spec.yaml").write_text(spec)
    if to_home_files:
        for rel, content in to_home_files.items():
            f = adir / "to_home" / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)


# ---------------------------------------------------------------------------
# build_manifest — single host.
# ---------------------------------------------------------------------------


def test_build_manifest_records_spec_yaml_sha256(tmp_path: Path) -> None:
    # Arrange
    agents_dir = tmp_path / "agents"
    _make_agent(agents_dir, "alpha", spec="hello\n")
    # Act
    m = build_manifest(host="local", agents_dir=agents_dir)
    # Assert
    assert m["agents"]["alpha"]["files"]["spec.yaml"]["sha256"] == _sha("hello\n")


def test_build_manifest_records_to_home_recursive(tmp_path: Path) -> None:
    # Arrange
    agents_dir = tmp_path / "agents"
    _make_agent(
        agents_dir,
        "alpha",
        to_home_files={
            "CLAUDE.md": "claude\n",
            ".claude/skills/x/SKILL.md": "skill\n",
        },
    )
    # Act
    m = build_manifest(host="local", agents_dir=agents_dir)
    files = m["agents"]["alpha"]["files"]
    # Assert
    assert (
        "to_home/CLAUDE.md" in files
        and "to_home/.claude/skills/x/SKILL.md" in files
    )


def test_build_manifest_marks_missing_agent_when_requested(tmp_path: Path) -> None:
    # Arrange
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    # Act
    m = build_manifest(host="local", agents_dir=agents_dir, only=["ghost"])
    # Assert
    assert m["agents"]["ghost"]["present"] is False


def test_build_manifest_skips_agent_without_spec_yaml(tmp_path: Path) -> None:
    # Arrange — a dir with no spec.yaml is NOT an agent (loud guard).
    agents_dir = tmp_path / "agents"
    (agents_dir / "stray" / "to_home").mkdir(parents=True)
    (agents_dir / "stray" / "to_home" / "f.txt").write_text("x")
    # Act
    m = build_manifest(host="local", agents_dir=agents_dir)
    # Assert
    assert "stray" not in m["agents"]


def test_build_manifest_records_mode_string(tmp_path: Path) -> None:
    # Arrange
    agents_dir = tmp_path / "agents"
    _make_agent(agents_dir, "alpha")
    spec = agents_dir / "alpha" / "spec.yaml"
    spec.chmod(0o600)
    # Act
    m = build_manifest(host="local", agents_dir=agents_dir)
    # Assert
    assert m["agents"]["alpha"]["files"]["spec.yaml"]["mode"] == "0600"


def test_build_manifest_includes_host_field(tmp_path: Path) -> None:
    # Arrange
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    # Act
    m = build_manifest(host="spartan", agents_dir=agents_dir)
    # Assert
    assert m["host"] == "spartan"


# ---------------------------------------------------------------------------
# diff_manifests — agreement / disagreement matrix.
# ---------------------------------------------------------------------------


def test_diff_returns_ok_true_for_identical_manifests(tmp_path: Path) -> None:
    # Arrange
    a = tmp_path / "a"
    b = tmp_path / "b"
    _make_agent(a, "alpha", spec="same\n", to_home_files={"CLAUDE.md": "x\n"})
    _make_agent(b, "alpha", spec="same\n", to_home_files={"CLAUDE.md": "x\n"})
    m_a = build_manifest(host="host-a", agents_dir=a)
    m_b = build_manifest(host="host-b", agents_dir=b)
    # Act
    d = diff_manifests({"host-a": m_a, "host-b": m_b})
    # Assert
    assert d["ok"] is True


def test_diff_flags_sha256_mismatch_on_spec_yaml(tmp_path: Path) -> None:
    # Arrange
    a = tmp_path / "a"
    b = tmp_path / "b"
    _make_agent(a, "alpha", spec="v1\n")
    _make_agent(b, "alpha", spec="v2\n")
    m_a = build_manifest(host="host-a", agents_dir=a)
    m_b = build_manifest(host="host-b", agents_dir=b)
    # Act
    d = diff_manifests({"host-a": m_a, "host-b": m_b})
    kinds = {c["kind"] for c in d["agents"]["alpha"]["conflicts"]}
    # Assert
    assert "sha256_mismatch" in kinds


def test_diff_flags_missing_to_home_file_on_one_host(tmp_path: Path) -> None:
    # Arrange
    a = tmp_path / "a"
    b = tmp_path / "b"
    _make_agent(
        a,
        "alpha",
        to_home_files={"CLAUDE.md": "x\n"},
    )
    _make_agent(b, "alpha")  # no to_home
    m_a = build_manifest(host="host-a", agents_dir=a)
    m_b = build_manifest(host="host-b", agents_dir=b)
    # Act
    d = diff_manifests({"host-a": m_a, "host-b": m_b})
    files_flagged = {
        c["file"] for c in d["agents"]["alpha"]["conflicts"]
    }
    # Assert
    assert "to_home/CLAUDE.md" in files_flagged


def test_diff_flags_agent_missing_on_host(tmp_path: Path) -> None:
    # Arrange
    a = tmp_path / "a"
    b = tmp_path / "b"
    _make_agent(a, "alpha")
    b.mkdir()  # bare b -- no alpha here
    m_a = build_manifest(host="host-a", agents_dir=a)
    m_b = build_manifest(host="host-b", agents_dir=b)
    # Act
    d = diff_manifests({"host-a": m_a, "host-b": m_b})
    kinds = {c["kind"] for c in d["agents"]["alpha"]["conflicts"]}
    # Assert
    assert "agent_missing_on_host" in kinds


def test_diff_flags_mode_mismatch_when_content_equal(tmp_path: Path) -> None:
    # Arrange
    a = tmp_path / "a"
    b = tmp_path / "b"
    _make_agent(a, "alpha", spec="same\n")
    _make_agent(b, "alpha", spec="same\n")
    (b / "alpha" / "spec.yaml").chmod(0o600)
    m_a = build_manifest(host="host-a", agents_dir=a)
    m_b = build_manifest(host="host-b", agents_dir=b)
    # Act
    d = diff_manifests({"host-a": m_a, "host-b": m_b})
    kinds = {c["kind"] for c in d["agents"]["alpha"]["conflicts"]}
    # Assert
    assert "mode_mismatch" in kinds


def test_diff_populates_diverged_hosts_minority_set(tmp_path: Path) -> None:
    # Arrange — two hosts agree on spec="v1", one host has spec="v2"
    a = tmp_path / "a"
    b = tmp_path / "b"
    c = tmp_path / "c"
    _make_agent(a, "alpha", spec="v1\n")
    _make_agent(b, "alpha", spec="v1\n")
    _make_agent(c, "alpha", spec="v2\n")
    m_a = build_manifest(host="host-a", agents_dir=a)
    m_b = build_manifest(host="host-b", agents_dir=b)
    m_c = build_manifest(host="host-c", agents_dir=c)
    # Act
    d = diff_manifests({"host-a": m_a, "host-b": m_b, "host-c": m_c})
    spec_conflict = next(
        c for c in d["agents"]["alpha"]["conflicts"] if c["file"] == "spec.yaml"
    )
    # Assert
    assert spec_conflict["diverged_hosts"] == ["host-c"]


def test_diff_per_host_payload_present_field_true_when_file_exists(
    tmp_path: Path,
) -> None:
    # Arrange
    a = tmp_path / "a"
    b = tmp_path / "b"
    _make_agent(a, "alpha", to_home_files={"CLAUDE.md": "x\n"})
    _make_agent(b, "alpha")
    m_a = build_manifest(host="host-a", agents_dir=a)
    m_b = build_manifest(host="host-b", agents_dir=b)
    # Act
    d = diff_manifests({"host-a": m_a, "host-b": m_b})
    conflict = next(
        c for c in d["agents"]["alpha"]["conflicts"]
        if c["file"] == "to_home/CLAUDE.md"
    )
    # Assert
    assert conflict["per_host"]["host-a"]["present"] is True


def test_diff_lists_fleet_hosts_in_input_order(tmp_path: Path) -> None:
    # Arrange
    a = tmp_path / "a"
    b = tmp_path / "b"
    _make_agent(a, "alpha")
    _make_agent(b, "alpha")
    m_a = build_manifest(host="host-a", agents_dir=a)
    m_b = build_manifest(host="host-b", agents_dir=b)
    # Act
    d = diff_manifests({"host-b": m_b, "host-a": m_a})
    # Assert
    assert d["fleet"] == ["host-b", "host-a"]


def test_diff_single_host_input_is_always_ok(tmp_path: Path) -> None:
    # Arrange — a one-host "fleet" cannot disagree with itself.
    a = tmp_path / "a"
    _make_agent(a, "alpha", spec="x\n")
    m_a = build_manifest(host="host-a", agents_dir=a)
    # Act
    d = diff_manifests({"host-a": m_a})
    # Assert
    assert d["ok"] is True


# EOF
