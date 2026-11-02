"""Owned OpenCode HTTP fixture and race-free native-child listener handoff.

The fixture's real child launcher passes the reserved socket as stdin only to
its synthetic serve process, which adopts FD0. The parent's stdin and stdlib
subprocess module stay untouched; the owner's local module binding is restored
in finally. All children are actual Popen processes, not fabricated results.
"""

import socket

_FAKE_OPENCODE = """#!/usr/bin/env python3
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


def _serve(port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, payload):
            import json as _json

            body = _json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/global/health":
                self._send(200, {"healthy": True})
            elif self.path.startswith("/session/"):
                self._send(200, {"id": self.path.rsplit("/", 1)[-1]})
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            if self.path == "/session":
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                self._send(200, {"id": "ses_stub1"})
            elif self.path.endswith("/message"):
                import json as _json

                length = int(self.headers.get("Content-Length", 0))
                body = _json.loads(self.rfile.read(length) or b"{}")
                log_path = os.path.join(os.environ["OC_TEST_STATE"], "posts.jsonl")
                with open(log_path, "a", encoding="utf-8") as handle:
                    handle.write(_json.dumps(body))
                    handle.write(chr(10))
                self._send(200, {"info": {"id": "msg_stub1"}, "parts": []})
            else:
                self.send_response(404)
                self.end_headers()

    import socket
    # The parent owns the bound socket until serve exits. FD0 is inherited by
    # the real Popen path, so another worker cannot steal a released free port.
    server = HTTPServer(("127.0.0.1", port), Handler, bind_and_activate=False)
    server.socket.close()
    server.socket = socket.fromfd(0, socket.AF_INET, socket.SOCK_STREAM)
    if server.socket.getsockname()[1] != port:
        raise RuntimeError("fixture inherited the wrong listener")
    server.serve_forever()


def _attach():
    import json as _json

    state_dir = os.environ["OC_TEST_STATE"]
    serve_path = os.path.join(state_dir, "opencode-serve.json")
    with open(serve_path, encoding="utf-8") as handle:
        url = _json.load(handle)["url"]
    with open(os.path.join(state_dir, "attach-marker"), "w", encoding="utf-8") as out:
        out.write(url)
    marker = os.path.join(state_dir, "attach-argv.json")
    with open(marker, "w", encoding="utf-8") as out:
        _json.dump(sys.argv[1:], out)
    raise SystemExit(int(os.environ.get("OC_TEST_ATTACH_RC", "0")))


if sys.argv[1] == "serve":
    _serve(int(sys.argv[sys.argv.index("--port") + 1]))
elif sys.argv[1] == "attach":
    _attach()
else:
    raise SystemExit(2)
"""

class _ReservedPort(int):
    def __new__(cls, listener):
        result = int.__new__(cls, listener.getsockname()[1])
        result.listener = listener
        return result


def _free_port(*, reserve=True):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        if reserve:
            listener.listen()
            return _ReservedPort(listener)
        return int(listener.getsockname()[1])
    finally:
        if not reserve:
            listener.close()


class _ListenerChildProcesses:
    """Transparent launcher: real subprocess behavior with owned serve stdin."""

    def __init__(self, real_module, listener):
        self.real_module = real_module
        self.listener = listener

    def __getattr__(self, name):
        return getattr(self.real_module, name)

    def Popen(self, command, *args, **kwargs):
        if command[:2] == ["opencode", "serve"]:
            kwargs["stdin"] = self.listener
        return self.real_module.Popen(command, *args, **kwargs)


def _run_with_listener(module, argv, port):
    original = module.subprocess
    try:
        module.subprocess = _ListenerChildProcesses(original, port.listener)
        return module.main(argv)
    finally:
        module.subprocess = original
        port.listener.close()
