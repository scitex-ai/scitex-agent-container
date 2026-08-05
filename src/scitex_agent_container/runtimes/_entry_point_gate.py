"""Start-time gate: the console scripts must RUN in the union the agent gets.

MEASURED 2026-08-03. scitex-hub's a2a inbox rail was dead from 2026-07-18 to
2026-08-03 -- sixteen days -- because three console-script wrappers were masked
in its overlay::

    <overlay>/upper/opt/venv-sac/bin/sac                     char device 0:0
    <overlay>/upper/opt/venv-sac/bin/sac-statusline          char device 0:0
    <overlay>/upper/opt/venv-sac/bin/scitex-agent-container  char device 0:0

Deleting a file that exists in the LOWER layer does not remove it; overlayfs
writes a name-specific WHITEOUT that hides the lower copy permanently. The SIF
shipped all three the whole time and `sac --version` exited 0 inside it. Only
the union was broken, and the union is what the agent runs in.

WHY THE EXISTING GATES COULD NOT SEE IT, both of them:

* ``containers/sif_symbol_probe.py`` runs at BAKE time, inside the SIF, with no
  overlay. It was GREEN for all sixteen days, correctly, about a different
  filesystem than the one that was broken.
* Any check of the form ``import scitex_agent_container`` is blind here by
  construction. site-packages was intact and the dist metadata still DECLARED
  both console scripts; the package imported perfectly. The failure is
  "importable but not invokable" -- one layer below what an import observes.

That is the same shape as the psycopg hole the day before, where a bare
``psycopg/`` directory with no ``__init__.py`` imported as a NAMESPACE PACKAGE
and ``hasattr(psycopg, "connect")`` was True with no driver underneath. Twice in
two days a gate sat one layer above the thing that actually breaks.

So this gate asserts the ENTRY POINT RUNS, and asserts it against the sif +
overlay union rather than the image.

THE PROBE ARGV IS DERIVED FROM THE REAL LAUNCH ARGV, deliberately. Rebuilding
the apptainer preamble here would create a second copy of the overlay/bind/
isolation logic, free to drift from the one that launches the agent -- and a
probe that measures a DIFFERENT union than the agent runs in is worse than no
probe, because it reports on a filesystem nobody uses. Reusing the launch argv
makes divergence impossible: same flags, same overlay, same image, by
construction rather than by discipline.
"""

from __future__ import annotations

import logging
import os
import subprocess

logger = logging.getLogger(__name__)

# Probe timeout. Generous: the first exec of a cold SIF pays a mount cost, and
# a probe that times out on a slow host would be reported as UNKNOWN anyway.
PROBE_TIMEOUT_S = 120

#: Set to ``"1"`` to skip the gate. Deliberately an OVERRIDE rather than a
#: default-off knob — the hazard belongs in the escape hatch, not in the
#: declaration, so nobody disables this by merely not knowing about it.
SKIP_ENV_VAR = "SAC_SKIP_ENTRY_POINT_GATE"

#: The ONLY exit codes that are evidence about the SCRIPT rather than about
#: apptainer. POSIX shells use 127 for "command not found" and 126 for "found
#: but not executable" — precisely the two states a masked wrapper produces.
#: Every other non-zero code (255 for a missing image, mount failures, an
#: overlay that will not attach) is a fault in the CONTAINER, with a different
#: repair, and must never be reported as a missing console script.
COMMAND_NOT_FOUND_CODES = (126, 127)

# The console script whose absence silently kills the agent's rails. `sac mcp
# start` (the MCP server) and `sac mcp channel` (the inbox adapter) are both
# spawned through this wrapper, so when it is missing the agent boots, works,
# and is unreachable -- which is exactly how hub went sixteen days unnoticed.
DEFAULT_CONSOLE_SCRIPT = "/opt/venv-sac/bin/sac"

_SIF_SUFFIX = ".sif"


class EntryPointGateError(RuntimeError):
    """A console script does not run in the union this agent will launch in.

    Carries the repair, not just the complaint -- the constitution's
    fail-fast/fail-loud/actionable-hint contract. Raised BEFORE the session
    starts, so the operator sees this instead of an agent that comes up
    healthy-looking and answers nobody.
    """


def probe_argv_from_launch(
    launch_argv: list[str],
    *,
    script: str = DEFAULT_CONSOLE_SCRIPT,
) -> list[str]:
    """Derive an entry-point probe argv from the agent's REAL launch argv.

    Keeps the apptainer preamble up to and including the ``.sif`` -- which is
    what carries ``--overlay``, the binds and the isolation flags -- and
    replaces the inner command with ``<script> --version``.

    Returns ``[]`` when no ``.sif`` appears in ``launch_argv``: with no image
    there is no union to probe, and a probe that cannot run must say so rather
    than invent a command. The caller treats empty as UNKNOWN, never as a pass.
    """
    argv = [str(a) for a in (launch_argv or [])]
    for i, arg in enumerate(argv):
        if arg.endswith(_SIF_SUFFIX):
            return [*argv[: i + 1], script, "--version"]
    return []


