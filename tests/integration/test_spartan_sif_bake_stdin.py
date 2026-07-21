"""The bake script must never let an ``srun`` step eat the script itself.

MEASURED INCIDENT (2026-07-17 → 2026-07-19, five dead bakes, zero SIFs).
``sac image bake-remote`` delivers this script by PIPING it into a remote
``bash -l -s --`` (``_image_remote_bake.py``), so bash reads the script text
from fd 0 — a non-seekable ssh pipe. ``srun`` forwards ITS stdin to the
launched task by default, and that is the SAME pipe, still holding the unread
remainder of the script. srun therefore swallowed the gate, the publish, the
rotate and the final ``SAC_BAKE_RESULT`` line; bash then hit EOF and exited
**0**. No error, no signal, no verdict — the build succeeded every time and
the store filled with ``.partial`` files nothing ever renamed.

The A/B that established this on the real host (identical scripts piped over
ssh, single variable — srun's ``--input=none``):

    RUN A (no --input=none)     RUN B (--input=none)
    M1_START                    M1_START
    M2_JID=27305397             M2_JID=27305397
    M3_INSIDE_SRUN              M3_INSIDE_SRUN
    ssh_rc_A=0                  M4_AFTER_SRUN rc=0
                                M5_SECOND_LINE_AFTER_SRUN
                                M6_FINAL_MARKER
                                ssh_rc_B=0

These tests reproduce that mechanism LOCALLY and BEHAVIOURALLY against the
shipped script: a stand-in ``srun`` that drains fd 0 exactly as the real one
forwards it, and the real ``"$SRUN"`` invocations lifted verbatim out of the
file. A guard that stops the drain keeps the tail of the script alive; no
guard and the tail is gone — precisely what production did.

Controls live alongside, because a stdin guard is trivially "achievable" by
deleting the thing being guarded: the srun steps must still launch into the
standing lease, the gate must still exec the symbol probe against the
artifact, and the build must still tee its log.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import scitex_agent_container

BAKE_SCRIPT = (
    Path(scitex_agent_container.__file__).resolve().parent
    / "containers"
    / "spartan-sif-bake.sh"
)

# A stand-in for srun that is faithful in the ONE dimension under test: real
# srun forwards its own stdin to the launched task, so it consumes fd 0 —
# unless it is told not to (`--input=none`) or handed a different fd 0
# (`< /dev/null`). Both guards fall out naturally here: with --input=none it
# never reads, and with a /dev/null fd 0 there is nothing to read.
_FAKE_SRUN = """#!/usr/bin/env bash
for arg in "$@"; do
    if [ "$arg" = "--input=none" ]; then
        exit 0
    fi
