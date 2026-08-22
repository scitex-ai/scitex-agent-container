---
description: |
  [TOPIC] sac CLI reference
  [DETAILS] Lifecycle (start/stop/restart/status/health/tail/recall/check/find), image lifecycle (build/sandbox/update/freeze/list/switch/rollback/status/snapshot — delegates to scitex-container), account/quota, network/peer/A2A, db, registry, event, MCP introspection. Long-form flag tables in 10_cli.md.
tags: [scitex-agent-container-cli-reference]
---

# CLI Reference

Entry: `sac` (alias for `scitex-agent-container`).

```
sac [OPTIONS] COMMAND [ARGS]...
```

Global flags:
- `-h / --help` — show help
- `--help-recursive` — show help for the root command and every subcommand
- `--json` — emit structured JSON where supported (status / actions / events)
- `--on PEER` — dispatch the rest of argv via ssh on `PEER` (defined in `config.yaml`'s `peers:` block)

## Agent lifecycle (most-used)

| Command | Purpose |
|---|---|
| `sac agents start <agent> [--foreground]` | Start the agent. Daemon by default; `--foreground` streams stdio + blocks. Honors `spec.host` / `spec.hosts` (cross-host placement; see [11_remote-deploy.md](11_remote-deploy.md)) and `spec.a2a.port` (HTTP inbound). |
| `sac agents stop <agent>` | SIGTERM the runner; escalate to SIGKILL after 5 s. ssh-mediated for remote agents. |
| `sac agents restart <agent>` | Stop + start, preserving session_id resume. |
| `sac agents status [<agent>]` | Per-agent rich payload, or fleet view if no name. `--snapshot` persists a state capsule, `--priority` adds the singleton-yield report. |
| `sac agents health <agent>` | Health-method poll (`sdk-alive`). |
| `sac agents tail   <agent>` | Render `session.jsonl` (user / assistant / tool / result events). ssh-tails remote logs. |
| `sac agents recall <agent>` | Human summary of the agent's session. |
| `sac agents check  <agent>` | Preflight: validate yaml + probe runtime deps (docker/python). |
| `sac agents find   <capability>` | Search agents by capability label. |

Multi-target: `sac agents start a b c` works for daemon mode; `--foreground` is single-target only.

The earlier verbs `validate`, `take-snapshot`, `check-priority`, `inspect`, `logs`, `list` were folded into `check`, `status --snapshot`, `status --priority`, `status` (per-agent), `tail`, and `status` (fleet view) respectively.

## Image lifecycle (`sac image`)

Delegates the heavy lifting to [`scitex-container`](https://github.com/ywatanabe1989/scitex-container).

| Command | Purpose |
|---|---|
| `sac image build [base\|scitex] [--sandbox]` | Build a layered Apptainer image. Default target: `base`. `--sandbox` builds a writable rootfs directory instead of an immutable SIF. Sac is apptainer-only since 2026-05-13 — no `--runtime` flag. |
| `sac image sandbox SOURCE` | Convert an existing SIF (or layer name) into a writable sandbox. |
| `sac image update SANDBOX [-p PKG]` | Refresh packages inside a sandbox via `pip install --upgrade`. Default: `scitex[all]`. |
| `sac image freeze SANDBOX OUT.sif` | Bake a sandbox back into an immutable SIF. |
| `sac image list` | Installed SIF versions on disk. |
| `sac image switch <version>` | Atomically switch the active SIF symlink to a different version. |
| `sac image rollback` | Restore the previous active version. |
| `sac image status` | Unified container dashboard (active version, sandboxes, sizes). |
| `sac image snapshot [-o env.json]` | Reproducibility capsule: pip + apt + conda + git + active SIF hash. |

Typical "scitex updates often" cycle:

```
sac image build scitex --sandbox       # one-time
sac image update sandbox/              # any time
sac image freeze sandbox/ scitex-X.sif # when stable
sac image switch X
```

## Account / quota (`sac accounts`)

| Command | Purpose |
|---|---|
| `sac accounts list` | Stored Claude Code accounts + active block; per-store freshness column (`VALID (+Xh)` / `EXPIRED (-Xh)` / `ABSENT`). |
| `sac accounts save <name>` | Snapshot current credentials under `<name>` for later rotation. |
| `sac accounts sync-live` | Snapshot the live credential into its matching store (store-name = email slugified). Idempotent; fails loud on an expired/absent live cred (never saves a stale token). |
| `sac accounts watch-live` | Daemon: watch `~/.claude/.credentials.json` (inotify or poll) and auto-run `sync-live` on every change — "the moment I log in → auto-saved". |
| `sac accounts switch <name>` | Switch active credentials to a stored account. |
| `sac accounts delete <name>` | Remove a stored account. |
| `sac accounts status` | One-shot quota snapshot (5h%, 7d%, account email + tier). |
| `sac accounts watch-quota` | Monitor quota and auto-rotate when threshold exceeded. |

## Network / peer / A2A

| Command | Purpose |
|---|---|
| `sac host list / add / remove / set / probe / exec / validate` | Local hostname + peer machine routing (ssh round-trip / exec on PEER). |
| `sac host add-peer / list-peers / remove-peer` | Cross-host `sac listen` bearer registry (who may push into this host). |
| `sac host ssh-opts` | Print sac's ssh ControlMaster flags shell-quoted — use as `ssh $(sac host ssh-opts) host cmd`. |
| `sac host probe-hub` | WSL → fleet-hub layered connectivity probe (DNS, gateway, TCP, HTTPS). |
| `sac peer post-turn AGENT TEXT` | Outbound A2A — POST a turn to another agent's `/v1/turn`. |
| `sac peer resolve-url AGENT` | Print the URL `peer post-turn` would target. |
| `sac a2a serve <yamls...>` | A2A inbound HTTP server (sidecar mode for non-SDK runtimes). For `runtime: apptainer` agents the runner hosts `POST /v1/turn` itself. |
| `sac a2a doctor AGENT` | Probe an agent's A2A AgentCard endpoint and report health. |

## Fleet (`sac fleet`)

| Command | Purpose |
|---|---|
| `sac fleet launch PEER <name>...` | Rsync each agent's spec to PEER and run `sac agents start <name>` there. |
| `sac fleet notify done\|blocker\|status --summary "..."` | Agent→lead push channel (ADR-0013 Phase 1) — POST a typed event to the lead's `sac listen` inbox. `--detail`, `--conversation-id`, `--dry-run`, `--json`. |

## State database / registry / events

| Command | Purpose |
|---|---|
| `sac db query / show / clean / migrate / tick` | Inspect and maintain the sac state database (`state.db`). `db clean` replaces the legacy `registry clean`. |
| `sac db export / import` | Dump state.db rows as a JSON delta / ingest a dump (cross-host registry sync). |
| `sac registry reconcile` | Reconcile singleton agent placement across the fleet. |
| `sac event ingest` | Append a Claude Code hook event to the per-agent ring buffer. |

## Build / install / templates

| Command | Purpose |
|---|---|
| `sac installation` | Bootstrap helpers for a new fleet host. |
| `sac installation setup-cron` | Add (or remove) the post-merge-pull crontab entry. |
| `sac template render-contributor-spec` | Materialize a contributor agent spec from the v3 template. |

## Other

| Command | Purpose |
|---|---|
| `sac doctor [--fleet]` | Diagnose agent-spec source drift (locally, or `--fleet` across peers). Also runs the poller-singleton check. |
| `sac doctor --pollers` | Is more than one live Telegram poller holding the same bot token on this host? Read-only; `ok` / `violation` / `unknown`, never a token value. |
| `sac subagent get-state` | Pure state data for every matching Claude Code Agent-tool subagent (Type 2). |
| `sac mcp list-tools` | Local MCP introspection (no MCP server bundled — sac agents spawn their own via `to_home/.mcp.json`). |
| `sac skills list / get` | Bundled agent-facing skills. |
| `sac list-python-apis` | Enumerate the public Python API. |
| `sac auto-accept` | Auto-accept TUI handler for legacy claude-code agents (the apptainer/SDK runner doesn't need it). |

## See also

- [10_cli.md](10_cli.md) — long-form flag tables + Python-API mirror
- [03_python-api.md](03_python-api.md) — programmatic surface (mirrors the lifecycle commands)
- [11_remote-deploy.md](11_remote-deploy.md) — SSH deployment internals
- [13_observability.md](13_observability.md) — `status --json` contract
