---
description: |
  [TOPIC] claude-session migration — historical (complete)
  [DETAILS] The CLI/TUI (`claude-code`) → SDK (`claude-session`) migration is DONE and the host-side runtime choice no longer exists. sac is apptainer-only: `runtime: apptainer` is the sole accepted value and the SDK runner runs inside the SIF. There is no `runtime: claude-code` to migrate FROM and no YAML key to flip. This leaf is retained only to explain why old specs that set `runtime: claude-session`/`claude-code` are rejected.
tags: [scitex-agent-container-claude-session-migration, claude-session, migration, fleet]
---

# claude-session migration — historical (complete)

The migration this leaf used to describe — flipping
`runtime: claude-code` → `runtime: claude-session` per agent — is **done
and the runtime-choice no longer exists**. Do not look for a YAML key to
flip.

## Current reality (verify in `config/_validation.py`)

- sac is **apptainer-only** since 2026-05-13. `config/_validation.py`
  accepts only `runtime: apptainer` (empty/unset defaults to apptainer);
  any other value — including `claude-code` and `claude-session` — is a
  hard validation error.
- The SDK runner (`scitex_agent_container._runners.claude_session`) is
  **not a runtime you select**. It is the in-container runner module that
  `ApptainerContainerRuntime` `apptainer exec`s inside the SIF for every
  `kind: Agent`. See [15_claude-session.md](15_claude-session.md).
- The legacy `claude-code` CLI/TUI runtime (the `claude` binary inside
  tmux/screen with auto-accept + pane scraping) and the host-side
  bare-Python runner were **removed**:
  - docker/podman ripout — 2026-05-13 (`runtimes/__init__.py`,
    `runtimes/claude_session.py`).
  - bare-metal subprocess + SSH-dispatch (`spec.remote`) ripout — WI-6,
    2026-05-20 (`config/_types.py`: `RemoteSpec` deleted; validator
    rejects `spec.remote`).

## If you hit `spec.runtime must be 'apptainer'`

An old spec set `runtime: claude-code` or `runtime: claude-session`.
Update it to the canonical apptainer shape (full template in
[15_claude-session.md](15_claude-session.md)):

```yaml
spec:
  runtime: apptainer
  apptainer:
    image: /home/me/.scitex/agent-container/containers/sac-base.sif
    relaxed: true
    raw_args: [--userns, --containall, --home, /home/agent,
               --overlay, /home/me/.scitex/agent-container/containers/overlays/<name>/]
  claude:
    model: claude-opus-4-7[1m]
  startup_commands:
    - command: '[ -x /opt/venv-agent/bin/python ] || { cd /work && uv venv /opt/venv-agent --python python3 && uv pip install --python /opt/venv-agent/bin/python -e ".[all]"; }'
```

Drop any `multiplexer:` line (vestigial — [02_multiplexer.md](02_multiplexer.md))
and any `spec.remote:` block (deleted — use `spec.host` / `sac --on <peer>`
for cross-host placement).

## Cross-host placement (replaces the old per-agent rollout)

The old "migrate fleet agents one at a time, flip the runtime key" flow is
obsolete. Cross-host work now goes through `spec.host` pinning and
`sac --on <peer>` dispatch (F-CS12), not a runtime swap. See
[11_remote-deploy.md](11_remote-deploy.md).

## Related skills

- [15_claude-session.md](15_claude-session.md) — the SDK runner inside apptainer.
- [24_image-build.md](24_image-build.md) — building the `sac-base.sif`.
- [01_config-v3.md](01_config-v3.md) — v3 config format.
