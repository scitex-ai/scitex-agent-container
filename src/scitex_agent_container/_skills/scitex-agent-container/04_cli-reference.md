---
description: |
  [TOPIC] sac CLI — map of command groups + the non-obvious gotchas
  [DETAILS] Which `sac` group does which job (agents / image / accounts / host / peer / a2a / fleet / db / mcp), plus renamed-verb history, apptainer-only build, the image sandbox cycle, and other judgment the `--help` output does NOT tell you. The exhaustive per-command flag list is self-describing — run `sac --help-recursive`.
tags: [scitex-agent-container-cli-reference]
---

# CLI Reference — intent map, not a flag dump

Entry point: `sac` (alias for `scitex-agent-container`) — one Click app.

**The authoritative, always-current command + flag list is the CLI
itself.** Do not read it from a skill (it drifts):

```bash
sac --help-recursive        # root + every subcommand, one shot
sac <group> --help          # e.g. sac agents --help
```

This leaf keeps only what `--help` can't tell you: which group to
reach for, and the gotchas.

## Which group for which job

| Job | Group |
|---|---|
| Start/stop/inspect an agent | `sac agents …` (start, stop, restart, status, health, tail, recall, check, find, send) |
| Resume a running agent for one more turn | `sac agents send <name> "…"` (see [15_claude-session.md](15_claude-session.md)) |
| Build / switch the Apptainer image | `sac image …` (delegates to [scitex-container](https://github.com/ywatanabe1989/scitex-container)) |
| Rotate / snapshot Claude credentials | `sac accounts …` (see [26_credentials-rotation.md](26_credentials-rotation.md)) |
| Host routing / cross-host exec | `sac host …` |
| Drive another agent's `/v1/turn` | `sac peer post-turn`, `sac a2a …` (see [07_a2a-protocol.md](07_a2a-protocol.md)) |
| Push a typed event to the lead | `sac fleet notify done\|blocker\|status` |
| Inspect the state DB | `sac db …` |
| HTTP/JSON control plane | `sac listen …` (see [10_cli.md](10_cli.md)) |

Global flags worth knowing: `--json` (structured output where
supported), `--on PEER` (dispatch the rest of argv over ssh to a peer
from `config.yaml`).

## Gotchas the help text won't warn you about

- **Renamed verbs.** The old `validate` / `take-snapshot` /
  `check-priority` / `inspect` / `logs` / `list` were folded into
  `check` / `status --snapshot` / `status --priority` / `status` /
  `tail` / `status` (fleet view). If a script calls an old verb, this
  is why it broke.
- **`--foreground` is single-target only.** `sac agents start a b c`
  works in daemon mode; `--foreground` streams stdio and blocks, so
  one agent at a time.
- **Apptainer-only since 2026-05-13.** `sac image build` has no
  `--runtime` flag; there is no docker path.
- **`sac a2a serve` is the sidecar path only.** For `runtime:
  apptainer` (SDK) agents the runner hosts `POST /v1/turn` itself —
  you don't run `a2a serve` for them.
- **`sac accounts sync-live` fails loud on a stale/absent live
  credential** — it never snapshots a stale token. That's intentional.

## The image sandbox cycle (workflow, not obvious from --help)

`scitex[all]` updates often; rebuild-per-change is slow. The pattern:

```bash
sac image build scitex --sandbox        # one-time writable rootfs
sac image update sandbox/               # pip --upgrade any time
sac image freeze sandbox/ scitex-X.sif  # bake when stable
sac image switch X                      # atomically flip the symlink
```

## See also

- [10_cli.md](10_cli.md) — `sac listen` control-plane + Python-API mirror
- [03_python-api.md](03_python-api.md) — the programmatic surface
- [11_remote-deploy.md](11_remote-deploy.md) — SSH deployment internals
- [13_observability.md](13_observability.md) — `status --json` contract
