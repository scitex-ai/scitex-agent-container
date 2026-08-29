"""Is a stored account still ENTITLED to run Claude Code?

INCIDENT 2026-08-25. The operator cancelled the ``wyusuuke@gmail.com``
subscription. Nothing noticed. Agents kept being routed onto it and
failed mid-turn with::

    Your organization has disabled Claude subscription access for
    Claude Code - Use an Anthropic API key instead, or ask your admin
    to enable access

Measured that morning, one read-only request per stored account::

    scitex-01-scitex-ai   200 OK
    ywata1989-gmail-com   200 OK
    ywatanabe-scitex-ai   200 OK
    wyusuuke-gmail-com    403 permission_error
                          "OAuth authentication is currently not
                           allowed for this organization"

WHY EVERY EXISTING GATE PASSED IT. Account health is snapshot
FRESHNESS -- a non-expired ``claudeAiOauth.expiresAt`` (see
:mod:`._account_health`). **OAuth refresh is independent of
entitlement**: the headless refresh kept succeeding, most recently at
09:17 UTC that same morning with a new expiry of 17:17, so the account
read ``VALID`` to every gate while being unusable for an actual turn.

Freshness is not entitlement. They are different questions and only
one of them was being asked.

Three-valued, per constitution
------------------------------
"Every signal is three-valued: true, false, and *unknown*. Collapsing
unknown into either pole is the most common bug we ship."

So this module reports :data:`ENTITLED`, :data:`FORBIDDEN`, or
:data:`UNKNOWN`, and the caller must handle all three:

* ``FORBIDDEN`` is a MEASUREMENT -- an HTTP 403 whose body names an
  OAuth/permission error. Only that becomes FORBIDDEN.
* ``UNKNOWN`` is everything else: never probed, a record too old to
  trust, an unparseable record, a timeout, DNS failure, a 5xx. It must
  NOT block a boot. A network blip is not a cancelled subscription,
  and collapsing UNKNOWN into FORBIDDEN would evict the entire pool
  the first time this host lost its uplink -- turning a transient
  outage into a fleet-wide one.
* Equally, UNKNOWN must not be silently rendered as ENTITLED. It is
  reported as its own state so the operator-facing diagnosis can say
  "we do not know" instead of implying we checked.

Why a CACHE and not a live probe
--------------------------------
:func:`._pick_healthy.pick_healthy_account` runs at every agent boot
and is documented as "cache-only read, NO live Claude API call, so it
never burns account quota at boot". Probing entitlement inline would
break that promise and add a network round-trip to every start.

So the live probe (:func:`probe_entitlement`) runs OUT OF BAND, from
the host timer that already walks every account, and writes a small
record next to the credential. The boot path only ever
:func:`read_entitlement`\\ s that record -- a local file read.

AUTO-HEAL IS THE POINT. The operator's workflow is to cancel a
subscription and restore it later ("I put the subscription back when I
need the account again"). Because the timer rewrites this record on
every pass, a restored subscription flips FORBIDDEN -> ENTITLED on its
own and the account rejoins the pool with no spec edit, no symlink
rename, and no human step. That is the whole reason this is a cached
signal rather than a hand-maintained deny-list.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ENTITLED",
    "FORBIDDEN",
    "UNKNOWN",
    "Entitlement",
    "DEFAULT_MAX_AGE_S",
    "entitlement_path",
    "read_entitlement",
    "write_entitlement",
    "probe_entitlement",
]

ENTITLED = "ENTITLED"
FORBIDDEN = "FORBIDDEN"
UNKNOWN = "UNKNOWN"

#: How long a stored verdict is trusted. Beyond this the record reads
#: UNKNOWN rather than being believed indefinitely -- a FORBIDDEN from
#: last month must not keep an account out after the subscription came
#: back, and an ENTITLED from last month must not vouch for one that
#: has since lapsed. The host timer refreshes well inside this window;
#: the age limit only bites when the timer has stopped, which is
#: exactly when its answers should stop counting.
DEFAULT_MAX_AGE_S = 24 * 3600

#: The probe endpoint. A minimal request: the cheapest call that still
#: exercises the ENTITLEMENT path rather than mere token validity.
_API_URL = "https://api.anthropic.com/v1/messages"
_PROBE_TIMEOUT_S = 20


@dataclass(frozen=True)
class Entitlement:
    """One account's entitlement verdict and the evidence behind it.

    Attributes
    ----------
    name
        The stored-account name this verdict is about.
    state
        :data:`ENTITLED`, :data:`FORBIDDEN` or :data:`UNKNOWN`.
    checked_at
        Unix seconds when the live probe ran; ``None`` when no record
        exists.
    http_status
        The status the probe saw, when it got one.
    detail
        Short operator-facing evidence -- the API's own error text for
        a FORBIDDEN, or why the state is UNKNOWN. Never a credential.
    """

    name: str
    state: str
    checked_at: float | None = None
    http_status: int | None = None
    detail: str = ""

    @property
    def blocks_use(self) -> bool:
        """True only for a MEASURED denial.

        UNKNOWN deliberately returns False: not knowing is not the same
        as knowing it is dead, and this is the property a boot path
        gates on.
        """
        return self.state == FORBIDDEN


def entitlement_path(account_dir: Path) -> Path:
    """Where one account's verdict lives.

    Beside the credential it describes, so the two travel together and
    a copied/backed-up account dir carries its own evidence.
    """
    return account_dir / "entitlement.json"


def read_entitlement(
    name: str,
    account_dir: Path,
    *,
    now: float | None = None,
    max_age_s: float = DEFAULT_MAX_AGE_S,
) -> Entitlement:
    """Read a stored verdict. NEVER raises, NEVER touches the network.

    This is the function the boot path calls. Every failure mode --
    absent file, unreadable file, malformed JSON, unrecognised state,
    a record older than ``max_age_s`` -- degrades to
    :data:`UNKNOWN` with a ``detail`` saying which, so a boot is never
    blocked by our own bookkeeping and the operator can still see why
    we have no answer.
    """
    path = entitlement_path(account_dir)
    now_ts = now if now is not None else time.time()

    # stx-allow: fallback (reason: a boot-path read of best-effort
    # bookkeeping must degrade to UNKNOWN, never crash a start.)
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return Entitlement(name, UNKNOWN, detail="never probed")
    except (OSError, ValueError) as exc:
        return Entitlement(
            name, UNKNOWN, detail=f"unreadable record: {type(exc).__name__}"
        )

    if not isinstance(raw, dict):
        return Entitlement(name, UNKNOWN, detail="record is not an object")

    state = raw.get("state")
    if state not in (ENTITLED, FORBIDDEN, UNKNOWN):
        return Entitlement(name, UNKNOWN, detail=f"unrecognised state {state!r}")

    checked_at = raw.get("checked_at")
    if not isinstance(checked_at, (int, float)):
        return Entitlement(
            name, UNKNOWN, detail="record has no numeric checked_at"
        )

    age = now_ts - float(checked_at)
    if age > max_age_s:
        # Deliberately not believed. See DEFAULT_MAX_AGE_S.
        return Entitlement(
            name,
            UNKNOWN,
            checked_at=float(checked_at),
            detail=(
                f"record is {age / 3600:.1f}h old "
                f"(limit {max_age_s / 3600:.0f}h) - treating as unknown"
            ),
        )

    status = raw.get("http_status")
    return Entitlement(
        name=name,
        state=state,
        checked_at=float(checked_at),
        http_status=status if isinstance(status, int) else None,
        detail=str(raw.get("detail", "")),
    )


def write_entitlement(account_dir: Path, verdict: Entitlement) -> bool:
    """Persist a verdict beside its credential. Returns success.

    Never raises: this is bookkeeping written from a timer, and a
    read-only or full disk must not take the timer down.
    """
    path = entitlement_path(account_dir)
    payload = {
        "state": verdict.state,
        "checked_at": verdict.checked_at,
        "http_status": verdict.http_status,
        "detail": verdict.detail,
    }
    # stx-allow: fallback (reason: best-effort bookkeeping write from a
    # timer; failing to record a verdict must not fail the timer.)
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        tmp.replace(path)
        return True
    except OSError:
        return False


def _classify_http_error(exc: urllib.error.HTTPError) -> Entitlement:
    """Turn an HTTP error into a three-valued verdict.

    ONLY a 403 naming an OAuth/permission problem is FORBIDDEN. A 401
    is a token problem, which the freshness gate already owns; a 429 is
    a quota problem, which the quota ranker owns; 5xx is the server
    having a bad day. Each of those is UNKNOWN *for the entitlement
    question* -- answering a question we were not asked is how a signal
    starts lying.
    """
    # stx-allow: fallback (reason: the error body is diagnostic text;
    # an unreadable body must still yield a verdict.)
    try:
        body = exc.read().decode("utf-8", "replace")
    except Exception:
        body = ""
    snippet = " ".join(body.split())[:200]
    now_ts = time.time()

    if exc.code == 403 and (
        "oauth" in body.lower() or "permission_error" in body.lower()
    ):
        return Entitlement(
            name="",
            state=FORBIDDEN,
            checked_at=now_ts,
            http_status=403,
            detail=snippet or "403 permission_error",
        )
    return Entitlement(
        name="",
        state=UNKNOWN,
        checked_at=now_ts,
        http_status=exc.code,
        detail=f"HTTP {exc.code} is not an entitlement verdict: {snippet}",
    )


def probe_entitlement(
    name: str,
    account_dir: Path,
    *,
    url: str = _API_URL,
    timeout_s: float = _PROBE_TIMEOUT_S,
    opener=None,
) -> Entitlement:
    """LIVE probe. Never call this from a boot path -- see module docs.

    Reads the account's stored access token and makes one minimal
    request. Never raises; every failure becomes a verdict.

    ``opener`` is injected by the tests so they exercise the real
    classification logic against synthetic responses without a network
    (no mocks of our own code -- only the transport is substituted).
    """
    cred = account_dir / ".credentials.json"
    # stx-allow: fallback (reason: a missing/rotten credential is a
    # verdict, not a crash; the freshness gate owns that failure.)
    try:
        token = json.loads(cred.read_text())["claudeAiOauth"]["accessToken"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return Entitlement(
            name,
            UNKNOWN,
            checked_at=time.time(),
            detail=f"no usable access token: {type(exc).__name__}",
        )

    req = urllib.request.Request(
        url,
        data=json.dumps(
            {
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "hi"}],
            }
        ).encode(),
        headers={
            "authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
            "content-type": "application/json",
        },
    )
    _open = opener or urllib.request.urlopen

    # stx-allow: fallback (reason: every transport failure must become
    # UNKNOWN rather than propagate out of a timer.)
    try:
        resp = _open(req, timeout=timeout_s)
        status = getattr(resp, "status", None)
        return Entitlement(
            name,
            ENTITLED,
            checked_at=time.time(),
            http_status=status,
            detail=f"HTTP {status}",
        )
    except urllib.error.HTTPError as exc:
        verdict = _classify_http_error(exc)
        return Entitlement(
            name=name,
            state=verdict.state,
            checked_at=verdict.checked_at,
            http_status=verdict.http_status,
            detail=verdict.detail,
        )
    except Exception as exc:
        # Timeout, DNS, TLS, connection reset. NOT a subscription
        # verdict -- see the module docstring on why this must not
        # collapse into FORBIDDEN.
        return Entitlement(
            name,
            UNKNOWN,
            checked_at=time.time(),
            detail=f"probe failed: {type(exc).__name__}: {exc}",
        )
