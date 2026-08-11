"""The command that moves the bytes, and the parse that reads the counts back.

Both halves are pinned WITHOUT a second machine: the argv is a pure rendering and
the measurement parse is a pure read of captured output, which is the same seam
the probe adapter uses. What cannot be tested here — that two real hosts agree —
is exactly what the canary run is for.

THE SNAPSHOT HALF IS TESTED AGAINST REAL FILES. ``snapshot_transcripts`` renders a
shell script, so the only honest way to know it finds the last newline is to run
it: the ``exec_fn`` seam hands the argv to ``subprocess.run`` and ``wc``/``head``
actually execute against files written to ``tmp_path``. Nothing is mocked.
"""

from __future__ import annotations

import subprocess

import pytest

from scitex_agent_container._lifecycle._relocate_probe_ssh import RemoteRun
from scitex_agent_container._lifecycle._relocate_shell import Shell
from scitex_agent_container._lifecycle._relocate_transport import TranscriptFile
from scitex_agent_container._lifecycle._relocate_transport_ssh import (
    MARK_FILE,
    build_copy_argv,
    measure_transcripts,
    parse_measurements,
    snapshot_transcripts,
)

LOCAL = Shell(host="src-host", is_local=True)
REMOTE = Shell(host="tgt-host")


def _real_exec(argv, timeout_s=None):
    """Actually run the argv. The seam the production code already takes."""
    done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    return {
        "exit_code": done.returncode,
        "stdout": done.stdout,
        "stderr": done.stderr,
        "timed_out": False,
    }


def _snap(*names, offset=120, lines=4):
    return [
        TranscriptFile(name=n, byte_count=offset, line_count=lines) for n in names
    ]


def _argv(files):
    return build_copy_argv(
        source=LOCAL,
        source_dir="/src/projects/-p",
        target=REMOTE,
        target_dir="/tgt/projects/-p",
        files=files,
    )


# --------------------------------------------------------------------------
# The copy command: exact names, exact byte counts
# --------------------------------------------------------------------------


def test_the_carried_names_are_passed_as_paths_not_a_glob() -> None:
    # Arrange: THE reason this is not scp-with-a-remote-glob. The allowlist
    # decided an exact set; a glob would be re-expanded at copy time and carry
    # whatever matched a moment later.
    # Act
    pipeline = _argv(_snap("a.jsonl", "b.jsonl"))[-1]
    # Assert
    assert "/src/projects/-p/a.jsonl" in pipeline and "/src/projects/-p/b.jsonl" in pipeline


def test_no_glob_character_reaches_either_shell() -> None:
    # Arrange: the same rule, stated as the property that must hold rather than
    # the string that happens to appear.
    # Act
    pipeline = _argv(_snap("a.jsonl"))[-1]
    # Assert
    assert "*" not in pipeline


def test_the_read_is_bounded_by_the_recorded_offset() -> None:
    # Arrange: the 2026-08-12 fix. The copy carries the number that was recorded
    # BEFORE it started, so a source that grows underneath it changes nothing.
    # Act
    pipeline = _argv(_snap("a.jsonl", offset=108412278))[-1]
    # Assert
    assert "head -c 108412278" in pipeline


def test_every_file_gets_its_own_bound() -> None:
    # Arrange: one offset per file, because they were snapshotted independently
    # and a shared bound would truncate one and overrun another.
    files = [
        TranscriptFile("a.jsonl", byte_count=10, line_count=1),
        TranscriptFile("b.jsonl", byte_count=99, line_count=3),
    ]
    # Act
    pipeline = _argv(files)[-1]
    # Assert
    assert "head -c 10" in pipeline and "head -c 99" in pipeline


def test_a_file_with_no_recorded_offset_is_refused() -> None:
    # Arrange: carrying "however much is there now" restores the exact race the
    # snapshot removes, and the mismatch it produces is reported against a
    # source that has already moved.
    unbounded = [TranscriptFile("a.jsonl")]
    # Act
    call = lambda: _argv(unbounded)  # noqa: E731
    # Assert
    with pytest.raises(ValueError):
        call()


def test_the_pipeline_uses_pipefail_so_a_failed_source_is_not_masked() -> None:
    # Arrange: without it a pipeline reports only the LAST stage, so a read that
    # failed outright is hidden by an ssh that cheerfully wrote nothing.
    # Act
    pipeline = _argv(_snap("a.jsonl"))[-1]
    # Assert
    assert "set -o pipefail" in pipeline


