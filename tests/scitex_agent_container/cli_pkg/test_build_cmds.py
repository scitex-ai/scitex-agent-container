"""Tests for ``sac agent check`` / ``sac agent validate`` (build_cmds).

No-mocks rewrite (PA-306). The previous version monkeypatched the
module-level ``resolve_config`` / ``validate_config`` / ``load_config``
callables and stubbed configs with ``SimpleNamespace``, then patched
``build_cmds.shutil.which`` and ``build_cmds.subprocess.run`` to fake
the runtime probe. This version exercises real production paths:

* ``resolve_config`` is fed an explicit ``.yaml`` path (real on-disk
  file) -- success and miss are both real outcomes, no patching.
* ``validate_config`` sees real YAML (valid v3 spec / invalid YAML) and
  returns the actual error list it computes.
* ``load_config`` runs end-to-end on a real spec file -- it constructs
  a real ``AgentConfig``, not a ``SimpleNamespace``.
* The runtime probe (``shutil.which`` + ``subprocess.run``) is steered
  by the shared ``subprocess_shim`` fixture, which plants a real fake
  binary on ``PATH``; production code invokes the real ``shutil.which``
  + ``subprocess.run`` and finds the shim via genuine PATH lookup.
* The ``load_config`` error-after-validation branch was deleted: it is
  a tiny defensive-catch-all, and the only way the previous test hit
  it was by mocking ``load_config`` to raise -- pure mock-only
  behaviour, no honest rewrite available.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.build_cmds import check, validate

# ---------------------------------------------------------------------------
# Real YAML spec helpers
# ---------------------------------------------------------------------------


_MINIMAL_VALID_SPEC = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata: {}
spec:
  runtime: apptainer
"""


def _write_spec(tmp_path: Path, body: str = _MINIMAL_VALID_SPEC) -> Path:
    spec = tmp_path / "spec.yaml"
    spec.write_text(body)
    return spec


def _write_spec_with_binds(tmp_path: Path, binds: list[str]) -> Path:
    bind_lines = "\n".join(f"    - {b}" for b in binds)
    body = (
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "metadata: {}\n"
        "spec:\n"
        "  runtime: apptainer\n"
        "  apptainer:\n"
        "    binds:\n"
        f"{bind_lines}\n"
    )
    return _write_spec(tmp_path, body)


@contextmanager
def _isolated_path(*, include: list[Path]) -> Iterator[None]:
    """Replace ``PATH`` with exactly ``include`` for the duration.

    Lets a test prevent the host's real ``apptainer`` / ``python3`` from
    being discovered by ``shutil.which`` when it wants the missing-binary
    branch. Auto-restores ``PATH`` on exit.
    """
    saved = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join(str(p) for p in include)
    try:
        yield
    finally:
        os.environ["PATH"] = saved


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_with_missing_explicit_path_exits_one(tmp_path):
    # Arrange
    missing = tmp_path / "does-not-exist.yaml"
    runner = CliRunner()
    # Act
    result = runner.invoke(validate, [str(missing)])
    # Assert
    assert result.exit_code == 1


def test_validate_with_missing_explicit_path_prints_not_found(tmp_path):
    # Arrange
    missing = tmp_path / "does-not-exist.yaml"
    runner = CliRunner()
    # Act
    result = runner.invoke(validate, [str(missing)])
    # Assert
    assert "not found" in result.output.lower()


def test_validate_with_valid_v3_spec_exits_zero(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path)
    runner = CliRunner()
    # Act
    result = runner.invoke(validate, [str(spec)])
    # Assert
    assert result.exit_code == 0


def test_validate_with_valid_v3_spec_prints_valid_marker(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path)
    runner = CliRunner()
    # Act
    result = runner.invoke(validate, [str(spec)])
    # Assert
    assert "valid" in result.output


