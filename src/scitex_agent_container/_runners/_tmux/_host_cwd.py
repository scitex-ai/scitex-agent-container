"""Host-side pane-cwd resolution for the tmux runner.

Split out of ``tmux.py`` so the fallback is a pure, hermetically
testable function (the no-mock test doctrine: CI containers have no
tmux binary, so anything inside ``TmuxManager.start`` is unreachable
there).
"""

from __future__ import annotations

from pathlib import Path

from ..._logging import get_logger


def resolve_host_cwd(workdir: str) -> str:
    """Return a host-usable tmux pane cwd for ``workdir``, creating it.

    A CONTAINER-path workdir (e.g. an apptainer caller passing
    ``spec.workdir: /work``, backed only by an in-container bind) is not
    host-creatable — the previously unconditional mkdir in
    ``TmuxManager.start`` died on ``PermissionError: '/work'`` before
    the pane's ``cd`` ever ran (2026-07-05, paper-scitex-clew capsule
    launch). The pane cwd is semantically irrelevant to such callers
    (the container's real cwd is apptainer's ``--pwd``), so fall back to
    ``/tmp`` with a loud WARN instead of failing the whole launch.

    The WARN goes through scitex-logging, not ``print``. It used to land on
    bare STDOUT, which is the worst place for it: this runs on the launch path
    of a tmux-backed agent, where stdout is whatever the launcher happened to
    be attached to — frequently nothing at all. The OSError is deliberately
    absorbed (the pane cwd is semantically irrelevant to a container caller),
    so this line was the ONLY account of it, and it was being written where
    nobody reads. It now carries its module origin and is durable in the
    scitex-logging runtime log.
    """
    try:
        Path(workdir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        get_logger(__name__).warning(
            f"tmux workdir {workdir!r} is not host-creatable "
            f"({exc}); using /tmp as the pane launch cwd instead."
        )
        return "/tmp"
    return workdir