def test_the_stages_are_chained_so_the_first_failure_stops_the_rest() -> None:
    # Arrange: one connection per file means several ways to fail. Carrying on
    # after one has failed would leave a half-populated target that the count
    # check then has to untangle.
    # Act
    pipeline = _argv(_snap("a.jsonl", "b.jsonl"))[-1]
    # Assert
    assert " && " in pipeline


def test_the_destination_is_created_on_the_target_before_writing() -> None:
    # Arrange: the target's transcript root may not exist at all — it is the
    # agent's own directory on a host it has never run on.
    # Act
    pipeline = _argv(_snap("a.jsonl"))[-1]
    # Assert
    assert "mkdir -p /tgt/projects/-p" in pipeline


def test_the_bytes_land_under_the_targets_own_directory() -> None:
    # Arrange: the destination is recomputed from the TARGET's workdir, and a
    # file written under the source's name is present, intact and invisible.
    # Act
    pipeline = _argv(_snap("a.jsonl"))[-1]
    # Assert
    assert "/tgt/projects/-p/a.jsonl" in pipeline


def test_an_empty_file_list_is_refused() -> None:
    # Arrange: a copy that transfers nothing and exits 0 is the exact failure
    # shape this feature exists to prevent.
    empty: list[TranscriptFile] = []
    # Act
    call = lambda: _argv(empty)  # noqa: E731
    # Assert
    with pytest.raises(ValueError):
        call()


def test_a_credential_is_refused_at_the_point_the_bytes_would_move() -> None:
    # Arrange: select_transferable already declines these, but THIS function is
    # generic and will carry any named file. A transport that would copy a
    # credential if asked is one that eventually does.
    named = _snap(".credentials.json")
    # Act
    call = lambda: _argv(named)  # noqa: E731
    # Assert
    with pytest.raises(ValueError):
        call()


def test_a_path_component_in_a_name_is_refused() -> None:
    # Arrange: a "/" would place the file outside the directory the allowlist was
    # applied to, which quietly widens the allowlist to the whole filesystem.
    escaping = _snap("../elsewhere/a.jsonl")
    # Act
    call = lambda: _argv(escaping)  # noqa: E731
    # Assert
    with pytest.raises(ValueError):
        call()


def test_a_local_source_is_read_without_an_intervening_ssh() -> None:
    # Arrange: the source IS where the coordinator runs, in the ordinary case.
    # ssh-ing to ourselves is one more connection that can fail.
    # Act
    pipeline = _argv(_snap("a.jsonl"))[-1]
    # Assert
    assert pipeline.startswith("set -o pipefail; head -c 120 -- /src/projects/-p/a.jsonl")


def test_a_remote_source_is_read_over_ssh() -> None:
    # Arrange: the general case — a coordinator relocating between two hosts that
    # are both somebody else.
    # Act
    argv = build_copy_argv(
        source=Shell(host="src-host"),
        source_dir="/src/p",
        target=REMOTE,
        target_dir="/tgt/p",
        files=_snap("a.jsonl"),
    )
    # Assert
    assert argv[-1].startswith("set -o pipefail; ssh ")


# --------------------------------------------------------------------------
# The snapshot: real files, real offsets
# --------------------------------------------------------------------------


def _write(tmp_path, body: str, name="sess.jsonl"):
    directory = tmp_path / "projects"
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return directory, path


def test_a_file_ending_in_a_newline_snapshots_its_whole_length(tmp_path) -> None:
    # Arrange: the ordinary case. Every record is complete, so the snapshot is
    # the file and nothing is given up.
    directory, path = _write(tmp_path, '{"a":1}\n{"b":2}\n')
    # Act
    snap = snapshot_transcripts(
        Shell(host="h", is_local=True), str(directory), ["sess.jsonl"], exec_fn=_real_exec
    )
    # Assert
    assert snap[0].byte_count == path.stat().st_size


def test_a_half_written_record_is_cut_at_the_last_newline(tmp_path) -> None:
    # Arrange: THE case. A record is mid-flight, so the snapshot stops at the
    # last complete line. Cutting anywhere else hands the target malformed JSON
    # inside a file that still parses as JSONL everywhere else.
    directory, _ = _write(tmp_path, '{"a":1}\n{"b":2}\n{"half":')
    # Act
    snap = snapshot_transcripts(
        Shell(host="h", is_local=True), str(directory), ["sess.jsonl"], exec_fn=_real_exec
    )
    # Assert
    assert snap[0].byte_count == len('{"a":1}\n{"b":2}\n')


