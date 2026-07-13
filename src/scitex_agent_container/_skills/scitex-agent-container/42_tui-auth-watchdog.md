---
description: |
  [TOPIC] TUI auth-failure pattern-recognition — the watchdog that reads a `tui-<agent>` pane and tells a REAL "Login expired" banner apart from an agent QUOTING it
  [DETAILS] SDK-runner agents write a machine-readable `session.jsonl`; TUI agents write their transcript INSIDE the container overlay, invisible to file scans, so the ONLY way to see a login-stuck TUI agent is to read its tmux pane. Under the operator's `:ro`-mount + hard-restart auth ruling this pane-reader is the SOLE safety net (no mount mode prevents the stale-in-memory-OAuth-token death — only detect→restart recovers it). This leaf is the detection contract's CORE invariants (§1–§4); the package-matcher distance heuristic (§5), tmux topology (§6), auth-heal wiring, and the extend-the-matcher runbook live in the companion [43_tui-auth-watchdog-maintenance.md](43_tui-auth-watchdog-maintenance.md). Load it before touching `tui_auth_detect.py`, `auth_status.py`, or `sac agents auth-status`.
tags: [scitex-agent-container-tui-auth-watchdog, auth, tui, watchdog, detection]
---

# TUI auth-failure pattern recognition (the watchdog)

The fleet runs two kinds of agent:

| Kind | tmux session | Transcript | Auth failure is… |
|---|---|---|---|
| **SDK runner** | none (SDK loop) | `runtime/<agent>/session.jsonl` — machine-readable | a 401 line in the tail; scanned by `auth-heal.py` |
| **TUI agent** | `tui-<agent>` | INSIDE the container overlay — invisible to any host file scan | a rendered banner on the tmux pane; must be READ from the screen |

Because a TUI agent's transcript is unreachable from the host, the **only**
signal that one is wedged on *"Login expired · Please run /login"* is the
banner painted on its pane. Under the operator's auth ruling — credential
mount `:ro`, recovery by **hard restart** — no mount mode prevents the
underlying death (a stale in-memory OAuth token the running process will
never re-read; see [26_credentials-rotation.md](26_credentials-rotation.md)).
**Detect → restart is the only recovery, so this pane-reader is the sole
safety net.** On 2026-07-11 the `figrecipe` TUI agent died in a *"Login
expired"* loop and nothing detected it — the operator had to notice himself.
That incident is why this system exists, and why it is **load-bearing AND
fragile**: it depends on Anthropic's Ink TUI **rendering**, which changes with
Claude Code releases. A maintainer's job is to keep the matcher seeing the
current rendering — that is what this skill is for.

## Two matchers, two homes — know which is live

There are **two** implementations. Do not confuse them.

