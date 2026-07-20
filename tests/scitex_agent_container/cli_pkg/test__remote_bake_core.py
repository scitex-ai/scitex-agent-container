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


# ---------------------------------------------------------------------------
# The stdin-guard PREFLIGHT (measured 2026-07-19)
#
# PR #771 added `--input=none` to every srun in the bake script, and the very
# next bake failed identically: build done, `.partial` left, no
# SAC_BAKE_RESULT. The script the run PIPED came off the INSTALLED wheel,
# whose bytes still predated #771 — the wheel cache is keyed on
# (name, version) and the version had not moved, so a reinstall served the
# stale build back under the same number. A merged fix is not a deployed fix,
# and the version string cannot tell the two apart. Only the bytes can, so
# the caller now reads them before spending an hour of lease.
# ---------------------------------------------------------------------------
_GUARDED_SRUN = (
    '"$SRUN" --input=none --jobid="$JID" --overlap --ntasks=1 \\\n'
    '    --job-name="sac-sif-bake-$LAYER" \\\n'
    '    bash -c "build" < /dev/null\n'
)

_UNGUARDED_SRUN = (
    '"$SRUN" --jobid="$JID" --overlap --ntasks=1 \\\n'
    '    --job-name="sac-sif-bake-$LAYER" \\\n'
    '    bash -c "build" < /dev/null\n'
)


def test_an_srun_without_input_none_is_reported_as_an_offender() -> None:
    # Arrange
    script = f"set -uo pipefail\n{_UNGUARDED_SRUN}"
    # Act
    offenders = core.unguarded_srun_invocations(script)
    # Assert
    assert len(offenders) == 1


def test_an_unguarded_srun_is_reported_with_its_line_number() -> None:
    # Arrange — naming the LINE is the point: "something is wrong" is what
    # the old failure said, and it cost six silent runs.
    script = f"set -uo pipefail\n{_UNGUARDED_SRUN}"
    # Act
    offenders = core.unguarded_srun_invocations(script)
    # Assert
    assert offenders[0][0] == 2


def test_a_guarded_srun_is_not_an_offender() -> None:
    # Arrange
    script = f"set -uo pipefail\n{_GUARDED_SRUN}"
    # Act
    offenders = core.unguarded_srun_invocations(script)
    # Assert
    assert offenders == []


def test_continuation_lines_of_a_guarded_srun_are_not_separate_offenders() -> None:
    # Arrange — the guard sits on the FIRST physical line of a multi-line
    # invocation, so a per-physical-line check would flag the continuations
    # and cry wolf on a correct script.
    script = f"set -uo pipefail\n{_GUARDED_SRUN}{_GUARDED_SRUN}"
    # Act
    offenders = core.unguarded_srun_invocations(script)
    # Assert
    assert offenders == []


def test_a_commented_out_srun_is_not_an_offender() -> None:
    # Arrange — the script's own STDIN RULE header quotes srun invocations
    # in prose; documentation must not read as a defect.
    script = '# "$SRUN" --jobid="$JID" would eat this file\n'
    # Act
    offenders = core.unguarded_srun_invocations(script)
    # Assert
    assert offenders == []


def test_stale_bake_script_error_names_the_offending_file() -> None:
    # Arrange
    script = Path("/opt/venv/lib/python3.11/site-packages/x/spartan-sif-bake.sh")
    # Act
    message = core.stale_bake_script_error(
        script=script, offenders=[(240, '"$SRUN" --jobid=1')], version="0.22.0"
    )
    # Assert
    assert str(script) in message


def test_stale_bake_script_error_names_the_installed_version() -> None:
    # Arrange — the version is the value that LIED, so it must be quoted
    # back at the reader rather than trusted.
    # Act
    message = core.stale_bake_script_error(
        script=Path("/x/bake.sh"), offenders=[(240, "srun")], version="0.22.0"
    )
    # Assert
    assert "0.22.0" in message


def test_stale_bake_script_error_states_the_remedy() -> None:
    # Arrange — "say what to do about it": a cache-busting reinstall is the
    # only thing that moves the bytes when the version has not moved.
    # Act
    message = core.stale_bake_script_error(
        script=Path("/x/bake.sh"), offenders=[(240, "srun")], version="0.22.0"
    )
    # Assert
    assert "--no-cache-dir" in message


def test_describe_remote_failure_carries_the_remote_exit_status() -> None:
    # Arrange
    # Act
    message = core.describe_remote_failure(
        verdict=BakeVerdict.FAILED,
        script=Path("/x/bake.sh"),
        ssh_rc=17,
        stdout="",
        stderr="",
    )
    # Assert
    assert "ssh rc=17" in message


def test_describe_remote_failure_carries_the_tail_of_remote_stderr() -> None:
    # Arrange — the remote's own words are the diagnosis; throwing them
    # away is what made `bake-remote FAILED:` unreadable.
    stderr = "\n".join(f"line-{n}" for n in range(1, 21))
    # Act
    message = core.describe_remote_failure(
        verdict=BakeVerdict.FAILED,
        script=Path("/x/bake.sh"),
        ssh_rc=1,
        stdout="",
        stderr=stderr,
    )
    # Assert
    assert "line-20" in message


def test_describe_remote_failure_reports_the_last_remote_stdout_line() -> None:
    # Arrange — "Build complete: ...partial" as the LAST word out of the
    # remote is the whole fingerprint of the stdin-eating srun.
    stdout = "starting\nINFO: Build complete: /store/sac-base-x.sif.partial\n"
    # Act
    message = core.describe_remote_failure(
        verdict=BakeVerdict.NO_RESULT,
        script=Path("/x/bake.sh"),
        ssh_rc=0,
        stdout=stdout,
        stderr="",
    )
    # Assert
    assert "Build complete" in message


def test_describe_remote_failure_says_so_when_remote_stderr_was_empty() -> None:
    # Arrange — silence must render as SILENCE, never as an empty string
    # the reader mistakes for "nothing was wrong".
    # Act
    message = core.describe_remote_failure(
        verdict=BakeVerdict.FAILED,
        script=Path("/x/bake.sh"),
        ssh_rc=1,
        stdout="",
        stderr="",
    )
    # Assert
    assert "the remote said nothing" in message


def test_no_result_failure_says_what_to_check() -> None:
    # Arrange — NO_RESULT means the script DIED; the reader needs to be
    # pointed at the installed script, which is where the answer was.
    # Act
    message = core.describe_remote_failure(
        verdict=BakeVerdict.NO_RESULT,
        script=Path("/x/bake.sh"),
        ssh_rc=0,
        stdout="",
        stderr="",
    )
    # Assert
    assert "--input=none" in message
