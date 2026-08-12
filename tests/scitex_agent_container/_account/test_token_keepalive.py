"""Tests for ``sac accounts keepalive`` — access-only credential distribution.

The invariant under test is the one the 2026-08-10 fleet outage taught:
exactly ONE host holds refresh material, everyone else holds ACCESS-ONLY
copies, and nothing is ever restarted onto a credential the far side has
not accepted.

No-mocks (PA-306 / STX-NM002). The guards are pure functions driven with
real ``store_dir`` / ``home`` / ``now`` parameters against a real temp
account store. The far-side tests drive the REAL production transport —
the real :class:`SshTransport` renders its argv through the real
``build_ssh_argv`` and the real ``subprocess.run`` resolves ``ssh`` on the
real ``$PATH``; only the NETWORK HOP is replaced, by the ``ssh_exec_shim``
helper that runs the remote command LOCALLY exactly as OpenSSH + sshd
would. The remote ``mkdir`` / ``dd`` / ``chmod`` / ``stat`` / ``cp`` /
``mv`` are the real coreutils on a real tree, and the probe is the REAL
probe, executed by the real ``python3``, making a REAL HTTP request to a
REAL ``http.server`` on loopback.

Distinctive sentinels stand in for token material; every no-leak assertion
searches for those exact bytes.

AAA marker comments; one assertion per test.
"""

from __future__ import annotations

import json
import stat as stat_mod
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scitex_agent_container._account._keepalive_guards import (
    KeepaliveError,
    assert_access_only,
    assert_is_refresh_holder,
    assert_not_downgrading,
    build_payload,
    find_refresh_keys,
    holds_refresh_material,
    refresh_holder_accounts,
)
from scitex_agent_container._account._keepalive_remote import (
    backup_remote,
    ensure_remote_dir,
    install_probe,
    probe_source,
    read_remote_state,
    remove_probe,
    verify_remote_token,
)
from scitex_agent_container._account._snapshot_publish import (
    IN_PLACE,
    publish_verified,
)
from scitex_agent_container._account.snapshot_push import (
    SnapshotPushError,
    resolve_peer_transport,
)
from scitex_agent_container._account.token_keepalive import keepalive_push

_ACCESS = "ACCESS-TOKEN-MUST-NEVER-BE-PRINTED"
_REFRESH = "REFRESH-TOKEN-MUST-NEVER-BE-PRINTED"
_LABEL = "alpha-example-com"
_PEER = "spartan"

# A WHOLE-second `now`. `seconds_left` truncates, so a fractional clock makes
# a "120s left" fixture report 119 — an off-by-one that says nothing about
# the code under test. Pinning the second keeps the reported figure exact.
_WHOLE_NOW = float(int(time.time()))

# A `mv` that fails the way a bind-mounted destination does: rename onto a
# mount point is EBUSY. Real program, real exit status, real stderr — the
# same honest-replacement technique the snapshot-push tests use for a
# lying `chmod`.
_EBUSY_MV = """#!/bin/sh
echo "mv: cannot move: Device or resource busy" >&2
exit 1
"""


class _Handler(BaseHTTPRequestHandler):
    """Answers with whatever status the server was told to answer with."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.send_response(self.server.reply_status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args) -> None:
        return


@pytest.fixture
def api(request):
    """A REAL HTTP server on loopback standing in for the Anthropic API."""
    status = getattr(request, "param", 200)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.reply_status = status
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1/models"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def peer_config(tmp_path: Path, env_save_restore) -> Path:
    """A real config.yaml, pinned with the env var sac's peer lookup reads."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("peers:\n  spartan:\n    ssh: ywatanabe@spartan-login1\n")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
    return cfg


def _write_account(
    store: Path, label: str, *, expires_at_ms: int, refresh: str | None = _REFRESH
) -> Path:
    """Materialise a stored account. ``refresh=None`` = an access-only replica."""
    acct = store / label
    acct.mkdir(parents=True, exist_ok=True)
    oauth = {"accessToken": _ACCESS, "expiresAt": expires_at_ms, "scopes": ["a"]}
    if refresh is not None:
        oauth["refreshToken"] = refresh
    path = acct / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": oauth}), encoding="utf-8")
    (acct / "account.json").write_text(json.dumps({"name": label}), encoding="utf-8")
    return path


