"""Install the child-interpreter coverage hook used by the owning suite."""

import sysconfig
from pathlib import Path


def ensure_subprocess_coverage_shim(purelib: Path | None = None) -> None:
    """Drop an idempotent ``.pth`` shim in site-packages so every child
    Python interpreter auto-starts coverage via
    ``coverage.process_startup()``.
    """
    purelib = Path(sysconfig.get_paths()["purelib"]) if purelib is None else purelib
    pth = purelib / "_scitex_agent_container_subprocess_coverage.pth"
    # ``import coverage`` MUST stay inside the guard: a ``.pth`` runs at
    # every interpreter startup, so an unconditional import taxes ~120ms
    # onto each non-coverage `python`/`sac` invocation. Mirror the shape
    # of the sibling ``a1_coverage.pth``.
    # site executes only lines beginning with "import "; keep the conditional
    # in one executable line, with real newlines inside exec's string literal.
    guarded = (
        "if os.environ.get('COVERAGE_PROCESS_START'):\n"
        "    import coverage\n"
        "    coverage.process_startup()\n"
    )
    shim = "import os; exec(" + repr(guarded) + ")\n"
    try:
        if not pth.exists() or pth.read_text() != shim:
            pth.write_text(shim)
    except OSError:
        # site-packages may be read-only (e.g. system Python). Local
        # dev venvs are writable and that's where this matters.
        pass

