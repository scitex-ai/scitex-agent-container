---
description: |
  [TOPIC] TUI auth-failure pattern-recognition — the watchdog that reads a `tui-<agent>` pane and tells a REAL "Login expired" banner apart from an agent QUOTING it
  [DETAILS] SDK-runner agents write a machine-readable `session.jsonl`; TUI agents write their transcript INSIDE the container overlay, invisible to file scans, so the ONLY way to see a login-stuck TUI agent is to read its tmux pane. Under the operator's `:ro`-mount + hard-restart auth ruling this pane-reader is the SOLE safety net (no mount mode prevents the stale-in-memory-OAuth-token death — only detect→restart recovers it). This skill teaches a maintainer the detection contract, how to extend the patterns when Claude Code's TUI rendering changes, and how to verify the matcher has not gone BLIND. Load it before touching `tui_auth_detect.py`, `auth_status.py`, or `sac agents auth-status`.
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
| Consumed by | `auth-heal.py` **cron** — the actual running safety net | `sac agents auth-status` — **on-demand** CLI check |
| Corroboration | frozen **whole-pane SHA-1 signature** across two runs | frozen **(banner-kind, distance-from-prompt)** across two runs + near-prompt gating |
| Status | **this is what protects the fleet today** | hardened on-demand check + intended future replacement |

**They are not wired together yet.** The near-prompt / distance heuristic
(below) lives **only** in the package matcher; the LIVE cron watchdog still
uses the frozen-signature banner match. Replacing the LIVE watchdog's core
with the package matcher is tracked separately — **do not assume the cron
watchdog already has the distance heuristic.** Where the two diverge, this
skill says so explicitly.

## The detection contract (hard-won invariants)

Each rule below is a guardrail added after a real false-positive or miss.
Break one and you either flag a healthy agent or (worse) miss a dead one.

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
  **kind and distance from the prompt**; see §5.

Volatile-stripping is why a genuine 5-minute tool call (spinner ticking every
second) is never mistaken for stuck: only cosmetic chrome moves, so its
signature is stable **only when the agent has truly stopped producing output**.

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
but off-tail. They are not yet unified (see §two matchers).

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

- [03_auto-accept.md](03_auto-accept.md) — the *other* pane matcher (`prompts.py`), for startup-prompt auto-accept on the `-L sac` server
- [26_credentials-rotation.md](26_credentials-rotation.md) — the stale-in-memory-OAuth-token death this watchdog recovers from
- [27_credentials-relogin.md](27_credentials-relogin.md) — verified re-login + 401 recovery
- [28_credential-refresh.md](28_credential-refresh.md) — refresh without restart (running agents re-read on next turn)
- [13_observability.md](13_observability.md) — `sac agents status` JSON contract
