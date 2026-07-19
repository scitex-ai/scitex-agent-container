# ADR-0017 — Credential rotation, live-bind, and the refresh-token race (2026-06-03)

## Status

Accepted. Ships in two PRs:

* **PR #262 / commits d70e608 + acf2cbb (2026-06-01)** — `_apptainer_creds.resolve_cred_file` returns the **snapshot path itself** for pinned accounts, and `_apptainer_auth.py` binds the snapshot's parent directory `:rw` into the agent at `/tmp/sac-claude`. The pre-existing stale-COPY path is gone.
* **PR #299 / commit dea298d (2026-06-03)** — `cli_pkg/_account_refresh.py::_collect_pinned_running_accounts()` extends `sac accounts refresh --all --skip-active` so the host cron skips **every** account currently pinned by a running local agent, not just the host's `~/.claude` active login.

This ADR exists because the operator asked, after the two-incident night of 2026-06-03, that the failure model and its two fixes be written down explicitly so the next maintainer does not re-derive them from a 401-storm.

## Context

Each Anthropic Pro / Max account that sac knows about lives in **one** canonical credential file under
`~/.scitex/agent-container/accounts/<account>/.credentials.json`
(the per-account "snapshot store" — see ADR-0016 §account axis and skill `26_credentials-rotation.md`).
Spec authors point a single agent at a specific account by setting
`spec.claude.account: <name>`; the runtime then binds **that account's** credentials into the agent.

Two facts about the underlying Anthropic OAuth flow are load-bearing here. Neither is sac-specific, both are easy to forget:

1. **Refresh-tokens rotate on use.**
   Every call to `/oauth/token` with `grant_type=refresh_token` returns a NEW `access_token` AND a NEW `refresh_token`, and **invalidates the previous refresh_token server-side immediately**. There is no grace window. If two independent processes refresh against the same account, whichever completes first wins; the other's already-spent refresh_token is dead.

2. **The bundled `claude` CLI caches the refresh-token in memory at startup.**
   The CLI loads `<config_dir>/.credentials.json` once when the SDK session is created and holds the refresh-token in process memory. When the access-token nears expiry the CLI rotates using the IN-MEMORY refresh-token, then writes the new pair back to disk via the `:rw` bind. It does **not** re-read the file before each rotation. So an out-of-band rotation that updates the file does **not** propagate into a running CLI — until that CLI exits and respawns.

These two facts combined with sac's deployment shape (a host that fans out to N pinned agents AND runs a periodic refresh cron) produce a class of failure where the snapshot file looks healthy to the operator, the running agent looks healthy to the heartbeat, and yet every API turn 401s.

The on-disk artifacts that make this concrete:

* `~/.scitex/agent-container/accounts/<acct>/.credentials.json` — the per-account snapshot. This is what `sac accounts list` shows and what the cron refresher rewrites.
* `~/.claude/.credentials.json` — the host's currently-active login (a real file or a symlink). What the operator's interactive `claude` session uses.
* `/tmp/sac-claude/.credentials.json` (inside each container) — where the in-SIF Claude CLI reads creds from; populated by the `--bind` argv the runtime emits at spawn time.

The runtime stitches these together via `_apptainer_creds.resolve_cred_file` + `_apptainer_auth.auth_argv`.

### Failure mode 1 — stale COPY (pre-#262)

Before #262, when `spec.claude.account` was set,
`_apptainer_creds.resolve_cred_file` called `shutil.copy2` from the snapshot into the agent's own per-agent state dir and returned **the copy path**. `_apptainer_auth.auth_argv` then bound that per-agent copy `:rw` into the agent at `/tmp/sac-claude/.credentials.json`.

The consequence:

* The in-container CLI refreshed the access-token by writing through the bind — but the bind target was the **agent-local copy**, not the snapshot store.
* The snapshot store never advanced past the boot-time access-token.
* When `claude /login` (or `sac accounts watch-live`, or the cron) refreshed the snapshot externally, the in-container CLI's per-agent copy stayed frozen — the bind pointed at the wrong file.
* Once the per-agent copy's refresh-token died (~8h to multi-day, depending on session activity), every turn 401'd. The heartbeat still ticked because the SDK session didn't crash; it just couldn't talk to the API.

