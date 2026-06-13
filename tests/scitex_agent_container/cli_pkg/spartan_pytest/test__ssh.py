"""Tests for the ssh-side helpers in :mod:`spartan_pytest._ssh`.

The pure helper ``_extract_job_id`` parses sbatch stdout into the
SLURM job id; the rest of the ssh leg (submit / poll / fetch) hits a
real cluster and is exercised behind ``$SAC_SPARTAN_HOST`` from
:mod:`test__run_cmd`.

Style: STX-TQ002 AAA markers, STX-TQ007 one assert per test, no mocks
(the parser is a pure function).
"""

from __future__ import annotations

from scitex_agent_container.cli_pkg.spartan_pytest import _extract_job_id


def test_extract_job_id_finds_canonical_sbatch_output():
    # Arrange
    stdout = "Submitted batch job 123456\n"
    # Act
    job_id = _extract_job_id(stdout)
    # Assert
    assert job_id == "123456"


def test_extract_job_id_returns_none_when_no_digits():
    # Arrange
    stdout = "sbatch: error: invalid reservation"
    # Act
    job_id = _extract_job_id(stdout)
    # Assert
    assert job_id is None
