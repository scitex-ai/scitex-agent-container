"""Can this host actually produce a WORKING agent?

Distinct from every other readiness surface in sac, which answers INSTALLATION
questions — is sac present, is the daemon up, are there specs on disk. Those
all passed on scitex-compute-01 on 2026-08-23 while an agent started there came
up with zero MCP servers and no error. See :mod:`._node` for the measurements.

A subpackage rather than a flat module because the outcome question has more
than one instance in it (tools today; identity resolution and image currency
are already carded), and because grouping by topical responsibility is what the
project-structure rule asks for once the package root fills up.
"""

from ._node import (
    NodeReadiness,
    ServerVerdict,
    assess_node_readiness,
    node_readiness_for_this_host,
)

__all__ = [
    "NodeReadiness",
    "ServerVerdict",
    "assess_node_readiness",
    "node_readiness_for_this_host",
]
