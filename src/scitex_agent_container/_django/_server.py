"""Standalone server adapter for the Agents dashboard.

Boots the SAME Django app a host mounts (``scitex_agent_container._django``)
via ``scitex_app.embed``. The guarded launcher (``serve_gui``) records runtime
state so ``gui status`` / ``gui stop`` and ``--force`` work.
"""

from __future__ import annotations

from functools import partial
from typing import Callable

from ._constants import DEFAULT_PORT

APP_MODULE = "scitex_agent_container._django"


def _run_server(
    *,
    port: int = DEFAULT_PORT,
    host: str = "127.0.0.1",
    open_browser: bool = False,
    hot_reload: bool = False,
) -> None:
    from scitex_app.embed import run_standalone

    run_standalone(
        app_module=APP_MODULE,
        port=port,
        host=host,
        open_browser=open_browser,
        hot_reload=hot_reload,
    )


def run(
    *,
    port: int = DEFAULT_PORT,
    host: str = "127.0.0.1",
    open_browser: bool = False,
    hot_reload: bool = False,
) -> None:
    """Run the app in the foreground. ``run_server`` for ``serve_gui``."""
    _run_server(
        port=port, host=host, open_browser=open_browser, hot_reload=hot_reload
    )


def run_server(
    *,
    port: int = DEFAULT_PORT,
    host: str = "127.0.0.1",
    hot_reload: bool = False,
) -> Callable[[], None]:
    """A zero-arg blocking callable for :func:`scitex_app.embed.serve_gui`."""
    return partial(
        _run_server, port=port, host=host, open_browser=False, hot_reload=hot_reload
    )


def serve(
    *,
    package: str,
    project_dir: str,
    port: int,
    host: str,
    force: bool = False,
    hot_reload: bool = False,
) -> int:
    """Launch the guarded standalone server. Returns an exit code."""
    from scitex_app.embed import serve_gui

    return serve_gui(
        package=package,
        project_dir=project_dir,
        port=port,
        host=host,
        force=force,
        run_server=run_server(port=port, host=host, hot_reload=hot_reload),
    )


__all__ = ["APP_MODULE", "DEFAULT_PORT", "run", "run_server", "serve"]