@pytest.fixture
def store(tmp_path: Path) -> Path:
    d = tmp_path / "accounts"
    d.mkdir()
    return d


def _payload_error(store: Path, home: Path, **kwargs) -> KeepaliveError:
    """Build a payload that MUST be refused; return the raised error."""
    try:
        build_payload(_LABEL, peer=_PEER, store_dir=store, home=home, **kwargs)
    except KeepaliveError as exc:
        return exc
    raise AssertionError("expected build_payload to refuse")


def _transport():
    return resolve_peer_transport(_PEER)


def _access_only_error(payload) -> KeepaliveError:
    """Run a guard that MUST refuse; return the error so the test asserts once."""
    try:
        assert_access_only(payload, account=_LABEL, peer=_PEER)
    except KeepaliveError as exc:
        return exc
    raise AssertionError("expected assert_access_only to refuse")


def _downgrade_error(state, expires_at_ms: int, now_s: float) -> KeepaliveError:
    """Run the downgrade guard that MUST refuse; return the error."""
    try:
        assert_not_downgrading(
            state,
            account=_LABEL,
            peer=_PEER,
            expires_at_ms=expires_at_ms,
            now_s=now_s,
        )
    except KeepaliveError as exc:
        return exc
    raise AssertionError("expected assert_not_downgrading to refuse")


def _holder_error(store: Path, home: Path) -> KeepaliveError:
    """Run the origin guard that MUST refuse; return the error."""
    try:
        assert_is_refresh_holder(_LABEL, peer=_PEER, store_dir=store, home=home)
    except KeepaliveError as exc:
        return exc
    raise AssertionError("expected assert_is_refresh_holder to refuse")


def _verify_error(transport, probe: str, remote: str) -> SnapshotPushError:
    """Run a verification that MUST fail; return the error."""
    try:
        verify_remote_token(transport, probe, remote)
    except SnapshotPushError as exc:
        return exc
    raise AssertionError("expected verify_remote_token to fail loud")


# ---------------------------------------------------------------------------
# REFUSAL: refresh material in the payload
# ---------------------------------------------------------------------------


def test_refresh_key_is_found_at_any_depth() -> None:
    # Arrange — the incident's own shape: refresh material nested one level
    # down, which a top-level `in` check would miss entirely.
    payload = {"claudeAiOauth": {"accessToken": _ACCESS, "refreshToken": _REFRESH}}
    # Act
    found = find_refresh_keys(payload)
    # Assert
    assert found == ["claudeAiOauth.refreshToken"]


def test_snake_case_refresh_key_is_also_found() -> None:
    # Arrange — the other dialect seen on disk.
    payload = {"claudeAiOauth": {"refresh_token": _REFRESH}}
    # Act
    found = find_refresh_keys(payload)
    # Assert
    assert found == ["claudeAiOauth.refresh_token"]


def test_refresh_bearing_payload_is_refused() -> None:
    # Arrange
    payload = {"claudeAiOauth": {"accessToken": _ACCESS, "refreshToken": _REFRESH}}
    # Act
    error = _access_only_error(payload)
    # Assert — the message says WHY, not just that it refused.
    assert "Cloning a refreshToken onto a second host" in str(error)


def test_refusal_message_names_the_offending_key_path() -> None:
    # Arrange
    payload = {"claudeAiOauth": {"accessToken": _ACCESS, "refreshToken": _REFRESH}}
    # Act
    error = _access_only_error(payload)
    # Assert — a failure must name what is wrong, never just that it is.
    assert "claudeAiOauth.refreshToken" in str(error)


def test_refusal_message_never_carries_the_refresh_token() -> None:
    # Arrange
    payload = {"claudeAiOauth": {"accessToken": _ACCESS, "refreshToken": _REFRESH}}
    # Act
    error = _access_only_error(payload)
    # Assert
    assert _REFRESH not in str(error)


