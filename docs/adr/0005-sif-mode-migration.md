# ADR: SIF-mode migration for Spartan agents (2026-05-17)

**Status:** Accepted.
**Context:** Spartan GPFS punim2354 project volume hit 88% inode usage
while running five `proj-scitex-*` agents from `--writable` apptainer
sandboxes. Each sandbox is a directory of ~50K small files, so every
new agent consumed inode quota in proportion to its OS image, even
when the agent itself wrote nothing.

## Problem

Three constraints landed at once on 2026-05-17:

1. **Inode pressure.** punim2354 inode quota was at 88% (704K free /
   5.8M total). Each sandbox is ~50K files; restarting agents
   regularly meant clones, which kept growing inode usage.
2. **Disk pressure.** punim0264 was at 99% byte-use; relocating the
   sandboxes to punim2354 fixed bytes but worsened inodes.
3. **`pip` failed inside sandboxes.** Debian 12 + Python 3.12 marks
   `/usr/lib/python3.12` as `EXTERNALLY-MANAGED` (PEP 668), so
   `uv pip install --system` was a no-op (warnings only, no install).

The agents were stuck: sidecar processes started but the runner
crashed during `startup_commands`, leaving the lead unable to talk
to them.

## Decision

Migrate every sac agent from `--writable sandbox` to
`SIF + rootless + auto isolation flags`. Concrete steps:

1. **Build a SIF** for each container family (`sac-base`, `clew`,
   `neurovista`). One file per container = one inode per container.
2. **Run rootless** so apptainer maps the host UID inside the
   container (no `--fakeroot`, no subuid mapping, no UID 0). This
   keeps `claude --dangerously-skip-permissions` happy while
   avoiding root-mapped namespace inode issues.
3. **Let sac auto-add the canonical isolation flags** by setting
   `apptainer.relaxed: false`. That enables `--containall
   --cleanenv --writable-tmpfs --home /home/agent`, which together
   give the container a fresh PATH, a writable `/tmp`, and a
   writable home that comes from the lead-side
   `runtime/<name>/home/`.
4. **Replace runtime `pip install -e .` with `PYTHONPATH`.** SIFs
   are read-only, and uv's `--user` mode is unsupported. The
   package source is already bind-mounted under
   `/home/agent/proj/<pkg>/src` — point Python at it via
   `--env PYTHONPATH=/home/agent/proj/<pkg>/src:/opt/venv-sac/lib/python3.12/site-packages`.
   Edits to the bound source are visible immediately, same UX as
   the prior editable install.
5. **Override `SCITEX_AGENT_CONTAINER_STATE_DB` per agent.** sac's
   default `/state/state.db` lives OUTSIDE the writable
   `runtime/<name>/` bind (which lands at `/state/<name>/`). On
   SIF the default path is read-only → `sqlite3.OperationalError:
   attempt to write a readonly database` → sidecar crash. Each
   spec sets
   `--env SCITEX_AGENT_CONTAINER_STATE_DB=/state/<name>/state.db`
   to land in the writable bind.
6. **Pre-install uv in `/opt/venv-sac/bin/uv`.** The sandbox
   bootstrap fetched uv into `/root/.local/bin`, which doesn't
   survive `apptainer build` (root home is excluded). Install
   `uv` into the venv-sac before rebuilding the SIF so it's on
   the canonical PATH for every container.
7. **Do not add a custom credentials bind.** sac already binds
   `~/.claude/.credentials.json` to
   `/tmp/sac-claude/.credentials.json` (writable, via
   `--writable-tmpfs`) and sets `CLAUDE_CONFIG_DIR=/tmp/sac-claude`.
   Adding `binds: - <host>/.credentials.json:/home/agent/.claude/.credentials.json`
   on top fails because SIFs can't auto-create file targets.

## Consequences

**Positive:**

- 1 inode per container instead of ~50K.
- punim2354 inode usage drops back to its pre-sandbox baseline.
- SIF integrity check (`apptainer verify`) is cryptographically
  sound — every restart loads the same bits.
- Rebuilding the SIF is the single hook for image refresh; no
  more per-agent `pip install -e ...@develop` race.

**Negative:**

- Image refresh requires a sandbox-side install followed by
  `apptainer build`. Roughly 2-3 min per SIF on Spartan's CPU node.
  The lead has to remember the two-step workflow (`apptainer exec
  --writable` then `apptainer build`) when upgrading
  scitex-dev / sac packages.
- Editable-mode debugging that mutated `/opt/venv-sac/` no
  longer works — the SIF is immutable. Package source edits go
  through the bound `/home/agent/proj/<pkg>/src` instead, which is
  what we wanted anyway.
- The spec.yaml grows by ~5 lines per agent for the SIF-mode
  workarounds. Each line carries a comment so future readers know
  why.

## Related work

- Lead `~/.scitex/agent-container/agents/proj-scitex-*` specs
  carry a `SIF-mode workarounds` header documenting each of the
  five flags above.
- `~/proj/scitex-lead/GITIGNORED/RUNNING/sif-migration.md` (this
  session's running notes).
- Future: extend the same pattern to `proj-paper-scitex-clew` and
  `proj-neurovista` once their SIFs ship via the same
  rebuild-on-upgrade workflow.
