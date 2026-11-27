"""Conftest must not touch site-packages or caller coverage env vars."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

CONFTEST_PATH = Path(__file__).resolve().parents[1] / "conftest.py"


def test_conftest_leaves_site_packages_untouched() -> None:
    # Arrange
    child_code = textwrap.dedent(
        """\
        import os
        import runpy
        import site
        import sys

        conftest_path = sys.argv[1]

        os.environ["COVERAGE_PROCESS_START"] = "sentinel-process-start"
        os.environ["COVERAGE_FILE"] = "sentinel-coverage-file"

        dirs = []
        try:
            dirs.extend(site.getsitepackages())
        except Exception:
            pass
        try:
            dirs.append(site.getusersitepackages())
        except Exception:
            pass

        def snapshot():
            snap = {}
            for d in dirs:
                try:
                    snap[d] = sorted(os.listdir(d))
                except OSError:
                    snap[d] = None
            return snap

        before = snapshot()
        runpy.run_path(conftest_path)
        after = snapshot()

        assert before == after, "conftest modified site-packages"
        assert os.environ.get("COVERAGE_PROCESS_START") == "sentinel-process-start"
        assert os.environ.get("COVERAGE_FILE") == "sentinel-coverage-file"
        """
    )
    # Act
    proc = subprocess.run(
        [sys.executable, "-c", child_code, str(CONFTEST_PATH)],
        capture_output=True,
        text=True,
    )
    # Assert
    assert proc.returncode == 0, proc.stderr