def test_access_only_payload_passes_the_guard() -> None:
    # Arrange — what `mint_access_only_artifact` actually produces.
    payload = {"claudeAiOauth": {"accessToken": _ACCESS, "expiresAt": 1}}
    # Act
    result = assert_access_only(payload, account=_LABEL, peer=_PEER)
    # Assert
    assert result is None


# ---------------------------------------------------------------------------
# REFUSAL: an expired payload, and too little validity left
# ---------------------------------------------------------------------------


def test_expired_master_token_is_refused(store, tmp_path) -> None:
    # Arrange — a snapshot whose expiry is an hour in the past.
    now = time.time()
    _write_account(store, _LABEL, expires_at_ms=int((now - 3600) * 1000))
    # Act
    error = _payload_error(store, tmp_path, now=now)
    # Assert
    assert "unhealthy" in str(error)


def test_expired_refusal_names_the_account_and_peer(store, tmp_path) -> None:
    # Arrange
    now = time.time()
    _write_account(store, _LABEL, expires_at_ms=int((now - 3600) * 1000))
    # Act
    error = _payload_error(store, tmp_path, now=now)
    # Assert — a failure must name the file or the host, never go silent.
    assert _LABEL in str(error) and _PEER in str(error)


def test_too_little_validity_is_refused(store, tmp_path) -> None:
    # Arrange — 120s left, under the 300s floor. Still VALID, so this is a
    # different refusal from the expired one above.
    now = _WHOLE_NOW
    _write_account(store, _LABEL, expires_at_ms=int((now + 120) * 1000))
    # Act
    error = _payload_error(store, tmp_path, now=now)
    # Assert
    assert "under the 300s floor" in str(error)


def test_too_little_validity_message_quotes_the_seconds_left(store, tmp_path) -> None:
    # Arrange — a whole-second `now`, so the reported figure is exact rather
    # than one short from truncating a fractional remainder.
    now = _WHOLE_NOW
    _write_account(store, _LABEL, expires_at_ms=int((now + 120) * 1000))
    # Act
    error = _payload_error(store, tmp_path, now=now)
    # Assert — seconds are safe to print; tokens are not.
    assert "120s of validity left" in str(error)


def test_ample_validity_is_accepted(store, tmp_path) -> None:
    # Arrange — an hour left, comfortably over the floor.
    now = _WHOLE_NOW
    _write_account(store, _LABEL, expires_at_ms=int((now + 3600) * 1000))
    # Act
    payload = build_payload(_LABEL, peer=_PEER, store_dir=store, home=tmp_path, now=now)
    # Assert
    assert payload["seconds_left"] == 3600


def test_built_payload_carries_no_refresh_material(store, tmp_path) -> None:
    # Arrange
    now = time.time()
    _write_account(store, _LABEL, expires_at_ms=int((now + 3600) * 1000))
    # Act — the bytes that would actually go on the wire.
    payload = build_payload(_LABEL, peer=_PEER, store_dir=store, home=tmp_path, now=now)
    # Assert
    assert _REFRESH.encode() not in payload["bytes"]


def test_built_payload_fingerprint_is_opaque(store, tmp_path) -> None:
    # Arrange
    now = time.time()
    _write_account(store, _LABEL, expires_at_ms=int((now + 3600) * 1000))
    # Act
    payload = build_payload(_LABEL, peer=_PEER, store_dir=store, home=tmp_path, now=now)
    # Assert — a fingerprint, never a substring of the token.
    assert (
        payload["access_fp"].startswith("sha256:")
        and _ACCESS not in (payload["access_fp"])
    )


# ---------------------------------------------------------------------------
# REFUSAL: overwriting a valid remote credential with a dead one
# ---------------------------------------------------------------------------


def test_expired_payload_over_valid_remote_is_refused() -> None:
    # Arrange — the peer still works; the payload does not.
    now = time.time()
    state = {"absent": False, "expires_at_ms": int((now + 3600) * 1000)}
    # Act
    error = _downgrade_error(state, int((now - 60) * 1000), now)
    # Assert
    assert "left untouched" in str(error)


