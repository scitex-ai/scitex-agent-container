---
description: |
  [TOPIC] Cross-host agent placement (v3)
  [DETAILS] Pinning an agent to one or more peers via spec.host / spec.hosts; the v2 spec.remote: block has been removed.
tags: [scitex-agent-container-remote-deploy]
---

# Cross-host agent placement

The v2 `spec.remote:` block (host / user / ssh timeout / remote venv) is
**no longer accepted** by the v3 validator
(`config/_validation.py::_V3_REMOVED_FIELDS["remote"]`). A spec that
still ships `spec.remote` hard-errors at `sac agents check` time with a
redirect to `spec.host` / `spec.hosts`.

Cross-host placement is now declared with two mutually exclusive fields
plus the `sac --on <peer>` dispatcher.

## YAML — singleton on one peer

```yaml
apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: apptainer
  host: gpu-box                 # singleton; only this peer runs it
  # host: [gpu-box, laptop]     # singleton with fallback priority list
  a2a:
    host: 127.0.0.1             # inbound HTTP bind (loopback)
    port: auto                  # or a fixed int
  apptainer:
    image: ~/.scitex/agent-container/containers/sac-base.sif
  claude:
    model: claude-opus-4-7[1m]
```

`spec.host` accepts a string (one peer) or a list of strings (try in
order; first available wins). The agent runs on exactly one peer at a
time.

## YAML — multi-instance, one per peer

```yaml
spec:
  runtime: apptainer
  hosts: [laptop, gpu-box, nas] # one independent instance per peer
  # hosts: all                  # every peer in config.yaml peers:
```

`spec.hosts` accepts a list of peer names or the string `"all"` (every
peer declared in `~/.scitex/agent-container/config.yaml` under `peers:`).
Each peer gets its own runner with its own session state.

`spec.host` and `spec.hosts` are mutually exclusive — declaring both
fails validation.

## Dispatch

The `sac` CLI itself uses `--on <peer>` to ssh-dispatch any subcommand:

```bash
sac --on gpu-box agents start my-agent     # run "agents start my-agent" on gpu-box
sac --on gpu-box agents status my-agent
sac --on gpu-box agents stop my-agent
```

Under the hood `sac --on` ssh-hops with the peer's connection settings
from `config.yaml::peers.<peer>` (host, user, identity, etc.) — no
per-spec `remote:` block is involved.

## How a multi-host start fans out

When you `sac agents start <name>` with `spec.hosts`, the dispatcher:

1. Resolves the peer list (`hosts: all` → every entry in `peers:`).
2. For each peer, ssh-dispatches `sac agents start <name>` via the
   `--on <peer>` machinery.
3. Each remote `sac` reads the same agent dir (synced separately — sac
   does not auto-rsync; use `sac fleet sync` or your own deploy
   pipeline).
4. Each peer runs an independent apptainer SIF instance.

For singleton (`spec.host`), only one peer is dispatched (the first
healthy entry in the priority list).

## Requirements on each peer

- `scitex-agent-container` installed (same version recommended)
- `apptainer` ≥ 1.2 + the agent's `.sif` image present at
  `spec.apptainer.image`
- The peer is declared in `~/.scitex/agent-container/config.yaml`
  under `peers:` with reachable SSH settings
- SSH key-based auth from the dispatcher host to the peer

## Migrating from `spec.remote`

| v2 (removed) | v3 replacement |
|---|---|
| `spec.remote.host: mba` | `spec.host: mba` |
| `spec.remote.host: [mba, laptop]` | `spec.host: [mba, laptop]` (singleton fallback) |
| `spec.remote.host: [a, b]` + start-all | `spec.hosts: [a, b]` (one per peer) |
| `spec.remote.user`, `spec.remote.timeout`, `spec.remote.login_shell` | declared in `config.yaml::peers.<peer>` (not per-agent) |
| `spec.venv` (remote-only path) | `spec.apptainer.image` + in-container venv at `/opt/venv-agent` |

## See also

- `docs/spec-reference.md` "Top-level shape" — host/hosts canonical reference
- `07_a2a-protocol-extension-fields.md` — `x-scitex-agent-container.scheduling`
  card field derived from `spec.host` / `spec.hosts`
