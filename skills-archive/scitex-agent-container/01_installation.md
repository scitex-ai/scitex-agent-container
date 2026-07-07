---
description: |
  [TOPIC] Installing scitex-agent-container
  [DETAILS] pip install + optional extras + auth setup (Pro/Max OAuth) + per-host hook convention. Three deployment shapes: local-only, remote via ssh, Spartan HPC compute.
tags: [scitex-agent-container-installation]
---

# Installation

## pip install

```bash
pip install scitex-agent-container          # core CLI + apptainer runtime + claude-agent-sdk
pip install 'scitex-agent-container[all]'   # everything (mcp + telegram + slurm + dev + docs)
```

`claude-agent-sdk` and `uvicorn` (the inbound `/v1/turn` HTTP endpoint)
are **core** dependencies — they ship in the base install, not behind an
extra. The declared extras are `[mcp]`, `[telegram]`, `[slurm]`, `[dev]`,
`[docs]`, and the `[all]` aggregate (see `pyproject.toml`). There is no
`[sdk]` extra. The CLI ships as both `scitex-agent-container` and the
short alias `sac`.

## What ships in the wheel

The pip install includes the layered runtime recipes, so you can build
images without cloning the repo:

```
<site-packages>/scitex_agent_container/containers/
  apptainer-base.def       # OS + dev tools
  apptainer-scitex.def     # FROM :base + scitex[all] + sac
```

Built artifacts (SIFs, sandboxes) land under user state, never in the
wheel. `sac image build` delegates to `scitex-container`, which owns the
dir-per-layer layout (`cli_pkg/image_group.py`):

```
~/.scitex/agent-container/containers/
  sac-base.sif        -> sac-base/sac-base.sif          (symlink)
  sac-base/
    sac-base.sif
    sac-base.def                         # recipe snapshot at build time
    .def-hash                            # def fingerprint; skips rebuild when unchanged
    sac-base.build-YYYY-MMDD-HHMMSS.log  # full build log, one per build
  sac-scitex.sif      -> sac-scitex/sac-scitex.sif      (symlink)
  sac-scitex/
    sac-scitex.{sif,def}, .def-hash, sac-scitex.build-*.log
  overlays/<agent-name>/                 # per-agent directory overlays (upper/, work/)
  dpkg-lock.txt  node-lock.txt  requirements-lock.txt   # build reproducibility locks
  sac-*/*.sandbox/                       # optional writable sandbox builds (only when built)
```

The `.sif` symlinks at the `containers/` root are the stable paths specs
reference; the dir-per-layer keeps each build's `.def` snapshot, hash,
and logs alongside the image. `overlays/<agent-name>/upper/` is the
relaxed-mode writable upper layer (see ADR-0009); `work/` is its
apptainer overlay workdir.

Build with `sac image build base -y && sac image build scitex -y`. See
[`02_quick-start.md`](02_quick-start.md) for the full first-agent flow.

## Auth (cost-critical)

The SDK runtime reads Anthropic auth in this precedence (see
`runtimes/_sdk_common.py::provision_anthropic_auth`):

1. `~/.claude/.credentials.json` Pro/Max OAuth (default — flat-rate, no per-token billing). Run `claude /login` once to populate it.
2. `SAC_ANTHROPIC_API_KEY` env (sac-namespaced handoff for headless contexts — CI, SLURM, cron — mirrored into `ANTHROPIC_API_KEY` for the SDK).

A bare host `ANTHROPIC_API_KEY` is **never honoured**: if
`SAC_ANTHROPIC_API_KEY` is unset, `provision_anthropic_auth` *pops*
`ANTHROPIC_API_KEY` from the env so a stale dotfiles export can't
silently switch you to pay-per-token billing or shadow a working OAuth
credentials file. Set neither and you get a clear `SDKCommonError`.

## Remote / HPC agents

sac is apptainer-only: the runner always launches via `apptainer exec`
inside the SIF — there is no host-side bare-Python launch path. The old
per-host `~/.scitex/agent-container/hosts/$(hostname).sh` + `SAC_RUNNER_PREFIX`
hook (a host-side `python -m ... claude_session` wrapper) was removed with
the bare-metal/SSH-dispatch ripout (WI-6, 2026-05-20). The
`_runners/_remote_launch` module that generated it still exists but is
dead code (no live importers).

Cross-host placement now goes through:

- **`spec.host`** — pin an agent to a host (or a priority list). See
  [11_remote-deploy.md](11_remote-deploy.md).
- **`sac --on <peer>`** (F-CS12) — dispatch a `sac` command on a peer
  defined in `config.yaml`'s `peers:` block.

HPC-specific environment (Lmod modules, SLURM tenancy, etc.) belongs in
the apptainer image / overlay or the agent's `to_home/` (see
[25_claude-setup-delivery.md](25_claude-setup-delivery.md)), not in a
host-side launch hook.

## Verify

```bash
sac --version
sac agents status           # fleet view — empty on a fresh install
sac mcp list-tools         # confirms package internals importable
sac image list             # built SIFs (none on a fresh install)
```

## See also

- [02_quick-start.md](02_quick-start.md) — 30-second first-agent walkthrough
- [03_python-api.md](03_python-api.md) — programmatic surface
- [20_env-vars.md](20_env-vars.md) — full env-var reference