def test_downgrade_refusal_is_not_tunable_away() -> None:
    # Arrange — this guard runs BELOW --min-validity, so it must still fire
    # for a caller who lowered the floor to zero.
    now = time.time()
    state = {"absent": False, "expires_at_ms": int((now + 3600) * 1000)}
    # Act
    error = _downgrade_error(state, int((now - 1) * 1000), now)
    # Assert
    assert "still valid" in str(error)


def test_fresh_payload_over_valid_remote_is_allowed() -> None:
    # Arrange
    now = time.time()
    state = {"absent": False, "expires_at_ms": int((now + 60) * 1000)}
    # Act
    result = assert_not_downgrading(
        state,
        account=_LABEL,
        peer=_PEER,
        expires_at_ms=int((now + 3600) * 1000),
        now_s=now,
    )
    # Assert
    assert result is None


def test_absent_remote_is_not_a_downgrade() -> None:
    # Arrange — first push to a peer that has nothing yet.
    now = time.time()
    # Act
    result = assert_not_downgrading(
        {"absent": True, "expires_at_ms": None},
        account=_LABEL,
        peer=_PEER,
        expires_at_ms=int((now + 3600) * 1000),
        now_s=now,
    )
    # Assert
    assert result is None


# ---------------------------------------------------------------------------
# REFUSAL: fanning out from a host that is itself an access-only replica
# ---------------------------------------------------------------------------


def test_replica_cannot_fan_out(store, tmp_path) -> None:
    # Arrange — an access-only copy: no refreshToken on disk.
    _write_account(
        store, _LABEL, expires_at_ms=int((time.time() + 3600) * 1000), refresh=None
    )
    # Act
    error = _holder_error(store, tmp_path)
    # Assert
    assert "access-only replica, not the origin" in str(error)


def test_refresh_holder_may_fan_out(store, tmp_path) -> None:
    # Arrange
    _write_account(store, _LABEL, expires_at_ms=int((time.time() + 3600) * 1000))
    # Act
    result = assert_is_refresh_holder(
        _LABEL, peer=_PEER, store_dir=store, home=tmp_path
    )
    # Assert
    assert result is None


def test_holder_detection_reads_presence_not_value(store, tmp_path) -> None:
    # Arrange
    _write_account(store, _LABEL, expires_at_ms=int((time.time() + 3600) * 1000))
    # Act
    held = holds_refresh_material(_LABEL, store_dir=store, home=tmp_path)
    # Assert — a bool, never the material.
    assert held is True


def test_all_lists_only_the_accounts_this_host_originates(store, tmp_path) -> None:
    # Arrange — one origin account and one replica account side by side.
    future = int((time.time() + 3600) * 1000)
    _write_account(store, _LABEL, expires_at_ms=future)
    _write_account(store, "beta-example-com", expires_at_ms=future, refresh=None)
    # Act
    holders = refresh_holder_accounts(store_dir=store, home=tmp_path)
    # Assert
    assert holders == [_LABEL]


# ---------------------------------------------------------------------------
# Far side — real ssh argv, real coreutils, real probe, real HTTP
# ---------------------------------------------------------------------------


def test_backup_copies_the_previous_credential(
    tmp_path, peer_config, ssh_exec_shim
) -> None:
    # Arrange
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    remote.parent.mkdir(parents=True)
    remote.write_text("previous")
    # Act
    backup = backup_remote(_transport(), str(remote), stamp="20260810T000000Z")
    # Assert
    assert Path(backup).read_text() == "previous"


def test_backup_is_0600(tmp_path, peer_config, ssh_exec_shim) -> None:
    # Arrange
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    remote.parent.mkdir(parents=True)
    remote.write_text("previous")
    remote.chmod(0o644)
    # Act
    backup = backup_remote(_transport(), str(remote), stamp="20260810T000000Z")
    # Assert — a copy of a credential must not inherit a loose mode.
    assert oct(stat_mod.S_IMODE(Path(backup).stat().st_mode))[2:] == "600"


