"""scitex-agent-container CLI package.

Split out from the original monolithic ``cli.py`` so each topic area
stays under the project's 512-line limit. The public entry point is
``main``, re-exported from :mod:`._main`.
"""

from ._main import main

__all__ = ["main"]
