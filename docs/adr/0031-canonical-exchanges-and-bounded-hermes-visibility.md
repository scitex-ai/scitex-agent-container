# ADR-0031 — Canonical exchanges and bounded Hermes visibility

Status: Accepted (2026-09-13)

## Context

SciTeX message delivery crosses Cards, SAC, and Hermes. Treating a successful
HTTP request or terminal write as delivery created incompatible retry rules and
made a final failure appear reusable. Fetching a complete Hermes transcript to
prove an old delivery also made retry cost grow with session history.

Two read-only production observations define the visibility constraint. On the
Hub session, `session.activate(omit_messages=False)` returned 17,318,347 bytes
for 7,442 messages and took 2.014 seconds. For an existing successful delivery,
the authenticated exact-marker session search returned HTTP 200, 777 bytes,
one `user` result, and the literal delivery marker; a query for a marker without
the delivery wrapper returned zero results.

## Decision

Public SciTeX delivery uses `scitex_dev.status` and its canonical exchange id:

1. The responder persists and returns `StatusCode(kind="http", code=202, ...)`
   with the exchange id.
2. Callers poll that exchange. Finality is determined only by
   `StatusCode.final`; `http/102` is non-final and keeps the same exchange.
3. A terminal `http/200` is success. A terminal `http/5xx` is an immutable
   failed attempt. Retrying the same delivery and operation after terminal
   failure requires a newly persisted exchange; no component rewrites a final
   ledger row.
4. Adopted Cards exchanges are accepted only when exchange id, initiator,
   responder, operation, and delivery identity agree.

Hermes acceptance uses native `prompt.submit`, never keyboard or tmux message
injection. Visibility checks activate the session with `omit_messages=True`,
which retains bounded live inflight and queue state without reconstructing the
transcript. Historical retry proof uses authenticated `/api/sessions/search`,
an exact delivery phrase, literal marker verification, `role=user`, and the
active session id or lineage root. The response is capped at 256 KiB and fails
closed if oversized or malformed. Therefore retry work is bounded by the
projection, not by total transcript length.

SciTeX shared state for exchanges, Cards, and SAC is the PostgreSQL store named
only by `SCITEX_STORE_DSN` on port 55432. `SCITEX_CARDS_DB` is not a supported
fallback. Hermes may retain its own `.hermes/state.db`; that file is private
session/context state and is never described or used as SciTeX shared state.

## Consequences

An HTTP 202 is an acceptance receipt, not completed text. Pollers remain active
through non-final statuses and stop only at `StatusCode.final`. Terminal failure
rotation preserves audit history while preventing accidental replay into a
closed exchange. Long-running Hermes sessions do not put complete transcript
frames on the visibility path. Missing canonical store configuration and
unbounded or ambiguous visibility evidence fail loudly without exposing DSNs.