def test_backup_of_nothing_is_none(tmp_path, peer_config, ssh_exec_shim) -> None:
    # Arrange — first push: the peer has no credential yet.
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    remote.parent.mkdir(parents=True)
    # Act
    backup = backup_remote(_transport(), str(remote), stamp="20260810T000000Z")
    # Assert
    assert backup is None


def test_probe_source_carries_no_token(tmp_path) -> None:
    # Arrange — the probe is a FILE dropped on the peer; it must be inert.
    sentinels = (_ACCESS.encode(), _REFRESH.encode())
    # Act
    source = probe_source()
    # Assert
    assert not any(sentinel in source for sentinel in sentinels)


def test_remote_state_reports_the_expiry(tmp_path, peer_config, ssh_exec_shim) -> None:
    # Arrange
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    remote.parent.mkdir(parents=True)
    remote.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": _ACCESS, "expiresAt": 4242}})
    )
    transport = _transport()
    probe = install_probe(transport, str(remote))
    # Act — the REAL probe, run by the REAL python3.
    state = read_remote_state(transport, probe, str(remote))
    # Assert
    assert state["expires_at_ms"] == 4242


def test_remote_state_flags_a_peer_still_holding_refresh_material(
    tmp_path, peer_config, ssh_exec_shim
) -> None:
    # Arrange — the incident's shape: a cloned session on a follower.
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    remote.parent.mkdir(parents=True)
    remote.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": _ACCESS,
                    "refreshToken": _REFRESH,
                    "expiresAt": 4242,
                }
            }
        )
    )
    transport = _transport()
    probe = install_probe(transport, str(remote))
    # Act
    state = read_remote_state(transport, probe, str(remote))
    # Assert — reported as a fingerprint, never as the value.
    assert state["refresh_fp"].startswith("sha256:")


def test_remote_state_of_a_missing_file_is_absent(
    tmp_path, peer_config, ssh_exec_shim
) -> None:
    # Arrange
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    remote.parent.mkdir(parents=True)
    transport = _transport()
    probe = install_probe(transport, str(remote))
    # Act
    state = read_remote_state(transport, probe, str(remote))
    # Assert
    assert state["absent"] is True


def test_probe_is_removed_from_the_peer(tmp_path, peer_config, ssh_exec_shim) -> None:
    # Arrange
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    remote.parent.mkdir(parents=True)
    transport = _transport()
    probe = install_probe(transport, str(remote))
    # Act
    remove_probe(transport, str(remote))
    # Assert
    assert not Path(probe).exists()


def test_far_side_verification_reports_200(
    tmp_path, peer_config, ssh_exec_shim, api
) -> None:
    # Arrange — a REAL credential file and a REAL HTTP server answering 200.
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    remote.parent.mkdir(parents=True)
    remote.write_text(json.dumps({"claudeAiOauth": {"accessToken": _ACCESS}}))
    transport = _transport()
    probe = install_probe(transport, str(remote), verify_url=api)
    # Act — the real probe makes a real request with the real token.
    status = verify_remote_token(transport, probe, str(remote))
    # Assert
    assert status == 200


@pytest.mark.parametrize("api", [401], indirect=True)
def test_far_side_verification_reports_401(
    tmp_path, peer_config, ssh_exec_shim, api
) -> None:
    # Arrange — the exact failure the fleet saw: a revoked token.
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    remote.parent.mkdir(parents=True)
    remote.write_text(json.dumps({"claudeAiOauth": {"accessToken": _ACCESS}}))
    transport = _transport()
    probe = install_probe(transport, str(remote), verify_url=api)
    # Act
    status = verify_remote_token(transport, probe, str(remote))
    # Assert
    assert status == 401


def test_unreachable_endpoint_fails_loud(tmp_path, peer_config, ssh_exec_shim) -> None:
    # Arrange — nothing is listening; an unverifiable token is a failure,
    # never an assumption.
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    remote.parent.mkdir(parents=True)
    remote.write_text(json.dumps({"claudeAiOauth": {"accessToken": _ACCESS}}))
    transport = _transport()
    probe = install_probe(
        transport, str(remote), verify_url="http://127.0.0.1:1/v1/models"
    )
    # Act
    error = _verify_error(transport, probe, str(remote))
    # Assert
    assert "NOT restarting anything" in str(error)


