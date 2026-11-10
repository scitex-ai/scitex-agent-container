"""CLI tests for ``sac host add / remove / set`` (peer CRUD).

PA-306: no ``unittest.mock``. Real ``CliRunner``, real ``tmp_path``
config.yaml surfaced via the ``SCITEX_AGENT_CONTAINER_CONFIG`` env
override. Each test follows AAA (TQ002), asserts a single fact
(TQ007), and uses a behaviour-shaped name (TQ003).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._host_crud import (
    host_add,
    host_remove,
    host_set,
)


@pytest.fixture
def cfg_path(tmp_path: Path, env_save_restore) -> Path:
    """Real config.yaml at tmp_path, surfaced via the env override."""
    p = tmp_path / "config.yaml"
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(p))
    return p


def _read_peers(path: Path) -> dict:
    return (yaml.safe_load(path.read_text()) or {}).get("peers") or {}


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


def test_host_add_writes_ssh_target_to_yaml(cfg_path: Path):
    # Arrange
    runner = CliRunner()
    # Act
    runner.invoke(host_add, ["gpu-box", "--ssh", "u@gpu.lan"])
    # Assert
    assert _read_peers(cfg_path)["gpu-box"]["ssh"] == "u@gpu.lan"


def test_host_add_exits_zero_on_success(cfg_path: Path):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(host_add, ["gpu-box", "--ssh", "u@gpu.lan"])
    # Assert
    assert result.exit_code == 0


def test_host_add_with_via_writes_list_under_via_key(cfg_path: Path):
    # Arrange
    cfg_path.write_text("peers:\n  mba: {ssh: m}\n  spartan: {ssh: s}\n")
    runner = CliRunner()
    # Act
    runner.invoke(host_add, ["bm198", "--ssh", "bm198", "--via", "mba,spartan"])
    # Assert
    assert _read_peers(cfg_path)["bm198"]["via"] == ["mba", "spartan"]


def test_host_add_rejects_duplicate_name_with_exit_2(cfg_path: Path):
    # Arrange
    cfg_path.write_text("peers:\n  gpu-box: {ssh: old}\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(host_add, ["gpu-box", "--ssh", "new"])
    # Assert
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_host_remove_deletes_existing_peer_from_yaml(cfg_path: Path):
    # Arrange
    cfg_path.write_text("peers:\n  gpu-box: {ssh: u@gpu}\n")
    runner = CliRunner()
    # Act
    runner.invoke(host_remove, ["gpu-box", "--yes"])
    # Assert
    assert "gpu-box" not in _read_peers(cfg_path)


def test_host_remove_rejects_missing_peer_with_exit_2(cfg_path: Path):
    # Arrange
    cfg_path.write_text("peers: {}\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(host_remove, ["nope", "--yes"])
    # Assert
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# set
# ---------------------------------------------------------------------------


def test_host_set_overwrites_ssh_target(cfg_path: Path):
    # Arrange
    cfg_path.write_text("peers:\n  gpu-box: {ssh: old@gpu}\n")
    runner = CliRunner()
    # Act
    runner.invoke(host_set, ["gpu-box", "--ssh", "new@gpu"])
    # Assert
    assert _read_peers(cfg_path)["gpu-box"]["ssh"] == "new@gpu"


def test_host_set_writes_via_chain(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        "peers:\n  mba: {ssh: m}\n  spartan: {ssh: s}\n  bm198: {ssh: bm198}\n"
    )
    runner = CliRunner()
    # Act
    runner.invoke(host_set, ["bm198", "--via", "mba,spartan"])
    # Assert
    assert _read_peers(cfg_path)["bm198"]["via"] == ["mba", "spartan"]


def test_host_set_rejects_missing_peer_with_exit_2(cfg_path: Path):
    # Arrange
    cfg_path.write_text("peers: {}\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(host_set, ["nope", "--ssh", "x"])
    # Assert
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# round-trip comment preservation
# ---------------------------------------------------------------------------


def test_host_add_preserves_top_of_file_comment(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        "# top comment\npeers:\n  foo:\n    ssh: bar  # inline comment\n"
    )
    runner = CliRunner()
    # Act
    runner.invoke(host_add, ["baz", "--ssh", "qux"])
    # Assert
    assert "# top comment" in cfg_path.read_text()


def test_host_add_preserves_inline_comment(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        "# top comment\npeers:\n  foo:\n    ssh: bar  # inline comment\n"
    )
    runner = CliRunner()
    # Act
    runner.invoke(host_add, ["baz", "--ssh", "qux"])
    # Assert
    assert "# inline comment" in cfg_path.read_text()


# ---------------------------------------------------------------------------
# validation rollback
# ---------------------------------------------------------------------------


def test_host_add_reverts_file_when_validation_fails(cfg_path: Path):
    # Arrange — adding a peer whose via: hop is unknown trips
    # Config.validate; pre-edit bytes must survive intact.
    original = "peers: {}\n"
    cfg_path.write_text(original)
    runner = CliRunner()
    # Act
    runner.invoke(host_add, ["new-peer", "--ssh", "x", "--via", "ghost"])
    # Assert
    assert cfg_path.read_text() == original


def test_host_add_exits_nonzero_when_validation_fails(cfg_path: Path):
    # Arrange
    cfg_path.write_text("peers: {}\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(host_add, ["new-peer", "--ssh", "x", "--via", "ghost"])
    # Assert
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# symlink-safety: fleet-shared config layouts symlink
# ~/.scitex/agent-container/config.yaml to a shared file. Writing the
# symlink path directly would replace the link with a regular file and
# silently break the shared relationship. See foundation-polish bug 3.
# ---------------------------------------------------------------------------


@pytest.fixture
def symlinked_cfg(tmp_path: Path, env_save_restore) -> tuple[Path, Path]:
    """Return ``(link_path, target_path)`` where ``link_path`` is a symlink to ``target_path``.

    The CLI is pointed at ``link_path`` via the env override; the target
    holds the actual YAML bytes (mimicking a fleet-shared layout).
    """
    target = tmp_path / "shared" / "config.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("peers: {}\n")
    link = tmp_path / "agent-container" / "config.yaml"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(link))
    return link, target


def test_host_add_preserves_symlink_after_write(symlinked_cfg):
    # Arrange
    link, _target = symlinked_cfg
    runner = CliRunner()
    # Act
    runner.invoke(host_add, ["gpu-box", "--ssh", "u@gpu.lan"])
    # Assert — symlink invariant must hold: the link is still a link.
    assert link.is_symlink(), (
        "host add must not replace the symlink with a regular file"
    )


def test_host_add_writes_through_symlink_to_target(symlinked_cfg):
    # Arrange
    _link, target = symlinked_cfg
    runner = CliRunner()
    # Act
    runner.invoke(host_add, ["gpu-box", "--ssh", "u@gpu.lan"])
    # Assert — content lands on the resolved target.
    assert "gpu-box" in target.read_text()


def test_host_remove_preserves_symlink_after_write(symlinked_cfg):
    # Arrange
    link, target = symlinked_cfg
    target.write_text("peers:\n  gpu-box: {ssh: u@gpu}\n")
    runner = CliRunner()
    # Act
    runner.invoke(host_remove, ["gpu-box", "--yes"])
    # Assert
    assert link.is_symlink()


def test_host_set_preserves_symlink_after_write(symlinked_cfg):
    # Arrange
    link, target = symlinked_cfg
    target.write_text("peers:\n  gpu-box: {ssh: old@gpu}\n")
    runner = CliRunner()
    # Act
    runner.invoke(host_set, ["gpu-box", "--ssh", "new@gpu"])
    # Assert
    assert link.is_symlink()


# ---------------------------------------------------------------------------
# generated-config guard (ADR-0021): on a client host, config.yaml is
# renderer output pushed from the master. CRUD-editing it in place would
# be drift the next push-config shouts about, so add/remove/set refuse
# and point at the master. The fixture content is REAL renderer output —
# the guard must recognise exactly what the renderer emits.
# ---------------------------------------------------------------------------


def _write_generated(cfg_path: Path) -> str:
    """Put a real renderer-generated client config at ``cfg_path``."""
    from scitex_agent_container._hostsync import render_peer_config
    from scitex_agent_container._state.host_config import load as _load

    text = render_peer_config(
        "clienty",
        _load(cfg_path.parent / "no-such-master.yaml"),
        master_name="master-x",
        master_sha="ab" * 32,
    )
    cfg_path.write_text(text)
    return text


def test_host_add_refuses_generated_config_with_exit_2(cfg_path: Path):
    # Arrange — the local config is a generated CLIENT file.
    _write_generated(cfg_path)
    runner = CliRunner()
    # Act
    result = runner.invoke(host_add, ["gpu-box", "--ssh", "u@gpu.lan"])
    # Assert
    assert result.exit_code == 2


def test_host_add_refusal_names_push_config(cfg_path: Path):
    # Arrange
    _write_generated(cfg_path)
    runner = CliRunner()
    # Act
    result = runner.invoke(host_add, ["gpu-box", "--ssh", "u@gpu.lan"])
    # Assert — the refusal must name the next command.
    assert "sac host push-config" in result.output


def test_host_add_leaves_generated_bytes_untouched(cfg_path: Path):
    # Arrange
    original = _write_generated(cfg_path)
    runner = CliRunner()
    # Act
    runner.invoke(host_add, ["gpu-box", "--ssh", "u@gpu.lan"])
    # Assert — a refused edit must not half-write anything.
    assert cfg_path.read_text() == original


def test_host_set_refuses_generated_config_with_exit_2(cfg_path: Path):
    # Arrange
    _write_generated(cfg_path)
    runner = CliRunner()
    # Act
    result = runner.invoke(host_set, ["master-x", "--ssh", "new@x"])
    # Assert
    assert result.exit_code == 2


def test_host_remove_refuses_generated_config_with_exit_2(cfg_path: Path):
    # Arrange
    _write_generated(cfg_path)
    runner = CliRunner()
    # Act
    result = runner.invoke(host_remove, ["master-x", "--yes"])
    # Assert
    assert result.exit_code == 2


def test_host_add_still_edits_a_hand_written_config(cfg_path: Path):
    # Arrange — a normal (hand-written) config must stay editable.
    cfg_path.write_text("# my config\npeers: {}\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(host_add, ["gpu-box", "--ssh", "u@gpu.lan"])
    # Assert
    assert result.exit_code == 0


@pytest.mark.parametrize("approval", [[], ["--yes"], ["-y"]])
def test_host_remove_preview_never_reads_config(cfg_path: Path, approval):
    # Arrange
    original = b"invalid UTF8 \xff"
    cfg_path.write_bytes(original)
    # Act
    result = CliRunner().invoke(
        host_remove, ["owned-peer", "--dry-run", "--json", *approval]
    )
    # Assert
    assert (result.exit_code, json.loads(result.stdout), cfg_path.read_bytes()) == (
        0,
        {
            "dry_run": True,
            "action": "remove",
            "peer": "owned-peer",
            "path": str(cfg_path),
        },
        original,
    )


def test_host_remove_requires_approval_before_config_access(cfg_path: Path):
    # Arrange
    original = b"invalid UTF8 \xff"
    cfg_path.write_bytes(original)
    # Act
    result = CliRunner().invoke(host_remove, ["owned-peer", "--json"])
    # Assert
    assert (
        result.exit_code,
        result.stdout,
        "requires --yes" in result.stderr,
        cfg_path.read_bytes(),
    ) == (2, "", True, original)


@pytest.mark.parametrize("approval", ["--yes", "-y"])
def test_host_remove_approval_preserves_comments_and_other_peers(
    cfg_path: Path, approval
):
    # Arrange
    cfg_path.write_text(
        "# owned config\npeers:\n  gone: {ssh: g}\n  kept: {ssh: k} # keep\n"
    )
    # Act
    result = CliRunner().invoke(host_remove, ["gone", approval, "--json"])
    # Assert
    assert (
        result.exit_code,
        _read_peers(cfg_path),
        "# keep" in cfg_path.read_text(),
    ) == (0, {"kept": {"ssh": "k"}}, True)


def test_host_remove_invalid_remaining_route_reverts_exact_bytes(cfg_path: Path):
    # Arrange
    original = (
        "# preserve me\npeers:\n  relay: {ssh: r}\n  target: {ssh: t, via: [relay]}\n"
    )
    cfg_path.write_text(original)
    # Act
    result = CliRunner().invoke(host_remove, ["relay", "--yes", "--json"])
    # Assert
    assert (result.exit_code, cfg_path.read_text()) == (1, original)
