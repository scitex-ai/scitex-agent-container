"""Tests for ``_remote_bake_core`` — verdict parsing, atomic swap, prune,
and the wheel-shipped remote script's contract pins.

The pull/verify/publish chain (including its WATCH-IT-FAIL scenarios) is
covered in ``test__remote_bake_pull.py``.

No mocks: the symlink-swap / prune legs run against a real ``tmp_path``
store shaped exactly like the live ``~/.scitex/agent-container/containers/``
layout, and the script pins read the actual file the CLI pipes over ssh.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container.cli_pkg import _remote_bake_core as core
from scitex_agent_container.cli_pkg._remote_bake_core import (
    BakeVerdict,
    RemoteBakeOutcome,
    parse_bake_result,
    prune_local,
    swap_live_symlinks,
)

_BAKED_LINE = (
    "lease: resolved\n"
    'SAC_BAKE_RESULT={"verdict":"BAKED","layer":"base",'
    '"ts":"2026-0717-182108","head":"65004f4b","sif":"/store/sac-base/'
    'sac-base-2026-0717-182108.sif","sha256":"abc123","pruned":"",'
    '"duration_sec":900}\n'
)


def _make_store(tmp_path: Path, layer: str, names: list[str], live: str) -> Path:
    """Build a real containers-store shaped like the production one."""
    containers = tmp_path / "containers"
    layer_dir = containers / f"sac-{layer}"
    layer_dir.mkdir(parents=True)
    for name in names:
        (layer_dir / name).write_bytes(b"sif:" + name.encode())
    (layer_dir / f"sac-{layer}.sif").symlink_to(live)
    (containers / f"sac-{layer}.sif").symlink_to(f"sac-{layer}/{live}")
    return containers


# ---------------------------------------------------------------------------
# parse_bake_result — three states, never two
# ---------------------------------------------------------------------------


def test_parse_baked_verdict_is_baked() -> None:
    # Arrange — a real BAKED line as the remote script prints it.
    out = _BAKED_LINE
    # Act
    outcome = parse_bake_result(out, layer="base")
    # Assert
    assert outcome.verdict is BakeVerdict.BAKED


def test_parse_baked_verdict_carries_the_artifact_path() -> None:
    # Arrange
    out = _BAKED_LINE
    # Act
    outcome = parse_bake_result(out, layer="base")
    # Assert
    assert outcome.sif.endswith("sac-base-2026-0717-182108.sif")


def test_parse_baked_verdict_carries_the_checksum() -> None:
    # Arrange
    out = _BAKED_LINE
    # Act
    outcome = parse_bake_result(out, layer="base")
    # Assert
    assert outcome.sha256 == "abc123"


def test_parse_no_result_line_is_no_result_never_ok() -> None:
    # Arrange — a remote that died mid-flight prints no verdict line at
    # all. That is a DISTINCT state: never a soft failure of a known
    # step, and never a success.
    out = "ssh: connection closed\n"
    # Act
    outcome = parse_bake_result(out, layer="scitex")
    # Assert
    assert outcome.verdict is BakeVerdict.NO_RESULT


def test_parse_no_result_names_the_missing_line() -> None:
    # Arrange
    out = "ssh: connection closed\n"
    # Act
    outcome = parse_bake_result(out, layer="scitex")
    # Assert
    assert "no SAC_BAKE_RESULT" in outcome.detail


def test_parse_failed_verdict_is_failed() -> None:
    # Arrange
    out = (
        'SAC_BAKE_RESULT={"verdict":"FAILED","layer":"base",'
        '"step":"quota","reason":"quota-low"}\n'
    )
    # Act
    outcome = parse_bake_result(out, layer="base")
    # Assert
    assert outcome.verdict is BakeVerdict.FAILED


def test_parse_failed_verdict_names_the_reason() -> None:
    # Arrange
    out = (
        'SAC_BAKE_RESULT={"verdict":"FAILED","layer":"base",'
        '"step":"quota","reason":"quota-low"}\n'
    )
    # Act
    outcome = parse_bake_result(out, layer="base")
    # Assert
    assert outcome.detail == "quota-low"


def test_parse_garbage_json_is_no_result() -> None:
    # Arrange — a truncated/corrupt verdict line must not crash NOR pass.
    out = 'SAC_BAKE_RESULT={"verdict":"BAK'
    # Act
    outcome = parse_bake_result(out, layer="base")
    # Assert
    assert outcome.verdict is BakeVerdict.NO_RESULT


def test_green_verdict_without_artifact_identity_is_rejected() -> None:
    # Arrange — a BAKED with no sif/sha256 is a malformed green; the
    # validator must refuse it at construction (fail HERE, not in a
    # publish step someone acts on).
    kwargs = dict(verdict=BakeVerdict.BAKED, layer="base")

    # Act
    def _build():
        return RemoteBakeOutcome(**kwargs)

    # Assert
    with pytest.raises(ValueError, match="must carry sif\\+sha256"):
        _build()


# ---------------------------------------------------------------------------
# swap_live_symlinks / prune_local — the atomic-store legs
# ---------------------------------------------------------------------------


def test_swap_flips_the_top_level_symlink(tmp_path: Path) -> None:
    # Arrange — a store with an old live generation and a new artifact.
    old, new = "sac-base-2026-0715-000000.sif", "sac-base-2026-0717-000000.sif"
    containers = _make_store(tmp_path, "base", [old, new], live=old)
    # Act
    swap_live_symlinks(containers, "base", new)
    # Assert
    assert (containers / "sac-base.sif").resolve().name == new


def test_swap_flips_the_inner_boot_symlink(tmp_path: Path) -> None:
    # Arrange
    old, new = "sac-base-2026-0715-000000.sif", "sac-base-2026-0717-000000.sif"
    containers = _make_store(tmp_path, "base", [old, new], live=old)
    # Act
    swap_live_symlinks(containers, "base", new)
    # Assert
    assert (containers / "sac-base" / "sac-base.sif").resolve().name == new


def test_swap_refuses_non_canonical_names(tmp_path: Path) -> None:
    # Arrange — a name outside the timestamped shape (e.g. a partial or a
    # hand-copied file) must never become the live target.
    live = "sac-base-2026-0715-000000.sif"
    containers = _make_store(tmp_path, "base", [live], live=live)

    # Act
    def _swap():
        swap_live_symlinks(containers, "base", ".incoming-sac-base-x.sif")

    # Assert
    with pytest.raises(ValueError, match="non-canonical"):
        _swap()


def test_swap_refuses_a_missing_artifact(tmp_path: Path) -> None:
    # Arrange — pointing the live symlinks at a file that is not there
    # would brick every agent start; refuse before any link moves.
    live = "sac-base-2026-0715-000000.sif"
    containers = _make_store(tmp_path, "base", [live], live=live)

    # Act
    def _swap():
        swap_live_symlinks(containers, "base", "sac-base-2026-0718-000000.sif")

    # Assert
    with pytest.raises(FileNotFoundError):
        _swap()


def test_prune_keeps_the_live_target_regardless_of_age(tmp_path: Path) -> None:
    # Arrange — five generations; the LIVE one is deliberately the OLDEST
    # (a rollback scenario): prune must keep it regardless of age.
    names = [f"sac-base-2026-071{i}-000000.sif" for i in range(5)]
    containers = _make_store(tmp_path, "base", names, live=names[0])
    # Act
    prune_local(containers, "base", retain=2)
    # Assert
    assert (containers / "sac-base" / names[0]).exists()


def test_prune_keeps_the_retain_newest(tmp_path: Path) -> None:
    # Arrange
    names = [f"sac-base-2026-071{i}-000000.sif" for i in range(5)]
    containers = _make_store(tmp_path, "base", names, live=names[0])
    # Act
    prune_local(containers, "base", retain=2)
    # Assert
    assert (containers / "sac-base" / names[4]).exists() and (
        containers / "sac-base" / names[3]
    ).exists()


def test_prune_names_every_pruned_artifact(tmp_path: Path) -> None:
    # Arrange — no silent deletion: the caller echoes exactly this list.
    names = [f"sac-base-2026-071{i}-000000.sif" for i in range(5)]
    containers = _make_store(tmp_path, "base", names, live=names[0])
    # Act
    pruned = prune_local(containers, "base", retain=2)
    # Assert
    assert sorted(pruned) == [names[1], names[2]]


def test_prune_never_touches_dot_prefixed_partials(tmp_path: Path) -> None:
    # Arrange — an in-flight .incoming-* transfer must survive a prune
    # pass (it is resumable state, and it never matches the canonical
    # name shape).
    live = "sac-base-2026-0715-000000.sif"
    containers = _make_store(tmp_path, "base", [live], live=live)
    partial = containers / "sac-base" / ".incoming-sac-base-2026-0716-000000.sif"
    partial.write_bytes(b"partial")
    # Act
    prune_local(containers, "base", retain=0)
    # Assert
    assert partial.exists()


# ---------------------------------------------------------------------------
# wheel-shipped remote script — contract pins
# ---------------------------------------------------------------------------


def test_bake_script_ships_in_the_wheel_assets_dir() -> None:
    # Arrange — the CLI pipes this exact file over ssh; a missing file
    # would make the timer a nightly no-op.
    script = core.BAKE_SCRIPT
    # Act
    exists = script.is_file()
    # Assert
    assert exists


def test_symbol_probe_ships_in_the_wheel_assets_dir() -> None:
    # Arrange — the master-side verify runs this file inside the pulled
    # SIF; without it the gate cannot run and nothing may publish.
    probe = core.SYMBOL_PROBE
    # Act
    exists = probe.is_file()
    # Assert
    assert exists


def test_bake_script_resolves_the_lease_by_name() -> None:
    # Arrange — fleet doctrine: the lease job id changes at every ~7d
    # resubmit boundary, so the script must resolve by NAME.
    text = core.BAKE_SCRIPT.read_text()
    # Act
    resolved_by_name = '--name="$LEASE_NAME"' in text and "--states=RUNNING" in text
    # Assert
    assert resolved_by_name


def test_bake_script_runs_steps_with_overlap() -> None:
    # Arrange — work must land as srun STEPS inside the standing lease.
    text = core.BAKE_SCRIPT.read_text()
    # Act
    uses_overlap = "--overlap" in text
    # Assert
    assert uses_overlap


def test_bake_script_never_calls_sbatch() -> None:
    # Arrange — a fresh sbatch pays the queue tax (a real job sat PENDING
    # for 32h); the script must never submit one. Comment lines are
    # allowed to NAME sbatch (they document this very rule), so only
    # non-comment lines are scanned.
    code_lines = [
        ln
        for ln in core.BAKE_SCRIPT.read_text().splitlines()
        if not ln.lstrip().startswith("#")
    ]
    # Act
    calls_sbatch = any("sbatch" in ln for ln in code_lines)
    # Assert
    assert not calls_sbatch


def test_bake_script_probe_matches_the_wheel_probe_verbatim() -> None:
    # Arrange — the remote gate is a heredoc copy of sif_symbol_probe.py
    # (the remote host has no wheel). If the two drift, the master could
    # accept an artifact the remote gated differently — LOCKSTEP is
    # enforced here rather than promised in a comment.
    text = core.BAKE_SCRIPT.read_text()
    marker_start = "cat > \"$PROBE\" <<'PYEOF'\n"
    marker_end = "\nPYEOF\n"
    start = text.index(marker_start) + len(marker_start)
    end = text.index(marker_end, start)
    # Act
    heredoc = text[start:end] + "\n"
    # Assert
    assert heredoc == core.SYMBOL_PROBE.read_text()


def test_bake_script_emits_the_three_state_verdict_contract() -> None:
    # Arrange — every terminal path must print SAC_BAKE_RESULT with one of
    # the three remote verdicts (the fourth, NO_RESULT, is the ABSENCE of
    # the line and belongs to the parser).
    text = core.BAKE_SCRIPT.read_text()
    # Act
    verdicts_present = all(
        f'"verdict":{v}' in text for v in ('"BAKED"', '"SKIPPED"', '"FAILED"')
    )
    # Assert
    assert verdicts_present
