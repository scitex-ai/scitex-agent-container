"""The LOCAL guards of ``sac accounts keepalive`` — every refusal lives here.

Split out of :mod:`.token_keepalive` (512-line cap) along a real seam: this
module answers the questions that need no peer — am I the origin, does this
payload carry refresh material, is there enough validity left, would this
downgrade a working remote credential — while ``token_keepalive`` owns the
ORDER those answers are demanded in. Same convention as
``snapshot_push`` / ``_snapshot_publish`` next door.

Each guard exists because its absence cost a fleet outage on 2026-08-10;
the reasoning is on each function. None of them ever returns a token, and
none of them WRITES anything: note in particular that "does this host hold
refresh material" is answered by the PRESENCE of a field on disk, never by
probing whether the refresh token still works. Probing that is a write —
when a stale refresh token was rejected with 401 that night, Claude Code
CLEARED the refreshToken field outright.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Mapping

from ._rotation_audit import fingerprint_token
from .mint_token import MintError, mint_access_only_artifact

#: Minimum remaining validity, in seconds, below which a push is REFUSED.
#: Inherited verbatim from the shell prototype: a token that dies in
#: flight is worse than no push, because it looks like a fix.
MIN_VALIDITY_S = 300

#: Key names that mean "refresh material" in any credential dialect seen so
#: far. The payload is scanned for these at EVERY depth before it is sent.
_REFRESH_KEYS = frozenset({"refreshtoken", "refresh_token"})


class KeepaliveError(RuntimeError):
    """A keepalive push was REFUSED or could not be completed and verified.

    Always names the account, the peer or the file. NEVER carries token
    material.
    """


def find_refresh_keys(payload: Any, *, path: str = "") -> list[str]:
    """Return the dotted paths of every refresh-material key in ``payload``.

    Recursive on purpose: the guard must not depend on the payload's
    nesting shape, because the shape is exactly what changed between the
    ``.credentials.json`` dialects involved in the incident.
    """
    found: list[str] = []
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            here = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and key.lower() in _REFRESH_KEYS:
                found.append(here)
            found.extend(find_refresh_keys(value, path=here))
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            found.extend(find_refresh_keys(value, path=f"{path}[{index}]"))
    return found


def assert_access_only(payload: Any, *, account: str, peer: str) -> None:
    """Refuse a payload that still carries refresh material. Fail loud.

    ``mint_access_only_artifact`` strips the refresh token by design, so
    this guard should never fire — which is precisely why it is here. The
    one defect this command exists to prevent is refresh material reaching
    a second host, and a guard that only runs when the stripper is correct
    guards nothing.
    """
    offenders = find_refresh_keys(payload)
    if offenders:
        raise KeepaliveError(
            f"refusing to push account '{account}' to peer '{peer}': the "
            f"payload still carries refresh material at "
            f"{', '.join(sorted(offenders))}. Cloning a refreshToken onto a "
            "second host is the defect this command exists to prevent — an "
            "OAuth refresh rotates it, so whichever host refreshes first "
            "silently revokes the other. Nothing was sent."
        )


def holds_refresh_material(
    account: str,
    *,
    store_dir: Path | None = None,
    home: Path | None = None,
) -> bool:
    """Does THIS host's stored credential for ``account`` carry a refresh token?

    The one-bit test for "am I the origin". Exactly one host in the fleet
    should answer ``True`` for a given session; every other host holds an
    ACCESS-ONLY copy and has nothing to fan out. Only the PRESENCE of the
    value is read — never the value, and never its validity (checking
    whether a refresh token still works is a write, not a read).
    """
    from .._state.account_store import _store_path
    from .claude_usage import _read_tokens_at

    _home = home if home is not None else Path.home()
    creds = _store_path(store_dir, _home) / account / ".credentials.json"
    if not creds.is_file():
        return False
    _access, refresh, _client_id, _expires = _read_tokens_at(creds)
    return bool(refresh)


def refresh_holder_accounts(
    *,
    store_dir: Path | None = None,
    home: Path | None = None,
) -> list[str]:
    """Every stored account THIS host holds refresh material for, sorted.

    On the fleet's single refresh holder this is the exact set that should
    fan out; on an access-only replica it is empty, which is how the
    ``--all`` form recognises that it is running on the wrong host.
    """
    from .._state.account_store import list_accounts

    _home = home if home is not None else Path.home()
    names = [str(a.get("name", "")) for a in list_accounts(store_dir, _home)]
    return sorted(
        name
        for name in names
        if name and holds_refresh_material(name, store_dir=store_dir, home=_home)
    )


def assert_is_refresh_holder(
    account: str,
    *,
    peer: str,
    store_dir: Path | None = None,
    home: Path | None = None,
) -> None:
    """Refuse to fan out from a host that is itself an access-only replica.

    Distribution has a DIRECTION: refresh holder → everyone else. A replica
    re-pushing its own borrowed token is at best pointless and at worst
    papers over the fact that nothing is refreshing anywhere, which is the
    silent shape of the outage this command exists to end.

    This is also the sac-side mitigation for a real gap: ``scitex_dev``'s
    ``JobSpec`` has NO host-pinning field, so a scheduled keepalive cannot
    declare "only on the refresh holder". It can only refuse when it wakes
    up somewhere else — which is what this does.
    """
    if not holds_refresh_material(account, store_dir=store_dir, home=home):
        raise KeepaliveError(
            f"refusing to push account '{account}' to peer '{peer}': THIS "
            "host holds no refresh material for that account, so it is an "
            "access-only replica, not the origin. Distribution runs refresh "
            "holder -> replicas, one way. Run this on the host that holds "
            "the refresh token (`sac accounts list` shows the stored "
            "accounts; the holder is the one host that can run `sac accounts "
            "refresh`)."
        )


def seconds_left(expires_at_ms: int | None, now_s: float) -> int:
    """Whole seconds until ``expires_at_ms`` (unix ms). Negative = expired."""
    if not expires_at_ms:
        return 0
    return int(expires_at_ms / 1_000 - now_s)


def build_payload(
    account: str,
    *,
    peer: str,
    min_validity_s: int = MIN_VALIDITY_S,
    store_dir: Path | None = None,
    home: Path | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Read the master's CURRENT token and render the access-only bytes.

    Delegates the read + strip + health gate wholly to
    :func:`~.mint_token.mint_access_only_artifact` (no second stripper
    exists), then applies the two guards that are this command's own: the
    refresh-material scan and the minimum-validity floor.

    Returns ``{"bytes", "expires_at_ms", "seconds_left", "access_fp"}``.
    The bytes are the only place a token value appears, and they are never
    written to a local file.

    Raises:
        KeepaliveError: unknown/unhealthy account, refresh material
            present, or too little validity left to be worth pushing.
    """
    now_s = now if now is not None else time.time()
    try:
        envelope = mint_access_only_artifact(
            account, store_dir=store_dir, home=home, now=now_s
        )
    except MintError as exc:
        raise KeepaliveError(
            f"cannot keep account '{account}' alive on peer '{peer}': {exc}"
        ) from exc

    artifact = envelope["artifact"]
    assert_access_only(artifact, account=account, peer=peer)

    expires_at_ms = int(envelope["meta"]["expires_at"])
    left = seconds_left(expires_at_ms, now_s)
    if left < min_validity_s:
        raise KeepaliveError(
            f"refusing to push account '{account}' to peer '{peer}': the "
            f"master token has {left}s of validity left, under the "
            f"{min_validity_s}s floor. A token that expires in flight is "
            "worse than no push — it looks like the problem was addressed. "
            "Re-run after the master refreshes (`sac accounts refresh`)."
        )

    # stx-allow: STX-IO006 (reason: stdlib json is REQUIRED here, not a
    # shortcut. These bytes are OAuth token material headed for a peer's
    # stdin; routing them through stx.io would mean SERIALISING A SECRET TO
    # A LOCAL FILE, which this module's secrecy contract forbids outright.
    # There is also no provenance to track — the payload is a credential,
    # not a research artifact.)
    rendered = json.dumps(artifact, ensure_ascii=False)  # stx-allow: STX-IO006

    return {
        "bytes": rendered.encode("utf-8"),
        "expires_at_ms": expires_at_ms,
        "seconds_left": left,
        "access_fp": fingerprint_token(artifact["claudeAiOauth"]["accessToken"]),
    }


