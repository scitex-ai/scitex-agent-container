"""Real native proxy controls reject foreign health and wrong application identity."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import signal
import subprocess
import sys
import tempfile
import threading
from urllib.request import ProxyHandler, build_opener

import pytest

from tests.scitex_agent_container._runners._a2a_proxy_main_fixture import (
    _snapshot,
    wait_for_proxy,
)
from tests.scitex_agent_container._runners._a2a_proxy_workspace import proxy_workspace
from tests.scitex_agent_container._runners.test_a2a_proxy import (
    real_upstream_server as real_upstream_server,
)


@contextmanager
def _native_child(upstream):
    with proxy_workspace() as (local, child_env):
        state_dir = local / "owned-readiness"
        args = [
            sys.executable,
            "-m",
            "scitex_agent_container._runners.a2a_proxy",
            "--name",
            state_dir.name,
            "--upstream",
            upstream,
            "--state-root",
            str(local),
            "--a2a-port",
            "0",
        ]
        with tempfile.TemporaryFile(dir=local) as output:
            child = subprocess.Popen(
                args, stdout=output, stderr=subprocess.STDOUT, env=child_env
            )
            try:
                yield child, state_dir
            finally:
                try:
                    if child.poll() is None:
                        child.send_signal(signal.SIGTERM)
                        child.wait(timeout=2)
                except Exception as exc:
                    snapshot = _snapshot(child, state_dir)
                    output.seek(0, 2)
                    output.seek(max(0, output.tell() - 8192))
                    tail = output.read(8192).decode("utf-8", errors="replace")
                    exc.add_note(
                        f"Owned readiness child diagnostics: {snapshot}; output tail: {tail}"
                    )
                    raise
                finally:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=2)


@pytest.fixture
def still_fetching_proxy():
    entered = threading.Event()
    release = threading.Event()

    class DelayedCard(BaseHTTPRequestHandler):
        def do_GET(self):
            entered.set()
            release.wait(8)
            payload = b'{"name":"owned-upstream","skills":[]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    class ForeignHealth(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def log_message(self, *args):
            pass

    servers = [
        ThreadingHTTPServer(("127.0.0.1", 0), handler)
        for handler in (DelayedCard, ForeignHealth)
    ]
    threads = []
    for server in servers:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        threads.append(thread)
    base = f"http://127.0.0.1:{servers[0].server_port}"
    try:
        with _native_child(base) as (child, state_dir):
            try:
                if not entered.wait(5):
                    raise RuntimeError("Owned proxy did not request its upstream card")
                opener = build_opener(ProxyHandler({}))
                with opener.open(
                    f"http://127.0.0.1:{servers[1].server_port}/health", timeout=0.2
                ) as response:
                    if response.status != 200:
                        raise RuntimeError(
                            "Foreign listener control is not serving health"
                        )
                yield child, state_dir, base
            finally:
                release.set()
                # This control intentionally observed the child BEFORE startup.
                # Drain that startup before applying the unchanged shutdown budget.
                wait_for_proxy(child, state_dir, base, timeout=5)
    finally:
        release.set()
        for server in servers:
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=2)


def test_foreign_health_cannot_qualify_a_child_still_fetching(still_fetching_proxy):
    # Arrange — foreign /health is actually 200; our real child has no listener yet.
    child, state_dir, base = still_fetching_proxy

    # Act
    def wait():
        return wait_for_proxy(child, state_dir, base, timeout=0.2)

    # Assert
    with pytest.raises(TimeoutError, match="Owned proxy"):
        wait()


def test_actual_proxy_health_must_identify_the_expected_upstream(real_upstream_server):
    # Arrange — start the real native child with a kernel-selected port.
    base, _recorder = real_upstream_server
    with _native_child(base) as (child, state_dir):
        wait_for_proxy(child, state_dir, base, timeout=5)

        # Act
        def wait():
            return wait_for_proxy(child, state_dir, base + "/wrong-owner", timeout=0.2)

        # Assert
        with pytest.raises(TimeoutError, match="Owned proxy"):
            wait()
