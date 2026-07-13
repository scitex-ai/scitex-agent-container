---
description: |
  [TOPIC] Why `sac agents list` can show `auth-failed` — the persisted auth-verdict cache (`agent_auth_state`) that `sac agents auth-status` writes and the fleet view reads, so a tmux-green agent can no longer hide a dead token.
  [DETAILS] Continues [42_tui-auth-watchdog.md](42_tui-auth-watchdog.md): the pane matcher's verdict is UPSERTed into the `agent_auth_state` table in `state.db` (`_state/auth_state.py`) and `sac agents list` reads that cache instead of probing auth inline (two pane captures seconds apart per agent would undo the list's latency budget). Covers the three properties that keep the cache from lying (only observed agents are written; verdicts carry `checked_at` and go STALE; a verdict older than the agent's `started_at` is discarded), and why the status asserts the VERIFIABLE `auth-failed` rather than Claude's misleading "Login expired" banner — with the `expiresAt`-based `revoked` / `expired` / `unknown` cause table (`_account/auth_failure_reason.py`) that separates an automated restart from waking the operator for a re-login. Load before touching `auth_state.py`, `auth_failure_reason.py`, `sac agents auth-status`, or the `sac agents list` status column.
tags: [scitex-agent-container-agents-list-auth-cache, auth, list, cache, staleness]
---

# `sac agents list` and the persisted auth verdict

Companion to [42_tui-auth-watchdog.md](42_tui-auth-watchdog.md) (the detection
contract). That leaf explains how a wedged TUI agent is *detected*; this one
explains how the detection reaches the **fleet view**, so an operator reading
`sac agents list` sees the truth without running anything else.

The problem being solved: a `tmux` session stays perfectly green while the
Claude inside it is auth-dead and will never do another thing. On 2026-07-13 an
agent had to be checked by hand for exactly this.

## The verdict is PERSISTED — that is what makes `sac agents list` honest

`sac agents auth-status` is not only a report; it **writes**. Every verdict it
reaches is UPSERTed into the `agent_auth_state` table in `state.db`
(`_state/auth_state.py`), and `sac agents list` **reads that cache** — it never
probes auth inline, because detection costs two pane captures seconds apart per
agent and would undo PR #635's latency work. A failing agent therefore shows in
the fleet view as its own status, `auth-failed`, instead of a reassuring green
`running`. **So run `auth-status` on a timer: the freshness of that table is
exactly the freshness of the fleet view's auth column.**

Three properties keep the cache from lying:

- **Only observed agents are written.** An agent whose pane could not be
  captured produced no evidence; recording "fine" for it would manufacture the
  false green this whole system exists to abolish.
- **Verdicts age.** Each row carries `checked_at`; the list marks anything older
  than 15 min STALE and says so, rather than presenting it as current truth. If
  nothing has refreshed the table at all, the list says *that* too — green then
  means only "tmux is up".
- **A restart invalidates the past.** A verdict older than the agent's current
  `started_at` describes a dead incarnation and is discarded, so a just-restarted
  agent is not still branded broken.

`auth-failed` is a LIVE status, not a dead one: the agent's session and process
are up, it simply cannot call the API. Every consumer must therefore ask
`is_live_status()` rather than compare against `"running"` — which is why
`sac agents restart --all-running` still reaches an `auth-failed` agent. It is
precisely the agent that most needs restarting.

## Say what is VERIFIABLE: `auth-failed`, not "login expired"

Claude Code renders **every** 401 as `Login expired · Please run /login`. On this
fleet that text is usually **false**, and believing it is why the bug survived
weeks. The mechanism (proven 2026-07-13, four agents lost at once — accounts were
valid for another +4h56m..+7h28m and quota was at 14%): a sibling process ran an
OAuth refresh, consumed the **single-use** `refresh_token`, rotated the access
token, and thereby **REVOKED** the token every other process still held. Nothing
expired. sac's own restart pre-flight was one such refresher (fixed in PR #642).

So the status asserts only what the pane proves — *this agent cannot call the
API* — and the CAUSE is diagnosed separately from `claudeAiOauth.expiresAt`
(`_account/auth_failure_reason.py`):

| `expiresAt` | reason | what actually fixes it |
|---|---|---|
| in the **future** — the on-disk credential is VALID, yet the agent 401s ⇒ its in-memory token was rotated away | `revoked` | **restart** (it re-reads the good file; Claude Code never re-reads it by itself) |
| in the **past** — nothing on disk can authenticate | `expired` | **login** (new credentials must be minted) |
| unreadable / no numeric `expiresAt` | `unknown` | restart first — cheap, safe, and cures the common case |

That distinction is the payoff: it separates a 5-second automated restart from
waking the operator for a re-login. The banner cannot tell you which; `expiresAt`
can.
