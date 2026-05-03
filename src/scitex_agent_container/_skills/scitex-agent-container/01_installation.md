---
description: |
  [TOPIC] Installing scitex-agent-container
  [DETAILS] pip install + optional extras + auth setup (Pro/Max OAuth) + per-host hook convention. Three deployment shapes: local-only, remote via ssh, Spartan HPC compute.
tags: [scitex-agent-container-installation]
---

# Installation

## pip install

```bash
pip install scitex-agent-container          # core: claude-code + claude-session runtimes
pip install 'scitex-agent-container[sdk]'   # adds claude-agent-sdk + starlette/uvicorn for the inbound HTTP endpoint
pip install 'scitex-agent-container[slurm]' # adds scitex-hpc for runtime: slurm and slurm-tenant
pip install 'scitex-agent-container[all]'   # everything
```

The CLI ships as both `scitex-agent-container` and the short alias `sac`.

## Auth (cost-critical)

The SDK runtime reads Anthropic auth in this precedence (see `runtimes/_sdk_common.py`):

1. `ANTHROPIC_API_KEY` env (used verbatim if set — operator opt-in)
2. `~/.claude/.credentials.json` Pro/Max OAuth (default — flat-rate, no per-token billing)
3. `SCITEX_AGENT_CONTAINER_CI_ANTHROPIC_API_KEY_OAUTH` env (CI-only OAuth bridge)
4. `SCITEX_AGENT_CONTAINER_CI_ANTHROPIC_API_KEY` env (last resort — pay-per-token)

Run `claude /login` once to populate the credentials file. Set neither and you get a clear `SDKCommonError` rather than silent fall-through to API-key billing.

## Per-host hook (optional but recommended for remote/HPC)

When running agents on remote hosts via ssh, sac sources `~/.scitex/agent-container/hosts/$(hostname).sh` on the remote before launching the runner. Keeps per-host quirks (Lmod, env unsets, custom PATH, container wrappers) out of the package and in your private dotfiles.

Example for Spartan HPC compute nodes:

```bash
# ~/.scitex/agent-container/hosts/spartan-bm198.hpc.unimelb.edu.au.sh
module load GCCcore/11.3.0 OpenSSL/1.1
unset SCITEX_AGENT_CONTAINER_CI_ANTHROPIC_API_KEY
# Optional: re-exec the runner inside an existing SLURM allocation
if [ -z "$SLURM_JOB_ID" ]; then
    JOBID=$(squeue --me -h -n head-spartan -o "%i" | head -1)
    [ -n "$JOBID" ] && export SAC_RUNNER_PREFIX="srun --jobid=$JOBID --overlap"
fi
```

`SAC_RUNNER_PREFIX` is honored by the launch script; common values:

```bash
export SAC_RUNNER_PREFIX="srun --jobid=$JOBID --overlap"          # SLURM tenancy
export SAC_RUNNER_PREFIX="apptainer exec --bind ... my-sac.sif"   # SIF-pinned version
export SAC_RUNNER_PREFIX="conda run -n agent-env"                  # conda env
```

## Verify

```bash
sac --version
sac agent list             # registered agents (empty on a fresh install)
sac mcp list-tools          # confirms package internals importable
```

## See also

- [02_quick-start.md](02_quick-start.md) — 30-second first-agent walkthrough
- [03_python-api.md](03_python-api.md) — programmatic surface
- [20_env-vars.md](20_env-vars.md) — full env-var reference
