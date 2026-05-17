# ADR: Spartan apptainer canonical args (2026-05-18)

**Status:** Accepted.
**Context:** ADR-0005 migrated agents from `--writable sandbox` to
`SIF + overlay`, but the precise `apptainer` invocation that lets
`claude --dangerously-skip-permissions` run on Spartan was still
empirically being narrowed. Multiple combinations (`--fakeroot` with
file overlay, `--fakeroot` with sandbox, `--writable` everywhere) hit
different failure modes. This ADR pins the one combination that
works end-to-end and removes the alternatives.

## Problem

`claude --dangerously-skip-permissions` refuses to run when the
effective uid inside the container is 0 (root). On Spartan,
`/etc/subuid` is unpopulated for `ywatanabe` (only `splunk` is
registered), so:

- `--fakeroot` → apptainer falls back to *root-mapped namespace*
  (uid 0 inside, mapped to host uid for file ownership). Refused by
  claude.
- `--writable` sandbox → mounts the sandbox directory directly. Works
  for host uid, but inode-expensive (see ADR-0005) and one sandbox per
  agent fights for concurrent writes.
- `--overlay <file>.img` + `--fakeroot` → setup fails with
  `upper dir is not writable: permission denied`. File-image overlay
  needs `--fakeroot`, but `--fakeroot` lands in root-mapped namespace
  where the upper dir is unwritable.
- `--overlay <file>.img:ro` → read-only, no persistence.

The only combination tested on `spartan-gpgpu106` (2026-05-18 01:50)
that yields:

- host uid preserved (`uid=17107(ywatanabe)`),
- writable `$HOME`,
- working `claude --dangerously-skip-permissions` (creds picked up,
  Claude Code v2.1.126 launched, responded to a prompt),

is:

```
apptainer shell \
  --home /home/agent \
  --userns \
  --containall \
  --overlay <overlays-dir>/<pkg>/ \
  sac-base.sif
```

with `<overlays-dir>/<pkg>/` pre-created as a directory containing
`upper/` and `work/` subdirectories.

## Decision

Pin this as the canonical apptainer invocation for Spartan agents.
`scitex_agent_container.config._parsers.apptainer` emits these flags
unless the spec explicitly overrides them.

### Required flags

| Flag | Purpose |
| :--- | :--- |
| `--userns` | Rootless user namespace; preserves host uid inside (no fakeroot remap). |
| `--containall` | Clears env, $HOME, /tmp inherited from host so the container is reproducible across machines. |
| `--home /home/agent` | Sets `$HOME` to the agent's canonical path inside the container. Combined with the per-pkg dir-overlay, `$HOME` is writable and persistent. |
| `--overlay <dir>/` | Per-package writable layer. Must be a **directory** (with `upper/` + `work/` subdirs), not a file image. |

### Removed flags

- `--fakeroot` — Spartan-incompatible (no subuid → root-mapped ns →
  claude refuses uid 0).
- `--writable` sandbox path — inode-expensive (ADR-0005) and
  concurrent-write unsafe.
- `--overlay <file>.img` — `fakeroot+file-overlay` combination fails;
  `--userns` alone cannot mount a file overlay rw.

## whoami behavior

`whoami` returns `ywatanabe` inside the container. This is the
intentional public-image trade-off:

- The SIF does not bake a per-user `/etc/passwd` entry. Host uid 17107
  is resolved via NSS to the host's `/etc/passwd`, which on Spartan
  reads `ywatanabe`.
- Deployed on a different host (`bob@othermachine`, uid 17107), the
  same SIF would resolve `whoami` to `bob`. That is the correct
  behavior for a public image — it adapts to the host, instead of
  hard-coding the build-time author.
- If a downstream deployment needs `whoami=agent` regardless of host,
  bind a custom `/etc/passwd` containing
  `agent:x:<host-uid>:<host-gid>:agent:/home/agent:/bin/bash` and pass
  `--no-passwd`. Treat this as a per-deploy override, not the default.

## $HOME population

A fresh `<pkg>/upper/home/agent/` overlay starts empty. The agent's
`spec.to_home/` materialization (ADR-0006) populates `~/.claude/`,
`~/.config/`, etc. on first start. Bind mounts (e.g. `~/.ssh:ro`,
`~/.config/gh:ro`) supply credentials that should not live in the
overlay.

`claude` discovers credentials from `$HOME/.claude/.credentials.json`,
which `to_home/` materializes from
`/home/ywatanabe/.claude/.credentials.json` at startup. The 2026-05-18
test on `spartan-gpgpu106` confirmed this path resolves correctly:
Claude Code launched with `Welcome back Yusuke!` (organization
recognized) without a re-login prompt.

## Migration impact

- All five `proj-scitex-*` and `proj-paper-scitex-clew` specs need
  their `spec.apptainer.raw_args` rewritten to drop `--fakeroot` /
  `--writable` and add `--userns --containall --home /home/agent`.
- Per-package overlay dirs must be created under
  `~/.scitex/agent-container/overlays-dir/<pkg>/{upper,work}/` on each
  Spartan host before the corresponding agent starts.
- ADR-0005 (SIF migration) remains valid; this ADR is its successor on
  the specific question of `raw_args` content.

## Verification

Run on spartan-gpgpu106 by user 2026-05-18 01:50:

```
$ apptainer shell --home /home/agent --userns --containall \
    --overlay ~/.scitex/agent-container/overlays-dir/test/ \
    sac-base.sif
Apptainer> echo $HOME
/home/agent
Apptainer> id
uid=17107(ywatanabe) gid=12453(punim2354) groups=12453(punim2354),65534(nogroup)
Apptainer> claude --dangerously-skip-permissions
[Claude Code v2.1.126 — Welcome back Yusuke! launched, responded to "hello"]
```