def assert_not_downgrading(
    state: Mapping[str, Any],
    *,
    account: str,
    peer: str,
    expires_at_ms: int,
    now_s: float,
) -> None:
    """Refuse to replace a STILL-VALID remote credential with a dead one.

    The validity floor already refuses an expired payload, but that floor
    is caller-tunable (``--min-validity 0``). This guard is not: a peer
    whose credential still works must never be knocked over by one that
    does not, whatever the operator passed.
    """
    if state.get("absent"):
        return
    remote_left = seconds_left(state.get("expires_at_ms"), now_s)
    payload_left = seconds_left(expires_at_ms, now_s)
    if payload_left <= 0 < remote_left:
        raise KeepaliveError(
            f"refusing to push account '{account}' to peer '{peer}': the "
            f"payload is already expired ({payload_left}s) while the peer's "
            f"current credential is still valid ({remote_left}s left). "
            "Overwriting a working credential with a dead one would take the "
            "peer down. The peer was left untouched."
        )


__all__ = [
    "MIN_VALIDITY_S",
    "KeepaliveError",
    "assert_access_only",
    "assert_is_refresh_holder",
    "assert_not_downgrading",
    "build_payload",
    "find_refresh_keys",
    "holds_refresh_material",
    "refresh_holder_accounts",
    "seconds_left",
]
