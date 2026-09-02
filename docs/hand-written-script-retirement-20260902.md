# Hand-written host script retirement — 2026-09-02

Eleven scripts under `~/.local/bin` on the fleet host did work that `sac`
either already does or should. This is the record that lets a reader **delete
the host files with confidence**: one row per script, naming what it did, the
`sac` verb that supersedes it, the gap that remains after the port (if any),
and the decision.

The host files are NOT touched by this PR. Deletion is the fleet-operator
role's act, after this merges.

## How to read the "gap" column

A gap is a thing the script did that the verb does **not**. An empty gap means
the verb is a strict superset. A gap that is deliberate — a capability sac
refuses on purpose — says so; those are not TODOs.

---

## Ported into sac by this PR

| Script | Purpose | Superseding verb | Gap | Decision |
| --- | --- | --- | --- | --- |
| `sac-agent-env.sh` | `-a AGENT[,AGENT] VAR=VALUE …` — idempotent per-spec env edit (skip if already at target), backup to `<agent>/.old/<stamp>/spec.yaml`, `sed`-rewrite or insert after the first `env:`, YAML parse-check with restore on failure, then `sac agents restart --yes`, `sleep 25`, and a `/proc/<pid>/environ` verification table. | **NEW: `sac agents env set NAME KEY=VALUE… [--restart]` / `sac agents env unset NAME KEY… [--restart]`** (`cli_pkg/_agents_env.py`, engine in `config/_env_block_line.py`). | Two: the script's **multi-agent glob selection** (`-a 'handyman-*'`) and its **post-restart `/proc` verification table**. Neither is ported. Fan-out belongs to a `--all`/`--group` option shared with `sac agents send`, which has the same gap; the `/proc` check is a host-vantage probe a container cannot run (the script itself detects this and prints `BLIND`). | **ported** |
| `sac-prune-binds.py` | `<agent-name>` — drop every `spec.apptainer.binds` entry and every `raw_args --bind` pair whose source is absent on this host, then rewrite the whole document with `yaml.safe_dump`. No dry-run, no backup, always writes. | **Detection half only**, as a report-only section in **`sac agents check`** (`cli_pkg/build_cmds.py:_warn_absent_bind_sources`), reusing `_listen/_inline_spec_preflight.preflight_bind_sources` (which expands `~`/`$VAR`) and `_lifecycle/_relocate_bind_kind.classify_bind` (which says provision / carry / decide). | The **pruning half is deliberately not ported** — see below. | **detection ported, pruning refused** |

### Why prune-binds is not ported as a flag or a verb

The script is contrary to two operator rulings and to sac's settled posture.
Every existing sac surface **reports or refuses** an absent bind source and
names the remedy; none prunes:

* the start guard (`runtimes/_apptainer_bind_guard.spec_binds_checked`) raises
  on a credential bind and logs `ERROR` on any other, after which apptainer
  itself refuses (`FATAL: … mount source … doesn't exist`, rc 255);
* the inline-spawn preflight refuses with `kind="bind_unresolvable"`;
* the relocate preflight fails the dry run and prints a per-kind remedy.

The **2026-08-09 ruling** (recorded in `sac-here.py`, which explicitly declines
to prune) is *report and refuse, do not silently prune*; the **2026-08-19
ruling** is *a bind must be declared in the spec, never injected from code*.
The reason both hold: a missing source is three different problems — provision
it on this host, carry it with the agent, or decide it is obsolete — and
deleting the line answers none of them while making the spec lie about what
the agent needs.

The script was also unsafe on its own terms: no `~`/`$VAR` expansion (so a
`~/x:/y` bind sac *would* mount was judged absent and dropped), dict-form binds
dropped unconditionally, no dry-run, no backup, and a full `yaml.safe_dump`
rewrite that destroyed comments even when nothing was dropped.

What was genuinely missing is the **detection**: `sac agents check` said
nothing about bind sources, so an operator learned of one only at start. That
half is now a WARN — it never fails the check, because `check` is routinely run
on one host for a spec that runs on another, where a host-local source is
absent by design.

---

## Superseded — safe to delete, nothing to port

