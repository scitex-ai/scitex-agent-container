"""Reusable test helper: ssh-binary shim that performs a real HTTP POST.

The Stage-2 cross-host forwarder shells out to ``ssh <host> "<remote-curl
cmd>"``. To exercise that path end-to-end *without mocking subprocess*,
this fixture installs a fake ``ssh`` executable at the front of ``$PATH``
that:

1. Captures the production argv (so tests can verify ssh option / host
   shape).
2. Reads the JSON body from its stdin (the production code pipes the
   body via ssh stdin).
3. Parses the inner curl invocation for the URL (``http://127.0.0.1:PORT
   /path``) and the optional ``Authorization: Bearer ...`` header.
4. Performs a **real** :class:`httpx.Client` POST to that URL with the
   same body + Authorization header. That URL resolves to the local
   ``sac listen`` running on the test's loopback port — so the test
   substitutes the ssh tunnel with a direct loopback call to the same
   in-process destination, no Python-level mock anywhere.
5. Prints the destination's response body to stdout (curl-shaped). Exits
   0 when the destination responded; non-zero on connect / timeout.

The destination's listen is NOT mocked: it's a real
:func:`uvicorn.Server` on a loopback port, exactly like the Stage-1
cross-host tests already do. This shim only stands in for the ssh
*transport* — the rest of the round-trip stays real.

Honest-replacement notes:

* No ``unittest.mock`` / ``Mock`` / ``patch`` / ``mocker`` references.
* No ``monkeypatch`` fixture parameter (STX-NM002).
* ``$PATH`` mutation is done by the fixture's own yield/finally pair so
  the env restore stays explicit — same shape as the existing
  :func:`subprocess_shim` helper.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


class _SshHttpShim:
    """Controller for the fake-ssh-as-real-http shim."""

    def __init__(self, bin_dir: Path) -> None:
        self._bin = bin_dir
        self._argv_log = bin_dir / "ssh.argv.jsonl"
        self._stdin_log = bin_dir / "ssh.stdin.jsonl"

    def install(self, *, http_timeout_s: float = 15.0) -> Path:
        """Materialize the fake ``ssh`` executable at ``$bin_dir/ssh``.

        The script logs argv to ``ssh.argv.jsonl`` and the captured
        Authorization header / parsed URL / body to ``ssh.stdin.jsonl``
        (one JSON object per invocation). Returns the shim's path.
        """
        script = self._bin / "ssh"
        body = _SHIM_SOURCE.format(
            python=sys.executable,
            argv_log=json.dumps(str(self._argv_log)),
            stdin_log=json.dumps(str(self._stdin_log)),
            timeout_s=float(http_timeout_s),
        )
        script.write_text(body)
        script.chmod(0o755)
        return script

    def invocations(self) -> list[dict]:
        """Return every invocation as a list of dicts:
        ``{argv, host, remote_cmd, url, port, path, bearer, body, status}``.
        """
        if not self._argv_log.exists():
            return []
        argv_lines = [
            json.loads(ln)
            for ln in self._argv_log.read_text().splitlines()
            if ln.strip()
        ]
        stdin_lines = [
            json.loads(ln)
            for ln in self._stdin_log.read_text().splitlines()
            if ln.strip()
        ]
        out: list[dict] = []
        for argv, body in zip(argv_lines, stdin_lines):
            merged = dict(body)
            merged["argv"] = argv
            out.append(merged)
        return out

    def last(self) -> dict | None:
        """Return the most recent invocation record, or ``None``."""
        all_calls = self.invocations()
        return all_calls[-1] if all_calls else None


_SHIM_SOURCE = '''#!{python}
"""Real-HTTP ssh shim — see tests/scitex_agent_container/_helpers/ssh_http_shim.py."""

from __future__ import annotations

import json
import re
import sys

import httpx


def _parse_remote_curl(cmd: str) -> dict:
    """Parse a curl invocation built by ``_post_via_ssh_curl``.

    Returns ``{{port, path, url, framed}}``. Raises ``ValueError`` if
    the shape doesn't match.

    ``framed`` reports which stdin contract the production code chose.
    The AUTHENTICATED path deliberately keeps the bearer out of every
    argv — that is the security property under test — so there is no
    longer an ``Authorization`` header in this command string to scrape.
    Instead it emits ``curl --config -`` (the same shape
    ``_hostsync._push_tokens_io.probe_peer_listen_auth`` uses) and frames
    ssh stdin as ``<token>\\n<body>``. The shim therefore has to split
    stdin exactly as the remote snippet's ``read`` builtin would.
    """
    # URL is the final positional arg in the curl line.
    url_match = re.search(r"http://127\\.0\\.0\\.1:(\\d+)(/[^\\s]+)", cmd)
    if not url_match:
        raise ValueError(f"shim: could not parse URL from curl cmd: {{cmd!r}}")
    port = int(url_match.group(1))
    path = url_match.group(2)
    return {{
        "port": port,
        "path": path,
        "url": f"http://127.0.0.1:{{port}}{{path}}",
        "framed": "--config -" in cmd,
    }}


def main() -> int:
    argv = sys.argv[1:]
    with open({argv_log}, "a") as fh:
        fh.write(json.dumps(argv) + "\\n")

    # The remote command is the last positional arg in the ssh argv,
    # everything before it is ssh options / host. Walk from the end:
    # the host sits immediately before the remote cmd.
    if len(argv) < 2:
        sys.stderr.write("shim ssh: expected at least HOST <remote-cmd>\\n")
        return 2
    remote_cmd = argv[-1]
    host = argv[-2]

    try:
        parsed = _parse_remote_curl(remote_cmd)
    except ValueError as exc:
        sys.stderr.write(f"shim ssh: {{exc}}\\n")
        return 3

    stream = sys.stdin.buffer.read()
    bearer = None
    if parsed["framed"]:
        # Same split the remote snippet's `IFS= read -r` performs: the
        # first line is the token, everything after it is the body.
        token_bytes, _, body = stream.partition(b"\\n")
        bearer = token_bytes.decode("utf-8", errors="replace")
    else:
        body = stream

    headers = {{"Content-Type": "application/json"}}
    if bearer:
        headers["Authorization"] = f"Bearer {{bearer}}"

    log_record = {{
        "host": host,
        "remote_cmd": remote_cmd,
        "url": parsed["url"],
        "port": parsed["port"],
        "path": parsed["path"],
        "bearer": bearer,
        "body": body.decode("utf-8", errors="replace"),
    }}

    try:
        with httpx.Client(timeout={timeout_s}) as client:
            resp = client.post(parsed["url"], content=body, headers=headers)
    except (httpx.HTTPError, OSError) as exc:
        log_record["status"] = None
        log_record["error"] = str(exc)
        with open({stdin_log}, "a") as fh:
            fh.write(json.dumps(log_record) + "\\n")
        sys.stderr.write(f"shim ssh: HTTP transport failure: {{exc}}\\n")
        return 7

    log_record["status"] = resp.status_code
    with open({stdin_log}, "a") as fh:
        fh.write(json.dumps(log_record) + "\\n")

    # Curl prints the response body to stdout. Match that so the
    # production parser sees the same shape it would over real ssh.
    sys.stdout.buffer.write(resp.content)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


@pytest.fixture
def ssh_http_shim(tmp_path: Path):
    """PATH-prepended ssh shim that performs a real httpx POST.

    Yields a :class:`_SshHttpShim` controller. Auto-restores ``PATH``.
    """
    bin_dir = tmp_path / "_shim_bin"
    bin_dir.mkdir(exist_ok=True)
    saved_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{saved_path}"
    try:
        yield _SshHttpShim(bin_dir)
    finally:
        os.environ["PATH"] = saved_path
