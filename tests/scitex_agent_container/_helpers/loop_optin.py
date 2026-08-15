"""Opt a test file back INTO a background loop the suite floor turns off.

``tests/conftest.py`` force-sets the kill switches for the three
``sac listen`` lifespan loops that used to launch unconditionally — the
GitHub-CI poller, the TUI heartbeat writer and the SDK heartbeat writer.
The reason is in that file: they log through scitex-logging, whose
handler re-resolves ``sys.stderr`` at every emit, so a line written while
some other test is inside ``CliRunner.invoke`` lands in that invoke's
captured buffer and corrupts its ``--json`` assertion.

A file whose whole job IS one of those loops has to switch it back on.
That is what this helper is for, used as a file-local autouse fixture::

    @pytest.fixture(autouse=True)
    def _loop_enabled_for_this_file():
        with loop_enabled("SAC_TUI_HEARTBEAT_DISABLED"):
            yield

Explicit env save/restore, no monkeypatch (PA-306), and the restore puts
the floor back so the opt-in never escapes the file that asked for it.
"""

from __future__ import annotations

import contextlib
import os
from typing import Iterator

__all__ = ["loop_enabled"]


@contextlib.contextmanager
def loop_enabled(key: str) -> Iterator[None]:
    """Clear ``key`` for the duration of the block, then restore it."""
    saved = os.environ.pop(key, None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ[key] = saved
        else:
            os.environ.pop(key, None)