| Script | Purpose | Superseding verb | Gap | Decision |
| --- | --- | --- | --- | --- |
| `switch_account.py` | Re-point ONE spec's account fields at a stored account: sets `claude.credentials_file` to `accounts/<name>/.credentials.json` (or `accounts/anthropic/<name>/`; exit 2 if neither), moves it to the front of `claude.credentials_files` and drops entries naming that account. Dry-run by default; `--apply` writes a `.bak-acct-<stamp>` and re-parses. | Triage proposed `sac accounts switch NAME` — **verified NOT a match**: `_state/account_store.switch_account()` copies `accounts/<NAME>/*` into the host's live `~/.claude/` and logs a rotation audit; it never reads a spec. What actually covers the use case: (1) the **boot-time pool pick** — `_lifecycle/_start_preflight.py` treats `claude.credentials_files` as a POOL (a singular `credentials_file` is a 1-element pool) and `pick_healthy_account` chooses the quota-aware winner; (2) **`sac accounts pause NAME --reason`**, which removes a forbidden account fleet-wide (the picker excludes PAUSED; mint and keepalive skip it). | No verb edits a spec's account pin — but **the script's own premise is stale**: with a non-empty `credentials_files` the singular field it rewrites is overwritten at every start by the pool pick, and since the script KEEPS the other accounts in the list, the pick can still land on them. It pins nothing today. Per-agent pinning is a hand-written `credentials_files: [<one path>]`. | **delete** |
| `apply_account_creds.py` | Receiver side: stdin JSON → `~/.scitex/agent-container/accounts/anthropic/<name>/.credentials.json` on THIS host. Refuses (exit 2) empty/non-JSON stdin, a payload carrying `refreshToken`, no `accessToken`, <300 s validity, or a missing account dir. Backs up to `.bak-<stamp>` (0600), writes 0600 via `mkstemp`+`os.replace`, prints before/after sha256[:12] and minutes left. | **`sac accounts send-credentials --account SLUG --to PEER`** (hidden alias `keepalive`; scheduled every 15 min on the refresh holder — `_jobs/_specs_accounts.py`). Push from the master over ssh: `assert_access_only` (recursive), `--min-validity` 300, `assert_not_downgrading`, `.bak-<stamp>` 0600 on the peer, stage + chmod 600 + stat-verify mode AND size, atomic `mv`, then the peer's OWN copy must answer HTTP 200. | No receiver-side/stdin entry point — a host cannot apply a pasted payload itself; the master pushes. The verb writes the mint artifact shape rather than raw stdin bytes. Every refusal threshold the script has, the verb has, **plus far-side verification the script lacks**. | **delete** |
| `_apply_creds.py` | Same receiver, but the destination is the host's LIVE login `~/.claude/.credentials.json`. Refuses refresh-bearing / no-accessToken / expired / <300 s payloads; `.bak-<stamp>` 0600; 0600 atomic write; never prints a token. | **`sac accounts send-credentials … --remote-path /home/<user>/.claude/.credentials.json`** (absolute required — `_SAFE_REMOTE_PATH` rejects `~`). Same refusals, backup, 0600 and far-side HTTP 200 verification. | Pushing the live login is no longer the model: agents pinned via `claude.credentials_file(s)` bind the store snapshot (`runtimes/_apptainer_auth.py`), so only unpinned agents and the operator's own CLI read that file. No stdin entry point — by design, see the row above. | **delete** |
| `_apply_full_creds.py` | Install a FULL credential (`refreshToken` REQUIRED, exit 2 without it; exit 3 if expired) from stdin into `accounts/<acct>/.credentials.json`, creating the dir. Makes THIS host the refresh holder for that account. | **`sac accounts login NAME`** (drives `claude /login` in a tmux pane, delivers the OAuth URL, then reuses `account_save`) and **`sac accounts save NAME [--email]`** (snapshots `~/.claude/.credentials.json` into `accounts/<NAME>/` with a rotation-audit event). The `sync-live --poll` job does the same snapshot automatically every 2 min. | No stdin/`--from FILE` on `save`: the only way to install refresh material is a real login on that host. **Deliberate** — pasting refresh material between hosts is the clone that caused the 2026-08-10 outage, and the single-refresher invariant (`assert_is_refresh_holder`) forbids repeating it. The script was the one-time migration of the refresh holder to nas-03. | **delete** |
| `_strip_refresh.py` | Demote THIS host: delete `claudeAiOauth.refreshToken` from `~/.claude/.credentials.json` in place, keeping the access token. Idempotent; refuses (exit 2) an unparseable file, no `claudeAiOauth`, or no `accessToken`; `.bak-prestrip-<stamp>` 0600; prints minutes the access token keeps working. | The accounts-keepalive job (`sac accounts send-credentials`) — **verified PARTIAL**. `keepalive_push` converges the PEER's STORE path onto access-only material and reports `peer_held_refresh_material`, but only as a WARNING. | Two: (1) when the peer's access fingerprint already matches, the action is `already-current` and **nothing is written**, so a peer copy holding the current access token PLUS a `refreshToken` — exactly the clone the script strips — is left holding it unless `--force`; (2) the verb targets the store path, the script targets the live login. See the follow-up below. | **delete after the keepalive fix** |
| `restart-401.sh` | Over every `tmux ls` session: classify alive/dead/unknown from a 401/revoked/`Please run /login` banner in the last 12 lines, no busy indicator, pane unchanged across 6 s and banner still present → `sac agents restart NAME --yes`. UNKNOWN is printed, never restarted. Always acts. | **`sac agents restart-login-expired [--apply\|--check] [--limit] [--interval] [--json]`** (`cli_pkg/_agents_restart_login_expired.py` → `_authheal`). Detection is a system auth banner frozen directly above the prompt across two captures; phrase list `_AUTH_STARTS` plus `^API Error:\s*(401\|403)\b`; restarts through the pool-loading start path; 30-min/agent debounce, ≤2/agent/hour, board card when capped. Rate walls are `sac agents resume-rate-limited`; the remote form is `send-credentials --sweep`. | (1) Default is CHECK — the script's always-act behaviour needs `--apply`. (2) The literal `OAuth access token has been revoked` is not in `_AUTH_STARTS`; the 2026-08-09 rendering begins `API Error: 401 {…revoked…}`, which `_API_AUTH_RE` already matches, so coverage holds unless Claude Code ever renders that sentence as a standalone first line. (3) Roster is registered specs, not `tmux ls` — an ad-hoc session with no spec is not observed. (4) The busy-word list is replaced by the frozen-pane rule. (5) The scheduled timer has a deploy gate (the `auth-heal.py` cron must be retired first); the manual verb is always safe. | **delete** |
| `sac-here.py` | Place a spec IN PLACE on this host: exit 3 if `agents/<name>/spec.yaml` is missing, 4 if `spec.workdir` is absent locally, 5 if ANY bind source is absent (reports each; **never prunes** — the 2026-08-09 ruling); otherwise rewrites `spec.host = socket.gethostname()` and `yaml.safe_dump`s the spec back (comments lost, no backup). Starts nothing. | Triage proposed `sac agents spawn-from-here` — **verified NOT a match**: it POSTs `{name, caller, spec?}` to the host listen from inside a SIF; it neither edits a spec nor rewrites `host`. What covers it: (a) **`sac agents start NAME --no-redispatch`**, the documented force-local escape; (b) **`sac agents relocate NAME --to <this-host> --no-dry-run`**, the real 1→1 move — executing, journaled in per-host PG since 2026-08-28, resumable, with a preflight that refuses on a missing workdir and on missing binds with per-kind remedies, and which updates the **residency table**, the authority on host (spec `host:` seeds the db once and is then ignored). | No verb rewrites or deletes the legacy `host:` field, and start's dispatcher still reads it, so a spec pinned to another peer ssh-dispatches unless `--no-redispatch`. The script used `socket.gethostname()` rather than sac's canonical resolver (`$SAC_HOST` → `config.yaml` → short hostname), so it could write a non-canonical name. | **delete** |
| `sac-place-local.py` | 1→2 COPY: read `agents/<src>/spec.yaml`, set `spec.host` to this host, rewrite `overlays/<src>/`→`overlays/<new>/`, `/state/<src>/`→`/state/<new>/` and `SCITEX_TODO_AGENT_ID=<src>`→`=<new>` inside `raw_args`, write `agents/<new>/spec.yaml`. Despite its docstring it does NOT start the agent; no backup. | Triage proposed `sac agents relocate` — that is 1→1 (the agent moves; identity and count unchanged), not a copy. The 1→2 verb is **`sac agents twin PARENT --name NEW`** (inherits repo/workdir/image/binds/model, own name, fresh a2a port, identity-split env, session seeded from the parent). **`sac agents rename OLD NEW`** enumerates the FULL self-reference set this script only partially rewrites. | `twin` requires a RUNNING parent with a resolvable session and forks the conversation; there is **no cold-clone verb** (copy a STOPPED spec to a new name). The script's rewrite set is stale and unsafe regardless: `SCITEX_TODO_AGENT_ID` is retired (board identity is `SCITEX_CARDS_AGENT_ID`), the **a2a port is not bumped** (collision on the same host), the workdir/labels/`spec.env` identity are untouched, and spec `host:` is legacy. | **delete** |
| `greet-all.sh` | One-off broadcast with the 2026-08-09 401-rotation incident text hard-coded: for every `tmux ls` session, `timeout 100 sac agents send NAME "$MSG"` via `xargs -P 12`. | **`sac agents send NAME [PROMPT]`** (HTTP to the agent's A2A port; refuses without a recorded port). `sac fleet notify` is agent→lead; `sac a2a` has no broadcast. | No fan-out: `send` takes one NAME, and nothing offers `--all`/`--group`. The message is incident-specific and dated. | **delete** |

---

## Follow-ups this record deliberately does not close

1. **`keepalive_push` leaves a refresh-bearing peer copy alone when the access
   token already matches.** In `_account/token_keepalive.py`, `current = not
   absent and access_fp == payload["access_fp"]`; when true and not `--force`
   the action is `already-current` and nothing is written. Making `current`
   also require `state["refresh_fp"] is None` would back up, rewrite
   access-only and verify a peer copy that is holding refresh material,
   instead of only warning about it. `_strip_refresh.py` should be deleted
   **after** that lands, since until then nothing demotes a host in place.
2. **No fan-out on `sac agents send`** (`greet-all.sh`, and the multi-agent
   half of `sac-agent-env.sh`). A `--all`/`--group` selection shared with the
   `bulk_selection_options` that `stop`/`restart` already use would close both.
3. **No cold-clone verb** — copy a STOPPED spec to a new name, bumping the a2a
   port and every self-reference. `twin` needs a live parent; `rename` moves
   rather than copies.
4. **`relocate --help` still says dry-run is "the only supported mode"** —
   stale since the executing path landed (`cli_pkg/_relocate_cmd.py`).
5. **Optional belt-and-braces**: add the literal
   `OAuth access token has been revoked` to `_AUTH_STARTS` in
   `_runners/_tmux/auth_status.py`. Today's rendering is already matched by
   `_API_AUTH_RE`; this only guards a future one.

## What `sac agents env` does NOT inherit from the shell script

Three behaviours were dropped on purpose, not overlooked:

* **The `sed` rewrite.** The script rewrote **every** indented `K:` line in the
  file, in every block, and compared against the **first** `^\s+K:` line
  anywhere in the document — so `-a X model=…` read and wrote
  `spec.claude.model`. The verb anchors on the key PATH
  (`spec.apptainer.env`) through `config/_yaml_line_edit.find_block`, which is
  the convention `_a2a_host_line` and `_to_home_layers_line` already use.
* **Unconditional double-quoting of an unescaped value.** The script always
  emitted `K: "V"` — right about the quoting, wrong about the escaping: a `|`
  in the value broke its `sed` delimiter and an `&` expanded to the match. The
  verb renders the value as a proper double-quoted YAML scalar.
* **Silent success on a spec with no `env:` block.** The script inserted
  nothing and still reported the agent "edited". The verb creates the block.

Also intentionally different: **exit codes**. The script exited 0 for every
outcome except a usage error — including a YAML-broken restore, a failed
restart, and a failed verification. The verb exits 0 only when the spec now
says what was asked, 1 when the spec is missing or its shape is refused, and 2
on a malformed `KEY=VALUE`.
