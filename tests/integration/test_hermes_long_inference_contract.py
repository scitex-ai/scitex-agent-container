from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml

run_agent = pytest.importorskip(
    "run_agent",
    reason="runs against the pinned Hermes environment during image verification",
)

from scitex_agent_container.config._hermes_config import (  # noqa: E402
    compile_hermes_config,
)
from scitex_agent_container.config._launch_plan import (  # noqa: E402
    compile_launch_plan,
)


def _spec(endpoint_url: str) -> dict:
    return {
        "harness": "hermes",
        "launch_mode": "tui",
        "container": {"backend": "apptainer"},
        "engine": "qwen",
        "available_engines": {
            "qwen": {
                "model": "qwen-test",
                "timeouts": {
                    "upstream_deadline_seconds": 1,
                    "client_abandonment_seconds": 2,
                },
                "endpoints": {
                    "openai-chat-completions": {
                        "url": endpoint_url,
                        "auth": {"kind": "bearer", "env": "QWEN_TEST_KEY"},
                    }
                },
            }
        },
    }


class _SlowProvider(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *, live: bool):
        super().__init__(("127.0.0.1", 0), _SlowProviderHandler)
        self.live = live
        self.request_count = 0
        self.release = threading.Event()


class _SlowProviderHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(content_length)
        if self.path != "/v1/chat/completions":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.server.request_count += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if not self.server.live:
            self.server.release.wait(timeout=6)
            return
        time.sleep(1.25)
        chunks = (
            {
                "id": "slow-live",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "qwen-test",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "done"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "slow-live",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "qwen-test",
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                ],
            },
        )
        try:
            for chunk in chunks:
                body = f"data: {json.dumps(chunk)}\n\n".encode()
                self.wfile.write(body)
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


@contextmanager
def _provider(*, live: bool):
    server = _SlowProvider(live=live)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextmanager
def _profile(tmp_path, endpoint_url: str):
    rendered = compile_hermes_config(
        compile_launch_plan(_spec(endpoint_url), agent_name="timeout-probe"),
        workdir=str(tmp_path),
    )
    profile = tmp_path / ".hermes"
    profile.mkdir()
    (profile / "config.yaml").write_text(
        yaml.safe_dump(rendered, sort_keys=False), encoding="utf-8"
    )
    previous_home = os.environ.get("HERMES_HOME")
    previous_key = os.environ.get("QWEN_TEST_KEY")
    os.environ["HERMES_HOME"] = str(profile)
    os.environ["QWEN_TEST_KEY"] = "test-key"
    try:
        yield
    finally:
        if previous_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = previous_home
        if previous_key is None:
            os.environ.pop("QWEN_TEST_KEY", None)
        else:
            os.environ["QWEN_TEST_KEY"] = previous_key


def _agent(base_url: str):
    agent = run_agent.AIAgent(
        api_key="test-key",
        base_url=base_url,
        provider="sac-qwen",
        model="qwen-test",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    agent.api_mode = "chat_completions"
    agent._interrupt_requested = False
    return agent


def _invoke(agent, outcome: dict[str, object]) -> None:
    try:
        outcome["response"] = agent._interruptible_streaming_api_call(
            {
                "model": "qwen-test",
                "messages": [{"role": "user", "content": "hello"}],
            }
        )
    except BaseException as exc:
        outcome["error"] = exc


@pytest.mark.timeout(8)
def test_slow_live_provider_is_not_abandoned_or_duplicated(tmp_path):
    # Arrange
    with _provider(live=True) as provider:
        base_url = f"http://127.0.0.1:{provider.server_port}/v1"
        endpoint = f"{base_url}/chat/completions"
        with _profile(tmp_path, endpoint):
            agent = _agent(base_url)
            outcome: dict[str, object] = {}
            worker = threading.Thread(target=_invoke, args=(agent, outcome))

            # Act
            worker.start()
            time.sleep(1.05)
            still_waiting_after_upstream_window = worker.is_alive()
            worker.join(timeout=4)

            # Assert
            response = outcome.get("response")
            assert (
                still_waiting_after_upstream_window
                and not worker.is_alive()
                and "error" not in outcome
                and provider.request_count == 1
                and response.choices[0].message.content == "done"
            )


@pytest.mark.timeout(10)
def test_dead_provider_still_fails_at_the_declared_bound(tmp_path):
    # Arrange
    with _provider(live=False) as provider:
        base_url = f"http://127.0.0.1:{provider.server_port}/v1"
        endpoint = f"{base_url}/chat/completions"
        with _profile(tmp_path, endpoint):
            agent = _agent(base_url)
            outcome: dict[str, object] = {}
            worker = threading.Thread(target=_invoke, args=(agent, outcome))

            # Act
            started = time.monotonic()
            worker.start()
            worker.join(timeout=8)
            elapsed = time.monotonic() - started

            # Assert
            assert (
                not worker.is_alive()
                and "error" in outcome
                and provider.request_count == 3
                and 5.5 <= elapsed < 8
            )
