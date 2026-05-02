---
name: slurm-tenant
description: SLURM tenant runtime — many agents in one allocation — see file body for details.
tags: [scitex-agent-container, scitex-package]
---

# SLURM tenant runtime — many agents in one allocation

`runtime: slurm-tenant` (sac ≥0.10.0) launches an agent as a tenant of an existing `scitex-hpc` Reservation. Every tenant becomes a tmux session inside the same SLURM allocation, attached to a shared tmux server bootstrapped at `sbatch` script PID 1.

## When to use

| You want… | Runtime |
|---|---|
| One agent per SLURM job, with auto-resubmit before walltime | `runtime: slurm` |
| **Many agents inside one allocation, one queue wait for the pool** | **`runtime: slurm-tenant`** |
| Local agent (no SLURM) | `runtime: claude-code` |

The tenant pattern eliminates per-agent queue wait. Once you've booked a reservation, each `sac start` becomes one ssh round-trip — the agent is running ~10 seconds later, not ~10 minutes.

## Workflow

### 1. Book the reservation (once per pool)

```bash
scitex-hpc reservations book dev-pool \
    --host spartan --partition cascade \
    --cpus 8 --mem 32G --time 7-0 \
    --tmux-server sac --persistent
```

`--tmux-server sac` is **mandatory** for the tenant runtime — without it, tenant tmux sessions get cgroup-killed within seconds (see "Architecture" below). `--persistent` enables walltime auto-resubmit via `SIGUSR1`.

### 2. Write tenant YAMLs

```yaml
apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: slurm-tenant
  model: sonnet
  slurm:
    reservation: dev-pool       # name of the lease booked above
  claude:
    flags: [--dangerously-skip-permissions]
```

Drop multiple yamls under `~/.scitex/agent-container/agents/<name>/<name>.yaml` (or any directory listed in `$SCITEX_AGENT_CONTAINER_YAML_DIRS`).

### 3. Launch them

```bash
sac start dev-helper.yaml         # tmux session in dev-pool's allocation
sac start doc-builder.yaml        # second session, same allocation
sac start --all                   # or all yamls at once
```

### 4. Operate

```bash
sac list                          # tenants appear alongside other agents
sac attach dev-helper             # srun --pty + tmux attach on the compute node
sac show-logs dev-helper -n 100        # tmux capture-pane via srun --overlap
sac stop dev-helper               # kills the tmux session — does NOT release the allocation
sac stop --all                    # stops every tenant; reservation still alive
```

When the work is done, release the pool:

```bash
scitex-hpc reservations release dev-pool
```

## Architecture

### Why the tmux server has to be PID 1 of the sbatch script

SLURM's cgroup terminates *every process in a step's cgroup* when the step ends. A naive `tmux new-session -d` spawned via `srun --jobid --overlap …` runs in the step's transient cgroup — the daemon is killed within ~2 seconds of the step exiting (verified live on spartan-bm021 2026-04-28).

`scitex-hpc.Reservation.book(tmux_server="sac", ...)` solves this by wiring `tmux -L sac new-session -d -s _root 'sleep infinity'` into the sbatch script's hold body, so the tmux server is **the job's main process**. It lives in the job's primary cgroup and survives every `srun --overlap` step. Tenants connect via the same `-L sac` socket and their sessions are siblings of `_root`.

### What `sac start` does for a tenant

1. Looks up the Reservation by `spec.slurm.reservation` (raises if not booked or not booked with `tmux_server`).
2. Reads `extras.tmux_server` from the lease state file → socket name (typically `sac`).
3. Runs `tmux -L <socket> new-session -d -s sac-<agent-name> '<claude command>'` via `Reservation.exec()` (which is `ssh <host> 'bash -lc "srun --jobid=<X> --overlap <cmd>"'`).
4. Writes a sac registry entry so `sac list` / `sac stop` / `sac show-logs` / `sac attach` route correctly.

### What `sac stop` does for a tenant

`tmux kill-session -t sac-<agent-name>` via the same `srun --overlap` channel. **Does not** scancel the SLURM job — the reservation outlives its tenants. To free the allocation entirely, use `scitex-hpc reservations release <pool-name>` separately.

### What `sac attach` does for a tenant

`ssh -t <host> 'bash -lc "srun --jobid=<X> --pty bash -lc \"tmux -L sac attach -t sac-<agent-name>\""'`. Detach with the standard tmux prefix (Ctrl-B D).

## Compatibility with HPC policies

- **No persistent daemons.** Every `sac` operation is bastion-initiated SSH — your laptop reaches into Spartan, never the reverse. No `crontab @reboot`, no autossh, no cloudflared, no systemd-user-linger.
- **Walltime auto-resubmit uses SLURM's documented `SIGUSR1` mechanism**, not a custom watchdog daemon.
- The reservation itself is a normal `sbatch` job that just happens to run a long-lived `tmux` server + `tail -f /dev/null`. Cluster operators see one job; users get a multiplexer.

This was the design constraint after the 2026-04-26 IT Security ruling on Spartan (incident skill: `scitex-orochi-private/incident-2026-04-26-spartan-cloudflared-detection.md`).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `runtime: slurm-tenant requires spec.slurm.reservation` | yaml is missing the `reservation` field |
| `reservation 'foo' was not booked with tmux_server set` | re-book with `--tmux-server sac` |
| Tenant session disappears within seconds | reservation booked WITHOUT `--tmux-server`. Run `scitex-hpc reservations get <name>` and check `"extras": {"tmux_server": "sac"}` |
| `sac attach` exits immediately | session was killed externally; check `sac show-logs` for crash trace |
| Stale `job_id` after walltime auto-resubmit | run `scitex-hpc reservations refresh <name>` |

## See also

- `08_templates.md` — `ssh-slurm.yaml` template (single-agent SLURM)
- `40_troubleshooting.md` — broader sac troubleshooting
- `scitex-hpc/_skills/scitex-hpc/SKILL.md` — Reservation primitive details
- `docs/sphinx/slurm.rst` — public-facing docs version
