"""The generated .pth must activate real child coverage, independently of peers."""

import json
import os
import subprocess
import sys
import sysconfig

import pytest
from coverage import CoverageData

from tests._subprocess_coverage import ensure_subprocess_coverage_shim


def _run_child(tmp_path, *, enabled, twice=False):
    purelib = tmp_path / "owned site"
    purelib.mkdir()
    ensure_subprocess_coverage_shim(purelib)
    source = tmp_path / "source"
    source.mkdir()
    measured = source / "owned_measurement.py"
    measured.write_text("VALUE = 6 * 7\n")
    config = tmp_path / ".coveragerc"
    config.write_text(
        f"[run]\ndata_file = {tmp_path / '.coverage'}\n"
        f"parallel = true\nsource = {source}\n"
    )
    # -S removes peer .pth bootstraps; only the generated SAC hook is processed.
    # The dependency directory is on sys.path without processing its a1_coverage.
    code = (
        "import json,site,sys\n"
        f"sys.path.insert(0, {sysconfig.get_paths()['purelib']!r})\n"
        f"sys.path.insert(0, {str(source)!r})\n"
        f"site.addsitedir({str(purelib)!r})\n"
        + (f"site.addsitedir({str(purelib)!r})\n" if twice else "")
        + "coverage_imported = 'coverage' in sys.modules\n"
        "active = coverage_imported and sys.modules['coverage'].Coverage.current() is not None\n"
        "import owned_measurement\n"
        "print(json.dumps({'active':active,'coverage_imported':coverage_imported,'value':owned_measurement.VALUE}))\n"
    )
    env = {"PATH": os.defpath, "HOME": str(tmp_path)}
    if enabled:
        env["COVERAGE_PROCESS_START"] = str(config)
    child = subprocess.run(
        [sys.executable, "-S", "-c", code],
        env=env, cwd=tmp_path, capture_output=True, text=True, timeout=5,
    )
    assert child.returncode == 0, child.stderr
    return json.loads(child.stdout), measured, list(tmp_path.glob(".coverage.*"))


@pytest.mark.parametrize("twice", [False, True])
def test_generated_hook_records_executed_child_lines_without_other_bootstraps(
    tmp_path, twice
):
    # Arrange: tmp_path owns the source, site directory and coverage target.
    # Act: run the real site processor and coverage recorder.
    result, measured, files = _run_child(tmp_path, enabled=True, twice=twice)
    recorded = set()
    for file in files:
        data = CoverageData(basename=str(file))
        data.read()
        recorded.update(data.lines(str(measured)) or [])
    # Assert: active measurement and recorded execution, not just import/syntax.
    assert (result, len(files), 1 in recorded) == (
        {"active": True, "coverage_imported": True, "value": 42}, 1, True,
    )


def test_generated_hook_does_not_import_coverage_without_a_request(tmp_path):
    # Arrange: no coverage request is supplied to the isolated child.
    # Act
    result, _measured, files = _run_child(tmp_path, enabled=False)
    # Assert
    assert (result, files) == (
        {"active": False, "coverage_imported": False, "value": 42}, [],
    )


def test_installation_replaces_old_multiline_hook_then_keeps_exact_bytes(tmp_path):
    # Arrange
    file = tmp_path / "_scitex_agent_container_subprocess_coverage.pth"
    file.write_text("import os\nif os.getenv('COVERAGE_PROCESS_START'):\n    import coverage\n")
    # Act
    ensure_subprocess_coverage_shim(tmp_path)
    first = file.read_bytes()
    first_mtime = file.stat().st_mtime_ns
    ensure_subprocess_coverage_shim(tmp_path)
    # Assert
    assert (file.read_bytes() == first, file.stat().st_mtime_ns) == (True, first_mtime)