This was the 2026-06-01 fleet-wide silent outage diagnosed by the operator. Fixed by **PR #262**.

### Failure mode 2 — refresh-token rotation RACE (pre-#299)

After #262 landed, the snapshot store became the single source of truth: the in-container CLI binds it `:rw` directly, rotations there propagate up to the snapshot, the snapshot stays current, and every agent on the same account sees the new tokens at next read.

But sac also runs a host-side refresher: the federated systemd-user timer `sac.accounts-refresh.service` fires `sac accounts refresh --all --skip-active` every 2h (`OnUnitActiveSec=2h`), iterating the account store and rotating each account's tokens against `/oauth/token`. The `--skip-active` flag was designed to leave the **currently active host login** alone (the account `~/.claude/.credentials.json` symlink points at) so the operator's interactive session never had its refresh-token yanked.

What `--skip-active` did NOT skip: any account **pinned by a running agent** that is NOT the host's interactive login.

So on a host with three Max accounts where:

* Operator's interactive `claude` is logged in as `ywatanabe-gmail-com`.
* `proj-neurovista` is pinned to `alpha-example-com`.
* `clew` is pinned to `ywatanabe-scitex-ai`.

Every 2h the cron rotated **both** `alpha-example-com` and `ywatanabe-scitex-ai`'s refresh-tokens. The in-container CLIs for neurovista and clew were holding the **previous** refresh-tokens in memory (fact 2 above), so their next rotation attempt — when their access-token expired ~1h later — used a refresh-token Anthropic had already invalidated when the cron rotated it.

→ 401 storm, recurring on the 2h cron boundary, indistinguishable from a working credential to anyone reading the snapshot file (which the cron had just rewritten with brand-new tokens).

The 2026-06-03 17:35 401 storm matched the cron's `OnUnitActiveSec=2h` boundary to the minute (last firing at ~15:35, next at 17:35). Fixed by **PR #299**.

### Why a symlink doesn't solve this

Reframing the snapshot store as a directory of symlinks pointing at `~/.claude/.credentials-<acct>.json` (or any other single-canonical scheme) does **not** close the race. Both processes — the host cron and the in-container CLI — would still issue independent `/oauth/token` calls with the same starting refresh-token in their respective memories. Server-side rotation invalidates the loser's refresh-token regardless of how many filesystem indirections the loser dereferenced to find that token. The race is a **server-state race against the OAuth provider**, not a filesystem race; symlinks and atomic renames have nothing to address.

The only thing that closes it is structural: **one account, one refresher.**

## Decision

### Invariant: one account, one refresher

For any Anthropic account, exactly one process refreshes its OAuth tokens at any given time. Concretely:

* **When the account is pinned by a running local agent**: the in-container `claude` CLI is the sole refresher. The host cron MUST skip it. Other agents on the same host pinned to the same account ride the snapshot the first agent's CLI wrote.
* **When the account is parked** (no running agent, no interactive host login): the host cron is the sole refresher.
* **When the account is the host's interactive login** (`~/.claude/.credentials.json` resolves to it): the interactive `claude` CLI is the sole refresher; the host cron skips it via the pre-existing `--skip-active`/`_resolve_active_account_name` mechanism.

The skip-set is the **union** of the host-active account and the set of accounts pinned by running local agents. Neither subset overlaps with the other in steady state; both must be excluded from the refresh batch.

### Implementation

#### Live `:rw` dir-bind (PR #262 + the unpinned-branch follow-up)

`runtimes/_apptainer_creds.resolve_cred_file` returns:

