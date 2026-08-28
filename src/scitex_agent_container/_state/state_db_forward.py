#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Where to SEND to a node — addressability, split from locality.

``resolve_node_host`` answers LOCALITY ("which host is this agent on")
and ``is_local_node`` consults it reading ONLY ``host``. The forwarder
needed a different fact — an ADDRESS — and was reading the same return
value for it. One function, two questions, and they disagree precisely
when a live ``instances`` row carries no port:

* locality      — still answerable. The row says which host, and that
  stays true whether or not a port was recorded.
* addressability — NOT answerable. There is nowhere to POST.

Because both came from one call, the forwarder took ``{host,
a2a_port: None}`` as its answer and ``_forward_to_remote`` 502'd on the
falsy port, never consulting ``comms_nodes`` — which may hold a working
address for that same name.

MEASURED on ywata-note-win 2026-08-20::

    instances: scitex-dev  host=scitex-compute-04
               a2a_port=NULL  bound_port=NULL  ended_at=NULL

Live, and PERMANENT: the GC never reaps cross-host rows (``AND
remote=0``, deliberate per its own comment), and nothing back-fills the
port — five ``UPDATE instances`` sites exist and none touches it. So the
row cannot age out and cannot be repaired in place; the operator's
documented repair verb writes ``comms_nodes``, the very table the
unusable row was preventing anyone from reaching.

WHY NOT JUST MAKE ``resolve_node_host`` FALL THROUGH. That one-liner was
the obvious fix and it is wrong. Falling through on a portless row hands
the LOCALITY decision to ``comms_nodes``, which may name a DIFFERENT
host — so an agent that is genuinely local could start being forwarded
away, and a routing repair would have silently redefined "local". The
two questions get two functions instead.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["resolve_forward_target"]


def resolve_forward_target(
    *,
    name: str,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    """Return ``{host, a2a_port}`` usable for forwarding, else ``None``.

    Resolution order matches :func:`..state_db_nodes.resolve_node_host` —
    the live ``instances`` row first, then the ADR-0014 ``comms_nodes``
    federated graph — with one difference that is the entire point: a row
    that cannot supply a PORT does not end the search here.

    ``a2a_port`` is preferred and ``bound_port`` is the fallback, matching
    ``_send_resolve``; the writers populate both from one value, so a row
    carrying only the latter is still a usable address.

    ``None`` means no source could supply an address. The caller must
    treat that as "cannot forward", never as "not registered" — the name
    may be perfectly well known and simply unreachable.

    ``db_path`` is now ONLY the ``comms_nodes`` fallback's parameter. The
    ``instances`` half moved to PostgreSQL on 2026-08-28 and no longer
    names a file; the SELECT above became
    :func:`.state_db_instances.latest_active_instance`, which is the same
    statement ``resolve_node_host`` was carrying its own copy of.
    """
    if not name:
        return None
    from .state_db_comms_nodes import resolve_comms_node_host
    from .state_db_instances import latest_active_instance

    row = latest_active_instance(name)
    if row is not None:
        port = row.get("a2a_port")
        if port is None:
            port = row.get("bound_port")
        if port is not None:
            return {"host": str(row["host"]), "a2a_port": int(port)}
        # A live record carrying no port is not an ADDRESS. Fall through
        # rather than hand back a target the caller can only 502 on.
    return resolve_comms_node_host(name=name, db_path=db_path)
