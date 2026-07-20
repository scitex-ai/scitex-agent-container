# Changelog

All notable changes to `scitex-agent-container` (sac) are recorded here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [SemVer](https://semver.org/).

## [Unreleased]

### Changed

- **Start / start-failure log output is now readable, not a run-on wall**
  (operator 2026-07-19: "めっちゃ汚い"). Format-only — no control-flow, trigger,
  or suppression behaviour changed:
  - The **start-failure diagnostic** (`raise_start_failure` /
    `_start_failure_diag`) now renders as a headline plus clearly separated,
    indented sections (`tmux session`, `inner stderr`, `pane tail`) with each
    section body indented under its label, instead of one flush-left blob.
  - The per-start **`[sac:creds]` credentials-pool selection notice** is now a
    headline naming the agent + picked account, followed by indented fields
    (`file`, `policy=…`, `picked usage`) and a one-entry-per-line ranking-inputs
    list, instead of a single run-on sentence. It still writes through the
    caller's `log_stream` (the `preflight_from_config_path` dry-probe
    suppression seam is preserved — it is NOT routed to a logger).
  - Hook failures in `_hook_runner` now log at WARNING level (`logger.warning`)
    instead of `print`-ing `[WARN] …` with the severity baked into the message
    text.

## [0.24.1] - 2026-07-20

### Fixed

- **The v0.24.0 quota fail-loud hard-blocked `sac agents restart` and its own
  documented fix did nothing.** The boot picker reads the RUNTIME
  `~/.scitex/agent-container/runtime/quota-cache.json`, but `sac accounts
  refresh-quota-cache` defaulted to the LEGACY `~/.scitex/quota-cache.json` — so
  when the picker went blind (a transient stale-cache window), its error told
  the operator to run `refresh-quota-cache`, which populated a file the picker
  never read, leaving the restart hard-blocked (2026-07-20: `sac-restart
  scitex-dev`). The populator's default and the apptainer bind now resolve to
  the SAME runtime path the reader reads first, so the fail-loud's actionable
  hint actually clears the block. A path-SSOT test (`test_quota_cache_path_ssot`)
  pins writer-default == reader-first-candidate == bind so a re-split goes red.

## [0.24.0] - 2026-07-20

### Changed

- **sac records its own operational events, and no longer writes into a
  third-party application's store.** sac's unattended passes — the fleet
  reconciler, the auth-heal login-expired restarter, the host-sync drift check,
  the worktree GC, the accounts refresh — decide things about the fleet every
  few minutes, forever, with nobody watching. Their verdicts went to stderr and
  into another application's data store. Neither is sac's own record: the stderr
  line lands in a journal nobody opens (which is how a dead cron job stayed dead
  for 49 days), and a store sac does not own can be absent, unwritable or
  renamed — and when it is, sac retains no account of what its own timers
  decided. An unlogged decision is an undebuggable one.

  New `_events/` package: an append-only JSONL log of what sac observed and
  decided, in sac's own vocabulary (`pass-completed`, `subject-degraded`,
  `subject-unknown`, `subject-recovered`, `self-impaired`, `self-recovered`),
  following the shape `_authevents/_log.py` already set. Default
  `<runtime>/sac-events.jsonl`, relocatable with `SAC_EVENT_LOG`, resolved per
  call. Fail-open but never silent — a failed write always prints loudly.

  The four alarm modules each carried a near-identical private copy of the same
  routing helpers; all four are replaced by one shared implementation
  (`_events/_verdicts.py`), removing roughly 240 lines of duplication.

  Interface changes: `reconcile_pass()` / `auth_heal_pass()` take `events_path`
  where they took `store`; the `alarm` block of `sac host sync --json` and
  `sac worktree gc --json` now uses the uniform keys
  `degraded` / `unknown` / `recovered` / `failed`.

  `tests/scitex_agent_container/test__card_package_boundary.py` enforces the
  boundary with an AST scan asserting the importer set EXACTLY equals the two
  files still permitted, so both a new import and a stale allowance go red.

### Fixed

- **The account picker booted an agent onto a quota-exhausted account when the
  quota cache was empty.** It collapsed UNKNOWN quota into "OK" (constitution
  §2 — unknown is a third state, never a pole), kept a blind pin, and read
  `5h=? 7d=?` — on 2026-07-20 scitex-cards could not run after a plain restart
  because the picker kept selecting the exhausted pinned account.
  `pick_healthy_account` gains `require_quota_evidence`: a blind pin rotates off
  toward a known-headroom account, and a fully-blind pick fails loud with an
  actionable `sac accounts refresh-quota-cache` hint. The boot preflight gates
  it on `quota_cache_present()`, so a host WITH a cache whose populator produced
  nothing fails loud, while a cache-LESS host (fresh install / CI /
  quota-cron-less Spartan node) still degrades to freshness-only and boots — the
  documented never-block invariant is preserved. The health-probing layer moved
  to `_creds/_account_health.py` (behaviour-neutral extraction).

## [0.23.0] - 2026-07-20

### Fixed

- **`sac agents restart` inside a container reported success while restarting
  nothing.** The plain restart path decided whether to broker to the host's
  `sac listen` by INSPECTING AN EXCEPTION MESSAGE: it fell back only when the
  local restart raised an error containing the literal substring
  `"not found in registry"`. Local resolution has two legs — a registry row OR a
  resolvable spec — and specs are bind-mounted into every container, so the spec
  leg succeeded, nothing raised, the handler was never consulted, and the restart
  ran locally inside the SIF where it cannot touch the host's tmux session. It
  then printed `Agent 'x' restarted` and exited 0. The broker fired only for
  agents that did not exist at all. Measured: restarting a real agent from a
  container returned rc=0 with no `POST /agents/<name>/restart` in the host
  listen log and the target's pid unchanged 70 minutes later.

  Two faults, one refactor. Gating control flow on a substring of an error
  message is fragile (a reword silently disables the broker) and it INVERTS the
  logic (the fallback requires a failure that the silent success prevents).
  Both the plain and the `--fresh` path — which used a second, different
  predicate — now ask one readable question, `must_broker_to_host()`: *am I
  inside an apptainer SIF?* That is the same rule the start path already uses,
  and the same one the host's restart handler assumes when it strips the SIF env
  markers from the child it shells. In a SIF with no reachable listen the restart
  FAILS LOUD; there is deliberately no fall-through to the local no-op.

- **A restart that changed nothing no longer reports success.** `rc=0` meant
  "the call returned", never "the state changed" — the exit code was
  byte-identical between the no-op and a real restart. A locally-performed
  restart now captures the agent's identity-of-run
  (`<runtime-dir>/<agent>/instance_id`, a uuid7 minted at launch) before and
  after, and refuses to report success unless it CHANGED. The verdict is a
  ternary, never a binary: `true` (a new run exists), `false` (the run is
  unchanged, or gone entirely — both definitive, we held the before-evidence),
  and `null` (no marker either side — no evidence, so the verdict abstains
  rather than inventing a failure). It rides the `--json` envelope as
  `verified` / `verified_reason` / `run_before` / `run_after`; a brokered
  restart relays the HOST's verdict rather than re-deriving one it cannot see.

- **Restart routing is now logged.** Whether a restart was handled locally or
  brokered — and why — is appended as JSONL to
  `<runtime-dir>/logs/restart_decision.log` before any work starts, plus an
  outcome line after. Previously the fact that no request had been sent anywhere
  was recorded nowhere: the listen log can only show what ARRIVED.

- **The version lie, caught by a test instead of by an incident.** `pyproject.toml`
  is bumped to `0.23.0`, and a guard now fails CI whenever the CHANGELOG lists
  pending work under `[Unreleased]` while the declared version is one the
  CHANGELOG has already shipped.

  This is the third occurrence. `v0.21.22` was released to "stop the version lie
  (21 PRs shipped under a spent number)". `v0.22.1` was released because #771's
  `srun` fix never reached the machine — the installed wheel still held pre-#771
  bytes, `grep -c -- --input=none` returned 0, and its version read `0.22.0`
  *because the version was never bumped when #771 merged*. Both were repaired by
  hand, and neither left anything behind that would notice a third time.

  The third time arrived hours after the second: PR #782 merged 1691 lines and a
  new `[codex]` extra onto `develop` while `pyproject.toml` still read `0.22.1`,
  a version already published to PyPI. Installs key their build-wheel cache on
  `(name, version)` rather than on content, so `--force-reinstall` is free to
  serve the published wheel back, report success, and ship none of the new work.
  The version string cannot distinguish the two; only the bytes can.

  The guard is file-only — no network, no git, no tags — so it cannot flake on
  the GPFS-backed runners that have been dropping `_work/_temp` files out from
  under `actions/checkout`. It ships with controls that exercise the predicate
  against the incident state and its bumped counterpart, because a check that has
  never been observed to go red is a hope with a docstring on it.

### Removed

- **The fleet-default pre-stop rescue is ABOLISHED** (operator, 2026-07-19:
  「rescue 一切やめましょう」). `_lifecycle/_pre_stop_rescue.py` and
  `_lifecycle/_pre_stop_rescue_git.py` are archived to `.old/`, the call site
  in `_lifecycle/_stop.agent_stop` is gone, and stopping an agent now leaves a
  dirty worktree exactly as dirty as it found it.

  **Its central contract was enforced against the wrong verb.** The module
  docstring promised "no code path can publish on the agent's behalf" and
  there was deliberately no push primitive — yet rescue commits are reachable
  from `origin/develop` today. The leak was never a push. On a NON-protected
  topic branch the rescue committed IN PLACE; that branch later became a PR
  and was merged normally, carrying the rescue commit into `develop`. A
  no-push guard cannot stop a commit riding a legitimate merge. Verified with
  `git branch -a --contains`: `5340014c` sits on
  `fix/restart-preflight-auth-before-stop` and `1042139e` on
  `feat/a2a-default-communicate-and-role-visibility` — feature branches, not
  the `rescue/` side-branches the design assumed.

  **The damage was real.** Rescue commit `37d83977` (2026-07-01), an ancestor
  of both `develop` and `main`, committed nine `mode 160000` gitlinks under
  `.tmp-audit/` with no `.gitmodules`, breaking `actions/checkout` on every
  run of every workflow until PR #769 removed them. That is the generic
  failure, not bad luck: a broad `git add -A` over an agent's dirty tree
  sweeps up whatever happens to be sitting in it.

  **Nothing else depended on it.** `_state/worktree_safety.is_safe_to_reap`
  independently refuses to reap any worktree whose `git status --porcelain` is
  non-empty, so the lead-learnings/19 prune-destruction window stays closed
  without the rescue; the rescue only ever changed whether the work was
  already committed, never whether the janitor could destroy it. Uncommitted
  work also still survives a restart on its own — `workdir` is a host bind
  mount.

  Regression guard: `tests/scitex_agent_container/_lifecycle/test__stop_no_rescue.py`
  asserts HEAD is unmoved across a stop with a dirty topic-branch worktree
  (RED against the pre-removal code), with controls that the stop still
  succeeds and the runtime still tears down.

  The `<git-dir>/sac-owner` stamp written by the `WorktreeCreate` baseline
  hook is **retained but now has no consumer** — its only reader was the
  rescue's ownership gate. Kept because the write is one cheap out-of-tree
  file and the hook is a baked baseline asset; its docstring now says plainly
  that no ownership gate is enforced anywhere.

### Added

- Add `spec.claude.provider: codex` for keeping Claude Code as the harness
  while routing model calls through scitex-genai's ChatGPT Codex subscription
  gateway. The new `[codex]` extra installs the gateway, and `sac accounts
  list` discovers provider-qualified accounts from SAC's OpenAI store.

- Add `sac accounts sync-openai` and show collected OpenAI Codex/ChatGPT
  accounts without exposing tokens or API keys. The combined table and JSON
  account list distinguish identities such as `openai:person-example-com` and
  `claude-code:person-example-com` while preserving existing JSON fields.

## [0.22.1] - 2026-07-19

### Fixed

