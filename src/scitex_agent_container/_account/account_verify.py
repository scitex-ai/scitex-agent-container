"""Verify that a stored account's credential really belongs to that account.

INCIDENT 2026-08-12 — `sac accounts list` reported ``ywatanabe-scitex-ai``
at 2 % weekly while the Anthropic console showed 92 % for that account. The
number was not stale and not miscomputed: it was **truthful about a
different account**. ``accounts/anthropic/ywatanabe-scitex-ai/`` held a
credential that authenticates as ``ywata1989@gmail.com``, so one account was
rendered as two rows with two labels, and a capacity plan was built on the
resulting phantom headroom.

The defect is that account identity was resolved **by directory name**. A
stored credential carries no identity claim whatsoever — the OAuth snapshot
holds only ``accessToken`` / ``refreshToken`` / ``expiresAt`` / ``scopes`` /
``subscriptionType`` / ``rateLimitTier``, and the tokens are opaque
``sk-ant-oat01-…`` strings, not decodable JWTs. So the ONLY things naming an
account were the directory name and the ``account.json`` sidecar written
beside it, neither of which is re-checked after it is written. Any operation
that puts a credential in the wrong directory — a manual ``/login``, a store
consolidation onto a new canonical host, a rotation, a push to a peer —
relabels one account as another permanently and invisibly. Nothing looks
broken; every number stays internally consistent.

This module supplies the missing check. ``GET /api/oauth/profile`` returns
the account a bearer token actually authenticates as, and sac already calls
its sibling ``/api/oauth/usage`` with the same headers on the same host, so
the authoritative answer costs one extra GET.

Binding the cache to the token
------------------------------
A cached verification has exactly the staleness problem it exists to solve:
verify at 09:00, re-login at 09:30, and a 6-hour TTL keeps asserting the old
identity. So the cache is keyed to the FINGERPRINT of the token it verified
(``sha256`` prefix — a digest, never the token). When the credential file
changes for any reason, the fingerprint changes, the cached verdict no
longer applies, and the account is re-verified or reported UNVERIFIED. A
``/login`` therefore invalidates the verdict the instant it lands, rather
than after a timeout. The TTL remains as a second bound for the case where
the same token is held across a server-side account change.

States
------
``verified``    the profile endpoint answered and the account it named
                matches what this directory claims.
``mismatch``    the profile endpoint answered and named a DIFFERENT account.
                The directory label is wrong; every figure attributed to
                this row belongs to someone else.
``unverified``  no answer (offline, no token, non-2xx, stale fingerprint).
                sac does not know whose numbers these are.

``unverified`` is deliberately NOT merged into ``verified``: "could not
check" and "checked and fine" are different facts, and rendering them alike
is the defect this module exists to remove.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .account_identity import fetch_account_identity
from .token_refresh import _read_tokens_at

# Identity is stable, so a long TTL is fine; the token-fingerprint binding
# (see the module docstring) is what actually catches a re-login, and it is
# immediate. The TTL only bounds the case where the SAME token outlives a
# server-side account change.
_VERIFY_TTL_SECONDS = 6 * 3600

VERIFIED = "verified"
MISMATCH = "mismatch"
UNVERIFIED = "unverified"


@dataclass(frozen=True)
class AccountIdentity:
    """The verdict on one stored account's identity.

    ``claimed_email`` is what the store SAYS (``account.json``);
    ``verified_email`` / ``verified_uuid`` are what the token PROVED.
    ``duplicate_of`` is filled in by :func:`mark_duplicates` once the whole
    set is known — a single account cannot know it is a duplicate.
    """

    name: str
    state: str
    claimed_email: str | None = None
    verified_email: str | None = None
    verified_uuid: str | None = None
    verified_at: str | None = None
    duplicate_of: str | None = None

    @property
    def trustworthy(self) -> bool:
        """True only when the label was CHECKED and found correct.

        Any figure rendered under this account's name is attributable to
        that name only when this is true. ``unverified`` returns False —
        an unchecked label is not a correct one.
        """
        return self.state == VERIFIED and self.duplicate_of is None


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def token_fingerprint(access_token: str | None) -> str | None:
    """Stable non-reversible digest of a token, for cache invalidation.

    A truncated ``sha256`` — enough to detect that the credential changed,
    useless for authenticating. Never store or log the token itself.
    """
    if not access_token:
        return None
    return hashlib.sha256(access_token.encode("utf-8")).hexdigest()[:16]


def _cache_path(credentials_path: Path) -> Path:
    """Identity verdict lives beside the credential, like ``usage.json``."""
    return Path(credentials_path).parent / "identity.json"


def _read_cache(path: Path, *, fingerprint: str | None, now: datetime) -> dict | None:
    """Return the cached verdict iff it still applies to THIS token.

    Two independent gates, both of which must pass:

    1. the fingerprint matches — the verdict was reached about the token
       currently on disk, not a predecessor;
    2. the verdict is younger than the TTL.

    Either failing returns ``None``, i.e. "re-verify".
    """
    # stx-allow: fallback (reason: a missing/corrupt identity cache must mean
    # "re-verify", never a crash and never a silent pass.)
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return None
    if not isinstance(data, dict):
        return None
    if data.get("token_fingerprint") != fingerprint:
        return None
    stamp = data.get("verified_at")
    if not isinstance(stamp, str):
        return None
    # stx-allow: fallback (reason: malformed timestamp forces re-verification)
    try:
        verified_at = datetime.fromisoformat(stamp)
    except ValueError:  # stx-allow: fallback (reason: format mismatch)
        return None
    if verified_at.tzinfo is None:
        verified_at = verified_at.replace(tzinfo=timezone.utc)
    if (now - verified_at).total_seconds() >= _VERIFY_TTL_SECONDS:
        return None
    return data


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    """Persist a verdict atomically; best-effort, never raises."""
    # stx-allow: fallback (reason: the cache is an optimisation — a read-only
    # or full filesystem must not break `sac accounts list`.)
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(p) + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        tmp.rename(p)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net)
        pass


def _emails_match(claimed: str | None, verified: str | None) -> bool:
    """Case-insensitive email comparison; unknown on either side is no match."""
    if not claimed or not verified:
        return False
    return claimed.strip().lower() == verified.strip().lower()


def verify_account(
    name: str,
    credentials_path: Path,
    *,
    claimed_email: str | None = None,
    opener=None,
    now: datetime | None = None,
    use_cache: bool = True,
) -> AccountIdentity:
    """Resolve who the credential at ``credentials_path`` really belongs to.

    Returns an :class:`AccountIdentity` whose ``state`` is one of
    ``verified`` / ``mismatch`` / ``unverified`` (see the module docstring).
    Never raises and never returns the token.

    ``claimed_email`` is the store's own assertion (``account.json``'s
    ``email_address``). When the store makes no claim there is nothing to
    contradict, so a successful lookup is recorded as ``verified`` against
    the fetched email — the row then at least displays a REAL identity.
    """
    _now = now or _now_utc()
    creds = Path(credentials_path)
    access_token, _, _, _ = _read_tokens_at(creds)
    fingerprint = token_fingerprint(access_token)
    if fingerprint is None:
        return AccountIdentity(
            name=name, state=UNVERIFIED, claimed_email=claimed_email
        )

    cache_file = _cache_path(creds)
    cached = (
        _read_cache(cache_file, fingerprint=fingerprint, now=_now)
        if use_cache
        else None
    )
    if cached is not None:
        email = cached.get("verified_email")
        uuid = cached.get("verified_uuid")
        stamp = cached.get("verified_at")
    else:
        email, uuid = fetch_account_identity(creds, opener=opener)
        if email is None and uuid is None:
            # The endpoint did not answer. Report NOT-KNOWN rather than
            # falling back to the label, which is the very thing in doubt.
            return AccountIdentity(
                name=name, state=UNVERIFIED, claimed_email=claimed_email
            )
        stamp = _now.isoformat()
        _write_cache(
            cache_file,
            {
                "verified_email": email,
                "verified_uuid": uuid,
                "verified_at": stamp,
                "token_fingerprint": fingerprint,
            },
        )

    if claimed_email and not _emails_match(claimed_email, email):
        state = MISMATCH
    else:
        state = VERIFIED
    return AccountIdentity(
        name=name,
        state=state,
        claimed_email=claimed_email,
        verified_email=email,
        verified_uuid=uuid,
        verified_at=stamp if isinstance(stamp, str) else None,
    )


def _is_verified(identities: list[AccountIdentity], name: str) -> bool:
    """True iff the account called ``name`` in this set verified cleanly."""
    return any(i.name == name and i.state == VERIFIED for i in identities)


def mark_duplicates(identities: Iterable[AccountIdentity]) -> list[AccountIdentity]:
    """Flag stored accounts that resolve to the SAME Anthropic account.

    Two store directories holding two DIFFERENT tokens for one account is
    the exact shape of the 2026-08-12 incident, and it is invisible to any
    check that compares credential files. Grouping is by ``verified_uuid``
    when present (the strongest key), else by ``verified_email``.

    Every member of a duplicate group except the OWNER gets ``duplicate_of``
    set to the owner's name — so the aggregate can count the account ONCE.
    Double-counting one account as two is what turns a saturated fleet into
    apparent headroom.

    The owner is the group's ``verified`` member where there is one, and only
    otherwise the first seen. Order alone would be the wrong rule: in the
    2026-08-12 store the two directories were ``ywata1989-gmail-com``
    (correctly labelled) and ``ywatanabe-scitex-ai`` (holding the same
    account under the wrong name), and had they sorted the other way the
    MISLABELLED row would have become the owner — suppressing the usage of
    the account that was named correctly and keeping the one that was not.
    Preferring the verified member makes the outcome independent of
    directory naming.

    Accounts with no verified identity are never grouped: "both unknown" is
    not evidence of sameness.
    """
    idents = list(identities)

    def _key(ident: AccountIdentity) -> str | None:
        return ident.verified_uuid or (
            ident.verified_email.strip().lower() if ident.verified_email else None
        )

    # Pass 1: provisional owner is the first member of each group.
    owners: dict[str, str] = {}
    for ident in idents:
        key = _key(ident)
        if key and key not in owners:
            owners[key] = ident.name

    # Pass 2: a VERIFIED member displaces that provisional owner.
    for ident in idents:
        key = _key(ident)
        if key and ident.state == VERIFIED:
            owners.setdefault(key, ident.name)
            if not _is_verified(idents, owners[key]):
                owners[key] = ident.name

    out: list[AccountIdentity] = []
    for ident in idents:
        key = _key(ident)
        if not key or owners.get(key) == ident.name:
            out.append(ident)
            continue
        out.append(
            AccountIdentity(
                name=ident.name,
                state=ident.state,
                claimed_email=ident.claimed_email,
                verified_email=ident.verified_email,
                verified_uuid=ident.verified_uuid,
                verified_at=ident.verified_at,
                duplicate_of=owners[key],
            )
        )
    return out


__all__ = [
    "AccountIdentity",
    "MISMATCH",
    "UNVERIFIED",
    "VERIFIED",
    "mark_duplicates",
    "token_fingerprint",
    "verify_account",
]
