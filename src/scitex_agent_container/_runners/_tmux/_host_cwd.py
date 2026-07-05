"""Host-side pane-cwd resolution for the tmux runner.

Split out of ``tmux.py`` so the fallback is a pure, hermetically
testable function (the no-mock test doctrine: CI containers have no
tmux binary, so anything inside ``TmuxManager.start`` is unreachable
there).
"""

from __future__ import annotations

from pathlib import Path


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
    """
    try:
        Path(workdir).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(
            f"WARN: tmux workdir {workdir!r} is not host-creatable "
            f"({exc}); using /tmp as the pane launch cwd instead.",
            flush=True,
        )
        return "/tmp"
    return workdir
