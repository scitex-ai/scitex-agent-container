"""Bundled Apptainer ``.def`` files for the sac runtime layers.

The runtime materializes one of these on every ``sac image build`` —
see :mod:`scitex_agent_container.runtimes._apptainer_build`. The
``.def`` files themselves live alongside this module as package
data; :mod:`scitex_agent_container.containers.apptainer` exposes
their resolved :class:`pathlib.Path` so callers (tests, runtime
builders) can read them without re-deriving the location.
"""

from .apptainer import BASE_DEF, PROXY_DEF, SCITEX_DEF

__all__ = ["BASE_DEF", "PROXY_DEF", "SCITEX_DEF"]
