#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lifecycle commands package — start, stop, restart, delete, forget, cleanup.

Split out of the former single-file ``cli_pkg/lifecycle_cmds.py`` once
that file outgrew the 512-line project limit. The public surface
(click command objects) is unchanged — importers should keep using:

    from scitex_agent_container.cli_pkg.lifecycle import (
        start, stop, restart, delete, forget, cleanup,
    )
"""

from ._cleanup import cleanup
from ._delete import delete
from ._forget import forget
from ._restart import restart
from ._start import start
from ._stop import stop

__all__ = ["start", "stop", "restart", "delete", "forget", "cleanup"]