# ---------------------------------------------------------------------------
# Publish — the bind-mount EBUSY fallback
# ---------------------------------------------------------------------------


def test_bind_mounted_destination_falls_back_in_place(
    tmp_path, peer_config, ssh_exec_shim
) -> None:
    # Arrange — a real `mv` that answers EBUSY, as rename onto a bind mount
    # does. The staged file is already correct; only publishing must adapt.
    ssh_exec_shim.install_binary("mv", _EBUSY_MV)
    remote_dir = tmp_path / "peer" / "acct"
    remote_dir.mkdir(parents=True)
    remote = remote_dir / ".credentials.json"
    staged = Path(str(remote) + ".staged")
    payload = b'{"claudeAiOauth": {"accessToken": "x"}}'
    staged.write_bytes(payload)
    # Act
    method, _mode, _size = publish_verified(
        _LABEL,
        transport=_transport(),
        staged=str(staged),
        remote=str(remote),
        payload=payload,
    )
    # Assert — deliberately in place, not a silent failure.
    assert method == IN_PLACE


def test_in_place_fallback_actually_lands_the_bytes(
    tmp_path, peer_config, ssh_exec_shim
) -> None:
    # Arrange
    ssh_exec_shim.install_binary("mv", _EBUSY_MV)
    remote_dir = tmp_path / "peer" / "acct"
    remote_dir.mkdir(parents=True)
    remote = remote_dir / ".credentials.json"
    staged = Path(str(remote) + ".staged")
    payload = b'{"claudeAiOauth": {"accessToken": "x"}}'
    staged.write_bytes(payload)
    # Act
    publish_verified(
        _LABEL,
        transport=_transport(),
        staged=str(staged),
        remote=str(remote),
        payload=payload,
    )
    # Assert
    assert remote.read_bytes() == payload


def test_in_place_fallback_still_lands_0600(
    tmp_path, peer_config, ssh_exec_shim
) -> None:
    # Arrange
    ssh_exec_shim.install_binary("mv", _EBUSY_MV)
    remote_dir = tmp_path / "peer" / "acct"
    remote_dir.mkdir(parents=True)
    remote = remote_dir / ".credentials.json"
    staged = Path(str(remote) + ".staged")
    payload = b'{"claudeAiOauth": {"accessToken": "x"}}'
    staged.write_bytes(payload)
    # Act
    publish_verified(
        _LABEL,
        transport=_transport(),
        staged=str(staged),
        remote=str(remote),
        payload=payload,
    )
    # Assert — the non-atomic path must not weaken the mode contract.
    assert oct(stat_mod.S_IMODE(remote.stat().st_mode))[2:] == "600"


# ---------------------------------------------------------------------------
# End to end — the whole ordering, against a real peer tree
# ---------------------------------------------------------------------------


def _keepalive(store, home, remote, api_url, **kwargs):
    """Drive the real push with the probe pointed at the local API stand-in."""
    return keepalive_push(
        _LABEL,
        _PEER,
        remote_path=str(remote),
        store_dir=store,
        home=home,
        verify_url=api_url,
        **kwargs,
    )


def _keepalive_error(store, home, remote, api_url) -> KeepaliveError:
    """Run a keepalive that MUST fail; return the error."""
    try:
        _keepalive(store, home, remote, api_url)
    except KeepaliveError as exc:
        return exc
    raise AssertionError("expected keepalive_push to fail loud")


def test_end_to_end_publishes_access_only_bytes(
    store, tmp_path, peer_config, ssh_exec_shim, api
) -> None:
    # Arrange
    _write_account(store, _LABEL, expires_at_ms=int((time.time() + 3600) * 1000))
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    # Act
    _keepalive(store, tmp_path, remote, api)
    # Assert — the refresh token never reached the peer.
    assert _REFRESH not in remote.read_text()


