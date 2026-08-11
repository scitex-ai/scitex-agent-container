"""The command that moves the bytes, and the parse that reads the counts back.

Both halves are pinned WITHOUT a second machine: the argv is a pure rendering and
the measurement parse is a pure read of captured output, which is the same seam
the probe adapter uses. What cannot be tested here — that two real hosts agree —
is exactly what the canary run is for.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._relocate_probe_ssh import RemoteRun
from scitex_agent_container._lifecycle._relocate_shell import Shell
from scitex_agent_container._lifecycle._relocate_transport_ssh import (
    MARK_FILE,
    build_copy_argv,
    parse_measurements,
)

LOCAL = Shell(host="src-host", is_local=True)
REMOTE = Shell(host="tgt-host")


def _argv(files):
    return build_copy_argv(
        source=LOCAL,
        source_dir="/src/projects/-p",
        target=REMOTE,
        target_dir="/tgt/projects/-p",
        files=files,
    )


def test_the_carried_names_are_passed_as_arguments_not_a_glob() -> None:
    # Arrange: THE reason tar was chosen over scp/rsync. The allowlist decided an
    # exact set; a glob would be re-expanded at copy time and carry whatever
    # matched a moment later.
    # Act
    pipeline = _argv(["a.jsonl", "b.jsonl"])[-1]
    # Assert
    assert "-- a.jsonl b.jsonl" in pipeline


def test_no_glob_character_reaches_either_shell() -> None:
    # Arrange: the same rule, stated as the property that must hold rather than
    # the string that happens to appear.
    # Act
    pipeline = _argv(["a.jsonl"])[-1]
    # Assert
    assert "*" not in pipeline


def test_the_pipeline_uses_pipefail_so_a_failed_source_is_not_masked() -> None:
    # Arrange: without it the pipeline reports only the LAST stage, so a tar that
    # failed outright is hidden by an ssh that extracted nothing.
    # Act
    pipeline = _argv(["a.jsonl"])[-1]
    # Assert
    assert "set -o pipefail" in pipeline


def test_the_destination_is_created_on_the_target_before_extracting() -> None:
    # Arrange: the target's transcript root may not exist at all — it is the
    # agent's own directory on a host it has never run on.
    # Act
    pipeline = _argv(["a.jsonl"])[-1]
    # Assert
    assert "mkdir -p /tgt/projects/-p" in pipeline


def test_an_empty_file_list_is_refused() -> None:
    # Arrange: a copy that transfers nothing and exits 0 is the exact failure
    # shape this feature exists to prevent.
    empty: list[str] = []
    # Act
    call = lambda: _argv(empty)
    # Assert
    with pytest.raises(ValueError):
        call()


def test_a_credential_is_refused_at_the_point_the_bytes_would_move() -> None:
    # Arrange: select_transferable already declines these, but THIS function is
    # generic and will carry any named file. A transport that would copy a
    # credential if asked is one that eventually does.
    named = [".credentials.json"]
    # Act
    call = lambda: _argv(named)
    # Assert
    with pytest.raises(ValueError):
        call()


def test_a_path_component_in_a_name_is_refused() -> None:
    # Arrange: a "/" would place the file outside the directory the allowlist was
    # applied to, which quietly widens the allowlist to the whole filesystem.
    escaping = ["../elsewhere/a.jsonl"]
    # Act
    call = lambda: _argv(escaping)
    # Assert
    with pytest.raises(ValueError):
        call()


def test_a_local_source_is_read_without_an_intervening_ssh() -> None:
    # Arrange: the source IS where the coordinator runs, in the ordinary case.
    # ssh-ing to ourselves is one more connection that can fail.
    # Act
    pipeline = _argv(["a.jsonl"])[-1]
    # Assert
    assert pipeline.startswith("set -o pipefail; tar -C /src/projects/-p")


def test_a_remote_source_is_read_over_ssh() -> None:
    # Arrange: the general case — a coordinator relocating between two hosts that
    # are both somebody else.
    # Act
    argv = build_copy_argv(
        source=Shell(host="src-host"),
        source_dir="/src/p",
        target=REMOTE,
        target_dir="/tgt/p",
        files=["a.jsonl"],
    )
    # Assert
    assert argv[-1].startswith("set -o pipefail; ssh ")


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
