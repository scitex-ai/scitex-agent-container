"""In-SIF CLI outcome → stdout JSON + process exit code.

PR-3 Checkpoint 2 / 3. When the in-SIF CLI auto-proxies
``sac agents <verb>`` to the host's ``sac listen`` (the PR-3
fall-back path so a SIF-resident agent can manage the host
registry without operator hands), the verb's HTTP response is
collapsed into a single :class:`InSifOutcome` record that drives
both:

  * the stdout JSON payload the caller (operator or parent SAC
    agent script) parses to branch on ``kind``;
  * the process exit code clew launcher / build pipelines see
    when they shell out to ``sac agents <verb>``.

The 5-kind taxonomy pinned with clew (POST/DELETE/send/tail
surface ``kind`` values) plus a client-side ``transport`` kind
gives a 6-entry exit-code table the CLI commits to. Pinned here
once so every in-SIF CLI verb maps the same shape to the same
exit code — no per-verb drift.

Stdout JSON wire shape (printed by every in-SIF CLI verb after
:func:`build_outcome`):

.. code-block:: json

   {
     "ok":          true | false,
     "kind":        null | "<5-kind value or "transport" or "startup_failed">",
     "exit_code":   <int>,
     "http_status": null | <int>,
     "details":     <verbatim server body, or transport-error dict>
   }

``ok`` is a convenience boolean (``exit_code == 0``); ``kind`` is
``null`` on success and the structured failure tag otherwise.
The full server body is echoed under ``details`` so the consumer
sees what the host actually said (including
``details.binds[*]`` arrays from preflight, the STARTUP_FAILED
flat-summary, etc. — PR-1+PR-2 contracts thread through
unchanged).

Exit-code table (PR-3 contract; tests pin this so a future drift
trips loud):

  ============== ==== =========== =================================
  ``exit_code``  HTTP ``kind``    Surface
  ============== ==== =========== =================================
  0              2xx  ``null``    All — success
  1              n/a  transport   All — host listen unreachable /
                                  DNS / timeout / connection refused
  2              400  bind_unre…  POST /agents — preflight bind miss
  3              400  spec_inva…  POST /agents — spec shape error
  4              409  already_e…  POST /agents — name clash, no
                                  ``overwrite``
  5              403  acl_deny    POST/DELETE/send/tail — caller
                                  lacks lineage-scoped permission
  6              410  startup_f…  DELETE /agents/<name> — stillborn
                                  agent (PR-1 lifecycle)
  ============== ==== =========== =================================

``exit_code=1`` (transport) is the only code without an HTTP
status — the response never made it back. Every other row maps
from the host's structured ``kind`` field, so the client never
needs to peek at the HTTP status code itself.

Stability contract: this module is the single SoT for the
mapping. Any new ``kind`` added to the listen surface MUST
either (a) get a fresh exit code added here in the same PR or
(b) be explicitly classified as a sub-shade of an existing kind
(e.g. a future ``acl_deny_per_spec`` still maps to exit 5).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Exit-code mapping (the contract)
# ---------------------------------------------------------------------------

# The frozen mapping. ``None`` is the success row.
_KIND_TO_EXIT: dict[str | None, int] = {
    None: 0,
    "transport": 1,
    "bind_unresolvable": 2,
    "spec_invalid": 3,
    "already_exists": 4,
    "acl_deny": 5,
    "startup_failed": 6,
}

# The fallback for any structured ``kind`` the listen surface
# starts emitting before this module catches up. 99 picked
# deliberately: high enough to not collide with the explicit
# table, low enough to fit in a single byte for pipeline glue.
# A future PR introducing a new kind MUST give it a dedicated
# code so the operator never sees this fall-through silently.
_UNKNOWN_KIND_EXIT = 99


# ---------------------------------------------------------------------------
# Outcome record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InSifOutcome:
    """One in-SIF CLI verb's mapped outcome.

    * ``ok`` — ``True`` iff ``exit_code == 0`` (= the host returned
      a 2xx). Convenience for callers branching in shell scripts:
      ``[ $(jq -r .ok <<<"$out") = true ]``.
    * ``kind`` — ``None`` on success, otherwise the wire-stable tag
      (one of the 5-kind taxonomy + ``transport`` + ``startup_failed``).
    * ``exit_code`` — the integer the CLI process exits with.
    * ``http_status`` — the HTTP status code from the host listen;
      ``None`` when the request never landed (transport error).
    * ``details`` — the host's response body, verbatim. For
      transport errors, a synthesised dict ``{"error": "<message>",
      "url": "<base_url>"}`` so the consumer sees what was tried.
    """

    ok: bool
    kind: str | None
    exit_code: int
    http_status: int | None
    details: Any


# ---------------------------------------------------------------------------
# Public builders
# ---------------------------------------------------------------------------


def build_outcome(
    *,
    http_status: int | None,
    body: Any,
) -> InSifOutcome:
    """Map ``(http_status, body)`` into a frozen :class:`InSifOutcome`.

    Used by every in-SIF CLI verb after it receives a response
    from the host listen. The mapping is:

      * ``http_status`` in 2xx → ``ok=True``, ``kind=None``,
        ``exit_code=0``, ``details=body``.
      * ``body["kind"]`` is one of the 5-kind taxonomy →
        ``exit_code`` from :data:`_KIND_TO_EXIT`.
      * ``body["kind"]`` is unrecognised → ``exit_code=99``
        (sentinel — the operator MUST add a fresh row to the
        contract above).
      * No ``kind`` in body (malformed / pre-PR-1 host) →
        ``kind="transport"`` shape: ``exit_code=1`` with
        ``details=body`` so the operator still sees what came
        back. Treats the missing-kind case as transport-class
        because the consumer cannot branch on a structured tag.

    Args:
        http_status: The HTTP status from the host. ``None`` only
            from :func:`transport_outcome`.
        body: The parsed response body (dict for structured
            responses; may also be ``str`` / ``None`` when the
            host returned non-JSON, in which case the result
            still carries ``http_status`` so debugging is
            possible).
    """
    if http_status is not None and 200 <= http_status < 300:
        return InSifOutcome(
            ok=True,
            kind=None,
            exit_code=0,
            http_status=http_status,
            details=body,
        )
    kind = _extract_kind(body)
    if kind is None:
        # Non-2xx with no structured ``kind`` is treated as a
        # transport-class outcome (= the host responded but in
        # a shape the contract doesn't cover; the consumer can't
        # branch on a tag). exit_code=1 keeps the consumer code
        # simple: anything below 2 is "couldn't even classify".
        return InSifOutcome(
            ok=False,
            kind="transport",
            exit_code=_KIND_TO_EXIT["transport"],
            http_status=http_status,
            details=body,
        )
    exit_code = _KIND_TO_EXIT.get(kind, _UNKNOWN_KIND_EXIT)
    return InSifOutcome(
        ok=False,
        kind=kind,
        exit_code=exit_code,
        http_status=http_status,
        details=body,
    )


def transport_outcome(reason: str, *, url: str | None = None) -> InSifOutcome:
    """Build the canonical transport-class outcome.

    Called by the in-SIF CLI verb when the HTTP request itself
    raises (host listen unreachable, DNS error, connection
    timeout, etc.). Exit code 1; ``details`` carries the
    operator-facing reason + the URL we tried so the diagnosis
    isn't a guessing game.
    """
    details: dict[str, Any] = {"error": reason}
    if url is not None:
        details["url"] = url
    return InSifOutcome(
        ok=False,
        kind="transport",
        exit_code=_KIND_TO_EXIT["transport"],
        http_status=None,
        details=details,
    )


# ---------------------------------------------------------------------------
# Rendering for stdout
# ---------------------------------------------------------------------------


def outcome_to_stdout_json(outcome: InSifOutcome) -> str:
    """Render the outcome as a single JSON line for stdout.

    Compact (no indent) so a shell-pipe consumer can ``jq -r .kind``
    without buffering multiple lines. The trailing newline IS
    included so a CLI writing this to stdout terminates the line
    cleanly.
    """
    payload = {
        "ok": outcome.ok,
        "kind": outcome.kind,
        "exit_code": outcome.exit_code,
        "http_status": outcome.http_status,
        "details": outcome.details,
    }
    return json.dumps(payload, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_kind(body: Any) -> str | None:
    """Return ``body["kind"]`` when shape-safe; ``None`` otherwise."""
    if not isinstance(body, dict):
        return None
    kind = body.get("kind")
    if not isinstance(kind, str) or not kind:
        return None
    return kind


__all__ = [
    "InSifOutcome",
    "build_outcome",
    "outcome_to_stdout_json",
    "transport_outcome",
]