def test_end_to_end_lands_0600(
    store, tmp_path, peer_config, ssh_exec_shim, api
) -> None:
    # Arrange
    _write_account(store, _LABEL, expires_at_ms=int((time.time() + 3600) * 1000))
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    # Act
    _keepalive(store, tmp_path, remote, api)
    # Assert
    assert oct(stat_mod.S_IMODE(remote.stat().st_mode))[2:] == "600"


def test_end_to_end_backs_up_what_it_replaced(
    store, tmp_path, peer_config, ssh_exec_shim, api
) -> None:
    # Arrange — the peer already holds a DIFFERENT credential.
    _write_account(store, _LABEL, expires_at_ms=int((time.time() + 3600) * 1000))
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    remote.parent.mkdir(parents=True)
    remote.write_text(json.dumps({"claudeAiOauth": {"accessToken": "OLD-TOKEN"}}))
    # Act
    record = _keepalive(store, tmp_path, remote, api)
    # Assert
    assert "OLD-TOKEN" in Path(record["backup_path"]).read_text()


def test_end_to_end_converges_and_then_stops_rewriting(
    store, tmp_path, peer_config, ssh_exec_shim, api
) -> None:
    # Arrange — a second run with the master's token unchanged. Measured
    # 2026-08-10: the token does NOT rotate on most ticks, so the second run
    # must be a verified no-op rather than another write + another backup.
    _write_account(store, _LABEL, expires_at_ms=int((time.time() + 3600) * 1000))
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    _keepalive(store, tmp_path, remote, api)
    # Act
    second = _keepalive(store, tmp_path, remote, api)
    # Assert
    assert second["action"] == "already-current"


def test_converged_run_writes_no_second_backup(
    store, tmp_path, peer_config, ssh_exec_shim, api
) -> None:
    # Arrange
    _write_account(store, _LABEL, expires_at_ms=int((time.time() + 3600) * 1000))
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    _keepalive(store, tmp_path, remote, api)
    # Act
    second = _keepalive(store, tmp_path, remote, api)
    # Assert — an every-15-minutes schedule must not bury the peer in backups.
    assert second["backup_path"] is None


def test_changed_master_token_is_pushed(
    store, tmp_path, peer_config, ssh_exec_shim, api
) -> None:
    # Arrange — converge, then rotate the master (as a real refresh would).
    _write_account(store, _LABEL, expires_at_ms=int((time.time() + 3600) * 1000))
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    _keepalive(store, tmp_path, remote, api)
    creds = store / _LABEL / ".credentials.json"
    data = json.loads(creds.read_text())
    data["claudeAiOauth"]["accessToken"] = "ROTATED-ACCESS-TOKEN"
    creds.write_text(json.dumps(data))
    # Act
    record = _keepalive(store, tmp_path, remote, api)
    # Assert — convergence, driven by the fingerprint and not by a clock.
    assert record["action"] == "pushed"


@pytest.mark.parametrize("api", [401], indirect=True)
def test_rejecting_peer_is_a_loud_failure(
    store, tmp_path, peer_config, ssh_exec_shim, api
) -> None:
    # Arrange — the far side refuses the credential we just published.
    _write_account(store, _LABEL, expires_at_ms=int((time.time() + 3600) * 1000))
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    # Act
    error = _keepalive_error(store, tmp_path, remote, api)
    # Assert — nothing may be restarted onto an unverified credential.
    assert "NOT restarting anything" in str(error)


def test_end_to_end_leaves_no_probe_behind(
    store, tmp_path, peer_config, ssh_exec_shim, api
) -> None:
    # Arrange
    _write_account(store, _LABEL, expires_at_ms=int((time.time() + 3600) * 1000))
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    # Act
    _keepalive(store, tmp_path, remote, api)
    # Assert
    assert not (remote.parent / ".sac-keepalive-probe.py").exists()


def test_ensure_remote_dir_hardens_to_0700(
    tmp_path, peer_config, ssh_exec_shim
) -> None:
    # Arrange
    remote = tmp_path / "peer" / "acct" / ".credentials.json"
    # Act
    remote_dir = ensure_remote_dir(_transport(), str(remote))
    # Assert
    assert oct(stat_mod.S_IMODE(Path(remote_dir).stat().st_mode))[2:] == "700"
