"""Construct the proxy's HTTP server without taking runner signal ownership."""

from contextlib import nullcontext


def build_server(config):
    """Leave SIGTERM/SIGINT with the runner that owns its heartbeat lifecycle.

    Uvicorn 0.27 uses ``install_signal_handlers``; newer releases wrap serving
    in ``capture_signals``. Disable both hooks without replacing process-global
    handlers or depending on uvicorn's private ``_serve`` implementation.
    """
    import uvicorn

    class RunnerOwnedSignalsServer(uvicorn.Server):
        def install_signal_handlers(self):
            """The enclosing runner installs the process signal handlers."""

        def capture_signals(self):
            """Keep the runner's dispositions throughout the HTTP lifecycle."""
            return nullcontext()

    return RunnerOwnedSignalsServer(config)