| | LIVE watchdog | PACKAGE matcher |
|---|---|---|
| File | `~/.scitex/agent-container/bin/tui_auth_detect.py` (dotfiles-tracked) | `src/scitex_agent_container/_runners/_tmux/auth_status.py` (merged PR #627) |
| Consumed by | `auth-heal.py` **cron** — the actual running safety net | `sac agents auth-status` — on-demand check **and the cache WRITER** |
| Corroboration | frozen **whole-pane SHA-1 signature** across two runs | frozen **(banner-kind, distance-from-prompt)** across two runs + near-prompt gating |
| Status | **this is what protects the fleet today** | hardened check + intended future replacement |

The package matcher does not only report — it **writes**. Its verdict is
persisted, and `sac agents list` reads that cache, which is what lets the fleet
view show `auth-failed` instead of a reassuring green `running`. That contract
(what gets written, how a verdict ages, and why the status says `auth-failed`
rather than repeating Claude's misleading *"Login expired"*) is in
[46_agents-list-auth-cache.md](46_agents-list-auth-cache.md).

**They are not wired together yet.** The near-prompt / distance heuristic
(§5, in [43_tui-auth-watchdog-maintenance.md](43_tui-auth-watchdog-maintenance.md)) lives **only** in the package matcher; the LIVE cron watchdog still
uses the frozen-signature banner match. Replacing the LIVE watchdog's core
with the package matcher is tracked separately — **do not assume the cron
watchdog already has the distance heuristic.** Where the two diverge, this
skill says so explicitly.

## The detection contract (hard-won invariants)

Each rule below is a guardrail added after a real false-positive or miss.
Break one and you either flag a healthy agent or (worse) miss a dead one.
Invariants §1–§4 are covered here; §5 (near-prompt distance) and §6 (two
tmux servers) continue in [43_tui-auth-watchdog-maintenance.md](43_tui-auth-watchdog-maintenance.md).

### 1. Only the CURRENT VISIBLE pane — never scrollback

Both matchers capture with `tmux capture-pane -p` and **no `-S` history flag**.
A *"Login expired"* that already scrolled up is **stale** — it does not mean
the agent is stuck *now*. The LIVE `capture_pane()` uses `-p`; the CLI
`_capture()` uses `-p -J` (`-J` joins a banner wrapped across physical lines —
see §topology). Neither passes `-S`.

### 2. The SYSTEM banner, never the phrase in prose

The critical false-positive, observed live 2026-07-11: an agent **discussing**
the incident quotes every trigger phrase in its own output. A naive substring
match flags that agent. Both matchers reject it by requiring the banner to be
**structurally a system line**, not prose:

- **LIVE** (`_banner_line`): strip left TUI decoration with `.lstrip(_MARKERS)`,
  then the line must match `_BANNER_RE` — it **STARTS** with a known reason and
  **ENDS** at `Please run /login` (bounded middle `\b.{0,40}Please run /login$`,
  so a long prose sentence containing the words fails) — **or** `_API4XX_RE`
  (`^API Error:\s*4\d\d\b`) — **or** `_BARE_LOGIN_RE` (`^Please run /login\s*$`).
- **PACKAGE** (`banner_kind`): after `_strip_markers`, the line must **START
  WITH** one of `_AUTH_STARTS`, or match `_API_AUTH_RE`. Anchoring on the START
  rejects prose like `* figrecipe died in a "Login expired" loop` — stripped it
  begins `figrecipe died …`, which starts with none of the phrases.

The `_PROSE` fixture in `tui_auth_detect.py` is exactly the 2026-07-11
offending message (it names figrecipe, quotes *"Login expired"*, *"Please run
/login"*, *invalid_grant*, *authentication_error*). Both the self-test
(`prose.discussing_auth.NOT_flagged`) and the canary
(`prose-false-positive`) assert it stays UN-flagged. **Any change to the
matcher must keep this fixture green.**

### 3. Codepoints that WILL burn you

The TUI is Unicode. Matching the wrong codepoint silently blinds the matcher.

| Glyph | Codepoint | Role | Gotcha |
|---|---|---|---|
| `❯` | **U+276F** | input-prompt marker | **NOT** ASCII `>` (U+003E). `PROMPT_MARKER = "❯"` in `prompts.py`. |
| `⎿` | **U+23BF** | tool/result box marker | precedes a rendered result line, e.g. `⎿  Not logged in · Please run /login`. |
| NBSP | **U+00A0** | the gap Ink renders after `⎿` and `❯` | a real capture reads `⎿\xa0Please run /login` and `❯\xa0` — an ASCII-space match misses it. |

The NBSP is a genuine live-vs-package **divergence**, verified in source:

- **PACKAGE** `_MARKERS` includes `chr(0xA0)` explicitly (added for the
  `head-mba` capture where `⎿\xa0Please run /login` otherwise stripped to
  `\xa0Please run /login` and went undetected). `prompts.py`
  `_detect_compose_pending_unsent` likewise matches `❯[ \t\xa0]+\S`.
- **LIVE** `_MARKERS` (space, tab, `⎿ ✻ ● · │ ┃ ─ ⏺ └ ╭ > ❯ ' " |` backtick
  `* -`) does **NOT** contain the NBSP. Its `_DEAD` fixture (neurovista's real
  capture) happens to render regular spaces after `⎿`, so it passes — but a
  maintainer adding an NBSP-prefixed **bare** `Please run /login` line to the
  LIVE matcher must add U+00A0 to its `_MARKERS`, or `_BARE_LOGIN_RE` will not
  fire.

### 4. STUCK needs corroboration across TWO runs (frozen)

A banner on one capture is not enough — a live agent can be mid-render. Both
matchers require the pane to be **frozen** across two consecutive reads, but
compute "frozen" differently:

- **LIVE** — `pane_signature()` is the SHA-1 of the pane with blank lines and
  every `_VOLATILE_RE` line removed (usage/context gauges, `ctx:NN%` /
  `Nh:NN%` compact bar, elapsed clocks `(1h 32m`, spinner words
  `Fermenting…`/`Pondering…`/…, `Running scheduled task`, `esc to interrupt`,
  token counters, `↓`/`↑`, `Tip:`). `evaluate(pane, prev_sig)` returns
  `stuck = has_error AND prev_sig is not None AND sig == prev_sig`. The three
  outcomes:
  - **idle-healthy** → no banner → never flagged (never even reaches the frozen test).
  - **live / long tool call** → its spinner/clock is volatile-stripped, but a
    *real* new output line changes the signature → **not stuck** (the
    `_DEAD_MOVING` and `_BUSY_1`/`_BUSY_2` fixtures).
  - **frozen on the banner** → banner present AND identical signature on run 2
    → **flagged** (`_DEAD` → `_DEAD_LATER`, where only the clock advanced).
- **PACKAGE** — instead of hashing the whole pane it tracks the banner's
  **kind and distance from the prompt**; see §5 in [43_tui-auth-watchdog-maintenance.md](43_tui-auth-watchdog-maintenance.md).

Volatile-stripping is why a genuine 5-minute tool call (spinner ticking every
second) is never mistaken for stuck: only cosmetic chrome moves, so its
signature is stable **only when the agent has truly stopped producing output**.

## See also

- [43_tui-auth-watchdog-maintenance.md](43_tui-auth-watchdog-maintenance.md) — package matcher §5–§6, `auth-heal.py` guards, the extend-the-matcher runbook, and the blindness canary
- [03_auto-accept.md](03_auto-accept.md) — the *other* pane matcher (`prompts.py`), for startup-prompt auto-accept on the `-L sac` server
- [26_credentials-rotation.md](26_credentials-rotation.md) — the stale-in-memory-OAuth-token death this watchdog recovers from
- [27_credentials-relogin.md](27_credentials-relogin.md) — verified re-login + 401 recovery
- [28_credential-refresh.md](28_credential-refresh.md) — refresh without restart (running agents re-read on next turn)
- [13_observability.md](13_observability.md) — `sac agents status` JSON contract