def test_the_snapshot_counts_only_complete_lines(tmp_path) -> None:
    # Arrange: the line count travels with the offset and is what arrival is
    # checked against, so the partial record must not be counted as a line.
    directory, _ = _write(tmp_path, '{"a":1}\n{"b":2}\n{"half":')
    # Act
    snap = snapshot_transcripts(
        Shell(host="h", is_local=True), str(directory), ["sess.jsonl"], exec_fn=_real_exec
    )
    # Assert
    assert snap[0].line_count == 2


def test_a_file_with_no_newline_at_all_snapshots_nothing(tmp_path) -> None:
    # Arrange: there is no complete record to carry. Zero is the honest answer —
    # and it is a MEASURED zero, which verification can check the target against.
    directory, _ = _write(tmp_path, '{"half":')
    # Act
    snap = snapshot_transcripts(
        Shell(host="h", is_local=True), str(directory), ["sess.jsonl"], exec_fn=_real_exec
    )
    # Assert
    assert snap[0].byte_count == 0


def test_the_snapshot_is_smaller_than_the_file_when_the_file_is_still_growing(
    tmp_path,
) -> None:
    # Arrange: the whole point. The source grows after the snapshot; the
    # recorded offset does not follow it.
    directory, path = _write(tmp_path, '{"a":1}\n')
    snap = snapshot_transcripts(
        Shell(host="h", is_local=True), str(directory), ["sess.jsonl"], exec_fn=_real_exec
    )
    # Act
    with open(path, "ab") as handle:
        handle.write(b'{"b":2}\n')
    # Assert
    assert snap[0].byte_count < path.stat().st_size


def test_a_file_that_is_not_there_is_absent_rather_than_zero(tmp_path) -> None:
    # Arrange: absent and empty are different answers, and only one of them
    # means the copy has nothing to do.
    directory, _ = _write(tmp_path, '{"a":1}\n')
    # Act
    snap = snapshot_transcripts(
        Shell(host="h", is_local=True), str(directory), ["gone.jsonl"], exec_fn=_real_exec
    )
    # Assert
    assert snap == ()


def test_the_target_side_measurement_reads_the_whole_file(tmp_path) -> None:
    # Arrange: the two measurements are deliberately different questions. The
    # source is asked for its last COMPLETE line; the target is asked what it
    # actually holds, and the comparison between them is the verdict.
    directory, path = _write(tmp_path, '{"a":1}\n{"half":')
    # Act
    measured = measure_transcripts(
        Shell(host="h", is_local=True), str(directory), ["sess.jsonl"], exec_fn=_real_exec
    )
    # Assert
    assert measured[0].byte_count == path.stat().st_size


# --------------------------------------------------------------------------
# The parse
# --------------------------------------------------------------------------


def test_measurements_are_read_off_the_marker_lines() -> None:
    # Arrange: parsed per line so a shell that also prints a warning cannot be
    # mistaken for a measurement.
    run = RemoteRun(
        stdout=f"warning: something\n{MARK_FILE}a.jsonl\t120\t4\n",
        stderr="",
        exit_code=0,
    )
    # Act
    files = parse_measurements(run)
    # Assert
    assert files[0].byte_count == 120


def test_a_line_count_is_read_alongside_the_byte_count() -> None:
    # Arrange: the two catch different things — bytes catch a truncated write,
    # lines catch a transport that rewrote line endings and left the size
    # plausible.
    run = RemoteRun(stdout=f"{MARK_FILE}a.jsonl\t120\t4\n", stderr="", exit_code=0)
    # Act
    files = parse_measurements(run)
    # Assert
    assert files[0].line_count == 4


def test_an_unreadable_count_is_not_measured_rather_than_zero() -> None:
    # Arrange: an empty field means wc could not answer. Reading it as 0 turns an
    # unreadable file into an empty one, and verification would then compare two
    # zeros and pass.
    run = RemoteRun(stdout=f"{MARK_FILE}a.jsonl\t\t\n", stderr="", exit_code=0)
    # Act
    files = parse_measurements(run)
    # Assert
    assert files[0].measured is False


def test_output_with_no_marker_lines_yields_no_measurements() -> None:
    # Arrange: a script that ran and said nothing measured nothing. The caller
    # then sees the file as absent on that host, which refuses.
    run = RemoteRun(stdout="ls: no such directory\n", stderr="", exit_code=1)
    # Act
    files = parse_measurements(run)
    # Assert
    assert files == ()
