"""Authenticated client for the SAC host control plane (``sac listen``).

The GUI is a PROJECTION, not a store. Every read and mutation goes through
this client to the committed public HTTP surface (ADR-0004): ``GET /agents``,
``GET /agents/<name>/status`` and the lifecycle verbs. Nothing in here re-
derives lifecycle state from registry files or reaches into ``_lifecycle``
internals — those are the listener's job.

Token resolution order (first that yields a value):
  1. ``SCITEX_AGENT_CONTAINER_API_TOKEN``            (direct value)
  2. ``SCITEX_AGENT_CONTAINER_API_TOKEN_FILE``       (file containing token)
  3. the listener's own auto-generated token at
     ``~/.scitex/agent-container/tokens/listen-<host>.token``

where ``<host>`` is the base URL's host — so a co-located standalone server on
loopback finds the token ``sac listen`` wrote with zero configuration.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from socket import timeout as SocketTimeout
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from ._constants import (
    API_URL_ENV,
    DEFAULT_API_URL,
    TOKEN_ENV,
    TOKEN_FILE_ENV,
)

# Wall-clock ceiling for one HTTP exchange.
#
# These are NOT arbitrary. Measured against the real control plane on
# scitex-compute-03 (22 agents, 2026-09-17): ``GET /agents`` 200 in 5.11s /
# 5.23s / 6.91s warm and 18.99s cold, with one spike at 117.8s. The former 8.0s
# ceiling sat inside that band, so the fleet page rendered "listener
# unreachable" while the listener was healthy and answering — every agent and
# the whole lifecycle surface vanished behind one slow fan-out.
#
# The ceiling now has to clear the COLD latency of a real fleet, because a cold
# read is the normal case after a restart, and a bounded wait that reports
# "unavailable" for a fleet that is merely slow is a false negative. Fleet
# status is now enriched by one batched ``GET /agents`` request rather than an
# HTTP request per agent.
DEFAULT_TIMEOUT_SECONDS = 60.0


class RemoteOperationError(RuntimeError):
    """Typed non-success response from the SAC host control plane."""

    def __init__(self, status_code: int, message: str, kind: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind


class FleetUnavailableError(RuntimeError):
    """The web process cannot reach the SAC listener at all."""

    def __init__(self, base_url: str, reason: str) -> None:
        super().__init__(f"could not reach the SAC listener at {base_url}: {reason}")
        self.base_url = base_url
        self.reason = reason


def safe_error_message(exc: Exception) -> str:
    """A browser-safe message for a control-plane read failure.

    The raw exception carries internal detail (the listener URL, an
    environment-variable name, a transport reason) that must never reach the
    browser - it is operator/deployment information. Map each failure to a fixed
    operator-facing phrase and keep the raw text server-side (logs / audit).
    """
    if isinstance(exc, FleetUnavailableError):
        return "the control plane did not answer"
    if isinstance(exc, RemoteOperationError):
        return "the control plane returned an error"
    return "the request could not be completed"


#: LEAST-DISCLOSURE PUBLIC TEXT for a typed listener error.
#:
#: A typed error carries TWO things: a CODE (``kind``) drawn from the listener's
#: own committed vocabulary, and a free-form human message. Only the CODE is fit
#: to publish - it is a member of a fixed set, so public text can be MAPPED from
#: it. The message cannot be: an all-alphabetic message such as
#: ``listener compute-fixture.internal`` or ``api key SYNTHETICONLYVALUE`` names
#: an internal host or a credential while passing every grammar check, and
#: punctuation/secret-word rules only mask the SHAPES someone happened to
#: enumerate. So the message stays server-side (logs / audit) and the browser
#: gets fixed text: this map for a known code, ``_REDACTED`` for anything else.
_PUBLIC_DETAIL_BY_CODE: dict[str, str] = {
    "spec_resolution_failed": "The agent's spec could not be validated.",
    "ambiguous_registry": "More than one registry claims this agent's name.",
    "unknown_agent": "The control plane does not know this agent.",
    "spec_unreadable": "The agent's spec could not be read.",
}

#: The one phrase published when no trusted public text exists for the code -
#: including when a caller passes raw prose where a code belongs.
_REDACTED = "the control plane reported a typed error"


def public_detail(code: str | None) -> str:
    """The fixed public text for a TRUSTED typed-error code; never raw prose.

    ``code`` must be a member of the listener's committed error vocabulary;
    anything else resolves to ``_REDACTED``. There is deliberately no path from
    free-form text to the browser: a message is not published because it looks
    safe, only a code is, because it is KNOWN.
    """
    return _PUBLIC_DETAIL_BY_CODE.get(code or "", _REDACTED)


def redact_detail(text: str) -> str:
    """LEAST-DISCLOSURE projection of raw error text: none of it is published.

    The single entry point for a caller that holds a raw message, and the
    guarantee that no raw-text path exists. It returns ``_REDACTED``
    unconditionally, because no grammar and no list of masked shapes can certify
    prose as free of deployment or credential detail (see the note above
    ``_PUBLIC_DETAIL_BY_CODE``). Nothing is echoed, so there is no residue to
    mask and none to leak.
    """
    return _REDACTED


def _home_roots() -> list[Path]:
    """Candidate home roots, most-likely first.

    The agent process runs with ``$HOME`` set to the container home
    (``/home/agent``), but ``sac listen`` writes its token under the REAL user
    home (``/home/<user>``). Both are probed so a co-located standalone server
    finds the token either way.
    """
    roots: list[Path] = []
    env_home = os.environ.get("HOME")
    if env_home:
        roots.append(Path(env_home))
    try:
        import pwd

        real = pwd.getpwuid(os.getuid()).pw_dir
        if real:
            roots.append(Path(real))
    except (ImportError, KeyError):  # stx-allow: fallback (reason: non-POSIX or no passwd entry)
        pass
    # de-dupe, preserving order
    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        key = str(r)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _default_token_paths(base_url: str) -> list[Path]:
    """Candidate token paths, most-specific first.

    The listener names its token ``listen-<bind-host>.token`` where bind-host
    is whatever ``sac listen --bind`` was given — typically the local hostname,
    not the loopback IP. The token lives under the OPERATOR's home, which in a
    container may differ from ``$HOME``/passwd (both can be remapped to
    ``/home/agent``). So we cross the URL hostname and the local hostname with
    each candidate home root, then, as a bounded last resort, glob one level of
    ``/home/*`` for the local-hostname token. The first existing file wins.
    """
    from ._authorization import local_hostname

    url_host = urlsplit(base_url).hostname or "127.0.0.1"
    hostnames = [hn for hn in dict.fromkeys([url_host, local_hostname()]) if hn]
    paths: list[Path] = []
    for home in _home_roots():
        tdir = home / ".scitex" / "agent-container" / "tokens"
        for hn in hostnames:
            paths.append(tdir / f"listen-{hn}.token")
    # Bounded fallback across /home/* — the container remaps HOME to
    # /home/agent but the operator's token sits under /home/<operator>. The
    # local-hostname token is tried FIRST (a token named for another host would
    # be the wrong credential and 403); any other listen token is last resort.
    name = local_hostname()
    if name:
        try:
            primary = sorted(Path("/home").glob(f"*/.scitex/agent-container/tokens/listen-{name}.token"))
            others = sorted(Path("/home").glob("*/.scitex/agent-container/tokens/listen-*.token"))
            for found in primary + [o for o in others if o not in primary]:
                paths.append(found)
        except OSError:  # stx-allow: fallback (reason: /home may be absent/unreadable)
            pass
    return paths