* For an **unpinned** agent: `~/.claude/.credentials.json` (the host's active login).
* For a **pinned** agent (`spec.claude.account: <acct>`): the **snapshot Path** `~/.scitex/agent-container/accounts/<acct>/.credentials.json` — no copy.

`runtimes/_apptainer_auth.auth_argv` then dir-binds **unconditionally**:

```python
bind_src = cred_file.parent     # ~/.claude/ or accounts/<acct>/
bind_dest = "/tmp/sac-claude"
argv += ["--bind", f"{bind_src}:{bind_dest}:rw", ...]
```

The directory bind matters because both refresh paths — the bundled in-container CLI rotating the access-token in place, AND the host-side watch-live / sync-live daemons mirroring `~/.claude/.credentials.json` into the snapshot store — write atomically (write-to-tmp + rename). A single-file bind would survive that rename pointing at the OLD inode (visible as `...credentials.json//deleted` in `/proc/<pid>/mountinfo`), so the container reads the stale pre-rename token forever and 401s at natural expiry. A directory bind resolves child files by name through the underlying filesystem on every open, so a tmp+rename inside the dir is visible to the container immediately in both directions.

**Initial PR #262 made only the pinned branch dir-bind**, leaving the unpinned/host-live branch as the legacy single-file bind (justified at the time by "don't expose ~/.claude/" — but the cred-refresher agents are unpinned by design, and they hit the //deleted regression on 2026-06-04 03:00 fleet-wide). The follow-up retiring the single-file branch (Task #13) makes both branches dir-bind. The unpinned bound dir is `~/.claude/` (over-binds settings.json + chat history + projects DB compared to the per-account snapshot dir, but the recommended deployment is `spec.claude.account` pinning + watch-live daemon mirroring the host-active account into the snapshot store, so the unpinned dir-bind is a degraded fallback for the host-active-login case only).

#### Skip-set extension (PR #299)

`cli_pkg/_account_refresh.py` adds `_collect_pinned_running_accounts(home: Path | None = None) -> set[str]`:

* Reads the local file-based agent registry at `~/.scitex/agent-container/runtime/registry/*.json` (same JSONs `sac status` reads, written by `_lifecycle/_start` at spawn time).
* For each entry, loads its `config` spec via the production `load_config()` and extracts `spec.claude.account`.
* Returns the union of non-empty account names.

The `account_refresh` command unions this set with the result of `_resolve_active_account_name` (the existing host-active resolver) and excludes the union from the refresh batch. A stderr diagnostic names each excluded pinned-running account with the reason ("refresh-token rotation race guard") so the operator can see what got skipped.

Helper signature accepts an explicit `home` parameter and reads the registry directory via `_resolve_registry_dir(home)` (env override + HOME-derived fallback evaluated per-call). The pre-existing `Registry` class freezes its `REGISTRY_DIR` at module-import time; reading per-call rather than via that class is what makes the helper testable under pytest's HOME-redirect fixtures.

Tolerant on every read path: registry JSON parse failure, missing `config` field, missing or invalid spec, missing `claude.account` — each maps to "this entry contributes nothing" so one bad row never crashes the whole refresh.

#### Deploy shape

Both fixes have different deploy paths and the difference matters:

* **#262 (`_apptainer_creds` + `_apptainer_auth`)**: host-side argv builder, but the change affects what gets bound into NEW agent spawns. Running agents stay on their boot-time argv. Deploy = host `git pull` + agent restart.
* **#299 (`_account_refresh`)**: host-side cron command; runs from the editable install on `sac.accounts-refresh.service`'s `ExecStart` PATH lookup. NO SIF involvement. Deploy = host `git pull`; next 2h timer firing picks it up automatically.

See `docs/deploy-runbook.md` for the surface-by-surface deploy table. The merged≠deployed lesson applies; the runbook's "Host runtime — restart only" row is exactly #299, the "rebuild + restart agents" row is exactly the SIF half of #262.

## Consequences

**What gets better.**

* The 2h-cyclic 401 storm on pinned agents is closed structurally. Verified empirically: PR #299 force-probe at deploy time named all three pinned-running accounts in the operator's fleet (alpha-example-com / ywatanabe-scitex-ai / beta-example-com); the 19:35 cron cycle on 2026-06-03 passed with zero new 401 on the pinned neurovista agent.
* The single-source-of-truth model becomes meaningfully single. Before #262 the snapshot existed but was authoritative for **nobody** in a pinned-agent deployment (the agent wrote to its per-agent copy, the cron wrote to the snapshot, neither could see the other). After #262 + #299 the snapshot is what the in-container CLI binds to AND what the cron leaves alone when it's in use.
* The in-container CLI's in-memory cache stops being a foot-gun for sac itself. (It still IS one for any out-of-band rotator. The skip-set extension is what prevents sac from being its own out-of-band rotator.)

**What gets worse.**

* Stale registry entries over-skip. If an agent dies un-cleanly and leaves a JSON in `~/.scitex/agent-container/runtime/registry/`, the cron will skip that account until `Registry.cleanup_stale` (or a manual `rm`) removes the entry. Failure mode: under-refresh — the operator eventually notices an account whose snapshot hasn't moved in days and runs `sac accounts refresh <name>` manually. This is the **safe** direction. The opposite (over-refresh racing a live agent) is the bug being fixed.
* The refresh path now reads each running agent's spec at refresh time. On a host with N running pinned agents this is N spec parses per cron firing. At today's fleet sizes (≤10) this is invisible; if it ever grows to hundreds, cache the parsed account or add a `pinned_account` column to the `instances` table (see "Notes / open items" below).

**Known open items.**

* **Parked accounts still rely on the host cron alone.** An account that is stored but has neither a running agent nor an interactive host login depends entirely on the cron to keep its refresh-token alive. If the cron is paused (e.g., during operator maintenance) for longer than the refresh-token's expiry window, the parked account needs a real `claude /login` — see skill `27_credentials-relogin.md`. This is by design (parked accounts have no in-memory cache to race) but worth naming explicitly.
* **Watch-live daemon (skill 26 §6) is separate.** `sac accounts watch-live` listens to `~/.claude/` and syncs the operator's interactive logins into the snapshot store. It is not part of the rotation cron and is not affected by either fix. The `inotifywait` semantics — atomic copy on `close_write|moved_to|create` — interact correctly with both #262 (the snapshot is the bind source) and #299 (it doesn't rotate, it just mirrors the active login).
* **Write-side `pinned_account` column** on the `instances` table would let the cron query the skip-set with one SQL query instead of N spec-parses. Not done because (a) the spec-parse path is already tolerant and fast at current scale, and (b) it would require a schema migration. Filed as a possible follow-up.

