# 06 — CLI reference

> **Stub.** Scope and outline below; to be fully written in a follow-on.
> The live source of truth is always `sac --help-recursive`; this page will be
> the curated, narrative reference generated from it.

## Scope

Every `sac` command group and flag, organized for humans. Until this page is
filled in, run `sac --help`, `sac <group> --help`, or `sac --help-recursive`
(the full subcommand tree) for the authoritative, version-matched output.

## Command groups (as of v0.21.21)

| Group | Summary |
|-------|---------|
| `agents`   | Agent lifecycle, status, introspection, and snapshots. |
| `subagent` | Claude Code Agent-tool subagent monitoring. |
| `accounts` | Manage stored Claude accounts for credential rotation. |
| `host`     | Local host identity and peer routing. |
| `peer`     | Outbound A2A calls into other agents' `POST /v1/turn`. |
| `a2a`      | A2A protocol — generic agent-to-agent surface (no fleet deps). |
| `fleet`    | Peer-aware multi-agent orchestration across hosts. |
| `listen`   | Host HTTP/JSON control plane: `start` / `stop` / `restart` / `status`. |
| `db`       | Inspect and maintain the sac state database (`state.db`). |
| `registry` | Registry maintenance (folded into `sac db`). |
| `event`    | Event-log operations: ingest hook events into the per-agent ring buffer. |
| `image`    | Container image lifecycle (delegates to scitex-container). |
| `installation` | Bootstrap / install helpers for a new fleet host. |
| `doctor`   | Diagnose agent-spec source drift (local or `--fleet`). |
| `ports`    | List the ports sac/scitex uses, with live status. |
| `provenance` | Prove which code is actually loaded (commit, origin, fossil installs). |
| `pytest`   | Run pytest on remote pools (Spartan SLURM, …). |
| `mcp` · `skills` · `list-python-apis` · `versions` | Introspection surfaces. |
| `dev`      | Developer / maintainer plumbing (CI secrets, etc.). |

## TODO — this page will contain

- [ ] Per-group command tables with every subcommand and its one-line purpose.
- [ ] The high-traffic flags spelled out: `sac agents start` (`--foreground`, `--group`, `--force`, `--fresh`/`--continue`, `--no-redispatch`, `--broker-self`, `--concurrency`/`--stagger`, cold-start `<label>@<host>:/path` forms).
- [ ] `sac agents stop` / `restart` selection flags (`--all-running` / `--all-registry` / `--all`) and the `--json` contract cross-host dispatch relies on.
- [ ] Global options: `--json`, `--version` (commit + load path), `--help-recursive`.
- [ ] Shell completion: `install-shell-completion` / `print-shell-completion`.
- [ ] A note that this page should eventually be auto-generated from `sac --help-recursive` so it cannot drift.

<!-- EOF -->
