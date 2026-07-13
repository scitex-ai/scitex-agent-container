---
description: |
  [TOPIC] TUI auth watchdog — package-matcher distance heuristic, auth-heal wiring, and the extend-the-matcher runbook (companion to the detection-contract core in 42_tui-auth-watchdog.md)
  [DETAILS] Continues the TUI auth-banner detection contract from [42_tui-auth-watchdog.md](42_tui-auth-watchdog.md): §5 near-prompt/distance (the package matcher's prompt-anchored signal, PR #627) and §6 the two tmux servers, then how `auth-heal.py` wraps the pure matcher with grace/debounce/cap guards, the runbook for extending the patterns when Claude Code's Ink TUI rendering changes, and the `canary_ok()` blindness alarm. Read the core invariants §1–§4 in 42 first. Load this before touching `tui_auth_detect.py`, `auth_status.py`, `auth-heal.py`, or `sac agents auth-status`.
tags: [scitex-agent-container-tui-auth-watchdog-maintenance, auth, tui, watchdog, runbook]
---

# TUI auth watchdog — package matcher, guards & runbook

Companion to [42_tui-auth-watchdog.md](42_tui-auth-watchdog.md) — the detection contract's core invariants (§1–§4). This leaf continues with the package matcher's distance heuristic (§5), the two-tmux-server topology (§6), how the detector is wired into `auth-heal.py`, the extend-the-matcher runbook, and the blindness canary.

## Detection contract (continued)

### 5. Near-prompt + distance (package matcher, PR #627)

The package matcher adds a second, prompt-anchored signal (operator
refinement, 2026-07-12). Rationale: a **real** banner sits directly above the
input prompt (it is the conversation tail); an agent **quoting** the banner
pushes that quote up into scrollback as it keeps talking.

`probe_pane(pane)` (in `auth_status.py`):

1. Locate the input line with `prompts.prompt_line_index()` — the **bottom-most**
   line whose stripped text starts with `❯` (a scrollback echo of an earlier
   prompt box sits above the live one, so last-match wins).
2. Walk the non-chrome conversation lines **above** the prompt (`_is_chrome` =
   blank, `_SEPARATOR_RE` box-rule, or `_VOLATILE_RE`), keep the last
   `TAIL_LINES = 6`.
3. A banner counts **only** within that near-prompt tail; report the one
   NEAREST the prompt and its **distance** (count of non-chrome lines between
   it and the prompt).

The result is an `AuthProbe(prompt_found, present, distance, banner)`.
`probe_to_state()` serialises the caller-persisted **local state** —
`{present, distance, banner}` — and `is_stuck(probe, prev)` fires only when
the banner is present now AND `prev["present"]` AND `prev["distance"] ==
distance` AND `prev["banner"] == banner`. So:

- a wedged agent re-renders the SAME banner kind at the SAME distance → **stuck**;
- a working/quoting agent produces output → the distance CHANGES or the banner
  leaves the tail → **not stuck**.

`banner_kind()` returns the **normalised** phrase (e.g. `"Login expired"`, or
`"API Error: 4xx"`), never the raw line — so a volatile `request_id` /
timestamp embedded in the banner does not defeat the cross-run comparison (a
wedged `/loop` agent re-renders the same banner with a NEW request id each
wakeup). This distance heuristic **complements** the LIVE frozen-signature
one: both demand "frozen across two runs", but distance-from-prompt is robust
to a banner that a working agent's later output would have moved, whereas the
whole-pane signature is robust to a banner whose surrounding text is stable
but off-tail. They are not yet unified (see "Two matchers, two homes" in [42_tui-auth-watchdog.md](42_tui-auth-watchdog.md)).

### 6. Two tmux servers — scan the right one

| Fleet | Session name | tmux server | Read by |
|---|---|---|---|
| **TUI agents** | `tui-<name>` | **default** (no `-L`) | LIVE `list_tui_sessions()` + CLI `_list_tui_sessions()` — the watchdog |
| **SDK auto-accept** | `sac-<name>` | **`-L sac`** | `_runners/_tmux/pane_capture.py` — prompt auto-accept, NOT the auth watchdog |

The watchdog enumerates `tui-*` on the **default** server. `pane_capture.py`
(`tmux -L sac capture-pane -t sac-<name>`) is a different concern (auto-accept
of startup prompts, see [03_auto-accept.md](03_auto-accept.md)); do not point
the auth scan at the `-L sac` server.

## The caller's guards (`auth-heal.py`)

`tui_auth_detect.py` is intentionally pure logic (pane text in, verdict out —
no restart). `auth-heal.py::scan_tui()` wraps it with the same
grace/debounce/cap guards it uses for the `session.jsonl` heal, sharing one
state file so the two detectors cannot double-bounce:

| Guard | Value | Meaning |
|---|---|---|
| `RESTART_GRACE_S` | 720 s | after a restart, let the agent boot before re-judging |
| `RESTART_DEBOUNCE_S` | 1800 s | ≥ this between auto-restarts of the same agent |
| `PHONE_DEBOUNCE_S` | 3600 s | at most one persistent-failure phone call / agent / hour |
| `MAX_RESTARTS_PER_HOUR` | 5 | global runaway backstop across the whole fleet |

Past grace but still stuck ⇒ the restart did **not** fix it ⇒ phone the
operator once (genuine outage) and back off. The TUI import is **optional** in
`auth-heal.py` — a missing/broken detector module must never disable the
working `session.jsonl` heal.

## Extending the patterns when the rendering changes — runbook

Claude Code updates its Ink TUI; a reason string or chrome line moves; the
matcher stops flagging (or starts false-flagging). Do this:

1. **Capture the real pane.** From the host, on the default server:
   ```bash
   tmux capture-pane -t tui-<name> -p        # exactly what the matcher sees
   ```
   Save the offending pane verbatim (keep the NBSPs — do not retype it, or you
   will silently normalise U+00A0 to a space).
2. **Add it as a fixture.** In `tui_auth_detect.py`, add alongside
   `_HEALTHY` / `_DEAD` / `_DEAD_LATER` / `_DEAD_MOVING` / `_PROSE` /
   `_BUSY_1` / `_BUSY_2` (and the package matcher's fixtures). A true-positive
   capture goes as a new `_DEAD*`; a false-positive goes as a new `_PROSE*`.
3. **Extend the right pattern:**
   - a new banner **reason** → add to the `_BANNER_RE` alternation (LIVE) and
     `_AUTH_STARTS` (package).
   - a new **volatile** chrome line (a gauge/spinner/clock the update
     introduced) → add to `_VOLATILE_RE` in **both** files, or a frozen pane
     will look alive and never flag.
   - a new **decoration** glyph on the banner's left → add to `_MARKERS`
     (both). If it is a space-like glyph, confirm its codepoint (`hex(ord(c))`)
     — NBSP U+00A0 has bitten this twice.
4. **Run the self-test and the canary:**
   ```bash
   python3 tui_auth_detect.py --test      # 7 groups incl. the prose guard
   python3 tui_auth_detect.py --canary    # the drift-canary in isolation
   python3 tui_auth_detect.py --report    # live fleet, READ-ONLY
   # package matcher:
   pytest tests/scitex_agent_container/_runners/_tmux/   # auth_status + prompts
   sac agents auth-status --json          # live on-demand check
   ```
5. **The non-negotiable rule:** every rendering-dependent change ships a NEW
   fixture proving **BOTH** directions still resolve — the true-positive (a
   real dead pane flags) **and** the prose false-positive (an agent quoting
   the banner does NOT flag). One without the other is how the matcher goes
   quietly wrong.

## Verifying the matcher has not gone BLIND

`canary_ok()` is the drift alarm. `auth-heal.py` calls it **every cycle** and,
on regression, logs `CANARY-FAIL` and phones the operator: *"the matcher may
be BLIND, agents can die undetected."* It re-asserts four load-bearing
behaviours against the fixtures (`_DEAD` is neurovista's REAL captured banner,
so this tests actual rendering, not a guess):

| Canary check | Fails when |
|---|---|
| `real-banner-not-flagged` | a real captured banner no longer flags → **agents can die unseen** |
| `healthy-flagged` | a healthy pane flags → needless restarts |
| `prose-false-positive` | prose quoting the banner flags → the 2026-07-11 bug returns |
| `frozen-not-stuck` | a frozen dead pane no longer reads STUCK on run 2 → no auto-restart |

A CANARY FAIL is the **loudest** signal in the system: it means the sole
safety net may be blind. Treat it as a page — capture the current rendering
(runbook above) and fix the fixture/pattern before anything else.

## Prior art

The Emacs Claude Code project (**ECC**) at
`~/.emacs.d/lisp/emacs-claude-code` has independent TUI-state parsing (prompt
detection, spinner/quiescence classification). If sac's pattern set proves
insufficient for a new rendering, mine ECC for additional patterns before
inventing your own.

## See also

- [42_tui-auth-watchdog.md](42_tui-auth-watchdog.md) — the detection contract's core invariants §1–§4 (visible-pane-only, system-banner-not-prose, codepoints, frozen-corroboration)
- [03_auto-accept.md](03_auto-accept.md) — the *other* pane matcher (`prompts.py`), for startup-prompt auto-accept on the `-L sac` server
- [26_credentials-rotation.md](26_credentials-rotation.md) — the stale-in-memory-OAuth-token death this watchdog recovers from
- [27_credentials-relogin.md](27_credentials-relogin.md) — verified re-login + 401 recovery
- [28_credential-refresh.md](28_credential-refresh.md) — refresh without restart (running agents re-read on next turn)
- [13_observability.md](13_observability.md) — `sac agents status` JSON contract