def resolve_token(base_url: str) -> str:
    """Return the Bearer token for ``base_url`` or '' if none is configured."""
    value = os.environ.get(TOKEN_ENV, "").strip()
    if value:
        return value
    file_env = os.environ.get(TOKEN_FILE_ENV, "").strip()
    candidates: list[Path] = []
    if file_env:
        candidates.append(Path(file_env).expanduser())
    candidates.extend(_default_token_paths(base_url))
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text
    return ""


class RemoteFleet:
    """Small authenticated HTTP client matching the /agents row + status shape."""

    def __init__(
        self, base_url: str, token: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    @classmethod
    def from_environment(cls, base_url: str | None = None) -> "RemoteFleet":
        resolved = (base_url or os.environ.get(API_URL_ENV, "") or DEFAULT_API_URL).strip()
        token = resolve_token(resolved)
        if not token:
            # No credential at all: reads will 401. We still construct so the
            # view can present an explicit "listener not authenticated" state
            # instead of crashing — an unconfigured deployment is a STATE, not
            # an exception.
            token = ""
        return cls(resolved, token)

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = Request(f"{self.base_url}{path}", data=data, method=method, headers=headers)
        budget = self.timeout if timeout is None else timeout
        try:
            with urlopen(req, timeout=budget) as resp:
                payload = json.load(resp)
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8", errors="replace"))
            except (ValueError, OSError):
                payload = {}
            message = payload.get("error") if isinstance(payload, dict) else str(exc.reason)
            kind = payload.get("kind") if isinstance(payload, dict) else None
            raise RemoteOperationError(exc.code, str(message or exc.reason), kind) from exc
        except (OSError, json.JSONDecodeError) as exc:
            # ``socket.timeout`` IS an OSError, and a read that ran out of its
            # budget is precisely "the listener did not answer in time" — the
            # SAME conclusion as an unreachable one. Classifying it here (rather
            # than letting it escape as a bare socket error) is what lets a
            # caller bound a fan-out per item and still tell the two apart from
            # any other transport failure.
            if isinstance(exc, SocketTimeout):
                raise FleetUnavailableError(
                    self.base_url, f"no response within {budget:g}s"
                ) from exc
            raise FleetUnavailableError(self.base_url, str(exc)) from exc
        if not isinstance(payload, dict):
            raise FleetUnavailableError(self.base_url, "non-object JSON response")
        return payload

    # ── reads ────────────────────────────────────────────────────────────────
    def list_all(self) -> list[dict[str, Any]]:
        payload = self._request("/agents")
        rows = payload.get("agents")
        if not isinstance(rows, list):
            raise FleetUnavailableError(self.base_url, "'/agents' has no agents list")
        return [row for row in rows if isinstance(row, dict)]

    def read_status(self, name: str, *, timeout: float | None = None) -> dict[str, Any]:
        return self._request(
            f"/agents/{quote(name, safe='')}/status", timeout=timeout
        )

    def read_tail(self, name: str, *, max_bytes: int = 262144) -> str:
        """Read the bounded ``follow=false`` SSE tail of an agent's session log.

        Returns the raw stream text (data frames) for :mod:`._session` to parse.
        A 404 (no session.jsonl yet) returns "" — that is a legitimate state,
        not an error. The read is byte-capped so a long transcript cannot
        balloon the detail view or the request.
        """
        from urllib.request import Request as _Req
        from urllib.request import urlopen as _open

        url = f"{self.base_url}/agents/{quote(name, safe='')}/tail?follow=false"
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = _Req(url, method="GET", headers=headers)
        try:
            with _open(req, timeout=self.timeout) as resp:
                chunks = []
                total = 0
                while total < max_bytes:
                    chunk = resp.read(min(65536, max_bytes - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
            return b"".join(chunks).decode("utf-8", errors="replace")
        except HTTPError as exc:
            if exc.code == 404:
                return ""
            raise RemoteOperationError(exc.code, f"tail failed: {exc.reason}") from exc
        except (OSError, TimeoutError) as exc:
            raise FleetUnavailableError(self.base_url, str(exc)) from exc

    def read_statuses(self, names: list[str]) -> dict[str, dict[str, Any] | Exception]:
        """Project requested statuses from one enriched ``GET /agents`` read."""
        wanted = set(names)
        if not wanted:
            return {}
        return {
            str(row["name"]): row
            for row in self.list_all()
            if isinstance(row.get("name"), str) and row["name"] in wanted
        }

    # ── mutations (delegated to the authenticated listener) ─────────────────
    def lifecycle(self, name: str, action: str) -> dict[str, Any]:
        target = quote(name, safe="")
        if action == "start":
            return self._request(
                "/agents", method="POST", body={"name": name, "assume_yes": True}, timeout=120
            )
        if action == "stop":
            return self._request(f"/agents/{target}", method="DELETE", timeout=120)
        if action == "restart":
            return self._request(f"/agents/{target}/restart", method="POST", body={}, timeout=120)
        raise ValueError(f"unsupported lifecycle action: {action}")


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "FleetUnavailableError",
    "RemoteFleet",
    "RemoteOperationError",
    "public_detail",
    "redact_detail",
    "resolve_token",
    "safe_error_message",
]
