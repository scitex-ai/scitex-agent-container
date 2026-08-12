"""The a2a bind address a spec gets when it does not name one.

Split into its own module rather than added to :mod:`._types` because that
file is 519 lines against this repo's 512-line cap — every edit to it is
refused until it is split, and splitting a core types module is not this
migration's business.

The value is currently spelled as a bare ``"127.0.0.1"`` literal in five
places::

    config/_types.py:212               A2ASpec.host default
    config/_parsers/_a2a.py:24         parse_a2a -> A2ASpec.host
    runtimes/a2a_sidecar.py:109        --host passed to `a2a serve`  (the BIND)
    _lifecycle/health.py:110           AgentCard probe URL           (a client)
    cli_pkg/a2a_group.py:163           `sac a2a doctor` probe URL    (a client)

They agree today, which is the only reason five spellings have gone unnoticed.
Collapsing all five onto this constant is the right fix and is NOT done here:
two of the five files are over the line cap and cannot be edited at all, and a
partial collapse would imply a canonicality that does not hold. Instead the
agreement is PINNED BY TEST (``test__a2a_host_equivalence.py``), so the day one
of them drifts, the migration's zero-behaviour-change claim fails loudly
instead of quietly becoming false.
"""

from __future__ import annotations

#: The host every reader falls back to when ``spec.a2a.host`` is absent.
DEFAULT_A2A_HOST = "127.0.0.1"

__all__ = ["DEFAULT_A2A_HOST"]
