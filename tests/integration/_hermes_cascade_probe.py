"""Real HTTP fixture for the pinned Hermes cascade integration test."""

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

sys.path.insert(0, os.environ["SAC_HERMES_SOURCE_DIR"])
from scitex_agent_container.config import AgentConfig, ProviderSpec
from scitex_agent_container.config._engine_types import parse_engine_entry
from scitex_agent_container.config._hermes_config import compile_hermes_config
from scitex_agent_container.config._hermes_failover import HermesFailoverSpec
from scitex_agent_container.runtimes._hermes_failover import (
    configure_failover,
    materialize_pools,
)
from scitex_agent_container.runtimes._hermes_profile import (
    _launch_plan,
    _write_profile_env,
)

calls = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        key = self.headers.get("Authorization", "").removeprefix("Bearer ")
        calls.append((data["model"], key))
        codes = {"fake-a0": 401, "fake-a1": 429, "fake-b0": 402, "fake-b1": 200}
        code = codes[key]
        if code != 200:
            body = json.dumps(
                {
                    "error": {
                        "message": "Invalid Authorization header"
                        if code == 401
                        else "usage limit exceeded"
                        if code == 429
                        else "insufficient credits",
                        "reset_at": time.time() + 86400,
                    }
                }
            ).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if data.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for delta, finish in [
                ({"role": "assistant", "content": "CASCADE_OK"}, None),
                ({}, "stop"),
            ]:
                body = {
                    "id": "chatcmpl-diagnostic",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": data["model"],
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                self.wfile.write(("data: " + json.dumps(body) + "\n\n").encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        else:
            body = json.dumps(
                {
                    "id": "chatcmpl-diagnostic",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": data["model"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "CASCADE_OK"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 3,
                        "total_tokens": 13,
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
worker = threading.Thread(target=server.serve_forever, daemon=True)
worker.start()
try:
    with tempfile.TemporaryDirectory() as temporary:
        home = Path(temporary)
        os.environ["HERMES_HOME"] = str(home)
        os.environ["HERMES_RUNTIME_DIR"] = str(home / "runtime")
        root = "http://127.0.0.1:" + str(server.server_port)
        for name, token in [
            ("SAC_TEST_A0", "fake-a0"),
            ("SAC_TEST_A1", "fake-a1"),
            ("SAC_TEST_B0", "fake-b0"),
            ("SAC_TEST_B1", "fake-b1"),
        ]:
            os.environ[name] = token
        cfg = AgentConfig(
            name="cascade-diagnostic",
            harness="hermes",
            runtime="tui",
            workdir=temporary,
        )
        cfg.engine_key = "a"
        cfg.model = "diagnostic-a"
        cfg.claude.provider = ProviderSpec(
            base_url=root + "/a/v1", auth_token_env="SAC_TEST_A0"
        )
        cfg.engines["b"] = parse_engine_entry(
            "b",
            {
                "model": "diagnostic-b",
                "provider": {
                    "base_url": root + "/b/v1",
                    "auth_token_env": "SAC_TEST_B0",
                },
            },
        )
        cfg.hermes_failover = HermesFailoverSpec(
            accounts={
                "a": ["SAC_TEST_A0", "SAC_TEST_A1"],
                "b": ["SAC_TEST_B0", "SAC_TEST_B1"],
            },
            engines=["b"],
        )
        rendered = compile_hermes_config(
            _launch_plan(cfg, launch_mode="tui"), workdir=temporary
        )
        env, pools = configure_failover(cfg, rendered)
        (home / "config.yaml").write_text(yaml.safe_dump(rendered))
        _write_profile_env(home / ".env", env)
        materialize_pools(home, pools)
        from hermes_cli.runtime_provider import resolve_runtime_provider
        from run_agent import AIAgent

        provider = rendered["model"]["provider"]
        runtime = resolve_runtime_provider(requested=provider)
        agent = AIAgent(
            model=cfg.model,
            provider=provider,
            base_url=runtime["base_url"],
            api_key=runtime["api_key"],
            credential_pool=runtime["credential_pool"],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            enabled_toolsets=[],
            max_tokens=32,
            max_iterations=6,
            fallback_model=rendered["fallback_providers"],
        )
        result = agent.run_conversation("Reply CASCADE_OK.")
        expected = [
            ("diagnostic-a", "fake-a0"),
            ("diagnostic-a", "fake-a1"),
            ("diagnostic-b", "fake-b0"),
            ("diagnostic-b", "fake-b1"),
        ]
        ok = (
            calls == expected
            and agent.model == "diagnostic-b"
            and "CASCADE_OK" in str(result.get("final_response"))
        )
        auth = json.loads((home / "auth.json").read_text())["credential_pool"]
        print(
            json.dumps(
                {
                    "cascade_passed": ok,
                    "request_count": len(calls),
                    "model": agent.model,
                    "retained_keys": {key: len(value) for key, value in auth.items()},
                    "primary_states": [
                        row.get("last_status") for row in auth[provider]
                    ],
                }
            )
        )
        if not ok:
            raise SystemExit(1)
finally:
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)
