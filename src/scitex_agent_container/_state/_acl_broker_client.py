"""In-container HTTP client for the host-side ACL decision endpoints.

Task #27 PR B. Lets an in-container ``sac a2a {unblock,block,grant}``
write to the HOST listen's state.db instead of the per-container
state.db. Without this broker the in-container CLI writes silently
miss — the host listen's ACL checks consult ITS OWN state.db, not
the container's, so a "grant" written inside a container has no
effect on the host's ACL gate (lead FUTURE item 4, operator-greenlit
2026-06-01).

Mirrors the SAC-from-SAC ``_lifecycle._spawn_client`` shape:

* stdlib-only ``urllib.request`` (no httpx) — minimal deps inside
  the container; one one-shot POST per call, no streaming, no async
* injectable ``opener`` for tests — no monkeypatch
* ``BrokerRequestError`` — fail-loud on missing base URL / non-2xx /
  transport failure / malformed body. Never silently downgrade to
  "ok with empty result".

The CLI's dispatch logic chooses between this broker (in-SIF) and
the local DB-only helpers (bare host). Detection reuses
``_lifecycle._in_sif_broker.is_in_sif`` so the two broker paths
share the same in-SIF signal.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable
from urllib import error as urlerror
from urllib import request as urlrequest

logger = logging.getLogger(__name__)


__all__ = [
    "AclBrokerError",
    "broker_acl_decision",
]


_DEFAULT_TIMEOUT_S = 10.0


class AclBrokerError(RuntimeError):
    """Raised when the in-container ACL broker cannot reach the host listen.

    Carries the structured failure shape:

    * ``status`` — HTTP status code from the listen server (``None``
      for transport / missing-env failures).
    * ``body`` — parsed response dict / text fallback / ``None``.

    The fail-loud invariants this class encodes:

    * Missing ``SAC_LISTEN_BASE_URL`` → error with the env var name
      named in the message (apptainer runtime forgot to inject; never
      silently skip the broker and write to the wrong db).
    * Transport / 4xx / 5xx / malformed body → status + body
      preserved verbatim.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def _resolve_base_url(explicit: str | None) -> str:
    """Return the listen-server base URL or raise loudly.

    Resolution order: explicit argument → ``SAC_LISTEN_BASE_URL``
    via :func:`_env.getenv` (so either prefix works). Trailing
    slash is stripped so the caller can safely concatenate
    ``/v1/acl/...``.
    """
    if explicit:
        return explicit.rstrip("/")
    from .._env import getenv

    raw = (getenv("LISTEN_BASE_URL", "") or "").strip()
    if not raw:
        raise AclBrokerError(
            "ACL broker requires SAC_LISTEN_BASE_URL (the host-stable "
            "`sac listen` URL injected into the container by the apptainer "
            "runtime). Got empty/unset."
        )
    return raw.rstrip("/")


def _resolve_bearer(explicit: str | None) -> str | None:
    """Return the listen-server bearer token, or ``None`` if unset.

    Unlike ``SAC_LISTEN_BASE_URL``, an absent bearer is not fatal:
    a listen started without bearer auth still accepts the call.
    The server enforces its own auth contract.
    """
    if explicit is not None:
        return explicit or None
    from .._env import getenv

    return ((getenv("LISTEN_BEARER", "") or "").strip()) or None


def _parse_body(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:  # stx-allow: fallback (reason: non-JSON body surfaced verbatim to caller via AclBrokerError.body; never silently dropped)
        return raw.decode("utf-8", errors="replace")


def broker_acl_decision(
    decision: str,
    *,
    sender: str,
    target: str,
    note: str | None = None,
    base_url: str | None = None,
    bearer: str | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    opener: Callable | None = None,
) -> dict:
    """POST an ACL decision (``unblock`` / ``block`` / ``grant``) to host listen.

    ``decision`` selects the route — must be one of ``"unblock"``,
    ``"block"``, ``"grant"`` (the last is an alias of ``unblock``
    accepted server-side for back-compat).

    Returns the host's JSON response on 2xx — the listen handler
    returns the same envelope shape the local helper does
    (``{"sender", "target", "granted"|"blocked", "unblocked",
    "cleared_pending"}``). Raises :class:`AclBrokerError` on
    transport / 4xx / 5xx / malformed-body / missing-env failure.
    """
    if decision not in ("unblock", "block", "grant"):
        raise AclBrokerError(
            f"unknown ACL decision {decision!r} — expected "
            "'unblock', 'block', or 'grant'"
        )
    if not sender or not target:
        raise AclBrokerError("broker_acl_decision: sender and target must be non-empty")

    base = _resolve_base_url(base_url)
    tok = _resolve_bearer(bearer)
    body: dict[str, Any] = {"sender": sender, "target": target}
    if note is not None:
        body["note"] = note
    payload = json.dumps(body).encode("utf-8")
    url = f"{base}/v1/acl/{decision}"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"

    req = urlrequest.Request(url, data=payload, method="POST", headers=headers)
    opener_fn = opener if opener is not None else urlrequest.urlopen

    try:
        with opener_fn(req, timeout=timeout_s) as resp:
            raw = resp.read()
            status = int(getattr(resp, "status", 200))
    except urlerror.HTTPError as exc:
        raw_body = b""
        try:
            raw_body = exc.read() or b""
        except Exception:  # stx-allow: defensive — half-closed HTTPError body read may itself fail; we already have code + URL  # noqa: BLE001
            pass
        parsed = _parse_body(raw_body)
        logger.warning(
            "acl_broker: POST %s returned HTTP %s body=%r",
            url,
            exc.code,
            parsed,
        )
        raise AclBrokerError(
            f"ACL {decision} of {sender!r}→{target!r} rejected: listen "
            f"returned HTTP {exc.code} ({parsed!r})",
            status=exc.code,
            body=parsed,
        ) from exc
    except (urlerror.URLError, OSError, ValueError) as exc:
        logger.warning("acl_broker: POST %s transport error: %s", url, exc)
        raise AclBrokerError(
            f"ACL {decision} of {sender!r}→{target!r} failed: cannot reach "
            f"listen at {base!r} ({exc})"
        ) from exc

    parsed = _parse_body(raw)
    if status < 200 or status >= 300:
        logger.warning(
            "acl_broker: POST %s returned HTTP %s body=%r",
            url,
            status,
            parsed,
        )
        raise AclBrokerError(
            f"ACL {decision} of {sender!r}→{target!r} rejected: listen "
            f"returned HTTP {status} ({parsed!r})",
            status=status,
            body=parsed,
        )
    if not isinstance(parsed, dict):
        raise AclBrokerError(
            f"ACL {decision} of {sender!r}→{target!r} got non-JSON body: {parsed!r}",
            status=status,
            body=parsed,
        )
    return parsed
