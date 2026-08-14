"""Lazy ``scitex-logging`` accessor for sac's internal diagnostics.

WHY THIS EXISTS
---------------
sac reports its diagnostics through three channels, and only one of them was
missing a home:

1. **rich ``console.print``** (``cli_pkg/_helpers/_console.py``) — tabular and
   prose CLI *rendering*. 395 call sites. Correct as-is: a CLI printing its
   result to stdout is the product, not a log line.
2. **A caller-injected ``stream=`` / ``err_stream=`` / ``log_stream=``
   parameter** — a function's *reporting contract*. The caller decides where
   the report lands and tests capture it by passing a ``StringIO``. Correct
   as-is: the seam is deliberate.
3. **A raw ``print()`` to a HARDCODED ``sys.stdout`` / ``sys.stderr``** — a
   diagnostic with no origin, no level and no routing. That is the defect this
   module exists to fix.

A category-3 print has no caller contract to honour, so it can be routed
without changing any signature. Routing it through ``scitex-logging`` buys
three things a raw print cannot give:

* **WHERE it happened** — ``getLogger(__name__)`` stamps the emitting module,
  which is precisely the operator's 2026-08-14 directive
  (「さらにエラーがどこで起こったかを示せるととてもよい。scitex-logging は
  まさにそのためにあります」).
* **DURABILITY** — scitex-logging fans out to stderr *and* to a rotating file
  under ``~/.scitex/logging/runtime/``. A swallowed-exception report that
  previously died with the tmux pane now survives it. An error that is only
  ever printed to a stream nobody is reading is an error that was swallowed
  in every sense that matters.
* **LEVEL CONTROL** — the ecosystem's ``SCITEX_LOGGING_LEVEL`` /
  ``SCITEX_LOGGING_FORMAT`` env vars govern sac's diagnostics alongside every
  other scitex package, instead of sac being the one package that always
  shouts.

WHY THE IMPORT IS LAZY
----------------------
``scitex_logging`` auto-configures handlers on first import, which must not be
paid at *module import* time — the same rationale already documented on
``config.__init__._config_logger`` and ``runtimes._cct_token_pool._logger``.
Importing THIS module is free (no third-party import at module scope); the
``scitex_logging`` import happens inside :func:`get_logger`, at call time.

Callers therefore do::

    from .._logging import get_logger      # cheap, module scope

    def _something():
        ...
        get_logger(__name__).warning("...")   # scitex_logging imported here

and NOT a module-level ``logger = get_logger(__name__)``, which would move the
cost straight back to import time and defeat the point.
"""

from __future__ import annotations

from typing import Any

__all__ = ["get_logger"]


def get_logger(name: str) -> Any:
    """Return the ``scitex-logging`` logger for ``name``.

    ``name`` should be the caller's ``__name__`` so the emitted record carries
    the module the diagnostic came from — that origin is the whole point.

    The ``scitex_logging`` import is deliberately INSIDE the function; see the
    module docstring for why module-scope would be wrong.
    """
    import scitex_logging

    return scitex_logging.getLogger(name)
