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
- `--on PEER` — dispatch the rest of argv via ssh on `PEER` (defined in `sac.yaml`'s `peers:` block)

## Agent lifecycle (most-used)

| Command | Purpose |
|---|---|
| `sac agent start <agent> [--foreground]` | Start the agent. Daemon by default; `--foreground` streams stdio + blocks. Honors `spec.remote.host` (ssh dispatch) and `spec.a2a.port` (HTTP inbound). |
| `sac agent stop <agent>` | SIGTERM the runner; escalate to SIGKILL after 5 s. ssh-mediated for remote agents. |
| `sac agent restart <agent>` | Stop + start, preserving session_id resume. |
| `sac agent status [<agent>]` | Per-agent rich payload, or fleet view if no name. `--snapshot` persists a state capsule, `--priority` adds the singleton-yield report. |
| `sac agent health <agent>` | Health-method poll (`sdk-alive`). |
| `sac agent tail   <agent>` | Render `session.jsonl` (user / assistant / tool / result events). ssh-tails remote logs. |
| `sac agent recall <agent>` | Human summary of the agent's session. |
| `sac agent check  <agent>` | Preflight: validate yaml + probe runtime deps (docker/python). |
| `sac agent find   <capability>` | Search agents by capability label. |

Multi-target: `sac agent start a b c` works for daemon mode; `--foreground` is single-target only.

The earlier verbs `validate`, `take-snapshot`, `check-priority`, `inspect`, `logs`, `list` were folded into `check`, `status --snapshot`, `status --priority`, `status` (per-agent), `tail`, and `status` (fleet view) respectively.

## Image lifecycle (`sac image`)

Delegates the heavy lifting to [`scitex-container`](https://github.com/ywatanabe1989/scitex-container).

| Command | Purpose |
|---|---|
| `sac image build [base\|scitex] [--sandbox] [--runtime apptainer\|docker]` | Build a layered runtime image. Default target: `scitex`. Default runtime: `apptainer`. `--sandbox` builds a writable rootfs directory instead of an immutable SIF. |
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

## Account / quota (`sac account`)

| Command | Purpose |
|---|---|
| `sac account list` | Stored Claude Code accounts + the active credentials block. |
| `sac account save <name>` | Snapshot current credentials under `<name>` for later rotation. |
| `sac account switch <name>` | Switch active credentials to a stored account. |
| `sac account delete <name>` | Remove a stored account. |
| `sac account watch-quota` | Monitor quota and auto-rotate when threshold exceeded. (Was `sac quota watch`.) |

## Network / peer / A2A

| Command | Purpose |
|---|---|
| `sac host show / list / probe / exec / validate` | Local hostname + peer machine routing (ssh round-trip / exec on PEER). |
| `sac peer post-turn AGENT TEXT` | Outbound A2A — POST a turn to another agent's `/v1/turn`. |
| `sac peer resolve-url AGENT` | Print the URL `peer post-turn` would target. |
| `sac a2a serve <yamls...>` | A2A inbound HTTP server (sidecar mode for non-SDK runtimes). For `runtime: apptainer` agents the runner hosts `POST /v1/turn` itself. |
| `sac network probe` | WSL → fleet-hub connectivity probe. |

## State database / registry / events

| Command | Purpose |
|---|---|
| `sac db query / show / clean / migrate / tick` | Inspect and maintain the sac state database (`state.db`). `db clean` replaces the legacy `registry clean`. |
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
| `sac mcp list-tools` | Local MCP introspection (no MCP server bundled — sac agents spawn their own via `src_mcp.json`). |
| `sac skills list / get` | Bundled agent-facing skills. |
| `sac list-python-apis` | Enumerate the public Python API. |
| `sac auto-accept` | Auto-accept TUI handler for legacy claude-code agents (the apptainer/SDK runner doesn't need it). |

## See also

- [10_cli.md](10_cli.md) — long-form flag tables + Python-API mirror
- [03_python-api.md](03_python-api.md) — programmatic surface (mirrors the lifecycle commands)
- [11_remote-deploy.md](11_remote-deploy.md) — SSH deployment internals
- [13_observability.md](13_observability.md) — `status --json` contract