## Notes

* **Why not use `Registry().list_all()` directly in the helper.** The `Registry` class computes `REGISTRY_DIR` at module-import time from the then-current `$HOME` / `SCITEX_AGENT_CONTAINER_REGISTRY_DIR`. Tests that redirect `$HOME` via pytest fixtures see a stale `REGISTRY_DIR` because the module was imported earlier under the test process's original `$HOME`. Reading the directory path per-call (via `_resolve_registry_dir(home)`) sidesteps the freeze and is honest under fixtures. Production callers pay nothing — `Path.home()` is a cheap stdlib call.
* **Why a directory bind, not a file bind, for the pinned case.** The bundled CLI writes atomically (`tmp + rename`), so a single-file `:rw` bind would point at the pre-rename inode and miss the post-rename file. The dir-bind lets the in-container CLI see whichever file currently sits at `/tmp/sac-claude/.credentials.json` regardless of how it got there.
* **Why `--skip-active` keeps existing semantics.** The host-active account is the operator's interactive session; the in-memory cache argument from fact 2 applies to it just as it does to a pinned agent. The skip-set is just two subsets of the same invariant. Keeping the existing flag name + behaviour + adding the pinned-running subset preserves the "with `--all`, exclude the in-use refresh-token" mental model operators already had.
* **What this does NOT change.** The OAuth API contract; the preflight (`_state/_preflight_creds`); the watch-live daemon (`_account/creds_watch`); the `sac.accounts-refresh` systemd timer schedule (still 2h); the re-login flow (skill 27); the symlink convention for `~/.claude/.credentials-<acct>.json` host-side canonicals when sac is wired to use that scheme. The two PRs are surgical patches on top of an existing model — they fix what was structurally wrong, not what was working.
* Related ADRs: **0001** (isolation flags including HOME pinning) — the `--home /home/agent` flag and the `/tmp/sac-claude` choice for the bind target both come from there. **0016** (provider × account axes) — the conceptual home of `spec.claude.account` and the snapshot-store layout. **0009** (claude-setup delivery to home first) — why credentials never live in `to_home/` and the rationale for binding into `/tmp/`, not `$HOME`.
