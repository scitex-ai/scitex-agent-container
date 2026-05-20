"""Lazy loader for the real handover collaborator module.

Extracted from the former monolithic ``lifecycle.py`` (split for the
512-line module limit). ``lifecycle`` re-exports ``_load_handover_module``.
"""

from __future__ import annotations

from typing import Any


def _load_handover_module() -> Any:
    """Return the real :mod:`._lifecycle.handover` module.

    Separate function so callers can substitute a real hand-rolled
    handover collaborator with the same call surface
    (``ensure_instance_uuid``, ``hydrate_from_hub``,
    ``push_pre_stop_snapshot``, ``start_failback_poller``) via the
    ``handover_mod`` kwarg on the lifecycle entry points.
    """
    from . import handover as _h

    return _h
