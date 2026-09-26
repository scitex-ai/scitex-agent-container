"""PS-204 mirror coverage for ``_listen/_node_channel_forwarders.py``.

The end-to-end transport-selector behaviour (HTTP leg, ssh-shim leg,
ACL pass-through, cross-group grant) lives in
``test_server.py`` alongside the existing WI-4 cross-host suite —
that's the canonical mirror for the entire cross-host stack and the
five new ADR-0015 Stage-2 tests are appended there per the lead's
PS-204 directive.

This file holds the focused per-module probes that don't fit the
end-to-end harness: missing-port surface + missing-peer-token surface,
both as direct calls on ``_forward_to_remote``. Same single-assertion
shape as the rest of the package (PA-307).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import textwrap
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from starlette.responses import JSONResponse

from scitex_agent_container._listen import _node_channel_forwarders as _fwd
from scitex_agent_container._listen._node_channel_forwarders import (
    _forward_to_remote,
    _forward_via_ssh_curl,
)
from scitex_agent_container._listen.peer_tokens import write_peer_token
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._state import registry as _reg


@pytest.fixture
def isolated_home(tmp_path: Path):
    """Pin HOME / registry / runtime so the forwarder's peer-token
    lookup hits an empty registry under tmp_path."""
    saved_home = os.environ.get("HOME")
    saved_reg = _reg.REGISTRY_DIR
    saved_ss = _ss.DEFAULT_STATE_ROOT
    os.environ["HOME"] = str(tmp_path)
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"
    try:
        yield tmp_path
    finally:
        _reg.REGISTRY_DIR = saved_reg
        _ss.DEFAULT_STATE_ROOT = saved_ss
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home


def _read_json_body(resp: JSONResponse) -> dict:
    return json.loads(resp.body.decode("utf-8"))


def test_forward_to_remote_502s_when_target_port_is_missing(isolated_home):
    # Arrange
    body = {"method": "SendMessage", "params": {}}
    # Act
    resp = asyncio.run(
        _forward_to_remote(
            request=None,
            body=body,
            target_host="host-a",
            target_port=None,
            target_name="alice",
        )
    )
    # Assert
    assert resp.status_code == 502


def test_forward_to_remote_502_body_names_missing_a2a_port_field(isolated_home):
    # Arrange
    body = {"method": "SendMessage", "params": {}}
    # Act
    resp = asyncio.run(
        _forward_to_remote(
            request=None,
            body=body,
            target_host="host-a",
            target_port=None,
            target_name="alice",
        )
    )
    # Assert
    assert "missing a2a_port" in _read_json_body(resp).get("error", "")


def test_forward_to_remote_502_when_peer_token_file_is_absent(isolated_home):
    # Arrange — no peer-tokens written under HOME, so the loud-502
    # branch fires before any transport selector runs.
    body = {"method": "SendMessage", "params": {}}
    # Act
    resp = asyncio.run(
        _forward_to_remote(
            request=None,
            body=body,
            target_host="host-z",
            target_port=9999,
            target_name="alice",
        )
    )
    # Assert
    assert resp.status_code == 502


# --- ADR-0015 Stage-2 error-path coverage for _forward_via_ssh_curl ----------
#
# These tests drive _forward_via_ssh_curl directly with an installed
# subprocess_shim standing in for the real ssh binary. They exercise the
# rc!=0 / empty-stdout / non-JSON / ACL-error / non-ACL-error / bearer-
# validation branches that the loopback ssh_http_shim happy-path tests
# don't reach, so codecov/patch >= 80% on the diff.


def test_forward_via_ssh_curl_502s_when_ssh_shim_returns_nonzero_rc(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=1, stdout="", stderr="connect refused")
    body = {"method": "SendMessage", "params": {}}
    # Act
    resp = asyncio.run(
        _forward_via_ssh_curl(
            target_host="host-z",
            target_port=9999,
            target_name="alice",
            body=body,
            peer_bearer="abc",
            ssh_target="ssh-host-z",
        )
    )
    # Assert
    assert resp.status_code == 502


def test_forward_via_ssh_curl_502_body_carries_err_tail_from_stderr(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=255, stdout="", stderr="permission denied")
    body = {"method": "SendMessage", "params": {}}
    # Act
    resp = asyncio.run(
        _forward_via_ssh_curl(
            target_host="host-z",
            target_port=9999,
            target_name="alice",
            body=body,
            peer_bearer="abc",
            ssh_target="ssh-host-z",
        )
    )
    # Assert
    assert "permission denied" in _read_json_body(resp).get("error", "")


def test_forward_via_ssh_curl_200s_when_ssh_shim_emits_empty_stdout(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=0, stdout="", stderr="")
    body = {"method": "SendMessage", "params": {}}
    # Act
    resp = asyncio.run(
        _forward_via_ssh_curl(
            target_host="host-z",
            target_port=9999,
            target_name="alice",
            body=body,
            peer_bearer="abc",
            ssh_target="ssh-host-z",
        )
    )
    # Assert
    assert resp.status_code == 200


def test_forward_via_ssh_curl_200s_when_ssh_shim_emits_non_json_stdout(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=0, stdout="not-json banner output", stderr="")
    body = {"method": "SendMessage", "params": {}}
    # Act
    resp = asyncio.run(
        _forward_via_ssh_curl(
            target_host="host-z",
            target_port=9999,
            target_name="alice",
            body=body,
            peer_bearer="abc",
            ssh_target="ssh-host-z",
        )
    )
    # Assert
    assert "forwarded_body_text" in _read_json_body(resp)


def test_forward_via_ssh_curl_403s_when_destination_body_carries_acl_error(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    acl_body = json.dumps({"error": "ACL deny: cross-group send"})
    subprocess_shim.install("ssh", exit=0, stdout=acl_body, stderr="")
    body = {"method": "SendMessage", "params": {}}
    # Act
    resp = asyncio.run(
        _forward_via_ssh_curl(
            target_host="host-z",
            target_port=9999,
            target_name="alice",
            body=body,
            peer_bearer="abc",
            ssh_target="ssh-host-z",
        )
    )
    # Assert
    assert resp.status_code == 403


def test_forward_via_ssh_curl_502s_when_destination_body_carries_non_acl_error(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    non_acl_body = json.dumps({"error": "internal server failure"})
    subprocess_shim.install("ssh", exit=0, stdout=non_acl_body, stderr="")
    body = {"method": "SendMessage", "params": {}}
    # Act
    resp = asyncio.run(
        _forward_via_ssh_curl(
            target_host="host-z",
            target_port=9999,
            target_name="alice",
            body=body,
            peer_bearer="abc",
            ssh_target="ssh-host-z",
        )
    )
    # Assert
    assert resp.status_code == 502


def test_forward_via_ssh_curl_502s_when_helper_raises_value_error(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange — a NEWLINE in the bearer trips the helper's input validation
    # before any subprocess call; mapped to 502 by the forwarder.
    #
    # This used to use a single-quote bearer, which the helper rejected
    # because the value was spliced into a single-quoted shell literal. It no
    # longer is: the token now rides ssh stdin and never reaches an argv, so a
    # quote is harmless and refusing it would be cargo-cult. What the new
    # transport genuinely cannot carry is a newline — the token is the FIRST
    # LINE of the framed stdin, so one would spill into the request body.
    # The forwarder behaviour under test (ValueError -> 502) is unchanged;
    # only the input that legitimately provokes it moved.
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=0, stdout="{}", stderr="")
    body = {"method": "SendMessage", "params": {}}
    # Act
    resp = asyncio.run(
        _forward_via_ssh_curl(
            target_host="host-z",
            target_port=9999,
            target_name="alice",
            body=body,
            peer_bearer="line-one\nline-two",
            ssh_target="ssh-host-z",
        )
    )
    # Assert
    assert resp.status_code == 502


# --- Transport selector: the registry is the SSoT, HTTP is test-only ---------
#
# MEASURED 2026-09-02: scitex-compute-01 and scitex-compute-03 have NO
# ~/.scitex/agent-container/config.yaml, so ``host_config.load().peers`` is
# ``{}`` there and every cross-host send silently took the plain-HTTP leg —
# which cannot connect to a fleet host (agents bind 127.0.0.1) — and died with
# "All connection attempts failed". ``sac host probe`` on the same box already
# resolved the destination through the scitex-dev host registry's
# ``ssh_alias``. These tests pin the forwarder to that same SSoT.
#
# NO MOCKS: a real ``hosts.yaml`` at the real ``$SCITEX_DIR/dev/`` location
# (the same seam ``test__peer_resolve.py`` drives), a real absent/malformed
# ``config.yaml``, real peer-token files, the ``ssh`` shim on ``$PATH`` so the
# production ``subprocess.run`` records the argv it was handed, and — for the
# "HTTP is never attempted" claim — a real listener on 127.0.0.1 counting what
# ARRIVES, because a claim about arrival is settled at the receiving end.

_REGISTRY_ONLY_HOSTS_YAML = textwrap.dedent(
    """\
    hosts:
      scitex-compute-03:
        kind: workstation
        ssh_alias: compute-03-via-registry
        scitex_root: "~/.scitex"
      scitex-laptop-02:
        kind: workstation
        ssh_alias: null
        scitex_root: "~/.scitex"
    """
)

_BODY = {"method": "SendMessage", "params": {}}


@pytest.fixture
def registry_only_peers(isolated_home: Path, env_save_restore):
    """The measured compute-01 / compute-03 topology: a host registry with
    ssh aliases and NO config.yaml at all.

    ``SCITEX_AGENT_CONTAINER_CONFIG`` points at a file that does not
    exist (``host_config.load`` is missing-tolerant → ``peers == {}``);
    ``SCITEX_DIR`` points at a real ``dev/hosts.yaml`` written here, which
    is the location both the ``scitex_dev.hosts`` port and sac's degraded
    YAML reader resolve. Peer tokens are seeded for every host a test
    below targets so the bearer lookup (which runs FIRST) never masks
    the transport decision under test.
    """
    scitex_dir = isolated_home / "scitex-dir"
    (scitex_dir / "dev").mkdir(parents=True)
    (scitex_dir / "dev" / "hosts.yaml").write_text(_REGISTRY_ONLY_HOSTS_YAML)
    env_save_restore.set("SCITEX_DIR", str(scitex_dir))
    env_save_restore.delete("SCITEX_DEV_HOSTS_YAML")
    env_save_restore.set(
        "SCITEX_AGENT_CONTAINER_CONFIG", str(isolated_home / "absent-config.yaml")
    )
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(isolated_home / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    for host in (
        "scitex-compute-03",
        "scitex-laptop-02",
        "scitex-compute-02",
        "unrouted-host-z",
        "127.0.0.1",
        "host-a",
    ):
        write_peer_token(peer_host=host, token=f"bearer-for-{host}")
    return scitex_dir


def _forward(target_host: str, target_port: int = 19008) -> JSONResponse:
    return asyncio.run(
        _forward_to_remote(
            request=None,
            body=_BODY,
            target_host=target_host,
            target_port=target_port,
            target_name="scitex-hub",
        )
    )


@contextlib.contextmanager
def _loopback_json_server(payload: dict):
    """A real HTTP server on 127.0.0.1 answering every POST with ``payload``.

    Yields ``(port, hits)``; ``hits`` gains one entry per POST that
    actually arrived, so a test can assert on delivery — or its absence
    — at the receiving end.
    """
    hits: list[str] = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 — http.server's fixed method name
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            hits.append(self.path)
            data = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):  # keep pytest output clean
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# (a) absent from raw config, present in the registry → ssh leg, registry alias


def test_forward_to_remote_dials_the_registry_alias_when_config_yaml_is_absent(
    registry_only_peers, subprocess_shim
):
    # Arrange
    subprocess_shim.install("ssh", exit=0, stdout="{}", stderr="")
    # Act
    _forward("scitex-compute-03")
    # Assert — the ssh host argument sits right before the remote curl snippet
    assert subprocess_shim.argv_for("ssh")[-2] == "compute-03-via-registry"


def test_forward_to_remote_returns_the_ssh_legs_response_for_a_registry_host(
    registry_only_peers, subprocess_shim
):
    # Arrange
    subprocess_shim.install("ssh", exit=0, stdout='{"ok": true}', stderr="")
    # Act
    resp = _forward("scitex-compute-03")
    # Assert
    assert (resp.status_code, _read_json_body(resp)) == (200, {"ok": True})


# (b) absent from both → loud 502, HTTP never attempted


def test_forward_to_remote_502s_for_a_host_in_neither_config_nor_registry(
    registry_only_peers, subprocess_shim
):
    # Arrange
    subprocess_shim.install("ssh", exit=0, stdout="{}", stderr="")
    # Act
    resp = _forward("unrouted-host-z")
    # Assert
    assert resp.status_code == 502


def test_forward_to_remote_refusal_names_the_host_and_the_loopback_bind(
    registry_only_peers, subprocess_shim
):
    # Arrange
    subprocess_shim.install("ssh", exit=0, stdout="{}", stderr="")
    # Act
    error = _read_json_body(_forward("unrouted-host-z"))["error"]
    # Assert
    assert "'unrouted-host-z'" in error and "bind 127.0.0.1" in error


def test_forward_to_remote_refusal_names_both_fixes(
    registry_only_peers, subprocess_shim
):
    # Arrange
    subprocess_shim.install("ssh", exit=0, stdout="{}", stderr="")
    # Act
    error = _read_json_body(_forward("unrouted-host-z"))["error"]
    # Assert — the registry fix AND the config.yaml fix on THIS host
    assert (
        "ssh_alias" in error
        and "hosts.yaml" in error
        and ("peers: {unrouted-host-z: {ssh: <alias>}}" in error)
    )


def test_forward_to_remote_refusal_names_the_config_path_this_host_resolved(
    registry_only_peers, subprocess_shim, isolated_home
):
    # Arrange
    subprocess_shim.install("ssh", exit=0, stdout="{}", stderr="")
    # Act
    error = _read_json_body(_forward("unrouted-host-z"))["error"]
    # Assert
    assert str(isolated_home / "absent-config.yaml") in error


def test_forward_to_remote_never_posts_http_to_an_unroutable_host(
    registry_only_peers, subprocess_shim
):
    """Settled at the RECEIVING end. ``127.0.0.1`` as the destination
    label is neither a peer nor a ``host-*`` alias, so the pre-fix HTTP
    leg would have posted straight into this listener; the refusal must
    leave it untouched."""
    # Arrange
    subprocess_shim.install("ssh", exit=0, stdout="{}", stderr="")
    with _loopback_json_server({"reached": True}) as (port, hits):
        # Act
        resp = _forward("127.0.0.1", target_port=port)
    # Assert
    assert (resp.status_code, hits) == (502, [])


def test_forward_to_remote_does_not_dial_ssh_for_an_unroutable_host(
    registry_only_peers, subprocess_shim
):
    # Arrange
    subprocess_shim.install("ssh", exit=0, stdout="{}", stderr="")
    # Act
    _forward("unrouted-host-z")
    # Assert
    assert subprocess_shim.call_count("ssh") == 0


def test_forward_to_remote_502s_for_a_registry_row_without_an_ssh_alias(
    registry_only_peers, subprocess_shim
):
    """A registry row with ``ssh_alias: null`` (inbound ssh impossible) is
    NOT a route — it must refuse, never pass through to HTTP."""
    # Arrange
    subprocess_shim.install("ssh", exit=0, stdout="{}", stderr="")
    # Act
    resp = _forward("scitex-laptop-02")
    # Assert — the REFUSAL, not the HTTP leg's own connection failure
    assert (
        resp.status_code,
        "was not attempted" in _read_json_body(resp)["error"],
    ) == (
        502,
        True,
    )


def test_forward_to_remote_warns_once_per_unroutable_host(
    registry_only_peers, subprocess_shim, caplog
):
    # Arrange — a host this process has never refused, then two sends to it
    host = f"unrouted-{uuid.uuid4().hex[:8]}"
    write_peer_token(peer_host=host, token="bearer-for-once")
    subprocess_shim.install("ssh", exit=0, stdout="{}", stderr="")
    caplog.set_level(logging.WARNING, logger=_fwd.__name__)
    # Act
    _forward(host)
    _forward(host)
    # Assert
    warnings = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.WARNING and f"'{host}'" in rec.getMessage()
    ]
    assert len(warnings) == 1


# (c) the ``host-*`` test-loopback alias still takes the HTTP leg


def test_forward_to_remote_posts_http_to_loopback_for_the_host_a_test_alias(
    registry_only_peers, subprocess_shim
):
    # Arrange — a real loopback listener stands in for "host-a"
    subprocess_shim.install("ssh", exit=0, stdout="{}", stderr="")
    with _loopback_json_server({"forwarded": "via-http"}) as (port, hits):
        # Act
        resp = _forward("host-a", target_port=port)
    # Assert
    assert (resp.status_code, _read_json_body(resp), hits) == (
        200,
        {"forwarded": "via-http"},
        ["/agents/scitex-hub/message:send"],
    )


def test_is_test_loopback_alias_accepts_only_host_dash_labels():
    # Arrange
    probes = ("host-a", "host-b", "host-zz", "scitex-compute-03", "hosta", "")
    # Act
    verdicts = tuple(_fwd._is_test_loopback_alias(p) for p in probes)
    # Assert
    assert verdicts == (True, True, True, False, False, False)


# (d) config.yaml glob keys keep resolving through the merged map


def test_forward_to_remote_resolves_a_config_glob_key_through_the_merged_map(
    registry_only_peers, subprocess_shim, env_save_restore, isolated_home
):
    # Arrange — a pattern key with blank ssh: PeersMap synthesises ssh=<name>
    cfg = isolated_home / "glob-config.yaml"
    cfg.write_text("peers:\n  'scitex-compute-*': {}\n")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
    subprocess_shim.install("ssh", exit=0, stdout="{}", stderr="")
    # Act
    _forward("scitex-compute-02")
    # Assert
    assert subprocess_shim.argv_for("ssh")[-2] == "scitex-compute-02"


# (e) a malformed config.yaml must not disable forwarding — the registry routes


def test_forward_to_remote_still_routes_via_registry_when_config_yaml_is_malformed(
    registry_only_peers, subprocess_shim, env_save_restore, isolated_home
):
    # Arrange — ``peers:`` as a list makes host_config.load raise ValueError
    cfg = isolated_home / "broken-config.yaml"
    cfg.write_text("peers:\n  - not\n  - a\n  - mapping\n")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
    subprocess_shim.install("ssh", exit=0, stdout="{}", stderr="")
    # Act
    _forward("scitex-compute-03")
    # Assert
    assert subprocess_shim.argv_for("ssh")[-2] == "compute-03-via-registry"


def test_forward_to_remote_refusal_carries_the_config_parse_failure(
    registry_only_peers, subprocess_shim, env_save_restore, isolated_home
):
    # Arrange
    cfg = isolated_home / "broken-config.yaml"
    cfg.write_text("peers:\n  - not\n  - a\n  - mapping\n")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
    subprocess_shim.install("ssh", exit=0, stdout="{}", stderr="")
    # Act
    error = _read_json_body(_forward("unrouted-host-z"))["error"]
    # Assert
    assert "broken-config.yaml failed to load" in error