def test_validate_with_invalid_yaml_exits_one(tmp_path):
    # Arrange — bare YAML (no apiVersion) triggers real validator errors.
    spec = _write_spec(tmp_path, body="not_a_real_spec: true\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(validate, [str(spec)])
    # Assert
    assert result.exit_code == 1


def test_validate_with_invalid_yaml_prints_validation_failure(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, body="not_a_real_spec: true\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(validate, [str(spec)])
    # Assert
    assert "validation failed" in result.output.lower()


# ---------------------------------------------------------------------------
# check — preflight failures (resolve / validate)
# ---------------------------------------------------------------------------


def test_check_with_missing_explicit_path_exits_one(tmp_path):
    # Arrange
    missing = tmp_path / "no-such.yaml"
    runner = CliRunner()
    # Act
    result = runner.invoke(check, [str(missing)])
    # Assert
    assert result.exit_code == 1


def test_check_with_missing_explicit_path_reports_error(tmp_path):
    # Arrange
    missing = tmp_path / "no-such.yaml"
    runner = CliRunner()
    # Act
    result = runner.invoke(check, [str(missing)])
    # Assert
    assert "error" in result.output.lower()


def test_check_with_invalid_spec_exits_one(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, body="not_a_real_spec: true\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(check, [str(spec)])
    # Assert
    assert result.exit_code == 1


def test_check_with_invalid_spec_reports_validation_failure(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, body="not_a_real_spec: true\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(check, [str(spec)])
    # Assert
    assert "validation failed" in result.output.lower()


# ---------------------------------------------------------------------------
# check — runtime probe (real subprocess + real PATH via subprocess_shim)
# ---------------------------------------------------------------------------


def test_check_all_dependencies_present_exits_zero(tmp_path, subprocess_shim):
    # Arrange
    spec = _write_spec(tmp_path)
    subprocess_shim.install("apptainer", exit=0, stdout="apptainer version x.y")
    subprocess_shim.install("python3", exit=0, stdout="Python 3.11.0")
    runner = CliRunner()
    # Act
    result = runner.invoke(check, [str(spec)])
    # Assert
    assert result.exit_code == 0


def test_check_all_dependencies_present_prints_ready_marker(tmp_path, subprocess_shim):
    # Arrange
    spec = _write_spec(tmp_path)
    subprocess_shim.install("apptainer", exit=0, stdout="apptainer version x.y")
    subprocess_shim.install("python3", exit=0, stdout="Python 3.11.0")
    runner = CliRunner()
    # Act
    result = runner.invoke(check, [str(spec)])
    # Assert
    assert "Ready to deploy" in result.output


def test_check_with_apptainer_missing_from_path_exits_one(tmp_path, subprocess_shim):
    # Arrange — keep only the shim dir on PATH, then remove apptainer from it.
    spec = _write_spec(tmp_path)
    subprocess_shim.install("apptainer", exit=0, stdout="apptainer x")
    subprocess_shim.install("python3", exit=0, stdout="Python 3.11.0")
    bin_dir = Path(os.environ["PATH"].split(os.pathsep)[0])
    runner = CliRunner()
    # Act
    with _isolated_path(include=[bin_dir]):
        (bin_dir / "apptainer").unlink()
        result = runner.invoke(check, [str(spec)])
    # Assert
    assert result.exit_code == 1


def test_check_with_apptainer_missing_prints_fail_marker(tmp_path, subprocess_shim):
    # Arrange
    spec = _write_spec(tmp_path)
    subprocess_shim.install("apptainer", exit=0, stdout="apptainer x")
    subprocess_shim.install("python3", exit=0, stdout="Python 3.11.0")
    bin_dir = Path(os.environ["PATH"].split(os.pathsep)[0])
    runner = CliRunner()
    # Act
    with _isolated_path(include=[bin_dir]):
        (bin_dir / "apptainer").unlink()
        result = runner.invoke(check, [str(spec)])
    # Assert
    assert "FAIL" in result.output


def test_check_with_python_subprocess_nonzero_exits_one(tmp_path, subprocess_shim):
    # Arrange — python3 shim returns non-zero, real subprocess.run reports it.
    spec = _write_spec(tmp_path)
    subprocess_shim.install("apptainer", exit=0, stdout="apptainer x")
    subprocess_shim.install("python3", exit=1, stdout="")
    runner = CliRunner()
    # Act
    result = runner.invoke(check, [str(spec)])
    # Assert
    assert result.exit_code == 1


def test_check_with_no_python3_on_path_exits_one(tmp_path, subprocess_shim):
    # Arrange — apptainer present, python3 absent → real FileNotFoundError.
    spec = _write_spec(tmp_path)
    subprocess_shim.install("apptainer", exit=0, stdout="apptainer x")
    subprocess_shim.install("python3", exit=0, stdout="x")
    bin_dir = Path(os.environ["PATH"].split(os.pathsep)[0])
    runner = CliRunner()
    # Act
    with _isolated_path(include=[bin_dir]):
        (bin_dir / "python3").unlink()
        result = runner.invoke(check, [str(spec)])
    # Assert
    assert result.exit_code == 1


def test_check_with_no_python3_on_path_prints_python_not_found(
    tmp_path, subprocess_shim
):
    # Arrange
    spec = _write_spec(tmp_path)
    subprocess_shim.install("apptainer", exit=0, stdout="apptainer x")
    subprocess_shim.install("python3", exit=0, stdout="x")
    bin_dir = Path(os.environ["PATH"].split(os.pathsep)[0])
    runner = CliRunner()
    # Act
    with _isolated_path(include=[bin_dir]):
        (bin_dir / "python3").unlink()
        result = runner.invoke(check, [str(spec)])
    # Assert
    assert "python3 not found" in result.output


# ---------------------------------------------------------------------------
# D4 — host-mirroring bind-target warnings (ADR 0001 §D4)
# ---------------------------------------------------------------------------


@pytest.fixture
def _runtime_shims(subprocess_shim):
    subprocess_shim.install("apptainer", exit=0, stdout="apptainer x")
    subprocess_shim.install("python3", exit=0, stdout="Python 3.11.0")
    return subprocess_shim


def test_check_with_canonical_srv_bind_target_does_not_warn(tmp_path, _runtime_shims):
    # Arrange
    spec = _write_spec_with_binds(tmp_path, ["/srv/foo:/srv/foo:ro"])
    runner = CliRunner()
    # Act
    result = runner.invoke(check, [str(spec)])
    # Assert
    assert "mirrors a host path" not in " ".join(result.output.split())


def test_check_with_canonical_srv_bind_target_exits_zero(tmp_path, _runtime_shims):
    # Arrange
    spec = _write_spec_with_binds(tmp_path, ["/srv/foo:/srv/foo:ro"])
    runner = CliRunner()
    # Act
    result = runner.invoke(check, [str(spec)])
    # Assert
    assert result.exit_code == 0


def test_check_with_home_mirroring_bind_target_emits_warning(tmp_path, _runtime_shims):
    # Arrange — Rich may wrap the warning across lines; normalise whitespace.
    spec = _write_spec_with_binds(tmp_path, ["/home/me/proj:/home/me/proj:ro"])
    runner = CliRunner()
    # Act
    result = runner.invoke(check, [str(spec)])
    # Assert
    assert "mirrors a host path" in " ".join(result.output.split())


def test_check_with_home_mirroring_bind_target_still_exits_zero(
    tmp_path, _runtime_shims
):
    # Arrange — D4 is a warning, NOT a failure.
    spec = _write_spec_with_binds(tmp_path, ["/home/me/proj:/home/me/proj:ro"])
    runner = CliRunner()
    # Act
    result = runner.invoke(check, [str(spec)])
    # Assert
    assert result.exit_code == 0


def test_check_with_three_bad_targets_emits_three_warnings(tmp_path, _runtime_shims):
    # Arrange — /home, /Users, /root each warn; /srv does not.
    spec = _write_spec_with_binds(
        tmp_path,
        [
            "/home/a:/home/a:ro",
            "/Users/b:/Users/b:ro",
            "/root/c:/root/c",
            "/srv/d:/srv/d:ro",
        ],
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(check, [str(spec)])
    # Assert
    assert " ".join(result.output.split()).count("mirrors a host path") == 3


def test_check_with_mixed_bad_and_canonical_targets_still_exits_zero(
    tmp_path, _runtime_shims
):
    # Arrange
    spec = _write_spec_with_binds(
        tmp_path,
        [
            "/home/a:/home/a:ro",
            "/Users/b:/Users/b:ro",
            "/root/c:/root/c",
            "/srv/d:/srv/d:ro",
        ],
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(check, [str(spec)])
    # Assert
    assert result.exit_code == 0
