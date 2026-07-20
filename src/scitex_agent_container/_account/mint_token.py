"""Master-side ACCESS-ONLY credential minting for host cred-distribution.

This is the *master-side generation primitive* of a master→compute-host
credential-distribution scheme (co-designed with scitex-dev). The master
host mints an **access-only** credential artifact that compute hosts
consume READ-ONLY. The whole point is structural: the artifact carries
the OAuth ``accessToken`` (the distributable) but the ``refreshToken`` is
STRIPPED, so nothing on a compute host can ever trigger the single-use
refresh-token rotation that would invalidate the master's token.

Design rules (security-sensitive)
---------------------------------
1. The ``refreshToken`` MUST NEVER appear in the minted artifact. The
   ``accessToken`` leaves by design (it is the distributable). Neither
   token is ever logged.
2. MINT-ON-DEMAND: the CURRENT stored ``.credentials.json`` is read at
   call time (freshest token). No caching.
3. EXCLUSIVE-STRICT health gate: if the requested account's stored
   credential is UNHEALTHY (expired per
   :func:`_creds._pick_healthy.account_health`), minting FAILS loudly.
   A dead/expired token is NEVER minted.
4. Unknown account label fails loudly, listing available labels.

The consumer-side ``pull-token`` primitive is a SEPARATE later change —
this module only mints.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .._creds._pick_healthy import account_health
from .._state.account_store import _store_path, list_accounts
from .claude_usage import _load_json, _read_tokens_at

#: Structural marker for the artifact kind. The refresh_token is stripped.
_ARTIFACT_KIND = "access-only"
#: Bump when the envelope shape changes; consumers gate on this.
_ARTIFACT_VERSION = 1


class MintError(RuntimeError):
    """Raised when an access-only artifact cannot be minted.

    Carries an operator-facing message (never any token material). The
    CLI renders it to stderr and exits non-zero. Distinct exit paths:
    unknown label, unhealthy (expired) credential, missing accessToken.
    """


def _read_scopes(credentials_path: Path) -> list[str]:
    """Read ``claudeAiOauth.scopes`` (a non-secret list) from disk.

    Best-effort: a missing / malformed field yields an empty list. Only
    the scopes list leaves this helper — no token material is read here.
    """
    data = _load_json(credentials_path)
    if not isinstance(data, dict):
        return []
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return []
    scopes = oauth.get("scopes")
    if not isinstance(scopes, list):
        return []
    return [s for s in scopes if isinstance(s, str)]


def _resolve_master_host(hostname: str | None) -> str:
    """Resolve the master host label stamped into the artifact meta.

    Delegates to :func:`_state.state_db_hostname.resolve_host` — the
    canonical single-name resolver (``$SAC_HOST`` → config.yaml canonical
    → short ``socket.gethostname()``). ``hostname`` is an explicit
    override / test seam.
    """
    from .._state.state_db_hostname import resolve_host

    return resolve_host(hostname)


def mint_access_only_artifact(
    account: str,
    *,
    store_dir: Path | None = None,
    home: Path | None = None,
    hostname: str | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Mint the wrapped ``{artifact, meta}`` access-only envelope.

    Reads the CURRENT stored ``.credentials.json`` for ``account`` (a
    store slug, e.g. ``alpha-example-com``), gates on health, strips the
    refresh_token, and returns the distributable envelope. The returned
    dict is safe to serialise to stdout — it contains the accessToken
    (by design) but NEVER the refreshToken.

    Parameters
    ----------
    account
        The stored-account slug to mint from.
    store_dir, home, hostname, now
        Test seams. ``store_dir=None`` uses the SciTeX local-state
        cascade; ``home=None`` uses ``Path.home()``; ``hostname=None``
        resolves the canonical host; ``now=None`` uses ``time.time()``
        (SECONDS since epoch).

    Raises
    ------
    MintError
        * unknown ``account`` label (message lists available labels),
        * UNHEALTHY (expired/absent) stored credential — never mints a
          dead token,
        * a stored credential with no usable ``accessToken`` / expiry.
    """
    _home = home if home is not None else Path.home()
    now_s = now if now is not None else time.time()

    store = _store_path(store_dir, _home)
    account_dir = store / account
    creds_path = account_dir / ".credentials.json"

    # --- unknown label -----------------------------------------------------
    if not account_dir.is_dir():
        available = [a.get("name", "?") for a in list_accounts(store_dir, _home)]
        avail_str = ", ".join(sorted(available)) if available else "(none)"
        raise MintError(f"unknown account '{account}' — available labels: {avail_str}")

    # --- EXCLUSIVE-STRICT health gate --------------------------------------
    health = account_health(account, store_dir=store_dir, home=_home, now=now_s)
    if not health.is_healthy:
        if health.state == "EXPIRED":
            hrs = (
                f" (expired {abs(health.hours_remaining or 0):.1f}h ago)"
                if health.hours_remaining is not None
                else " (expired)"
            )
            detail = hrs
        else:  # ABSENT — dir exists but snapshot is missing/unparseable
            detail = " (no credential snapshot on disk)"
        raise MintError(
            f"cannot mint: account '{account}' credential is unhealthy"
            f"{detail} — run `claude /login` then `sac accounts sync-live`"
        )

    # --- read FRESH tokens at call time (mint-on-demand) -------------------
    # `_read_tokens_at` returns (access, refresh, client_id, expires_at_ms).
    # The refresh token is deliberately discarded here — it must NEVER reach
    # the artifact. Only the access token + expiry are carried forward.
    access_token, _refresh_token, _client_id, expires_at_ms = _read_tokens_at(
        creds_path
    )
    if not access_token or expires_at_ms is None:
        raise MintError(
            f"cannot mint: account '{account}' has no usable accessToken/"
            "expiresAt in its stored credential — run `claude /login`"
        )

    scopes = _read_scopes(creds_path)
    minted_at_ms = int(now_s * 1000)
    master_host = _resolve_master_host(hostname)

    # The envelope: `artifact` is the distributable (access-only), `meta`
    # is provenance. `refreshToken` is structurally absent from `artifact`.
    return {
        "artifact": {
            "claudeAiOauth": {
                "accessToken": access_token,
                "expiresAt": expires_at_ms,
                "scopes": scopes,
            }
        },
        "meta": {
            "account": account,
            "master_host": master_host,
            "minted_at": minted_at_ms,
            "expires_at": expires_at_ms,
            "artifact": _ARTIFACT_KIND,
            "artifact_version": _ARTIFACT_VERSION,
        },
    }


__all__ = ["MintError", "mint_access_only_artifact"]
