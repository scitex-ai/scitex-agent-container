"""Pytest fixtures and rootdir marker for this package.

An empty conftest.py at tests/ is the canonical SciTeX
convention (audit-project PS208) — it pins the pytest
rootdir and gives downstream fixtures a home.

Also wires subprocess + parallel coverage at module-import time so
child Python interpreters spawned via `subprocess.run(...)` are
included in the coverage report. `os.environ.setdefault` would be a
no-op here because pytest-cov has already set `COVERAGE_FILE` to a tmp
dir by the time this conftest is loaded — force-set is required. See
~/proj/scitex-dev/src/scitex_dev/_skills/general/05_development_06_subprocess-coverage.md.
"""

from __future__ import annotations

import os
import sysconfig
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Pin coverage's data file under tests/results/coverage/ (keeps the repo
# root clean) and point process_startup at our pyproject so child
# interpreters configure themselves correctly. COVERAGE_FILE is absolute
# so every child writes here regardless of its working directory.
_COVERAGE_DIR = _PROJECT_ROOT / "tests" / "results" / "coverage"
_COVERAGE_DIR.mkdir(parents=True, exist_ok=True)
os.environ["COVERAGE_PROCESS_START"] = str(_PROJECT_ROOT / "pyproject.toml")
os.environ["COVERAGE_FILE"] = str(_COVERAGE_DIR / ".coverage")

# incident-local-heavy-build: `sac image build` self-demotes its own
# process (CPU nice 19 + IO best-effort lowest) by default, and demotion
# is ONE-WAY for unprivileged processes. Tests drive the real CLI
# in-process via CliRunner, so without this opt-out the first build test
# would demote the entire remaining pytest run. The real demotion
# behavior is exercised in CHILD interpreters with a curated env — see
# tests/scitex_agent_container/test__build_priority.py.
os.environ.setdefault("SAC_BUILD_NO_NICE", "1")


def _ensure_subprocess_coverage_shim() -> None:
    """Drop an idempotent ``.pth`` shim in site-packages so every child
    Python interpreter auto-starts coverage via
    ``coverage.process_startup()``.
    """
    purelib = Path(sysconfig.get_paths()["purelib"])
    pth = purelib / "_scitex_agent_container_subprocess_coverage.pth"
    # ``import coverage`` MUST stay inside the guard: a ``.pth`` runs at
    # every interpreter startup, so an unconditional import taxes ~120ms
    # onto each non-coverage `python`/`sac` invocation. Mirror the shape
    # of the sibling ``a1_coverage.pth``.
    shim = (
        "import os\n"
        "if os.environ.get('COVERAGE_PROCESS_START'):\n"
        "    import coverage\n"
        "    coverage.process_startup()\n"
    )
    try:
        if not pth.exists() or pth.read_text() != shim:
            pth.write_text(shim)
    except OSError:
        # site-packages may be read-only (e.g. system Python). Local
        # dev venvs are writable and that's where this matters.
        pass


_ensure_subprocess_coverage_shim()

# Expose shared no-mocks helpers (subprocess_shim, env_save_restore,
# ssh_exec_shim, ssh_http_shim) as session-wide fixtures so any test under
# tests/ can use them by name.
from tests.scitex_agent_container._helpers.ssh_exec_shim import (  # noqa: E402,F401
    ssh_exec_shim,
)
from tests.scitex_agent_container._helpers.ssh_http_shim import (  # noqa: E402,F401
    ssh_http_shim,
)
from tests.scitex_agent_container._helpers.subprocess_shim import (  # noqa: E402,F401
    env_save_restore,
    subprocess_shim,
)