- **The SIF bake ran a script the fix had never reached** (follow-up to PR #771).
  #771 correctly identified that an unguarded `srun` was eating the bake script
  off its own stdin, and added `--input=none` to all three `srun` calls. The next
  real bake failed *identically* — build complete, `.partial` left, no
  `SAC_BAKE_RESULT`, and `bake-remote FAILED:` with nothing after the colon.

  The script that ran was never the fixed one. `sac image bake-remote` pipes the
  script off the **installed wheel**, and the installed wheel still held pre-#771
  bytes: `grep -c -- --input=none` on it returned **0**, with all three `srun`
  calls unguarded at lines 223, 266 and 279. Its version read `0.22.0` — and so
  did the checkout, because the version was never bumped when #771 merged. A
  wheel cache keyed on `(name, version)` had served the stale build straight back
  through a `--force-reinstall`. **The version string could not tell the two
  apart; only the bytes could.**

  Three changes, because each half failed on its own:

  - `version` is bumped to `0.22.1`, so the cache key moves with the fix. A
    merged fix that cannot reach a machine is not deployed.
  - `run_remote_bake` now **preflights the script it is about to pipe**
    (`unguarded_srun_invocations`) and refuses to bake when a guard is missing,
    naming the offending file, the installed version, the exact line numbers and
    the cache-busting reinstall that fixes it — instead of spending an hour of
    standing lease producing another orphan `.partial`.
  - Bake failures now **carry their evidence**. `bake-remote FAILED:` composes a
    self-contained headline (a bare colon is invisible to the grep an operator
    actually runs), and the reason carries the remote's exit status, the last
    line of its stdout and the tail of its stderr. Previously `run_remote_bake`
    discarded `returncode` and `stderr` entirely, so a failure that knew it had
    failed could not say why. That is how this bug survived six silent runs.

  Verified against the real artifact: the new preflight, run on the actual stale
  script still installed on the master, reports all three unguarded calls.
## [0.22.0] - 2026-07-19

### Added

- **A single collected fleet AUTH-EVENT log (`sac auth-events`)** (PR #763). Operator,
  2026-07-18: 「サーバーが落とすんだから、ログを取ればいいんじゃないですか？」 — the
  server is what drops us, so log it. New `_authevents` package: an append-only
  JSONL rail at `<runtime>/auth-events.jsonl` (beside `auth-heal.log`), one line
  per auth event, with UTC ISO timestamp, agent, event type, HTTP status,
  account and free-text detail. It **OBSERVES ONLY** — no detector, no
  restarter, no remediation; `auth-heal.py` keeps owning that.

  **Attempt and outcome are SEPARATE records, joined by `attempt_id`.** This is
  the point, not a detail: `auth-heal.log` carried 169 `-> auto-restart` lines
  over seven days whose `age=` field never reset (one reached 262200s = three
  days). Each stated an INTENT in the grammar of an EFFECT, and nothing existed
  that could contradict one. `unresolved_attempts()` now answers "which restarts
  were attempted and never shown to work" — covering both an outcome that says
  `succeeded: false` and an outcome that never arrived. The suite
  mutation-proves the separation: collapsing the two emissions into one combined
  "restarted" event turns four tests red.

  **The rotation event was never missing — it was unjoined.**
  `_account._rotation_audit` has recorded every credential rotation to
  `<accounts-store>/rotation-audit.jsonl` for weeks. Checked against the
  2026-07-18 incident, the last record before six agents died at ~19:30 JST
  reads `10:31:28 UTC` — the deaths, to the minute. So this PR adds **no second
  rotation writer**; `_authevents._timeline` PROJECTS that existing audit at
  read time, leaving `rotation-audit.jsonl` the single source of truth (and its
  fingerprint-only security contract intact) while putting rotations and their
  consequences in one ordered reading.

  Fail-open throughout: every write is best-effort and returns a bool, so an
  unwritable log can never abort the restart or refresh it observes. Fields are
  tri-state — an undeterminable account is written as `null`, never guessed and
  never omitted, since an absent key and an unknown value are different facts.
  No `http_status` is synthesised from a "Login expired" banner: Claude Code
  renders ANY 401 that way, sometimes when nothing expired, so a banner is
  recorded as a banner.

- **One state shape for a peer agent — every signal `True`/`False`/`None`
  (`sac agents state`, `_agentstate`)** (PR #766). Every failure of the
  2026-07-17/18 fleet incident was an UNKNOWN collapsed into a pole. The signals
  were never the bug — that night's own restart log already printed
  `delivery[unknown], process[dead], heartbeat[alive], registry[unknown]`,
  tri-state and correct — and the verdict collapsed anyway. What was wrong was
  the COMBINING, hidden at every call site, each folding whatever subset it
  happened to hold.

  `_spec.py` declares the signal set and which signals are LOAD-BEARING and
  DECISIVE, so adding a criterion is a spec change rather than an edit at N call
  sites. `_state.py` is the frozen `AgentState` dataclass: nine flat named
  predicates, each `Optional[bool]`, plus a per-signal reason map and the RAW
  captures they were read from — the shape never varies, and a non-bool raises
  rather than evaluating as a pole. `_assess.py` is THE single pure fold: True =
  every load-bearing signal healthy; False = one refutes with NO signal unread;
  None = something load-bearing is unread, and the output NAMES which one.
  `_journal.py` archives each reading to `<runtime>/agent-state.jsonl` with the
  full pane captures and the ps line — truncation is MARKED, rotation RENAMES
  and never condenses, because a verdict cannot be re-examined after the fact
  but a capture can.

  Two properties are the point. **Silence becomes a value**: a missing agent is
  an all-`None` row that assesses UNKNOWN, not an absent row that reads as fine
  — the shape that let scitex-hub sit login-expired unnoticed. And
  **disagreement becomes visible**: `auth-status` and `list`, asked minutes
  apart on one host, returned 12 agents and 11, with a live tmux session and a
  live pid on an agent the registry called `defined`; that is now one row
  showing `is_tmux_live=True` beside `is_registry_active=False`.

  Mutation-proved, not asserted: collapsing the `if unresolved:` branch in the
  fold turns 24 tests RED (77 still passing, so the gates are not trivially
  red). Builds ON #758 rather than duplicating it — `_adapt.states_from_detection`
  projects its `DetectionOutcome` + `Roster` into this shape, and the suite pins
  that the projection reproduces the detector's own partition so the two cannot
  drift.

### Fixed

- **`never-stop-when-task-remains`: exit 2 is not a verdict — gate it on a
  parseable payload** (PR #768). The hook shelled out to `scitex-cards may-stop`
  and read exit 2 as "work remains". Exit 2 is ALSO the universal CLI
  usage-error code, so on any host whose scitex-cards predated the verb, click's
  `No such command 'may-stop'` exited 2 and was consumed as an affirmative
  BLOCK — with the usage text forwarded as `reason`, which Claude Code hands
  back to the agent as its next instruction. Agents were told, repeatedly, that
  they may not stop because of an error we could not interpret. The fail-open
  path was never reached: the subprocess did not fail, it answered with a number
  that meant two different things.

  rc=2 now blocks ONLY when stdout also parses as the expected verdict (a
  `runnable` key); rc=2 with empty or unparseable stdout is UNKNOWN and fails
  open, loudly. `reason` is composed strictly from the parsed payload — raw
  stderr never reaches it, since an unstructured channel carrying deprecation
  notices and usage errors must not become an instruction. Volatile fields
  (`idle_seconds`) are excluded so the loop-guard signature is stable and the
  guard can actually trip; it could not before, because the reason moved every
  turn.

  A SECOND defect in the opposite direction was found while fixing: `may-stop`
  answers in its OWN schema (`{agent, runnable, items, idle_seconds}`) with no
  `decision` key, so `payload.get("decision") == "block"` was false and the gate
  ALLOWED every stop on hosts where the detector works correctly — the feature
  was inert wherever it was not harmful. The suite hid this by only ever feeding
  it hook-protocol JSON the detector never emits; fixtures are now captured from
  real runs. Diagnostics now report the argv executed, the resolved absolute
  binary, and the version it claims, and `MIN_CARDS_VERSION` states the floor
  rather than leaving skew to be discovered by an agent that cannot stop.

- **CI: drop 9 committed `.tmp-audit` gitlinks that made checkout re-clone every
  run** (PR #769). `develop` and `main` carried nine mode-160000 gitlinks under
  `.tmp-audit/` with no `.gitmodules`, swept in by the 2026-07-01 rescue autosave
  commit whose broad `git add` ran over a checkout with an unignored
  ecosystem-audit scratch dir. A gitlink with no mapping makes `git submodule
  status` fatal; `actions/checkout` runs that on every job, reads the fatal as
  "Bad Submodules found", and DELETES AND RE-CLONES the whole workspace — every
  run, every runner. On self-hosted Spartan runners sharing one filesystem that
  is not merely slow: it widens the window for the shared-FS races (ESTALE on
  the toolcache, locked `_temp` file-command files) that actually turn runs red.
  Verified: a clean checkout of develop reproduced the error byte-for-byte
  (rc=128); afterwards `git submodule status` exits 0 with no output.

- **Recipes: a floor alone does not upgrade — `-U`/`--upgrade` so the routine
  bake installs LATEST** (PR #765). Unpinning was only half of "a routine bake
  must pick up new code". Every requirement in both recipes is already a FLOOR,
  but a floor that is ALREADY SATISFIED is a no-op: uv/pip leave the installed
  build in place and exit 0. That is exactly the steady state for the `:scitex`
  layer, which bootstraps `From: ./sac-base.sif` and therefore INHERITS base's
  venv with every scitex package already present — so without `-U` a rebake
  re-resolves nothing, reships yesterday's packages, and reports success. A
  FRESH-base bake happens to work either way, which is why this went unnoticed.
  Operator ruling (2026-07-17): 「pin 止めしてたら routine 焼きする意味ないですからね」.
  The bake now runs on a timer, so the silent no-op was live, not theoretical.
  Both sac local-source installs are deliberately unchanged (`--reinstall-package`
  / `--force-reinstall --no-deps` are the stronger, content-correct levers), and
  the embedded freshness-gate heredoc still hashes identically in both `.def`
  files.

- **Security: make the creds-watch change key see a same-size token rotation**
  (PR #761). `creds_watch._signature` returned `(st_mtime, st_size)` — an
  equality key built from a float-second mtime, so two writes inside one
  timestamp granule with the same length compare equal and the watcher concludes
  nothing changed. That is not hypothetical for this file: a rotated token bundle
  is the SAME LENGTH as its predecessor (measured on this fleet, the live
  credential and its immediately-preceding `.bak` are both exactly 1102 bytes, as
  is a backup from four days earlier), so `st_size` cannot see a rotation at all
  and the timestamp was the only real term. Because a shared-account
  `refreshToken` is single-use — one rotation invalidates every co-tenant's
  in-memory token — a watcher that can miss a rotation is a watcher that cannot
  warn anyone. The key is now `(st_mtime_ns, st_size, st_ino)`.

## [0.21.26] - 2026-07-18

### Added

- **Periodic Spartan-side SIF bake + master pull/verify/atomic-swap (`sac image
  bake-remote`, `sac.spartan-sif-bake` timer)** (PR #739). Per the operator's
  2026-07-17 directive: bake fresh SIFs on Spartan and rsync them to the master,
  so the master gets fresh images without spending its own CPU.
  `containers/spartan-sif-bake.sh` is wheel-shipped and ssh-piped — nothing is
  deployed to Spartan — and resolves the standing CPU lease BY NAME, runs
  `srun --overlap` steps (never `sbatch`, never a login node), and gates on
  quota (an UNKNOWN quota is a failure, not a pass), the `.def` `%post` gate and
  an artifact symbol probe. The master leg pulls to a dot-prefixed `.incoming`
  name, RE-VERIFIES independently (sha256 against the remote sidecar plus the
  same symbol probe through local `apptainer exec`), then does an atomic
  double-symlink swap and keep-3 prune. A failed leg leaves the live image
  untouched and exits non-zero. The bake emits a single machine-readable
  `SAC_BAKE_RESULT` verdict (BAKED/SKIPPED/FAILED — absence is a distinct
  NO_RESULT state, not an implied pass). This PR also completes the
  `scitex-todo` → `scitex-cards` rename inside the `%post` gates: #734 flipped
  the install pins but left `md.version("scitex-todo")` and the dist-count loop
  on the old name, so the FIRST CLEAN bake died on `PackageNotFoundError` —
  earlier master bakes had only passed via a lingering pre-rename `.dist-info`.

- **sac REGISTERS scitex-cards' Stop hook, so an agent cannot sit idle while its
  board holds runnable work** (PR #755). The invariant: idle-with-work-pending
  must be unreachable BY DESIGN, not a state detected afterwards and repaired.
  On 2026-07-18 scitex-hub sat idle at its prompt for 80+ minutes holding 5
  `in_progress` cards and the OPERATOR noticed it twice; a notification changed
  nothing, because a stopped agent reads nothing. That is the structural flaw in
  every notify/sweep design — the repair arrives at the one moment its subject
  cannot act on it — so the trigger fires at the only instant the agent is still
  running: turn end. **The ownership boundary is the point of this change.** The
  first cut parsed scitex-cards' stdout JSON *and* their numbered stderr hints,
  making their output an API they could not change without breaking us — the
  exact coupling that was deleted in the other direction when cards' bridge was
  killed for depending on sac. We removed their dependency on us and then
  quietly built ours on them. The agreed split: **scitex-cards ships the hook
  EXECUTABLE** and emits the Stop-hook JSON itself (both ends of that contract
  are theirs); **sac REGISTERS it** — settings materialisation, deployment to
  every agent, the de-dupe algebra, the loop guard, fail-open. The parsing layer
  is gone (−2,115 lines net on the feature's own files); sac now reads only the
  Claude Code hook protocol, which both sides already target, and forwards their
  payload verbatim including unknown fields, so cards can evolve their output
  with no sac release. Three states, never two: allow / runnable / unknown — an
  exit 2 we merely failed to PARSE is still runnable and still blocks (the exit
  code already proved work exists); only a genuinely unreadable detector yields
  unknown, which ALLOWS the stop and logs loudly. Loop guard N=3 on consecutive
  blocks with an identical opaque block text; on trip it alarms and allows,
  because an agent that can never end its turn is a worse failure than an idle
  one.

### Fixed

- **A login-expired pass can no longer report a fleet it never read
  (`Verdict.UNOBSERVED`)** (PR #758). The detector computed a correct
  three-state verdict and then threw two of the states away:
  `detect_login_expired` returned `list[str]`, so `unknown` (pane unreadable)
  had nowhere to go and was dropped, and `exit_code()` fell through to a bare
  `return 0`. An empty report therefore meant BOTH "we checked everything and
  all is well" AND "we observed nothing at all" — and systemd recorded
  `Result=success ExecMainStatus=0` on every one of those passes. The population
  was collapsed the same way: `capture_live_panes` built its keys from live tmux
  sessions, so an agent whose session was gone never became a key and could not
  be reported as anything — **the enumeration WAS the population, which made
  absence invisible by construction.** Now a frozen `DetectionOutcome` carries
  auth_failed / ok / unknown out of the detector in buckets that partition the
  input (nothing handed in can vanish); `Roster` + `registered_agents()` supply
  an independent population reusing fleet-reconcile's own registry enumeration
  rather than a second source of truth, and a registry we cannot enumerate is
  `readable=False`, never an empty roster — an empty roster would silently
  certify that nobody is missing. `exit_code()` reserves 0 for "every roster
  agent was observed and none is wedged"; UNOBSERVED joins BUDGET_UNKNOWN at 2.
  The alarm no longer resolves an escalation card for an UNOBSERVED agent:
  clearing a card on a reading we never took is a false all-clear in its most
  durable form. Only `auth_failed` still authorises a restart.

- **`--force` is propagated end to end, and a no-op restart reports
  `restarted: false` instead of lying** (PR #756). An in-SIF RESTART could
  report success over an agent that never cycled. `agent_restart` calls
  `agent_start(force=True)` precisely because a restart must REPLACE the
  process, but the in-SIF broker fires BEFORE that force is consulted locally
  and had no `force` parameter at all, so the flag was silently dropped at the
  container boundary. The host then ran a plain, unforced
  `sac agents start <name>`, hit the idempotent "already running → no-op"
  branch, printed `SUCC: <name> started` and exited 0 — while the API answered
  `{"restarted": true, "dispatched": false}` over a process whose pid never
  changed, so a caller counting rc=0 marked an unrestarted agent as rolled.
  (Observed in the scitex-storage runtime dir, STARTUP_FAILED 2026-07-12,
  `phase=post_ack_liveness kind=post_ack_no_apptainer_pid exit_code=0`: because
  no container launched, no `apptainer_pid` was ever written, which is what
  tripped the post-ack probe.) `force` now crosses every hop through to the host
  handler's `--force`, emitted only when truthy so a pre-fix host ignores the
  absent field. The no-op branch returns a tagged `NOOP_ALREADY_RUNNING` that
  stays truthy — an idempotent `sac agents start` IS a success, and downgrading
  it would invent the mirror-image lie — but now carries WHY, so a caller whose
  contract is "the process must have CYCLED" can tell the two apart.
  `sac agents restart` reports `restarted: false` with
  `reason: "already-running"`, a hint naming the `--force` recovery, and a
  non-zero exit.

## [0.21.25] - 2026-07-18

### Added

- **A fleet-default env layer — ONE variable can now reach every agent without
  editing N specs in another repo** (`runtimes/_fleet_env.py`, PR #754). Before
  this the only path from config to container env was `spec.env`, so a
  fleet-wide flag meant editing every spec — and the specs live in the dotfiles
  repo, not here, which is why fleet-wide flags never happened. Precedence,
  lowest → highest: `FLEET_DEFAULT_ENV` (sac's declared data) → `config.yaml`
  `spec.fleet_default_env` (operator, host scope) → `spec.env` (per-agent,
  **always wins**). Unlike `_layer_merge.deep_merge_layers`, which RAISES on a
  scalar collision (correct for `to_home` peer layers, where each key is owned
  by exactly one layer), a collision here is the feature: a default exists in
  order to be overridden, so this follows the `_envrc` cascade idiom and logs
  overrides at INFO instead. The defaults are DATA — no sac logic names a
  consumer, and per-agent opt-out needs no new mechanism (set the key in
  `spec.env`, or `""` to neutralise). Seeded with `SCITEX_CARDS_DUAL_WRITE=1`
  and `SCITEX_CARDS_READ_BACKEND=sqlite`.

- **`auth-heal` is declared as a `kind="timer"` JobSpec (`sac.heal-agent-auth`)
  instead of a hand-written crontab line** (PR #753). The cron line was
  temporary BY CONSTRUCTION: `~/.dotfiles/src/.cron/copy_crontab` installs the
  tracked manifest WHOLESALE, so any line absent from `.crontab_list` is erased
  on its next run — and auth-heal has no line in that manifest at all, which is
  why the wrapper exporting `SAC_SECRETS_ENVRC` kept reverting. `kind="timer"`
  materialises a systemd `--user` timer with `Persistent=true`, so a window
  missed while the host slept fires on resume — the property a crontab line
  never had, and the one a laptop fleet needs most. Both `ExecStart` tokens are
  absolute, because a systemd `--user` unit's minimal PATH would otherwise
  resolve the script's `#!/usr/bin/env python3` to the SYSTEM python rather
  than the 3.11 venv the fleet runs on. **DEPLOY GATE:** this overlaps
  `sac.restart-login-expired-agents`, which reimplements this job's `scan_tui`
  natively. Declaring both is safe — a JobSpec is inert until `ecosystem up`
  installs it — but only ONE may ever be ENABLED, or two restarters with
  independent debounce state run on one fleet. Documented on both specs and
  pinned by a test.

### Fixed

- **The bake shipped pre-#742 sac through four consecutive green builds — three
  defects, all closed** (`apptainer-base.def` / `apptainer-scitex.def`, PR
  #752). (1) `%files` NESTING, the root cause: apptainer copies INTO an
  existing destination dir, so a base that already baked
  `/opt/scitex-agent-container-src` made the scitex layer's copy land nested,
  leaving the OUTER stale tree as the one uv built (measured in-image:
  outer=1edf17d0 pre-#742, nested=1e4870fd #742, installed=1edf17d0). Now
  flattened deterministically, then fail-loud if still nested. (2) PIP FLAGS
  PASSED TO UV — `--no-cache-dir` / `--force-reinstall` are pip spellings that
  uv does not honour as cache-bypass, so it reinstalled a cached stale wheel
  (the cache is keyed on name+version, and sac's version does not advance
  per-commit); uv's lever is `--reinstall-package`, which the recipe's own
  comment already named and never applied. (3) CONTENT-ASSERT FALSE-GREEN — it
  resolved the installed tree via `scitex_agent_container.__file__`, which
  during `%post` can point at the STAGED tree, comparing it to itself and
  passing regardless; now resolved from sysconfig purelib and fail-loud when
  sac is absent from site-packages or both operands resolve to one directory.

- **The build-time freshness gate compares shared `.py` CONTENT, not whole-tree
  set equality** (PR #751). PR #749's gate hashed the whole installed `.py`
  tree and required it to equal the staged-source tree-hash. That is a set
  equality and it false-positived: the installed package tree legitimately
  differs AS A SET from the source tree (the wheel excludes/relocates some
  `.py`), so a CORRECT build still FAILED — the bake was right, the assert
  lied, and it blocked EVERY bake. Now a per-file intersection compare: key
  each tree's `.py` by its package-relative path, compare content only for
  files present in BOTH, and ignore files in only one tree (packaging
  set-difference, not staleness). A real stale wheel still trips it, and a
  misalignment guard fails loudly below 50 shared files.

- **The pre-stop rescue only commits worktrees it OWNS** (PR #747). On a SHARED
  checkout (the scitex-cards lane runs 4 agents over one physical checkout)
  `.git` / `.worktrees` / `git worktree list` are shared, so the rescue walked
  peers' worktrees and committed them under the stopping agent's identity
  (observed 2026-07-17: chat committed gui's tree). The push half was already
  removed in #743; this closes the residual local mis-attribution. The
  ownership marker lives OUT of the working tree at `<git-dir>/sac-owner` so
  `git add -A` can never stage it; the `WorktreeCreate` hook stamps it, and a
  three-state `_ownership_allows()` gate DEFAULT-DENIES every `.worktrees/*`
  child whose owner mismatches OR is absent.

- **The `scitex-todo` → `scitex-cards` rename is finished at the surfaces the
  operator actually sees** (PR #754). Contracts with the deployed `.mcp.json`
  keep dual-name tolerance on purpose — a live fleet is rolled one agent at a
  time and that file is not sac's to flip, so a hard rename would classify
  every not-yet-migrated agent's HEALTHY board MCP as absent and manufacture a
  false alarm: `_mcp/_healthcheck.py` gains `SERVER_ALIASES` (a legacy key
  still resolves, reported under the canonical name) and
  `runtimes/_mcp_reliability.py` carries both spellings. Free-form display
  strings (`_listen/_card_event_delivery.py`, `_listen/_notify.py`) are
  straight flips. `BASE_REQUIRED_SKILLS` is deliberately NOT changed — it names
  a skill directory that only exists under the old name, and flipping it would
  put a nonexistent skill into every agent's CLAUDE.md.

- **A bot-less agent no longer ships a `claude-code-telegrammer` MCP entry that
  fails every boot** (`prune_tokenless_telegrammer_mcp`, PR #754). The shared
  baseline `.mcp.json` declares the telegrammer for every agent, so an agent
  with no bot launched it with an empty token, cct correctly refused, and the
  panel carried a permanent failed row — fail-loud that is right for a
  MISCONFIGURED agent and wrong for a deliberately bot-less one, and once the
  entry exists the two are indistinguishable. Wired into
  `_to_home.deploy_to_home` AFTER `ensure_cct_bot_token`; the ordering is
  load-bearing and is asserted end-to-end rather than in a unit test that would
  pass even with the call at the wrong point.

## [0.21.24] - 2026-07-18

### Added

- **`sac agents restart-login-expired` + a `sac.restart-login-expired-agents`
  timer — federated auto-restart for LIVE agents wedged behind a frozen "Login
  expired" banner** (PR #748). This is the half `sac.fleet-reconcile` leaves
  alone (reconcile only touches DEAD / no-session corpses). Detection is
  READ-ONLY and 2-run-corroborated (reuses the `sac agents auth-status`
  matcher, so a banner that MOVED between the two captures counts as working
  and is never restarted); restart goes through the pool-loading
  `agent_restart` path under reconcile's exact rate limits (30-min/agent
  debounce, <=2/agent/hour, <=10/pass); an agent still wedged after the cap
  gets an idempotent scitex-todo escalation card, never an infinite bounce. New
  `_authheal/` package (`_detect`/`_pass`/`_alarm`) with its own history file
  so the two restarters' debounces stay independent. **DEPLOY GATE:** the timer
  is PR-only and must NOT be enabled on a host until that host's legacy
  `auth-heal.py` `scan_tui` is retired — enabling both is a double-supervisor.
  Documented in `_jobs_plugin`, `_pass`, and the CLI help.

- **A guarded, fail-soft `direnv allow` is appended to every agent's default
  `startup_commands`** (PR #745). `load_v3` (the single `load_config`
  chokepoint) appends
  `command -v direnv >/dev/null 2>&1 && [ -f "$PWD/.envrc" ] && direnv allow "$PWD" || true`,
  so a project's non-secret `.envrc` surfaces in-container fleet-wide and is
  VISIBLE in the spec (`AgentConfig.startup_commands`) rather than buried in
  launch code. `$PWD` is the agent workdir at run time (the `bash -lc` wrapper
  inherits apptainer's `--pwd`, and no `cd` is emitted first). The guard skips
  silently when direnv is absent or the workdir has no `.envrc`; the trailing
  `|| true` never breaks boot; it is idempotent (a spec that already runs
  `direnv allow` is not doubled) and appended, not prepended, so an authored
  `startup_commands[0]` keeps its position. Secrets/identity stay
  sac-direct-injected and are never routed through direnv.

### Fixed

- **The bake pipeline now ships the staged source, not a stale uv wheel**
  (`apptainer-base.def` / `apptainer-scitex.def`, PR #749). uv's built-wheel
  cache is keyed on (name, version), and sac's metadata version does not
  advance per-commit, so the sac-source install could serve a byte-identical
  STALE wheel from a prior bake instead of rebuilding the newly staged tree
  (two independent bakers confirmed a green build shipping pre-#742 code).
  `--no-cache-dir` on the sac install forces a fresh build from source; a
  generic installed-vs-staged content assert (sha256 of the installed vs staged
  `.py` trees) folds into both freshness gates and fails the build if they
  differ, naming no feature so a rename never breaks it; and base.def's gate is
  migrated off the old `scitex-todo` dist name onto `scitex-cards` (now a
  metadata-only shim), fixing a `PackageNotFoundError` and the latent twin bug
  in scitex.def's dist-count loop.

- **The CCT bot-token pool no longer folds EMPTY on a caller with
  `SAC_SECRETS_ENVRC` unset** (PR #748, class fix). `sac start`/`restart`
  TRUSTED the caller's env for the token pool, so a cron / raw-ssh /
  federated-timer restart with the var unset stripped Telegram tokens (the root
  cause of the 2026-07-17 CCT token-stripping incident).
  `runtimes/_envrc.resolve_secret_files` now falls back to the canonical
  `$HOME/.bash.d/secrets/010_scitex/*.src` default (the same the listen-unit
  installer computes) when the var is unset, and `_cct_token_pool._pool_env`
  uses it — so ANY caller re-resolves the token, fixing the whole class rather
  than just the timer.

- **A `to_home` deploy now replaces a leftover symlink destination instead of
  writing THROUGH it** (`_clear_readonly_dst`, PR #746). For a symlink dst the
  clear step used to leave the link untouched, so `shutil.copy2` /
  `Path.write_text` followed it: a leftover host-merge link corrupted the
  operator's real host file with agent-interpolated content, and a dangling
  link made `copy2` raise `FileNotFoundError` and abort the deploy. This is the
  unguarded sibling of the same-file guard added for INCIDENT 2026-07-02
  (`_dst_resolves_to_source`, which only covered a link pointing back at the
  SOURCE). `_clear_readonly_dst` now UNLINKS a symlink dst so the write lands a
  real, hermetic file; the legitimate symlink-back-to-source case is still
  short-circuited earlier. Regression covered with real files (no mocks).

## [0.21.23] - 2026-07-18

### Added

- **Inert-feature detector (`_jobs_audit`) — a declaration with no live
  counterpart now fails a REQUIRED gate.** Four features shipped in one night
  (2026-07-17) with PRs, tests and ADRs, and none of them ever executed:
  `sac agents twin` (every derived spec unloadable), the auth `screen` verdict
  (computed, persisted nowhere), `restart.policy` (no enforcer in ~93 specs),
  and `auto-merge-to-develop` (0 runs since 07-09). `_jobs_plugin.py`'s own
  docstring had already named the pathology — *"shipped but scheduled nowhere,
  it was an inert alarm"* — which is the point: we diagnosed it in a comment
  and kept doing it. A postmortem in a comment is not a countermeasure.

  The detector checks two half-pair forms deterministically and reports three
  states (`LIVE` / `INERT` / **`UNKNOWN`** — "I cannot tell" is never "inert",
  because a false INERT that gets a working feature deleted is worse than the
  disease):
  - a JobSpec declared but unreachable via the real `discover_jobs()` — which
    swallows a raising provider with a mere `logging.warning`, so ONE bad spec
    silently drops all four of sac's timers;
  - a declared `kind` no consumer can see, or a consumer filtering on a kind
    outside `ALLOWED_KINDS` (one that can never match anything, ever).

  It runs in `pytest-matrix-on-ubuntu-py{3.11,3.12,3.13}`, a required status
  check on `develop` and `main` — deliberately NOT in `quality-audit`, whose
  every step is `continue-on-error: true` and therefore could never go red. A
  checker nobody runs is just the fifth instance of the disease.

  NOT covered, stated plainly: declared-vs-**deployed** (whether a timer is
  actually installed and enabled on the fleet host) is unanswerable from CI —
  the suite runs in a SIF with no access to the host's `systemctl --user`, and
  answering it from the JobSpec *source* is the exact trap that produced a P0
  diagnosis off a 4-day-stale schedule. Out of scope beats answered wrongly.

### Fixed

- **The pre-stop rescue no longer pushes — it saves work LOCALLY and stops
  there** (`_pre_stop_rescue`, PR #743). The fleet-default autosave that runs
  on `sac agents stop` force-pushed worktrees it did not own, under the
  stopping agent's identity, with no test gate. Observed 2026-07-17: a stopping
  agent force-pushed a peer's `feat/` branch to the shared remote (bytes a peer
  never reviewed, published under a name that never saw them); separately, two
  `rescue:` commits reached `origin/develop`. Operator ruling 2026-07-17
  (「プッシュはなしじゃない？」): the rescue commits locally — in place on a
  topic branch, onto a `rescue/<agent>-<ts>` side-branch on a protected branch
  — and never pushes either. `push_branch()` is **deleted** (function + both
  call sites), so the ban is structural, not conventional: no code path is one
  edit away from publishing again. `workdir` is a host bind mount, so the local
  commit already survives the restart this module exists to make cheap — the
  push was never what made the work durable. Two regression guards
  (`test_rescue_never_pushes_*`) exercise the rescue against a REAL reachable
  origin and assert nothing lands on it, so a reintroduced push goes red — the
  observe-don't-assert acceptance test the earlier #578 lacked. NOT covered:
  why two rescue commits reached `origin/develop` on 2026-07-13 stays open
  (narrowed to a drift/propagation question, not a broken guard — the guard
  fires correctly in current code); removing the push kills the irreversible
  harm regardless of that answer.

- **Every `sac dev` job verb was inert, and had been for weeks.**
  `_dev_jobs.py` passed the CLI GROUP NAME straight through as the JobSpec
  KIND filter, so `sac dev systemd list` asked for `kind="systemd"` — a value
  `JobSpec.validate()` rejects at construction (`ALLOWED_KINDS` is
  `{service,timer,cron}` since scitex-dev #153). All four of sac's real timers
  are `kind="timer"`, so the command printed "No sac systemd-kind jobs." and
  exited 0, forever. `sac dev daemon` was deader still: it filtered a
  never-legal kind AND delegated to `scitex-dev ecosystem daemon`, which is
  not an `ecosystem` subcommand at all. The group is removed; a long-running
  job is `kind="service"`, installed via the `systemd` group.

  The group→kind mapping is now `GROUP_KINDS`, a module-level SSOT that
  mirrors scitex-dev's own selection (`_jobs_cron.py` takes `cron`;
  `_jobs_systemd.py` takes `timer` + `service`), and the audit IMPORTS it
  rather than restating it — a checker that asserts its own opinion of what
  production ought to do is itself a dangling declaration.
  (Card `sac-dev-systemd-group-queries-dead-kind-20260716`, found 2026-07-16
  by watching it fail in production, deferred with a workaround; `docs/
  worktree-gc.md` had been telling operators to route around the broken
  wrapper.)

- **The fixture that hid it.** `test__dev_jobs.py` installed a hand-rolled
  fake `scitex_dev.jobs` whose `_Job` dataclass defaulted to `kind="systemd"`
  — a shape no real JobSpec can have — so 13 tests asserted in green that
  `sac dev systemd list` shows `sac.accounts-refresh` while production showed
  nothing. Identical in kind to the twin suite's 29 green tests over a
  `spec.env` shape v3 validation rejects. The tests now drive the REAL
  `scitex_dev.jobs` with REAL `JobSpec` objects, and skip rather than invent a
  stand-in when the contract is absent.

## [0.21.22] - 2026-07-17

**The version number was itself the bug.** develop carried 21 merged PRs
(#704–#725) while `pyproject.toml` still read `0.21.21` — a number ALREADY
PUBLISHED on PyPI, with DIFFERENT CONTENT. So the installed package and the
source tree agreed on the STRING and disagreed on the CODE, and every
instrument built to catch exactly that — `pip show`, `sac --version`,
`scitex-dev ecosystem check-versions` — compared the two, found them equal,
and reported agreement. All of them were wrong. A stale install and a current
one were INDISTINGUISHABLE by any check we had, which is the operator's
complaint stated precisely: 「どれがどれだかわかりにくい」 — you cannot tell
which is which. The remedy is not a better checker, it is a spent number: bump
and release aggressively, because a version that has been published can never
mean anything else again.

Measured while cutting this release, and the reason it is urgent: the master
host's venv reported `0.21.21` from its `.dist-info` while its `site-packages`
had `_hostsync` but NO `_reconcile` (#724) and NO `_maintenance` (#722) — a
tree matching no released version at all. Verification is by SYMBOL, never by
version string.

### Added

- **`sac agents reconcile` — the enforcer of "should be running => is
  running" (#724).** sac could observe drift and name it, but nothing closed
  the loop: an agent that should have been up simply stayed down until a human
  noticed. The verb makes the declared fleet state authoritative, with budgets
  and alarms so a reconcile pass cannot stampede the fleet.
- **`sac worktree gc` — permanent worktree-sprawl GC + cap alarm (#722).**
  Agent worktrees accumulated without bound; the GC reclaims them and the cap
  alarm fires before sprawl becomes an outage.
- **`sac whoami` + launch-time `CLAUDE.md` orientation (#717).** An agent
  could not reliably answer WHO it was or WHERE it ran, so it guessed —
  and guessed wrong.
- **`sac host push-config` — master-SSOT generated peer client configs
  (ADR-0021) (#718)**, with **bearer tokens riding the same guarded one-way
  channel (#721)** rather than growing a second path to the same hosts.
- **Cross-host liveness — a remote agent is probed ON ITS HOST (#708),** with
  `sac agents attach` reaching a remote agent over ssh (#707), the master
  showing remote-dispatched agents as running-on-peer via live probe (#710),
  and a multihop probe with honest UNKNOWN + spec-derived account (#711).
  Absence of evidence is reported as UNKNOWN, never as DEAD.
- **`sac ci why` — extract the real CI failure cheaply (#714).** Inverts the
  price of diagnosis: the answer costs a command instead of a log crawl.
- **WEDGED verdict state (#715).** A screen instrument, so an auth-dead agent
  sitting in a healthy tmux session can never read ALIVE. A PID is not a pulse.
- **`startup_commands` rejects unguarded `rm -rf $VAR` (#713)** — a
  195-repo landmine, defused at config-validation time.
- **ADR-0020: cross-host (Spartan) agent placement + a2a runbook (#706).**
- **Scheduling for the `sac host sync` drift detector, routed to a SEEN
  scitex-todo card (#716)** — a detector nobody reads is not a detector. (The
  `sac host sync` verb below shipped in 0.21.21 but was left filed under
  *Unreleased* by that release; #716 adds its scheduler.)
- **`sac host sync` — the centre can finally say WHICH CODE runs on a peer, and
  drift stops being silent.** sac could already LAUNCH an agent on Spartan but had
  no way to control the code version there, and nothing announced the difference:
  Spartan's checkout sat FIVE RELEASES STALE (v0.21.14 while develop was v0.21.20),
  with no post-merge pull anywhere, and it was found *by hand*. Two more silent
  divergences surfaced the same day — an agent left a branch checked out in
  Spartan's sac tree (which doubles as the CI runner's audit workspace), so a
  `develop` run audited that branch while claiming to test develop; and `~/.scitex`
  there had been a symlink into an unrelated paper project for weeks.

  The verb ships in two halves, and **detection is the product**:

  - `sac host sync --check <peer>` / `--check --all` — READ-ONLY. Mutates nothing,
    exits non-zero on drift, so it works as a cron alarm rather than a report
    nobody reads. On its first live run it immediately found that `mba`'s fetch
    fails (its code state is genuinely UNKNOWN) and that `nas` runs sac as a plain
    wheel with no checkout to reconcile at all.
  - `sac host sync <peer>` / `--all` — the fast-forward-only remedy, behind loud
    preconditions.

  One-way by construction — code flows centre → remote and a remote never
  originates it:

  - **AHEAD is an ALARM, not a merge.** A peer holding commits the centre lacks has
    already broken the one-way property. sac will not merge them back (that would
    make the remote a source of truth) and will not discard them (that would destroy
    them). It prints them by subject line and REFUSES. A diverged remote is a bug
    report, not a branch to reconcile.
  - **Dirty trees are refused**, never stashed. **`--force` overrides the CI-idle
    guard only** — it buys no destructive git operation, so the verb is safe to run
    unattended.
  - **UNKNOWN is not clean.** An unreachable peer, a failed fetch, or an unreadable
    CI state all refuse; sac never mutates on an unobserved negative.
  - **CI guard:** refuses while the peer's runners are busy *or* a run is merely
    queued — an idle runner is one queued job away from busy, which is less time
    than a merge takes to land.
  - **Verification is by SYMBOL, never by version string** (those are proven liars —
    eleven tags shipped nothing while reporting success). After the fast-forward sac
    re-probes the peer and asserts HEAD is the sha it aimed at, that the interpreter
    LOADS sac from inside that very checkout (catching a wheel or fossil `.dist-info`
    shadowing an editable install), and that a real symbol imports out of it.

  Drift is measured from the git OBJECT GRAPH (`rev-list --count`), never mtimes: a
  plain `git pull` rewrites mtimes without changing content, and GPFS clock skew
  across hosts makes them meaningless. The remote checkout is located by asking the
  peer's own interpreter where it loads sac from — never by expanding a `~` locally,
  which yields the *centre's* home. Everything dispatches through `build_ssh_argv`,
  sac's single remote choke point, so ProxyJump chains and Lmod preambles apply for
  free. Credential distribution deliberately does NOT ride along yet; when it is
  decided it should use this same guarded one-way channel rather than growing a
  second path to the same hosts.

### Fixed

- **A remote tmux probe must not use a login shell (#709).** `ssh peer bash -lc
  "tmux has-session"` returned rc=1 and `open terminal failed: not a terminal`
  against a LIVE session — the login profile poisoned the exit code, so a
  healthy remote agent read DEAD. The probe now calls tmux directly.
- **A stale heartbeat must not outvote a live tmux-DEAD on one instrument
  (#705).** Two readings of the same syscall are one witness, not two.
- **`sac whoami` renders underivable facts as a placeholder shape, not the word
  UNKNOWN (#719)** — "UNKNOWN" is a value a field can legitimately hold, so it
  could not be distinguished from an answer.
- **The privileged group may spawn (#720)** — operator ruling; the strongest
  group was absent from the spawn allowlist.
- **Wedged threads are joined at teardown so a straggler decrement cannot cross
  into the next test (#723).** The escaped thread landed its decrement in an
  unrelated test, which then failed for reasons that were nowhere in its own
  code — a race that made CI a liar rather than a gate.

### Changed

- **`uv` is pinned at all six `setup-uv` call sites (#725).** Every one
  installed `latest`, so CI silently re-rolled its own toolchain on someone
  else's release schedule — a green run proved nothing about tomorrow's.
- **`scitex-todo` pinned to `0.13.5` in the base + scitex container defs
  (#704).**

## [0.21.21] - 2026-07-15

The release exists to carry **#696** to the machines. Everything else here is
what had to be true for it to actually arrive.

### Fixed

- **`sac image build` has been dead since #652, and the SIF is how sac reaches the
  fleet.** #652 added a custom hatchling build hook (`[tool.hatch.build.targets.*
  .hooks.custom] path = "src/hatch_build.py"`). `stage_build_context()` copies
  pyproject.toml, README.md and the package — but nothing taught it about the new
  file, and the wheel never bundled it either. hatchling resolves a hook path
  against *the tree being built*, and in a SIF build that tree is the staged one,
  so every `sac image build` died ~8 minutes into `%post`, inside apptainer, on a
  machine nobody was watching:

      OSError: Build script does not exist: src/hatch_build.py

  Staging now copies **every path pyproject NAMES**, and the wheel force-includes
  the hook into the inert `_bundled/` data dir (no `__init__.py` — bundled is not
  packaged, so hatch_build.py's own rule that an `import hatchling` module never
  reaches the runtime path still holds).

  The reason no test caught it is worth more than the fix: every staging test ran
  against a fixture whose pyproject **declared nothing**. A fixture that declares
  nothing cannot disagree with a stager that copies nothing — the suite was green
  and blind at the same time. The new guard stages the **real** package root and
  asserts the general invariant, derived from the staged pyproject rather than
  hardcoded: *every path pyproject declares must exist in the staged tree.* It
  fails for the next forgotten file too, not just this one. A second test pins that
  the pyproject actually declares a hook, so the guard can never pass vacuously.

### Changed

- `pythonpath = [".", "src"]` — the rootdir joins the test import path, so
  rootdir-relative imports (`tests.*`) resolve identically from any cwd. #698's
  `pytest_sessionstart` guard was re-verified in both directions afterwards: it
  still aborts when `scitex_agent_container` resolves outside `<rootdir>/src`.

### Shipped from develop (already merged, released here)

- **#696 — `may_destroy`'s two witnesses were the same syscall.** Read from inside a
  container — which is where every fleet agent runs — the shipped v0.21.20 reported
  every healthy peer as DEAD with `may_destroy=True`. Its two "independent"
  witnesses were the same `os.kill(pid, 0)` on the same pid, an identity the module
  documented in its own docstring. `Signal` now carries a closed-set `instrument`,
  `may_destroy` dedupes by instrument, and a cross-namespace pid read is UNKNOWN,
  not DEAD. A false-RED is worse than a false-green here: its remedy destroys a
  healthy agent.
- **#698** — a bare `pytest` tested the INSTALLED sac, not the worktree.
- **#699** — the git hooks were advertised and enforced by nothing; they run now.
- **#700** — `check-merge-conflict` was INERT outside a merge (exit 0 without
  reading a byte).
- **#694 / #697** — the last 9 workflows off GitHub-hosted runners, plus the guard
  that keeps them off.

## [0.21.20] - 2026-07-14

### Fixed

- **The PR gate and the release gate are now the SAME environment — eleven tags
  shipped nothing.** PyPI sat at 0.21.17 while v0.21.18, v0.21.19 and eight older
  tags were GHOSTS: the tag exists, PyPI serves nothing. The two gates disagreed
  *by configuration* — the PR gate ran `tests/` single-process on a hosted ubuntu
  box; the release gate ran `-n $(nproc) --dist load` under xdist, inside the SIF,
  on a persistent-`$HOME` Spartan node. A green PR predicted **nothing** about the
  release, and the PR gate was *structurally incapable* of catching what killed it:
  a single-process run cannot race with itself. `test` failed on the release
  runner, `build`/`publish`/`release` were skipped via `needs: test`, and nothing
  reached PyPI. Silently. For months. `pytest-matrix` now runs the **same
  `run-in-sif.sh`** on the **same `vars.CI_RUNS_ON`** runners as the release
  workflow — not an equivalent setup, the same bytes. Green PR ⇒ green release,
  by construction.

- **`claim_port()` lost a race and raised a raw sqlite traceback — the bug that
  ghosted v0.21.18/19.** The pinned-port branch was a TOCTOU: it `SELECT`ed for a
  clash, then `INSERT`ed, with nothing in between. A concurrent claimant landing
  in that window tripped `UNIQUE(port)` and `sqlite3.IntegrityError` escaped to
  the caller. *Which* error you got — the intended diagnosis or a driver
  traceback — was decided purely by thread timing, which is why the failure moved
  between releases and read as a flake. Reproduced deterministically (16 threads,
  real sqlite, no mocks): 6 raw escapes, 9 clean errors. The claim is now a single
  atomic statement (`INSERT ... ON CONFLICT DO NOTHING` + read-back).

- **A lost port race is now resolved by ORIGIN, so a concurrent fleet relaunch
  survives it.** Atomicity alone was not enough — a caught-then-failed claim is
  still a *failed launch*. `resolve_a2a_port` overwrites its own input
  (`"auto"` → the int it claimed), which made two opposite states byte-identical:
  an **operator pin** (a foreign holder is a real misconfiguration → must raise)
  and a **port we auto-allocated** and merely re-claim across a restart (taken
  while we were down → a *fresh* port is correct; a dead agent is not). That lost
  provenance is why a routine `--force` restart of an ordinary auto-port agent was
  traversing the pinned-port code at all. The origin is now threaded through, so
  restarting many agents at once no longer fails politely.

- **A test could act on the operator's LIVE `sac listen` pidfile.**
  `_listen/_single_instance.default_lock_dir()` hard-coded `Path.home()` and
  ignored `SCITEX_AGENT_CONTAINER_RUNTIME_DIR`, so the harness's sandbox floor
  never reached it. Its docstring promised "tests pass an explicit `lock_dir`" — a
  promise enforced by nothing. On the self-hosted release runner (`$HOME` = the
  operator's real home) that directory holds the live control plane's pidfile;
  acting on it tears down the in-memory a2a broker and deafens every agent's inbox
  at once. It now honours the same variable `_session_state.DEFAULT_STATE_ROOT`
  reads (one runtime root, one override) and resolves per-call.

- **`DEFAULT_STATE_ROOT` leaked across tests inside one xdist worker.**
  `test__status_movement`'s `isolated_runtime` fixture reloaded `_session_state`
  with its own tmp dir and never reloaded it back, leaving a dangling root in the
  process global for every later test in that worker. A sibling assertion was
  reading a path belonging to a *different* test — and
  `test_state_dir_for_default_root_is_under_user_home` was asserting the operator's
  real home, i.e. asserting the very pollution the sandbox exists to prevent. Both
  fixed; the latter now exercises the resolution logic instead of the ambient value.

- The sac-state floor is per-xdist-worker (one project-relative floor is a
  host-global namespace shared by every worker in a leg), and `tests/results/` is
  gitignored.

### Changed

- **No GitHub-hosted runners in the test gate.** Per operator rule (2026-07-14):
  if Spartan misbehaves that is *ours* to fix, never a fallback to hosted. The
  `pytest-matrix` job name is deliberately unchanged — it is a required status
  check on both `develop` and `main`, and renaming it would leave every PR blocked
  with all checks passing. Nine other workflows still declare `ubuntu-latest` and
  are tracked separately.


### Added

- **`sac agents rename <old> <new>` — the rename you cannot do safely by hand.**
  An agent writes its own name into six places on disk *plus* the shared task
  board, and the one a human misses is silent. The worst is the board identity:
  `SCITEX_TODO_AGENT_ID` is how scitex-todo knows the agent, so changing it
  without migrating the cards **orphans every card that agent owns** — it can no
  longer see its own work, and nothing tells you. Measured on the live board
  before this verb existed: `scitex-todo` owned **158 cards** (84 of them scoped
  `agent:scitex-todo`); a hand rename would have stranded all of them.

  `rename` moves all seven together, or none:

  1. spec dir `agents/<name>/`
  2. the spec's own self-references — `metadata.labels.project` / `.purpose`,
     `spec.workdir`, the `--overlay` path, `SCITEX_AGENT_CONTAINER_STATE_DB`,
     `SCITEX_TODO_AGENT_ID`, and any identity-bearing bind
  3. overlay dir `containers/overlays/<name>/`
  4. runtime + state dir `runtime/<name>/` (bound at `/state/<name>`)
  5. registry entry `runtime/registry/<name>.json`
  6. `state.db` — 16 name columns + 2 path columns, in one transaction
  7. **task cards**, via scitex-todo's own `reassign_task`

  Properties: **preflight refuses a running agent** (physical evidence — a live
  pid, not a row that merely claims to be open — so a stale row cannot wedge a
  legitimate rename, and a wedged-but-alive agent cannot be renamed out from
  under itself); **atomic** — every step records its inverse and any failure,
  including a *partial* card migration, rolls the whole rename back; a
  postcondition step then proves nothing is left under the old name.
  `--dry-run` prints every location exactly and touches nothing; `-y`/`--yes` is
  **required** to apply and the verb never prompts, so it cannot hang under cron,
  CI, or an agent's non-tty shell.

  The spec is edited by **loading it and changing the known fields** (ruamel
  round-trip, so operator comments and key order survive), never by a regex over
  the YAML — and the rewrite re-parses its own output and refuses to write a spec
  whose semantic diff is anything other than the changes it planned.

  sac **calls** scitex-todo's `reassign_task`; it does not reimplement it. The
  board belongs to scitex-todo, and a forked copy of another package's store
  logic drifts into a worse version of it.

- `SCITEX_AGENT_CONTAINER_ROOT` — override the sac install root that the rename's
  seven locations all derive from. Resolved at call time.

## [0.21.19] — 2026-07-14

**v0.21.18 was a GHOST TAG** — its release run died on a2a port exhaustion caused
by tests that were not isolated from the real state DB. Nine tags in this repo's
history shipped nothing at all. This is the first release that **verifies its own
artifact**, so a ghost is now a RED release rather than a silent success.

### Fixed

- **No test may touch the real sac state (#681).** `agent_start` in a test claimed
  an a2a port in the *real* DB and never released it — `claim_port` consults only
  the database (never whether a port is bound) and `a2a_ports.name` is the primary
  key, so every distinct test-agent name burned another port. Within one run,
  across three concurrent matrix legs, the 1000-port range exhausted **mid-run**
  and killed the release. The guard is **function-scoped** — that is what does the
  work; a session-scoped one would share a single DB across ~10k tests and
  re-exhaust the range from the inside. A canary test pins the scope: unguarded it
  printed `assert 19007 == 19000` — 19007 because the operator's live fleet DB
  already held 7 rows.

### Added

- **The publish job VERIFIES ITS OWN ARTIFACT (#680).** A green `twine upload` is
  evidence the *call returned*, not that the *artifact exists*. Publish now queries
  the version-specific PyPI endpoint (never `/simple/` or `latest` — both are
  CDN-cached and will answer 200 for a version that is not there; measured: this
  repo's own chain read the previous version for ~34s after a successful publish),
  requires real uploaded files, retries for eventual consistency, and **fails the
  release otherwise**. An empty version fails as a *config fault*, explicitly not
  as a ghost — a false RED whose remedy is "do not ship" is worse than the bug it
  catches.

## [0.21.18] — 2026-07-14

### Fixed

- **`sac listen` was declared as a JobSpec that installs a SECOND supervisor (#672).**
  0.21.17 shipped a `sac.listen` `kind="service"` JobSpec. scitex-dev derives the
  unit name from the job name VERBATIM, so it materialises `sac.listen.service`
  (a DOT) beside the `sac-listen.service` (a HYPHEN) that has actually supervised
  the daemon since 2026-07-05 (`Restart=always`, `NRestarts=0`). systemd treats
  them as unrelated units, so `scitex-dev service ensure sac.listen` does not adopt
  the running supervisor — it installs a second one. Two units, both
  `Restart=always`, both binding 127.0.0.1:7878, fighting for the port forever —
  and **every listen restart destroys the in-memory Broker, deafening every
  agent's inbox at once**. The JobSpec is removed and a test now pins its absence.
  `scripts/systemd/README.md` had already warned that the two must not run
  together ("Pick ONE"); it merged anyway.

## [0.21.17] — 2026-07-14

### Added

- **`sac listen start` and `sac listen stop` — `listen` is a NOUN, so give it
  verbs.** `listen` is a command group like `agents` / `db` / `host`, but bare
  `sac listen` *booted a daemon*. Every other noun group in this CLI prints help
  when invoked bare; this one silently became a server on 7878 — so a typo or a
  stray tab-complete started one. That is the anti-pattern the scitex CLI
  convention names outright (§1: *"trailing noun, no action: **never**"*).

  The group already had `restart` and `status` (the latter easy to miss — it was
  absent from the help's example block). It now has the coherent set:

  ```bash
  sac listen start      # boot the daemon (the explicit form of bare `sac listen`)
  sac listen stop       # NEW — idempotent, like `systemctl stop`
  sac listen restart    # unchanged
  sac listen status     # unchanged, now discoverable in `-h`
  ```

  `start`, not `serve`: the §1d catalog reserves `start`/`stop`/`restart` for
  *daemonized* lifecycle (a background process with a pid) and gives `serve` to
  foreground, browser-facing surfaces under a `gui` group. This daemon writes a
  flock-backed pidfile that `stop`/`restart`/`status` all address by pid, and a
  systemd unit supervises it. `start` also keeps SSOT with `sac agents start`.

  Options work on the verb (`sac listen start --bind …`) or on the group
  (`sac listen --bind … start`); the verb wins.

  `stop` is `restart`'s stop half on its own — both now call one implementation
  (`_listen._stop.stop_listen`), so the two verbs cannot drift. It inherits the
  full self-heal: SIGTERM → grace → SIGKILL, verify-dead *before* clearing the
  pidfile, then force-kill a wedged remnant the pidfile never named (the "curl
  hangs forever" case). `--json` emits a machine-readable envelope.

### Deprecated

- **Bare `sac listen` (boot-by-default) — removal in v0.23.0.** It **still
  boots**, and deliberately so. Three launchers still invoke it bare:

  1. `scripts/systemd/sac-listen.service` — `ExecStart=/usr/bin/env sac listen`
  2. `_listen/_restart.py` — the respawn argv, `[sac_binary(), "listen"]`
  3. `_jobs_plugin.py` — the `sac.listen` systemd JobSpec (#543), `command="sac listen"`

  `sac listen` *is* the host control plane on 127.0.0.1:7878, and the whole fleet
  loses host access when it is down — flipping the bare form to show-help would
  take it offline. So this is phase W of the deprecation ladder: **warn and
  forward**, never break.

  Bare boot now prints a stderr warning naming `sac listen start` and the removal
  version. **Follow-up (required before v0.23.0):** move all three launchers to
  `sac listen start` — but only once a `sac` carrying the `start` verb is actually
  *deployed* everywhere, or a respawn/JobSpec would die on "No such command" and
  the control plane would never come back.
### Fixed

- **`a2a_send` reported SUCCESS for a message that reached nobody — agents were
  silently swallowing each other's messages.** Reported by the `grant` agent,
  who measured it live: of four peers that `a2a_peers` listed as running (pid,
  port, start time, group `active`), two delivered and two had
  `delivered_subscriber_count=0`. The tool *did* put "reached no live
  subscriber" in its response **body** — but returned it as a plain
  `list[TextContent]`, and the MCP low-level server stamps `isError=False` on
  anything a handler returns that way. So the protocol classified the call as
  successful. In grant's words: *"Had it returned a bare 200, I would have
  believed the message landed and moved on. How many agent-to-agent messages
  have been swallowed this way? Nobody would know."*

  A 0-subscriber send is now an **MCP error** (`isError=True`), so a caller
  cannot mistake it for delivery. It keeps the structured detail — target,
  machine-readable `code`, subscriber count — and adds what to do instead.
  Notably it does **not** tell you to re-send (sac persists to
  `channel_events` *before* it publishes, and replays undelivered rows on the
  target's next connect, so the message is queued, not lost — re-sending would
  double-deliver), and it does **not** tell you to restart the target
  (0 subscribers means a detached inbox adapter, not a dead agent).

- **`GET /agents` (and so `a2a_peers`) conflated REGISTERED with REACHABLE.**
  Every row said what the registry *declared* — a pid, a port, a group — and
  nothing said whether a message would actually wake anybody. That signal was
  load-bearing: the fleet constitution says to confirm a peer is "alive and
  able to act" before handing it work, and `a2a_peers` is the tool provided to
  do it. It answered yes for a deaf agent.

  Rows now carry `inbox_subscribers` (live SSE subscribers on that agent's
  inbox stream) and `inbox_reachable`, which is **ternary** on purpose:
  `reachable` / `unreachable` / `unknown` — the last for a peer on another host
  whose broker this listen cannot observe. "I could not check" is never
  rendered as either success or death. Same fields on
  `GET /agents/<name>/status` and in `sac agents health --json`.

  `healthy` is deliberately **not** derived from the subscriber count, and
  nothing auto-restarts on it. Measured during this investigation:
  `claude-code-telegrammer`'s zero was a *transient* reconnect window on a
  perfectly healthy agent — a restart would have destroyed a live session.

- **The account picker threw away ~10% of a 7-day Max-20x window, every cycle.**
  `sac accounts list` at the time of the report:

  ```
  alpha-example-com   5h 28%   7d 67%  (resets in 5d 14h)
  beta-example-com  5h  0%   7d 90%  (resets in 9h06m)
  ywatanabe-scitex-ai  5h  0%   7d 90%  (resets in 6 MINUTES)   <- 10% about to evaporate
  ```

  The picker sent every agent to `alpha` (67%), so `ywatanabe-scitex-ai`'s
  remaining **10% of a 7-day window was deleted unused six minutes later**
  (operator: 「毎回 90%で10%捨ててる」 — we bin it every cycle).

  Root cause: `_creds._quota_rank.pick_ranked` **had no notion of when a window
  resets.** It scored "90%, resets in 6 minutes" and "90%, resets in 6 days"
  *identically* and avoided both — but they are opposites. 90% with 6 days left
  is a genuine reserve (avoiding it is correct); 90% with 6 minutes left is
  **use-it-or-lose-it**, and avoiding it burns the capacity for nothing.

  The reset time was not merely unused — it was **unreachable**. The usage API
  returns `resets_at` for both windows and `_account.claude_usage` already parsed
  it, but `_account.quota_cache_refresh` **dropped it when writing the aggregate
  `quota-cache.json` the picker reads**, so the ranker was structurally unable to
  see it. (`sac accounts list` renders "resets in …" from a *different* cache —
  the per-account `usage.json` — which is why it was visible to the human and
  invisible to the picker.)

  Fixed in three parts:
  - `quota_cache_refresh` now persists `reset_at_5h` / `reset_at_7d` into the
    aggregate cache (additive; entries stay valid when upstream omits them).
  - `_quota_rank.is_expiring_7d` classifies a near-capped window whose reset is
    imminent as **expiring, not scarce**: it no longer suffers the `near_capped`
    demotion, and its rendezvous-hash weight is boosted so the fleet spends
    vanishing capacity before a reserve that persists.
  - `_pick_healthy` applies the same rule to the `preferred` account, so an agent
    is no longer rotated *off* the very account whose quota is about to evaporate.

  On the table above the expiring account goes from **0 of 60 agents to 37 of 60**,
  while the 9h-away reserve stays correctly avoided (**0 of 60**) and the fleet
  still spreads (23 of 60 to `alpha`).

  Bounded against a 429 (the risk of routing onto a 90% account): the 5h window
  remains the supreme, untouched gate; an account with <2% of its weekly window
  left is never treated as expiring capacity; the horizon is 2h, so worst-case
  "stuck at the cap" exposure is short; the preference is a spread *weight*, not
  a hard tier, so a bulk restart cannot stack the whole fleet onto a window with
  10% left; and a 429 at these usage levels already classifies as `Mode.ROTATE`
  (`_account.rate_limit_classifier`), which rotates the agent to a *different*
  healthy account — `rotate_account` excludes the current one, so it cannot
  thrash back onto the drained account.

  An old cache with no reset stamps degrades to exactly the previous behaviour.

- **Three CI legs shared ONE tmux socket — this is what failed the 0.21.16
  release (#667).** `SOCKET = "sac-probe-tests"` was a literal. A tmux socket is
  keyed by `(user, socket-NAME)`, **never by process**, so a literal name is a
  *host-global namespace*. The comment above it called the socket "private", and
  it was — private from the **operator's** fleet. Not from our own sibling CI
  legs:

  ```
  test (3.12)  runner=scitex-agent-container-03             23:24:23 -> 23:31:16  [ok]
  test (3.11)  runner=scitex-agent-container-02             23:24:23 -> 23:31:10  [ok]
  test (3.13)  runner=spartan-cpu-scitex-agent-container-01 23:24:23 -> 23:31:25  [FAIL]
  ```

  All three started **in the same second** and overlapped for seven minutes;
  `-01/-02/-03` are three runner *registrations of one physical node*, same user.
  So all three drove a **single** tmux server and killed each other's sessions.
  Note *which* two tests failed: the only two asserting an **exact global count**
  (`== {}` and `len == 30`). The four asserting *membership* structurally cannot
  see a collision. That asymmetry is the fingerprint.

  This is the **same dead invariant #662 already documented** in `exec-in-sif.sh`
  — "runs here are serialised (one job at a time)" stopped being true the day -02
  and -03 were registered. #662 removed *its own* dependence on it and nobody
  grepped for the rest. This was the rest.

  The socket name is now unique per process (pid + uuid), so concurrent legs and
  xdist workers cannot meet. The subprocess deadline went 10s → 30s: a fixed
  deadline tight enough to blow on a three-legs-at-once box is a race, and it
  raises inside the *fixture*, reporting as a broken test rather than a busy
  host. Measured, not assumed — old code, 2 concurrent legs: **FAILED**; new
  code, 3 concurrent legs: **6 passed, 6 passed, 6 passed**.

### Added

- **`sac listen` is now a supervised service (#543).** It is declared as a
  `kind="service"` JobSpec (`sac.listen`, `restart_policy="always"`) via the
  `scitex_dev.jobs` entry point, so `scitex-dev service ensure sac.listen`
  installs and keeps it alive (systemd `--user` where available, a respawn
  keep-alive otherwise). Until now **nothing restarted it**: the listen log
  showed two *clean* shutdowns followed by silence — it had no supervisor at
  all, and the fleet's control plane simply stayed down until a human noticed.

## [0.21.16] — 2026-07-14 *(tagged, never published — see 0.21.17)*

**⚠️ 0.21.16 WAS NEVER PUBLISHED EITHER.** Its release run died on the CI tmux
collision fixed above (#667) — `test (3.13)` failed, so `build`/`publish` never
ran and PyPI stayed on 0.21.14. The *code* below is sound and is included in
0.21.17. **Install 0.21.17.**

**⚠️ 0.21.15 WAS NEVER PUBLISHED — it is tagged, but nothing reached PyPI, and
that is deliberate.** It shipped #658 (the shared-executor fix) *with a hole*:
#658 did not introduce the cancellation race below, but it **widened the window
enormously**, so 0.21.15 would have traded a thread leak for a `sac listen` that
cannot be stopped. It was held back rather than published.

### Fixed

- **`sac listen` could ignore `stop` and `restart` entirely, and never shut down
  (#663).** On **Python 3.11 — the version the fleet runs** — `asyncio.wait_for`
  **silently swallows a cancellation** when the inner future resolves in the same
  instant:

  ```python
  except exceptions.CancelledError:
      if fut.done():
          return fut.result()      # the cancellation is dropped on the floor
  ```

  Every background loop is `while True: await run_blocking_or(...)`, so one
  swallowed cancel and **the loop ticks forever**: `task.cancel()` returns,
  `await task` never does, and the daemon does not die on SIGTERM. `agents stop`
  and `agents restart` leave it running; a "cancelled" task is still probing.
  3.12 rebuilt `wait_for` on `asyncio.timeouts` and has no such branch.

  Measured over 120 cancellations of a 20 Hz loop:

  | dispatch | 3.11.15 | 3.12.3 |
  |---|---|---|
  | `asyncio.wait_for` | **13 swallowed** | 0 |
  | bare `await` (the fix) | 0 | 0 |

  Fix: bare `await future` + a `loop.call_later` deadline. No `wait_for`, no
  swallow branch, on any version. (`asyncio.timeout()` would also work but is
  3.11+, and `requires-python` is `>=3.10`.) The regression test was confirmed to
  **fail against the old dispatch** — a test that has never failed proves nothing.

- **A live agent's registry row was buried 64 seconds after it started (#644).**
  A freshly booted agent has not written a heartbeat yet, and the reaper read
  *no heartbeat* as *dead* and ended its row — after which `agent_send` refused to
  deliver to a demonstrably live agent. Measured: `paper-scitex-clew` started
  18:21:20, row ended 18:22:24, agent alive with a real pid and a real port.
  **Absence of evidence is not evidence of death.** Liveness now yields UNKNOWN
  where it cannot measure, and only an *observed* absence may end a row.

- **Tests raced real servers against arbitrary 5-second deadlines (#661).** The
  loopback server wasn't dead — it was **slow** (`server.started` measured at
  7.49s, because the listen lifespan does a filesystem walk plus SQLite upserts
  before reporting ready). The old wait also **swallowed the server's startup
  exception**, burned its ceiling, and then blamed a timeout — so a *crashed*
  server and a *slow* one produced the identical error. The idiom was copy-pasted
  into **eight** places. Replaced with a shared helper that polls the real ready
  state and distinguishes slow from dead. Verified under 14-way CPU
  oversubscription: 6 failed → 10 passed.

- **The CI leftover-reap could SIGTERM its own sibling matrix legs (#662).** The
  reap is legitimate (leaked `sac` processes hold a2a ports until the allocator
  starves), but it justified itself with *"runs here are serialised (one job at a
  time)"* — true when there was **one** runner, and silently false once `-02` and
  `-03` were registered onto the same node. Now scoped by **age**, so the word the
  comment always used — *leftover* — lives in the mechanism instead of in an
  assumption the world can quietly invalidate.

## [0.21.15] — 2026-07-14 *(tagged, never published — see 0.21.16)*

Honesty release. Every fix here is the same bug wearing a different face:
**something reported a state it had not observed.** The headline (#658) is
the one that had been doing real damage for weeks while looking like
"flaky CI".

### Fixed
- **The shared executor leaked threads until nothing could run (#658).**
  `_off_loop.run_blocking` dispatched via `loop.run_in_executor(None, …)`
  — the event loop's **shared default `ThreadPoolExecutor`**. A
  `concurrent.futures` future that is *already running* **cannot be
  cancelled**, so when the `wait_for` timed out the worker thread was
  never stopped: it kept running the wedged call forever, still holding
  its slot in the pool. Six background loops did this on every
  overrunning tick.
  The default pool is only `min(32, cpu_count + 4)` — **6–8 threads** on
  a small host — and `asyncio.to_thread` uses **that same pool**. So once
  enough slots were gone, *a task that needs a thread simply never runs*:
  a trivial `to_thread(lambda: "ok")` never executed. That is why
  `sac listen` answered `/health` in 0.18s while **every authenticated
  route hung** (the fast path never touches the executor), why brokered
  spawns stalled, and — on Python 3.11, which is what the fleet runs —
  why `sac listen` **could not shut down at all**: pool workers are
  non-daemon, `shutdown_default_executor()` has no timeout before 3.12,
  and loop close joined an orphaned thread forever.
  Fix: a dedicated **daemon** thread per call. An abandoned call now
  leaks exactly one thread (you cannot kill a running thread in Python)
  but **starves nothing**, and outside the pool it is never joined at
  shutdown. Adds an `abandoned_call_count()` gauge, because a silent leak
  is how this survived. `_off_loop.py` had **zero tests** — that is how it
  shipped; the new starvation tests were confirmed to *fail* against the
  old dispatch.
  This one defect had been misread for weeks as "CI is slow", "the
  runners are slow", and "cancelled means flaky". It also **ghosted the
  v0.21.14 release** (the tag pushed; PyPI got nothing).
- **`agent_send` declared every live peer "stopped" (#657).** In a
  container `$HOME` is `/home/agent`, so the peer lookup resolved a
  private, empty state DB and reported `not running` for agents that were
  answering messages at that moment. It now brokers to the host `sac
  listen` through the same door `agent_status` and `agent_spawn` already
  use. Bare-host behaviour unchanged. An unreachable broker, an ACL deny,
  or a 5xx now yields **UNKNOWN — never "stopped"**: failing to *ask*
  is not evidence of death.
- **`restart` reported a restart that never happened (#656).** The stop
  leg warned "previous runtime still running … proceeding to start
  anyway", walked into the duplicate-session collision it had just
  predicted, and printed success over the survivor. It now escalates
  SIGTERM → SIGKILL against the tmux **pane** pid (the launcher exits
  immediately; killing it would look like a fix and do nothing) and
  raises `StopEscalationError` rather than starting on top of a live
  process.
- **`listen` asserted a cause nobody measured (#656).** "the broker is
  unreachable; it may be flapping" was emitted on any transport timeout —
  while the daemon was up and serving. Both clients now probe the
  unauthenticated `/v1/health` **before** naming a cause, and say which
  case they actually observed. The word "flapping" appears nowhere in
  that path: nothing on it can see a crash loop, so nothing on it may
  claim one.
- **A tmux-green agent could hide a dead token (#646).** `sac agents
  list` now surfaces **auth-failed** as distinct from `running` — a live
  session wrapper is not evidence the Claude inside it can make an API
  call. `--all-running` reaches auth-failed agents for `stop` too, not
  just `restart`.
- **Tests read and wrote the live fleet registry (#641).** An isolation
  fixture set `$HOME`, but the DB path is a module-level constant
  computed at **import** time — an env override cannot redirect it. The
  fixture only *looked* like isolation while the tests hit the real
  registry. Both read-paths are now redirected.

### Added
- **`sac ports`** — read-only port-hygiene inventory. One command shows
  every port sac uses with live status: the `sac listen` control-plane
  port (default 7878, resolved from `listen.port`) with its pidfile, and
  every a2a sidecar port CLAIM read in ONE `port_allocator.list_claims()`
  query (the same one-shot API `sac agents list` uses). Liveness is a
  bounded TCP-connect probe (`--timeout`, default 0.3s, run
  concurrently) so the command can never hang. Flags **CONFLICT** (two
  owners on one port) and **ORPHAN** (a claim with nothing listening),
  and prints a reference map of the scitex/sac port-assignment scheme
  (7878 · a2a range · the 3129X GUI/dashboard block). `--json` emits a
  machine-readable envelope. Mutates nothing — no claim is created,
  released, or changed.

## [0.21.14] — 2026-07-13

Incident-response release. The headline fix (#642) closes a fleet-wide
outage in which **`sac agents restart` itself rotated the shared OAuth
token**, killing every other agent on that account. Note that the bug
(#630) and its fix (#642) both land in this cycle, so the rotating
pre-flight **was never published to PyPI** — only `develop` installs
carried it.

### Fixed
- **The auth pre-flight was ROTATING the shared token to "check" it**
  (#642) — the fleet's "Login expired" outage.
  `_restart_preflight.probe_credential_usable` verified a credential
  *by refreshing it*. The OAuth `refresh_token` is **single-use**, so the
  probe **consumed it and minted a new access token**, leaving every other
  agent pinned to that account holding the token that had just been
  replaced. They 401'd, and Claude Code renders a 401 as the misleading
  **"Login expired · Please run /login"** — while nothing had expired and
  quota sat at 14%. Because it ran on **every** restart (pre-stop,
  unconditionally) and in `agent_start`'s force branch, *restarting agents
  to fix them rotated the token again each time, killing the agents just
  restarted*. It called `refresh_account_credentials` directly, bypassing
  the `sac accounts refresh` CLI's `skipped; token still fresh (TTL >= 2h)`
  guard, so it refreshed even a token with seven hours of life left. Only
  two real callers of that mutating refresh exist; this was the sole
  **unguarded** one. Fix: never probe a fresh token — spend the single-use
  grant only near expiry, the same threshold the host timer uses.
  *A probe that consumes the thing it probes is not a probe.*
- **`sac agents restart` reported a FAILED restart as a SUCCESS** (#642).
  `tui_session`'s duplicate-session guard returns `True` after *refusing*
  to start (idempotent for a plain `start`, a lie for a `restart`), so
  `agent_start` reported success, `agent_restart` propagated it, and the
  CLI printed green `Agent '<name>' restarted` directly beneath
  `FAIL: duplicate session`. Restart now **forces** its start leg (so it
  actually replaces the process), the CLI stops discarding the result,
  cross-host trusts the *peer's own verdict* rather than `ssh exited 0`,
  and the duplicate-session guard no longer recommends the very command
  that just failed.
- **Launcher secrets left argv** (#638, P1 security): `apptainer --env K=V`
  puts values in argv, which is world-readable at `/proc/<pid>/cmdline`.
  Secret-shaped vars now travel via a `0600` `--env-file`.
- **`sac listen` crash-loop on port contention** (#640): hot-standby +
  failover with `flock` as the sole atomic bind arbiter, so a duplicate
  daemon stands by instead of `exit 1`-looping under `Restart=always`.
- **Heartbeat tick blew its 30s budget and was abandoned** (#647): the tick
  cost **three `tmux` spawns per agent**, run serially. Batched to one
  `tmux list-sessions` for the whole fleet — **132 spawns / 5.20s → 1 spawn
  / 0.041s (125x)**, measured on 44 real sessions. An abandoned tick also
  stacked zombie threads that starved the shared executor `agent_restart`
  and `host_exec` dispatch through, which is why listen answered
  `/v1/health` 200 while restarts timed out.
- **Tests littered the repo root** (#643): the apptainer test shim wrote
  `sys.argv[2]` on `build`, but production appends an optional
  `--fakeroot` before the SIF — so it created a 1-byte file *named*
  `--fakeroot`, which the project audit then correctly failed. Red-lined
  every PR in the repo.
- **The registry could never prove an agent alive** (#649): **0 of 1229
  instance rows had *ever* carried a pid** — none of the four
  `record_instance_start()` call sites passed one, and the parameter
  defaults to `None`. So `_live_agent_pids()` dropped every agent, and
  `agent_send` refused to reach agents that were demonstrably running
  (tmux up, a2a port `LISTEN`). Corroboration: **0 `crashed` and 0
  `stale-cleared`** exit reasons in the whole history — *both* dead-agent
  reapers skip a NULL pid, so **neither had ever fired**. Now records the
  runtime's own long-lived pid (TUI → the tmux **pane** pid, since the
  launcher exits immediately and recording *it* would store a corpse;
  SDK → the apptainer pid). Remote rows stay NULL deliberately: a peer's
  pid could collide with an unrelated local process, and a *wrong* pid is
  worse than an honest unknown.
- Per-agent directory overlay auto-provisioned (#633); TUI boot prompt
  submitted via literal paste + idle-gated Enter (#632).

### Added
- **`sac --version` now reports the identity of the code that is actually
  LOADED**, not a declared string (#652):
  `scitex-agent-container, version 0.21.14 (g1513a4da wheel 2026-07-13) from /path/to/loaded/package`.
  A version string lies in both directions — a stale wheel, an orphaned
  `.dist-info`, or an image baked months ago all report a version that
  outlived their code. This release exists *because* of such a bug, and
  `0.21.13` reported the same number before and after the fix. The commit
  is read from whichever source is authoritative for the install kind: a
  **live `.git` read** for a source/editable checkout (a stamp written at
  install time goes stale on the next commit), and a **build-time bake**
  for a wheel/SIF (where no `.git` exists). Costs **+1.22 ms**. The heavy
  checks live in a new **`sac provenance`** (`--json`, `--strict` exits 1)
  — which also detects the *shadowed-import* trap, where tests silently
  exercise the installed package instead of your working tree.
- **`sac agents stop --all-running / --all-registry / --all`** (#648) —
  `restart` had bulk selection and `stop` had none, so there was no way to
  stop the fleet during an incident. (`examples/07` had been *documenting*
  `--all` for months without it existing.) Flags, enumeration and
  mutual-exclusion rules are now **shared** between the two verbs, with a
  test asserting they cannot drift apart again.
- `sac agents list`: local-tz `Started`, resolved `Host`, perf fixes (#635).
- `sac accounts list`: readable JST `Since`, relative reset hints, corrected
  fleet 7d-capacity semantics (#636).
- `agent_spawn` / `agent_twin` surfaced to agents, self-serve spawn
  directive, and a fail-loud broker-unreachable error (#639).

## [0.21.13] — 2026-07-11

First PyPI release since 0.21.11. v0.21.12 is a ghost tag (like v0.21.10
before it): its self-hosted release pipeline failed on a since-fixed
audit finding and nothing was published; its content (explicit spec
fields, no hidden defaults) ships here. This release also carries the
2026-07-10/11 incident-response wave.

### Fixed
- **Account-pool outage root cause** (#610): the OAuth token-refresh
  endpoint moved (`console.anthropic.com` → `platform.claude.com`);
  every refresh 404'd and was misreported as "refresh token rejected".
  Correct URL + `$SAC_ANTHROPIC_OAUTH_TOKEN_URL` override, honest
  transport-vs-rejected failure classes, loud per-account refresh
  alarms on every timer run, self-diagnosing picker errors, and the
  credential bind flipped `:ro` → `:rw`.
- **Quota-aware account pick** (#611): avoid 5h-blocked and
  7d-near-capped accounts; load-balance the fleet per agent.
- **`sac accounts list` dedupe** (#614): usage bars own the
  percentages (now with compact reset hints); the table slims to
  Account | Status | Last Update; Email/Plan columns removed.

### Added
- **`sac accounts login <name>`** (#608): semi-automated `claude /login`
  re-auth — only the browser-authorize step stays human.
- **Host-field routing** (#609): `host: local` banned with migration
  hints; `${HOSTNAME}` load-time resolution; transparent remote
  dispatch for lifecycle verbs when `spec.host` names a registered
  peer; fail-loud on unknown hosts.
- **Heavy-job demotion guard** (#612, P1 incident closure): baseline
  hook blocking undemoted image builds/mksquashfs/mass compression
  with the corrected `nice -n 19 ionice -c 2 -n 7` education; builds
  self-demote by default (#605) and warn remote-first under load.
- **Twin spawning** (#613): context-inheriting fork of a running agent
  (CLI verb + MCP tool + skill + ADR) — ephemeral cleaners or
  persistent companions; twins write under their own name.
- **Deterministic CCT bot-token pool injection** (#615): agent start
  resolves the Telegram bot token from the pool into `$HOME/.env`
  (0600, never argv, never logged); loud error naming the pool source
  when a channel-requesting spec has no token. Root `.envrc` untracked
  from the public repo (`.envrc.example` ships instead).

### Changed
- **Release pipeline moved to GitHub-hosted runners** (#616): the
  Spartan self-hosted SIF pipeline was a fail-quiet dependency;
  publishing now uses standard PyPI OIDC trusted publishing
  (org-transfer publisher config fixed 2026-07-11).
- **Load-time advisories route through scitex-logging** (#607) for
  consistent fleet-wide warning/error color coding.

## [0.21.11] — 2026-06-09

PATCH release. Re-cut of v0.21.10's accounts-list redesign after the
v0.21.10 tag's release workflow failed on `test_audit_all_clean` (PR
#349 itself introduced 3 STX-TQ007 violations in the new
`test__account_list_render.py` plus a stale SK-301 SKILL.md WARN that
was still on develop). v0.21.10 exists as a "ghost" tag — no PyPI
artifact, no GitHub Release; operators install
`scitex-agent-container==0.21.11` for the accounts-list redesign
PLUS the cleanup that unblocks the release pipeline.

### Fixed
- **chore(repo): clean 17 pre-existing audit-all violators** (#348,
  unblocks #341/#344/release). Splits 7 STX-TQ007 violators in
  `tests/scitex_agent_container/config/test__loaders_model_chain.py`
  (12→29 tests, 1-assert each via shared Arrange+Act helper); 4
  STX-TQ002 (AAA-marker) violators in
  `tests/scitex_agent_container/_runners/test__session_inbox.py`;
  relocates `tests/scitex_agent_container/containers/
  test_apptainer_scitex_def_libxcb.py` to `tests/integration/` to
  satisfy PS-204 (orphan-test mirror rule) while preserving the
  libxcb / libgl1 / libglib2.0-0 regression guards; adds the
  required `## <N> Interfaces` H2 and Four Freedoms blockquote to
  `README.md` (PS-107 / PS-110); trims `SKILL.md` from 6181→6070
  bytes to clear SK-301 (74-byte margin under the 6144 budget);
  fixes the post-relocation `parents[2]` repo-root walker in the
  moved apptainer-def test; and splits 3 STX-TQ007 violators
  introduced by #349 in `test__account_list_render.py`. No
  behaviour change — only test shape and docs.

### Included from v0.21.10 (ghost tag — not on PyPI)
- **feat(accounts): `sac accounts list` window-reset display**
  (operator gripe via lead, 2026-06-09). See the v0.21.10 entry
  below for the full description. Headers, per-row reset hints,
  and rolling-window legend all ship in v0.21.11.

## [0.21.10] — 2026-06-09

### Changed
- **feat(accounts): `sac accounts list` window-reset display**
  (operator gripe via lead, 2026-06-09). Three operator-visible
  clarifications to the Stored-accounts table; no behaviour change
  in the JSON path (`sac accounts list --json` schema is stable):
  - Column header `As-of` renamed to `Last Update` — the operator
    could not parse the abbreviation.
  - 5h%/7d% cells now carry an inline reset hint computed from the
    Anthropic OAuth usage API's `resets_at` field (parsed by
    `_account.claude_usage` into `reset_at_5h` / `reset_at_7d`).
    Example shapes: `42% (→21:05)` for the 5-hour rolling window,
    `15% (→Thu 17h)` for the 7-day rolling window. Local-tz
    precedence chain unchanged
    (`SCITEX_AGENT_CONTAINER_TZ` > `TZ` > system local).
  - When the upstream API did NOT return reset timestamps (older
    caches / API outage) the CLI prints a one-line legend below
    the table — `5h = rolling 5-hour window; 7d = rolling 7-day
    window` — so the operator still knows the windows are rolling
    rather than calendar-day. Headers stay compact so the table
    fits a typical ~120-col terminal.

  Implementation lifted the pure formatting helpers out of
  `_account_list_render.py` into a sibling `_account_list_format.py`
  so neither module exceeds the 512-line per-file cap (the renderer
  re-exports the helpers so existing imports keep working).
  Two new fields on `AccountRow`: `reset_at_5h` / `reset_at_7d`
  (optional, default `None`). `build_stored_rows` propagates them
  from the per-account `usage.json` cache.

## [0.21.9] — 2026-06-01

### Fixed
- **fix(channel): suppress empty-content delivery acks at the sender**
  (#260, lead handoff). Killed the empty-ack ping-pong spam hitting
  every inbox (incl. lead's). New ``_channel_ack_filter`` +
  ``_channel_auto_ack`` modules; sender-side filter drops empty-
  content acks before the wire so the receiver never sees them.
  Rate-limit env knobs (``SAC_AUTO_ACK_RATE_MAX`` /
  ``SAC_AUTO_ACK_RATE_WINDOW_S``) gate spammy auto-ack loops.
- **fix(smoke): update deny-row assertions for task #27 two-row
  contract** (#281). v0.21.8's pypa-publish failed because the
  smoke test ``test_listen_denied_send_persists_exactly_one_
  channel_events_row`` pinned the pre-task-#27 single-row
  contract; task #27 (block/unblock approve-flow, landed in
  v0.21.8's contents) intentionally added a second row (the
  ``approval_prompt`` push). Smoke test updated to the new two-
  row contract + 5 new assertions on the prompt body (embeds
  both ``sac a2a unblock`` and ``sac a2a block``, does NOT leak
  the sender's body). No production code change; v0.21.8's
  feature set still ships here.

v0.21.9 = v0.21.8's full feature set (#278 + #279, task #27 ACL
approve-prompt flow) re-cut after #260 + #281 unblocked the
release pipeline. The v0.21.8 tag exists on GitHub but is a
"ghost" — no PyPI artifact and no GitHub Release; operators
install ``scitex-agent-container==0.21.9`` for the same feature
set + the empty-ack fix + the smoke-test contract update.

## [0.21.8] — 2026-06-01

### Added
- **feat(acl): block/unblock approve-prompt flow** (#278 + #279,
  operator-requested via lead — task #27). Cross-group denied
  sends now emit ONE receiver-facing prompt embedding BOTH
  ``sac a2a unblock <s> <t>`` and ``sac a2a block <s> <t>`` so
  the receiver picks the verb. Dedupe: repeats from the same
  (sender, target) pair while pending DO NOT re-prompt. UNBLOCK
  writes ``comms_grants`` + removes any ``comms_blocks`` + clears
  the pending row (sender's future messages pass; the original
  denied message is NOT replayed — sender resends). BLOCK writes
  ``comms_blocks`` (block precedence over grant) + clears the
  pending row (sender's future attempts silently dropped — no
  receiver push, no approve-prompt re-fire, sender still gets
  403). New CLI verbs ``sac a2a unblock`` and ``sac a2a block``;
  legacy ``sac a2a grant`` aliased to unblock. The push body
  intentionally does NOT leak the denied message content —
  receivers decide on identity, not on content. Intentionally
  drops the earlier-design TTL knob + latest-wins / replay
  machinery per the operator's "fragile spam-debounce" feedback.
- **feat(acl): in-container broker for the ACL decision CLI verbs**
  (#279, operator-greenlit Q5 / lead FUTURE item 4). Today an
  in-container ``sac a2a {unblock,block,grant}`` used to write the
  per-container state.db — silently ineffective against the host
  listen's ACL checks (which consult the HOST'S state.db, a
  different file). This PR adds three new host-listen routes
  (``POST /v1/acl/{unblock,block,grant}``) + a stdlib-only HTTP
  broker (``_state/_acl_broker_client.py``) that mirrors the
  SAC-from-SAC ``_spawn_client`` pattern from #261. The CLI
  detects in-SIF via ``_lifecycle._in_sif_broker.is_in_sif`` and
  routes accordingly: in-SIF → host listen HTTP, bare-host →
  local DB helpers directly. Receivers can run the verb from any
  context and the write lands on the right db.

## [0.21.7] — 2026-06-01

### Fixed
- **fix(build): pin `hatchling<1.28` to dodge twine
  Metadata-Version 2.5 rejection.** hatchling ≥1.28 emits
  ``Metadata-Version: 2.5`` which the pypa-publish action's bundled
  twine rejects with ``InvalidDistribution: '2.5' is not a valid
  metadata version`` (verified on the v0.21.6 release build,
  workflow run 26728715926 — pypi-publish failed; no artifact landed
  on PyPI and no GitHub Release was created). Hatchling 1.27 emits
  ``Metadata-Version: 2.4`` which twine accepts. Same code contents
  as the unpublished v0.21.6 (see ``[0.21.6]`` section below for the
  feature/fix list); re-evaluate the pin when
  ``pypa/gh-action-pypi-publish`` ships a newer twine.

## [0.21.6] — 2026-06-01

### Added
- **feat(agents): `sac agents forget <name>`** (#270, operator
  backlog #3). New local-only registry-reset recovery verb for the
  "agent is gone, only stale rows persist" case (SLURM-reclaimed
  node, crashed peer that came back fresh, etc.). Tombstones the
  ``instances`` row with ``exit_reason='operator-forget'`` and
  unregisters the ``comms_nodes`` pin. NO ssh, NO local process
  signal. Refuses to act on a live instance unless ``--force`` is
  passed. Idempotent: no rows = no-op exit 0.

### Fixed
- **fix(start): preserve apptainer stderr in SIF build failures**
  (#271, operator backlog #4 partial). Pre-fix shape:
  ``_build_sif_from_{uri,def}`` returned ``False`` on apptainer
  build failure, silently dropping the stderr — callers saw only a
  generic "Failed to start agent" upstream with no diagnostic.
  Now ``capture_output=True`` + raises ``RuntimeError`` with the
  apptainer stderr verbatim. Success path unchanged.
- **fix(tests): drop unused `time` / `typing.Iterator` imports**
  (#273, ruff F401 cleanup on develop).
- **fix(tests): replace `monkeypatch` with `subprocess_shim` in
  test__forget** (#274, PA-306 §3 no-mocks). The new ``forget``
  test used ``monkeypatch.setattr`` which the audit gate
  forbids; refactored to the project's standard no-mocks
  ``subprocess_shim`` fixture (real fake ssh on $PATH).
- **fix(tests): drop literal `# noqa` from comment in
  test__handlers.py** (#275, ruff invalid-directive warning
  cleanup).

### Docs
- **docs(readme): refresh to v0.21.5** (#272). Adds ``sac agents
  forget``, the SAC-from-SAC broker subsection, the
  ``sac-listen.service`` systemd unit install recipe, the
  ``sac dev {systemd,cron,daemon}`` group, and ``sac registry
  sync``. Timestamp bumped.

## [0.21.5] — 2026-06-01

### Added
- **feat(listen): single-instance flock guard for `sac listen`** (#266,
  operator task #26 sub (1)). A second `sac listen` while one already
  holds the port used to crash uvicorn with bare `EADDRINUSE` + a
  Python traceback — loud but with no diagnostic about which process
  held the port. New `_listen/_single_instance.py` (`acquire_listen_lock`,
  `release_listen_lock`, `ListenAlreadyRunningError`, `default_lock_dir`)
  takes a port-scoped flock at `<lock_dir>/listen-<port>.pid` BEFORE
  uvicorn binds, stamps the current PID in the file body, and on
  conflict fails loud with the holding PID and lock-file path so
  `kill <pid>` is actionable without `lsof` / `netstat`. The flock is
  kernel-released on process exit (even SIGKILL / OOM) so a crashed
  listen never permanently jams the port. Acquired before
  `_register_self_comms_node` / `_maybe_sync_on_start` so a duplicate
  launch never touches the federated registry.
- **feat(systemd): `sac-listen.service` hand-maintained user unit**
  (#268, operator task #26 sub (3)). New `scripts/systemd/sac-listen.
  service` (`Type=simple` + `Restart=on-failure` + `RestartSec=5s` +
  `StandardOutput=journal`). The companion flock guard (sub (1))
  ensures `Restart=on-failure` cannot double-bind. The README is
  split into "federated scheduled jobs" (`sac.accounts-refresh`
  pattern, materialised from `scitex_dev.jobs`) vs "hand-maintained
  long-running services" (`sac-listen.service` lives here) so a
  future operator does not move this into the federated path by
  mistake. Install: copy to `~/.config/systemd/user/`,
  `daemon-reload`, `enable --now`.

### Tests
- **test(channel): pin SSE auto-reconnect across listen restart**
  (#267, operator task #26 sub (2)). Three no-mocks regression tests
  in `tests/scitex_agent_container/_mcp/test_channel_reconnect.py`
  using a real asyncio TCP server pin the invariant that the
  in-container SSE consumer (`_mcp/channel.py::_consume_sse`)
  reconnects after the listen drops the stream mid-flight (operator-
  restart scenario), records ≥ 2 actual TCP connection attempts, and
  recovers when the server starts AFTER the consumer is already
  trying. The exponential-backoff loop (0.5s → 30s cap) was already
  implemented; this PR pins it against regression. A flake-guard
  using a held-socket port reservation replaces a brittle "start →
  stop → restart on same port" approach.

## [0.21.4] — 2026-06-01

### Added
- **feat(broker): SAC-from-SAC — in-SIF `agent_start` brokers to host
  `sac listen`** (#261, operator-mandated 2026-06-01). When an agent
  runs INSIDE an apptainer SIF, `sac agents start <child>` (and the
  `agent_start` API) auto-detects the in-SIF condition
  (`APPTAINER_CONTAINER` / `SINGULARITY_CONTAINER`) and POSTs the
  spawn RPC to the host-side `sac listen` instead of trying nested
  apptainer (which is unsupported on the target HPC shape). The host
  re-runs `check_spawn`, records the parent → child lineage edge, and
  shells the real `sac agent start` against the bare host's
  apptainer. New `_lifecycle/_in_sif_broker.py`
  (`is_in_sif`, `broker_start_to_host`, `maybe_broker_in_sif_spawn`,
  `InSifBrokerError`); injection seam `in_sif_opener` on
  `agent_start`. Reuses the existing `SAC_LISTEN_BASE_URL` /
  `SAC_LISTEN_BEARER` env injection from
  `runtimes/_apptainer_listen_env.listen_env_flags`. Fail-loud
  contract: missing base URL / transport / 4xx / 5xx / malformed
  body → `InSifBrokerError` with status + body preserved verbatim.
  Bare-host path unchanged. Also fixes a latent listen-side bug
  exposed by the live test: `_listen/_agent_exec.py::agents_start`
  used to shell `["sac", "agent", "start", name]` (singular, removed
  in F-CS13); switched to `["sac", "agents", "start", name]` with a
  regression test pinning the argv shape.
- **feat(jobs): federate `sac.accounts-refresh` into `scitex_dev.jobs`.**
  New provider `scitex_agent_container._jobs_plugin:provide_jobs`
  registered under the `scitex_dev.jobs` entry-point group surfaces a
  single systemd `JobSpec` (`sac.accounts-refresh`, every 2h,
  `OnBootSec=15min`, `TimeoutStartSec=120s`) running `sac accounts
  refresh --all --skip-active`. New `sac dev {cron,daemon,systemd}`
  subcommands (`list`/`install`/`uninstall`) surface sac's own `sac.*`
  jobs by delegating to scitex-dev's ecosystem aggregator; they degrade
  gracefully (upgrade hint, exit 3) when the installed scitex-dev
  predates `scitex_dev.jobs` (requires `scitex-dev>=0.16.0` in
  production). The provider import is lazy so entry-point metadata is
  install-time only.
- **feat(accounts): `sac accounts refresh --skip-active`.** With
  `--all`, excludes the stored account whose `email_address` matches the
  currently-active `~/.claude` login (`~/.claude.json`
  `oauthAccount.emailAddress`, case-insensitive) so the in-use
  refresh_token is never rotated out from under the live session. No
  active account resolvable → skips nothing and logs it. Behaviour is
  unchanged without the flag.

### Fixed
- **fix(creds): :rw bind the per-account snapshot directly — no
  boot-time copy** (#262, operator task #15). Root cause of the
  2026-06-01 fleet-wide silent 401 outage: agents pinned via
  `spec.claude.account` got a FROZEN BOOT-COPY of the saved account's
  snapshot under `<state_dir>/claude/.credentials.json`. The
  in-container Claude CLI's ~1h OAuth refresh wrote back to that
  per-agent copy — not the source snapshot. After ~8h drift, every
  SDK turn 401'd silently (the telegram bridge still marked inbound
  👀 but the agent could not complete a turn). Hit hub, orochi, and
  proj-scitex-agent-container (revived only by restart). Fix:
  `runtimes/_apptainer_creds.resolve_cred_file` pinned branch now
  returns the snapshot path directly. The caller's existing `:rw`
  bind in `_apptainer_auth.auth_argv` lands on the snapshot — the
  in-container CLI's refresh writeback goes to the snapshot, which
  is now self-healing and never expires while any pinned agent keeps
  running. Same-account-pinned agents now share a single mount
  target; the Claude CLI's atomic refresh writeback (tmp+rename) is
  safe under concurrent refresh. Safety gates (`PinnedAccountError`
  on absent / missing-`expiresAt` / already-expired snapshot)
  preserved verbatim. Operators upgrading mid-deploy with a leftover
  `<state_dir>/claude/.credentials.json` are unaffected (resolver
  neither reads nor mutates the legacy dest; regression test in
  place).
- **fix(tests): satisfy STX-TQ002 AAA-marker rule on safety-gate
  raises tests** (#263). Follow-up cleanup of an audit-gate violation
  that slipped onto develop when #262 was auto-merged before
  pytest-matrix completed. Three safety-gate tests used a combined
  `# Act / Assert` comment; split into `# Act` + `# Assert —
  pytest.raises is the assertion` (matches the sibling
  `test__apptainer_creds.py` style). No production code change.

### Changed
- **refactor(account): extract `sac accounts refresh` into
  `cli_pkg/_account_refresh.py`** (registered onto the `account` group at
  import time, mirroring `_account_sync_live`) to keep `account_group.py`
  under the per-file line cap.
- **chore(systemd): retire the static `sac-accounts-refresh.{service,timer}`
  templates.** The unit files are now generated from the federated
  `JobSpec` via `sac dev systemd install` / `scitex-dev ecosystem systemd
  install`; `scripts/systemd/README.md` documents the new policy (the old
  templates were pinned to the superseded `--all`, every-4h cadence).
- **chore(tests): package-level `conftest.py` clears in-SIF env
  pollution.** Two autouse fixtures clear `APPTAINER_CONTAINER` /
  `SINGULARITY_CONTAINER` (would route every test through the new
  SAC-from-SAC broker) and `SCITEX_AGENT_CONTAINER_AGENT` / `SAC_AGENT`
  (leaks the running agent's identity into statusline tests).
  Side-effect: fixes 8 pre-existing in-SIF env failures that surfaced
  only when pytest runs inside an agent SIF.

## [0.21.3] — 2026-05-28

### Added
- **feat(comms): stage 1 — symmetric federated `comms_nodes`** (ADR-0014,
  #234). New `comms_nodes` table (globally-unique `name` → `(host, a2a_port)`
  routing tuple) added to `KNOWN_TABLES` so `sac db export/import` carries
  it. `resolve_node_host` falls through to `comms_nodes` after the
  `instances` lookup, so a node living on another host's listen (e.g.
  `lead` on `ywata-note-win`) resolves correctly from a peer host.
  Symmetric ssh-pull anti-entropy via new `sac registry sync
  [--from PEER | --to PEER | --all] [--dry-run]` reusing the existing
  `sac db export/import` primitive over the operator's `peers:` ssh
  trust. Listen-startup hook registers the host's operator identity
  (from `LeadConfig`) into local `comms_nodes`; agent-lifecycle hook
  writes/tombstones the agent's row alongside the `instances` row.
  Conflict policy: name globally unique, fail-loud (`CommsNodeConflictError`)
  on `(host, a2a_port)` mismatch between sources. Adds `sac db export
  --tables TABLE[,TABLE...]` filter.
- **feat(comms): stage 2 — cross-host push via ssh-transport selector +
  ACL e2e** (ADR-0015, #236). `_node_channel._forward_to_remote` now
  selects per-host: ssh-curl when `target_host` is a member of
  `host_config.peers` (including `spartan-*` glob), HTTP otherwise.
  Generalized `_network._ssh_curl._post_via_ssh_curl(host, port, path,
  body, bearer, timeout_s)` helper shared by `/v1/turn` and
  `/agents/<name>/message:send` — same argv shape, ControlMaster reuse,
  body via ssh-stdin into `curl -d @-`. Receiver-side ACL already
  correct (admin-bearer path → `metadata.from_agent` honoured →
  `has_grant` checked against the **receiver's** local `comms_grants`).
  Closes the structural WAN gap that blocked Spartan-agent → lead push.
  No-mocks ssh-shim fixture (`tests/.../_helpers/ssh_http_shim.py`)
  installs a real `ssh` binary on `$PATH` that performs an in-process
  `httpx.post` against the destination loopback listen — substitutes
  the ssh tunnel without mocking subprocess. Stage 2 added 17 NM+TQ-
  compliant tests (5 happy-path e2e + 12 error-path); codecov on the
  diff landed at 87.50%.

### Fixed
- **docs(changelog): drop orphan `>>>>>>> origin/develop` merge marker**
  that survived a prior develop→main resolution and rendered as raw
  conflict text inside the v0.21.1 entry.

## [0.21.2] — 2026-05-28

### Added
- **feat(apptainer): `spec.apptainer.tmpfs_size`** (default `2G`) — relocate
  the container `/tmp` + `/var/tmp` onto the host filesystem via `--workdir`,
  so a `--containall` agent isn't capped at the 64 MB session tmpfs mid-run.
  Fails loud (`TmpfsSpaceError`) below the requested size; no-op when `""`
  or when the operator already passes `--workdir`. (#187)
- **feat(fleet): `sac fleet sync`** — cross-host `spec.yaml` + `to_home/**`
  audit across every peer in `config.yaml`, fail-loud on any divergence
  (no auto-merge). Worker `--collect`, `--peer`, `--only`,
  `--allow-unresolvable`, JSON output. (#207)
- **feat(acl): Phase-3 server-managed ACL enforcement** (ADR-0010) —
  per-spec `spec.comms` (outbound/inbound siblings/parent allow|deny,
  `a2a.listen`) and `spec.lineage` (`group`, `may_spawn`), persisted to
  `state.db` and enforced at the listen send path and the spawn gate. (#206)
- **docs(readme): Models section** + spec-reference "Available models"
  (opus/sonnet/haiku aliases → current 4.x families, `[1m]` context, full
  versioned IDs, provider override); skills `spec.claude` field table
  (account + provider); ADR-0012; refreshed CLI surface (fleet/host/accounts).
- **feat(base-image): apt-based fd/rg/bat/eza** with Debian symlinks. (#205)

### Fixed
- **fix(mcp): account tools call renamed `accounts` subcommands** — the MCP
  `account_show` / `quota_watch` tools invoked the removed `account` /
  `quota watch` CLI verbs; repointed at `accounts status` /
  `accounts watch-quota`. (#231)
- **docs(skills): purge v2-era field references from `_skills/scitex-agent-container/`** —
  brought seven skills in lockstep with the v3 validator (`config/_validation.py`),
  which strictly rejects `spec.remote`, `metadata.name`, top-level
  `spec.model`, `spec.skills`, and `dot_claude`. Highlights: full rewrite
  of `11_remote-deploy.md` to `spec.host` / `spec.hosts` + `sac --on <peer>`
  dispatch; YAML example in `01_config-v3.md` no longer ships
  `spec.skills` / top-level `spec.model` / `multiplexer-alive`; A2A
  AgentCard mapping in `07_a2a-protocol-extension-fields.md` points at
  the dir-as-SSoT name source and the file-based skill layout;
  `19_full-agent-troubleshooting.md` replaces the `spec.skills.required` /
  `spec.skills.available` table with the `to_home/.claude/skills/<id>/`
  delivery mechanism. Added validator tests covering the unknown-spec-field
  catch-all and the `metadata.name` / `dot_claude` / `spec.skills`
  rejection messages; relocation-vs-unknown messages stay distinct.

## [0.21.1] — 2026-05-26

### Added
- **feat(ssh): connection multiplexing across all sac→peer ssh calls**
  — every sac call that shells out to ssh now prepends
  `-o ControlMaster=auto -o ControlPersist=60s -o ControlPath=<dir>/%C`
  so concurrent calls against the same peer share one TCP+SSH master.
  Fixes (a) "control socket dir is read-only" inside apptainer SIFs
  where the default `~/.ssh/sockets` ControlPath lands on the overlay,
  and (b) silent drops when fanning out across hosts that cap
  per-user concurrent sessions (Spartan `MaxSessions`,
  sshd `MaxStartups`). The ControlPath dir defaults to
  `${TMPDIR:-/tmp}/.sac-ssh-cm` (writable inside apptainer); override
  with `$SAC_SSH_CONTROL_DIR`; opt out entirely with
  `SAC_SSH_CONTROL_MASTER=0`. New helpers
  `scitex_agent_container._state.host_config.ssh_control_options()`
  and `ssh_control_options_str()`, applied centrally in `build_ssh_argv`
  and at three direct-ssh call sites (`_network.peer._post_turn_via_ssh`,
  `cli_pkg.priority_cmds._ssh_start_agent`,
  `cli_pkg._send_preflight.default_ssh_runner`). New CLI
  `sac host ssh-opts` prints the flags shell-quoted for use in agent
  prompts as `ssh $(sac host ssh-opts) host cmd`. See
  `_skills/scitex-agent-container/11_remote-deploy.md` §
  "SSH connection multiplexing". Failure mode is fall-through: if the
  control dir can't be created (read-only mount, ENOSPC) the helper
  returns `[]` and ssh argv stays byte-identical to pre-patch.

## [0.21.0] — 2026-05-25

### Added
- **feat(provider): vendor-agnostic Anthropic-SDK backend override (PR #208)** —
  `spec.claude.provider` with `base_url` + `auth_token_env` points a sac
  agent's Claude-SDK session at any Anthropic-SDK-compatible endpoint
  (DeepSeek first). Host-side `runtimes/_apptainer_provider.py` emits
  `ANTHROPIC_BASE_URL` + `SAC_ANTHROPIC_API_KEY` + `CLAUDE_CONFIG_DIR`
  at start; auth resolution via `scitex_config` cascade
  (shell-export > `$HOME/.env` > default → fail-loud
  `ProviderEnvError`). `claude-*` model alias check is relaxed under a
  provider override; `provider` + `spec.claude.account` are mutually
  exclusive. New: `config/_provider_types.ProviderSpec`,
  `runtimes/_apptainer_provider.py`, `runtimes/_apptainer_auth.py`,
  ADR-0011, `examples/agents/deepseek-agent/spec.yaml`. 73 targeted
  tests; full pytest matrix green.
- **chore(audit): exempt sac from §6 MCP-Python parity (PR #209)** —
  adds `[tool.scitex_dev] mcp_parity_exempt = true` to pyproject for
  the audit-cli §6 check. sac's MCP tools mirror CLI subcommands, not
  top-level Python APIs (4 orphan tools: `agent_spawn`,
  `list_python_apis`, `quota_watch`, `subagent_get_state`). Closes the
  only develop-shared audit-conformance violation;
  `tests/develop/test_audit.py::test_audit_all_clean` now green on
  develop.

### Added
- **feat(account): credential auto-sync substrate (`sac accounts
  sync-live` / `watch-live`)** — keep the per-account store fresh the
  moment the operator runs `claude /login`, with zero manual `sac
  accounts save`. `sync-live` reads the live `~/.claude/.credentials.json`
  + the active email from `~/.claude.json`, derives the store-name
  (email slugified, e.g. `ywatanabe@scitex.ai` → `ywatanabe-scitex-ai`),
  and atomically snapshots the live cred in when the matching store is
  absent / older / expired (idempotent no-op otherwise). `watch-live`
  is the always-on daemon: watches the live credential (inotify via
  `inotifywait` when available, else a poll loop) and runs the engine on
  every change, logging each sync to stderr or
  `~/.scitex/agent-container/runtime/logs/creds-watch.log`. New
  `_account/creds_sync.py` (engine) + `_account/creds_watch.py`
  (watcher).
- **feat(account): `sac accounts list` credential-freshness column** —
  every stored account now shows `VALID (+Xh)` / `EXPIRED (-Xh)` /
  `ABSENT` read OFFLINE from the snapshot's `expiresAt`, so rotted
  stores are visible at a glance. The `--json` output gains `freshness`
  and `freshness_hours` fields. New `_account/creds_sync.account_freshness`.

### Fixed
- **fix(account): pinned-account credential resolution now fails loud**
  — when `spec.claude.account` names a store that is ABSENT or its
  credential is EXPIRED, `sac agents start` now aborts with
  `PinnedAccountError` carrying the exact remedy (`claude /login` to
  that account + `sac accounts sync-live`). Previously it silently fell
  back to the host live file (a *different* account) or launched with a
  stale token — handing the agent the wrong identity. A pinned agent
  must never silently fall back. (`runtimes/_apptainer_creds.py`)

## [0.20.0] — 2026-05-24

### Added
- **feat(account): per-agent OAuth account assignment
  (`spec.claude.account`)** — pin an agent to a specific saved
  Anthropic account (from `sac account list`). Multi-account
  load-balance enabler: agents on distinct accounts no longer collide
  on one server-side rate limit. Frozen boot-copy mechanism — the
  apptainer runtime copies the named account's `.credentials.json`
  snapshot into the agent's own state dir at start and binds the copy
  RW, so two agents on two accounts never fight one mount and a host
  `/login` never moves a pinned agent. Takes effect on next
  start/restart; `account=""` (default) keeps the host live-file
  behaviour. New `runtimes/_apptainer_creds.resolve_cred_file`;
  load-time soft-WARN when the named snapshot is absent (never fails).
- **feat(account): `sac account list` plan/tier + usage columns** —
  OFFLINE plan label (Pro / Max 5x / Max 20x) and rate-limit tier for
  every stored account, read from the snapshot's whitelisted fields;
  CACHE-ONLY usage (`—` until a per-account usage cache exists). New
  `_state/account_store.read_account_plan` /
  `read_account_usage_cache`.
- **feat(status): pinned-account display** —
  `resolve_agent_account_label(assigned_account=)` so a pinned agent
  shows `<name> (<email>)` in `agent list` / `agent status` regardless
  of the host's current `/login`.

### Fixed
- **fix(spec): migrate bundled `sdk-test` spec off removed v3
  top-level fields** (`runtime: docker→apptainer`; `image`/`model`
  under their engine blocks; dropped `dockerfile`).

### Changed (BREAKING)
- **refactor(api): drop ``reply`` field; ``text`` is the single canonical
  field** — the A2A sidecar's ``POST /v1/turn`` response now returns
  ``{"text", "session_id", "exit_after", "metadata"}`` only; the
  back-compat ``reply`` alias (introduced alongside the bounded-timeout
  fix in 63deaee) is removed. Affects all consumers:
  - ``sac peer post-turn --json`` now emits ``{"text", "exit_after"}``
    (was ``{"reply", "exit_after"}``). Pipe consumers using
    ``jq -r .reply`` must update to ``jq -r .text``.
  - ``sac listen``'s live-runner forward response carries ``text``
    instead of ``reply``.
  - ``a2a_proxy`` runner's non-JSON upstream fallback wraps the
    upstream body under ``text`` instead of ``reply``.
  - The Python ``_network.peer.post_turn_to_url`` parses ``payload["text"]``
    explicitly; a missing key now raises ``PeerError`` (no silent
    ``.get`` fallback). Callers must adapt.

### Added
- **feat(mcp): expose `agent_send` tool** — the MCP server now ships
  37 tools (up from 36); the new `agent_send` lets a lead drive
  another agent via a prompt instead of shelling out to `sac agents
  send`. Returns a structured `{status, response_text,
  response_metadata}` dict (status one of `"ok"`, `"error"`,
  `"timeout"`); cross-host agents route through the same ssh+curl
  control plane the CLI uses. Library-facing helper lives at
  `cli_pkg/_send.py::send_to_agent` so the HTTP / ssh logic isn't
  duplicated.
- **feat(apptainer): declarative overlay auto-create via
  `spec.apptainer.overlay_size`** — operators no longer have to run
  `apptainer overlay create --size N proj-<peer>.overlay.img` by hand
  for every new peer. Set `spec.apptainer.overlay_size: "5G"` (units
  M/MB/G/GB) alongside `spec.apptainer.overlay: <path>` and sac
  creates the overlay image on first launch if it's missing. Gated by
  the new `overlay_create_if_missing` flag (default `true`); set to
  `false` to keep "operator must pre-create" semantics. When
  `overlay_size` is empty AND the overlay is missing, sac now fails
  earlier with a clear `FileNotFoundError` ("set
  `spec.apptainer.overlay_size` for auto-create, or pre-create with
  `apptainer overlay create`") instead of letting apptainer error out
  cryptically at exec time. Behaviour for specs with existing overlay
  files is unchanged. See `docs/isolation.md` §7.

### Changed
- **docs/spec-reference.md** — verified field-by-field against
  `config/_parsers/` + `config/_validation.py`; corrected
  `spec.runtime` (REQUIRED → optional with default), `spec.dot_claude`
  (no auto-discover), `spec.startup_commands[]` (list of dicts not
  strings), `spec.listen` (LIST of port-declarations, not host-listen
  override), `spec.telegram.*` (field names: `bot_token_env` /
  `allowed_users` / `auto_connect` / `greeting`, not `enabled` /
  `chat_id`), and apptainer `image` (REQUIRED → optional). Flagged
  `spec.apptainer.relaxed` / `fakeroot` as `(DESIGN — not yet
  implemented in parser)` and noted that `spec.multiplexer` /
  `spec.env-file` are parsed but currently missing from
  `_KNOWN_SPEC_KEYS` (parser/validator drift to fix in a follow-up).
  Companion audit at `docs/spec-reference.audit.md`.

### Removed
- **docs/agent-spec-schema.yaml** moved to
  `docs/legacy/agent-spec-schema-cld-v1.yaml`. It documented the
  pre-v3 `cld-agent/v1` schema (`metadata.name`, `runtime: claude-code
  | slurm | slurm-tenant`, `container:` block, `orochi:` block) which
  is now rejected at load time. Kept under `docs/legacy/` for
  historical reference only.

### Added
- **telegram fold (Phase 2 + Phase 3)** — the Telegram fold is now a
  first-class sac feature. `TelegramBridge` is fully ported from orochi
  (`_telegram/_bridge.py`): aiohttp long-poll against the Telegram Bot
  API, `allowed_users` filter enforced on inbound updates (empty list
  fails closed), and graceful shutdown that closes the API session and
  releases the singleton lock. The per-bot-token `flock`
  (`_telegram/_lock.py`) lives at
  `~/.scitex/agent-container/runtime/telegram/<token-hash>.lock` and
  reclaims the lock when the recorded PID is dead — the failure mode
  that the standalone telegrammer hits and that previously required a
  manual `rm`. The bridge boots from `_mcp/server.py` via
  `_maybe_boot_telegram_bridge`, which only fires when
  `LEAD_TELEGRAM_AUTH_TOKEN` is set in the env (i.e. only on the lead
  session — subagents inherit a sanitised env and get a structured
  `{"error": ...}` response from the tools). The six transport tools
  (`telegram_send`, `telegram_reply`, `telegram_react`,
  `telegram_edit_message`, `telegram_download_attachment`,
  `telegram_send_document`) are wired to the in-process bridge and
  registered by default — opt out via
  `SCITEX_AGENT_CONTAINER_TELEGRAM_FOLD=0` (previously opt-in via
  `=1`). New `sac.telegram` Python API submodule re-exports the verbs
  for §6 parity. Inbound messages emit `notifications/claude/channel`
  payloads shaped `{"content": ..., "meta": {"source": "telegram",
  "chat_id": ..., "message_id": ..., "user_id": ..., "username": ...,
  ...}}`; these only render when the Claude Code launcher is invoked
  with `--dangerously-load-development-channels
  server:scitex-agent-container` — see `docs/design/telegram-fold.md`
  for the launcher dependency note + WARN-log signal.
- **telegram fold (Phase 1)** — design + scaffolding for folding
  claude-code-telegrammer's transport tools into sac MCP (Option A from
  `GITIGNORED/dev/05_sac-mcp-telegram.md`). New `_telegram/` package with a
  `TelegramBridge` skeleton (Phase 2 port target documented in the module
  docstring) and `_mcp/_tools/_telegram.py` with 6 transport tool stubs
  (`telegram_send`, `telegram_reply`, `telegram_react`,
  `telegram_edit_message`, `telegram_download_attachment`,
  `telegram_send_document`). Registration is feature-flagged off behind
  `SCITEX_AGENT_CONTAINER_TELEGRAM_FOLD=1` — no user-visible behaviour
  change yet. Design doc at `docs/design/telegram-fold.md`.
- **F-CS15** — `sac mcp` server. New `_mcp/` package + `cli_pkg/mcp_cmds.py`
  Click group exposing `start`, `doctor`, `list-tools`, `install` against a
  FastMCP server with 36 `sac_*` tools mirroring the CLI surface (agent /
  db / host / image / template / account / skills / introspection). Install
  with `pip install scitex-agent-container[mcp]`.
- **F-CS3** phase 2 — runner-side autonomous loop: SDK runner consumes
  `spec.autonomous` and drives turns until `drive_until` matches or
  `max_turns` is reached. New `_runners/claude_session._autonomous_loop`
  coroutine + `--autonomous-*` CLI args; container.py forwards the spec.
- **OAuth credentials synthesis** — `provision_anthropic_auth` writes a
  minimal `~/.claude/.credentials.json` when given a `sk-ant-oat-*`
  access token via `SAC_ANTHROPIC_API_KEY`, so Pro/Max plans work in
  containers without the host file.

### Changed
- **F-CS17** stage 3a–3d — CLI/TUI runtime, SLURM runtime, action/context
  pane abstractions, and the legacy Dockerfile all removed. `sac` is now
  container-only: docker / podman / apptainer engines, the SDK-persistent
  base image, and the standalone Python runner under `_runners/`.
- **Auth handoff renamed** to `SAC_ANTHROPIC_API_KEY`. The previous
  `SCITEX_AGENT_CONTAINER_CI_ANTHROPIC_API_KEY[_OAUTH]` envs are gone.
  `provision_anthropic_auth` only honours `ANTHROPIC_API_KEY` when set
  explicitly by the operator; otherwise it bridges (api-key form) or
  synthesises a credentials file (oat- form) from `SAC_ANTHROPIC_API_KEY`.
- **Container daemon argv** — drops `--rm`; lifecycle.stop now does an
  explicit `docker rm -f` after stop. Adds `--env HOME=/tmp` so the
  bundled `claude` binary has a writable home even when the host UID
  has no `/etc/passwd` entry inside the image. Forwards
  `SAC_ANTHROPIC_API_KEY` (and `ANTHROPIC_API_KEY` when explicit).
- **F-CS16** — schema flatten (`spec.image`, `spec.dockerfile` top-level),
  auto-build at start, validator hard-errors on legacy runtimes.

### Removed
- F-CS17: `runtimes/slurm{,_tenant,sbatch_spartan}.py`, the entire CLI/TUI
  stack (`context_manager`, `actions/{compact,nonce_probe}`, `auto/response`,
  `cli_pkg/action_cmds`), the legacy `containers/Dockerfile`, and all
  associated tests (~7,500 LOC).

## [0.13.0] — pre-rewrite snapshot

Reference tag for the state immediately before F-CS17. See `git log v0.13.0`
for the prior history.