def entry_point_violation(
    launch_argv: list[str],
    *,
    runner,
    script: str = DEFAULT_CONSOLE_SCRIPT,
) -> str | None:
    """``None`` when the console script runs; an actionable message when not.

    ``runner`` is the injection seam: ``(argv) -> returncode``. Three-valued in
    spirit -- an UNPROBEABLE launch argv (no image) returns ``None`` rather than
    a violation, because "I could not look" must never be reported as "it is
    broken". Only a probe that ACTUALLY RAN and came back non-zero accuses.
    """
    probe = probe_argv_from_launch(launch_argv, script=script)
    if not probe:
        return None

    returncode = runner(probe)
    if returncode == 0:
        return None
    if returncode not in COMMAND_NOT_FOUND_CODES:
        # EXIT-CODE COLLISION, and it is the whole reason this branch exists.
        # `apptainer exec` returns the INNER command's status on success, but
        # its OWN failures (image missing, mount error, overlay unavailable)
        # arrive as non-zero too -- 255, typically. Reading "non-zero" as "the
        # wrapper is gone" therefore accuses the console script whenever
        # apptainer merely could not run, which is a different fault with a
        # different repair. Measured: the tui_session suite injects a
        # deterministic fake argv naming an image that does not exist, and the
        # first version of this gate refused 29 legitimate starts on exit 255.
        # Only "command not found / not executable" is evidence about the
        # SCRIPT; everything else is UNKNOWN and must not accuse.
        logger.info(
            "entry-point probe inconclusive (exit %s, not a not-found code): %s",
            returncode,
            probe,
        )
        return None

    return (
        f"{script} does not run in this agent's sif+overlay union "
        f"(exit {returncode}). The package can be perfectly installed and still "
        f"fail this way: an overlay whiteout masks the image's copy by NAME, so "
        f"site-packages stays intact, `import scitex_agent_container` succeeds, "
        f"and the wrapper is gone. That kills `sac mcp start` and "
        f"`sac mcp channel`, so the agent boots, looks healthy, and answers "
        f"nobody on the a2a inbox rail.\n"
        f"REPAIR: look for character-device whiteouts in the overlay's upper "
        f"layer at <overlay>/upper/opt/venv-sac/bin/ "
        f"(`find <upper>/opt/venv-sac/bin -maxdepth 1 -type c`). Deleting those "
        f"entries unmasks the image's originals -- which is a REVEAL, not a "
        f"write, so it adds no overlay drift for a future rebake to fight. "
        f"Verify with `{script} --version` returning 0.\n"
        f"Probe argv: {probe}"
    )


def _subprocess_runner(probe_argv: list[str]) -> int:
    return subprocess.run(
        probe_argv, capture_output=True, timeout=PROBE_TIMEOUT_S
    ).returncode


def assert_entry_point_runs(agent_name: str, launch_argv: list[str], *, runner=None):
    """Refuse to launch an agent whose console script does not run.

    RAISES rather than logs, deliberately. Logging would reproduce the very
    failure being prevented: the agent starts, every liveness probe calls it
    healthy, and it answers nobody — scitex-hub sat in exactly that state for
    sixteen days while its warning-shaped evidence sat in a log.

    Refusing is safe BY CONSTRUCTION here, because the underlying check is
    three-valued. An unprobeable launch argv yields no violation, and a probe
    that raises is caught below and treated as UNKNOWN. Only a probe that
    ACTUALLY RAN and returned non-zero can refuse a start — so a bug in the
    probe itself cannot brick the fleet, which is the failure mode that makes
    start-time gates dangerous.
    """
    if os.environ.get(SKIP_ENV_VAR) == "1":
        logger.info("entry-point gate skipped for %r via %s", agent_name, SKIP_ENV_VAR)
        return

    try:
        violation = entry_point_violation(
            launch_argv, runner=runner or _subprocess_runner
        )
    except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
        # A probe that ERRORS is UNKNOWN, never a verdict. Treating a timeout
        # or a missing apptainer as "the wrapper is gone" would refuse every
        # start on this host — a gate more dangerous than the fault it guards.
        logger.info("entry-point gate could not run for %r: %s", agent_name, exc)
        return

    if violation:
        raise EntryPointGateError(f"{agent_name}: {violation}")