done
cat >/dev/null
exit 0
"""

# Every variable the lifted srun blocks reference, bound to something inert.
# The blocks are executed for their REDIRECTION shape, not their payload.
_HARNESS_PREAMBLE = """set -uo pipefail
SRUN={srun}
APPTAINER=/bin/true
JID=1
CPUS=1
LAYER=base
USER=tester
WORKDIR={work}
CTX={work}
PARTIAL_SIF={work}/artifact.sif.partial
PROBE={work}/probe.py
BUILD_LOG={work}/build.log
"""

_TAIL_MARKER = "SAC_TEST_SCRIPT_TAIL_REACHED"

# The tail is deliberately several lines long: the failure mode is "bash reads
# EOF instead of the next line", so a one-line tail could be lost to some
# unrelated truncation and look identical.
_HARNESS_TAIL = f"""echo "{_TAIL_MARKER}"
echo "{_TAIL_MARKER}_2"
echo "{_TAIL_MARKER}_3"
"""

_GATE_MUST_KEEP = ('"$APPTAINER" exec', '"$PARTIAL_SIF"', '"$PROBE"')

# Each srun step, identified by something only that step carries.
_BUILD = "sac-sif-bake-"
_GATE = "sac-sif-gate-"
_PUBLISH = "sha256sum"


def _script_source() -> str:
    return BAKE_SCRIPT.read_text(encoding="utf-8")


def _srun_blocks(source: str) -> list[str]:
    """Every ``"$SRUN" ...`` invocation, backslash-continuations included."""
    lines = source.splitlines()
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        if '"$SRUN"' in lines[index]:
            block = [lines[index]]
            while block[-1].rstrip().endswith("\\") and index + 1 < len(lines):
                index += 1
                block.append(lines[index])
            blocks.append("\n".join(block))
        index += 1
    return blocks


def _block_containing(source: str, needle: str) -> str:
    matches = [blk for blk in _srun_blocks(source) if needle in blk]
    if len(matches) != 1:
        raise LookupError(
            f"expected exactly one srun block containing {needle!r}, "
            f"found {len(matches)} — the script's shape changed"
        )
    return matches[0]


def _run_piped_on_stdin(body: str, work: Path) -> subprocess.CompletedProcess[str]:
    """Feed ``body`` to ``bash -s`` on a PIPE — the transport the bake uses.

    ``input=`` gives bash a non-seekable pipe for fd 0, exactly like the ssh
    channel, so bash reads the text line by line and leaves the remainder
    sitting in the pipe where a stdin-consuming child can reach it.
    """
    srun = work / "fake-srun"
    srun.write_text(_FAKE_SRUN, encoding="utf-8")
    srun.chmod(0o755)
    script = _HARNESS_PREAMBLE.format(srun=srun, work=work) + body + _HARNESS_TAIL
    # cwd=work: the lifted blocks are executed for their REDIRECTION shape,
    # and a mutated block can create relative-path files. Anchoring bash in
    # the per-test tmp dir keeps any such droppings out of the repo root
    # (a stray 0-byte SAC_TEST_SCRIPT_TAIL_REACHED once rode a git add -A
    # into a PR from exactly this seam).
    return subprocess.run(
        ["bash", "-s"],
        input=script,
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(work),
    )


def _unguarded_build_run(work: Path) -> subprocess.CompletedProcess[str]:
    """The real build block with BOTH guards stripped back off."""
    block = _block_containing(_script_source(), _BUILD)
    mutated = block.replace("--input=none", "").replace("< /dev/null", "")
    return _run_piped_on_stdin(mutated, work)


@pytest.mark.parametrize(
    "needle",
    [_BUILD, _GATE, _PUBLISH],
    ids=["build", "gate", "publish"],
)
def test_srun_step_does_not_swallow_the_rest_of_the_script(
    needle: str, tmp_path: Path
) -> None:
    # Arrange — lift the REAL srun invocation out of the shipped script and
    # put it back on a piped stdin, the way bake-remote delivers it.
    block = _block_containing(_script_source(), needle)
    # Act
    proc = _run_piped_on_stdin(block, tmp_path)
    # Assert — the script must survive its own srun step. Production did not:
    # bash exited 0 with the whole tail unread.
    assert _TAIL_MARKER in proc.stdout, (
        f"srun block {needle!r} consumed the piped script: "
        f"rc={proc.returncode} stdout={proc.stdout!r}"
    )


def test_the_guarded_build_step_still_exits_zero(tmp_path: Path) -> None:
    # Arrange — the guard must buy the script's tail back without costing it
    # a clean exit; a non-zero rc here would trip the caller's own checks.
    block = _block_containing(_script_source(), _BUILD)
    # Act
    proc = _run_piped_on_stdin(block, tmp_path)
    # Assert
    assert proc.returncode == 0


def test_every_srun_step_guards_stdin_at_both_levels() -> None:
    # Arrange — `--input=none` is the slurm-level guard (the one the on-host
    # A/B proved) and `< /dev/null` is the shell-level belt-and-braces. None
    # of the three tasks reads stdin: apptainer build takes a .def path, the
    # gate takes a script path, sha256sum takes a file argument.
    blocks = _srun_blocks(_script_source())
    # Act
    unguarded = [
        blk for blk in blocks if "--input=none" not in blk or "/dev/null" not in blk
    ]
    # Assert
    assert unguarded == [], f"srun invocation(s) without a stdin guard: {unguarded}"


# ---------------------------------------------------------------------------
# CONTROLS — a stdin guard is trivially "achieved" by deleting the guarded
# work. These fail if the fix neuters the bake instead of protecting it.
# ---------------------------------------------------------------------------
def test_control_harness_still_loses_the_tail_without_the_guard(
    tmp_path: Path,
) -> None:
    # Arrange — MUTATION PROOF. Strip both guards back off the real build
    # block; if the tail survives THAT, this whole file is testing nothing
    # and its green means nothing.
    # Act
    proc = _unguarded_build_run(tmp_path)
    # Assert
    assert _TAIL_MARKER not in proc.stdout


def test_control_an_unguarded_srun_fails_silently_with_rc_zero(
    tmp_path: Path,
) -> None:
    # Arrange — the reason this cost five bakes and two days is that losing
    # the tail is SILENT. Pin that the unguarded shape reproduces production
    # exactly: success-shaped rc, no verdict.
    # Act
    proc = _unguarded_build_run(tmp_path)
    # Assert
    assert proc.returncode == 0


def test_control_the_script_still_has_exactly_three_srun_steps() -> None:
    # Arrange — the cheapest fake fix is to delete the gate and the publish
    # srun steps outright; then nothing can eat the script and nothing gets
    # verified or published either.
    source = _script_source()
    # Act
    blocks = _srun_blocks(source)
    # Assert
    assert len(blocks) == 3


def test_control_every_srun_still_launches_into_the_standing_lease() -> None:
    # Arrange — the other cheap fake fix is to drop srun and run on the login
    # node (login-node guard kills it; account sanctions). Every step must
    # still be a STEP inside the standing lease.
    blocks = _srun_blocks(_script_source())
    # Act
    into_lease = [
        blk for blk in blocks if '--jobid="$JID"' in blk and "--overlap" in blk
    ]
    # Assert
    assert len(into_lease) == len(blocks)


def test_control_the_gate_still_execs_the_probe_against_the_artifact() -> None:
    # Arrange — gate-not-run is not gate-passed. The guard must not have been
    # bought by pointing the gate at something other than the built file.
    gate = _block_containing(_script_source(), _GATE)
    # Act
    missing = [needle for needle in _GATE_MUST_KEEP if needle not in gate]
    # Assert
    assert missing == []


def test_control_the_build_still_tees_its_log() -> None:
    # Arrange — redirecting the build output instead of tee-ing it would also
    # quieten the channel; the remote store log AND the ssh channel must both
    # keep getting the build output.
    build = _block_containing(_script_source(), _BUILD)
    # Act
    tees = 'tee "$BUILD_LOG"' in build
    # Assert
    assert tees


def test_control_the_script_never_reassigns_its_own_stdin() -> None:
    # Arrange — the tempting one-line "fix" is `exec 0</dev/null` at the top.
    # It is catastrophic HERE: bash is READING THE SCRIPT from fd 0, so that
    # line discards everything after itself. Guard each srun, never fd 0.
    source = _script_source()
    # Act
    offenders = [
        line
        for line in source.splitlines()
        if line.strip().startswith(("exec 0<", "exec <"))
    ]
    # Assert
    assert offenders == []
