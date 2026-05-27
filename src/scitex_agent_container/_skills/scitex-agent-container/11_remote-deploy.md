---
description: |
  [TOPIC] Remote SSH Deployment
  [DETAILS] SSH remote deployment of agents to other machines..
tags: [scitex-agent-container-remote-deploy]
---

# Remote SSH Deployment

## YAML Config

```yaml
spec:
  remote:
    host: mba              # SSH hostname
    user: ywatanabe
    timeout: 180            # SSH command timeout (seconds)
    login_shell: true       # Use bash -l -c (default)
  venv: ~/.venv             # Activated on remote before commands
```

## How It Works

1. Copies `spec.yaml` + the whole `to_home/` directory (rsync, with a tar-pipe fallback) to `~/.scitex/agent-container/runtime/<name>/` on the remote
2. SSHs to remote and runs `sac agents start <name>` against the just-copied spec
3. Remote side handles auto-accept and startup commands
4. `remote:` section stripped from copied YAML (prevents recursion)

## Commands

```bash
scitex-agent-container start remote-agent.yaml       # Deploy and start
scitex-agent-container stop remote-agent.yaml        # Stop remote agent
scitex-agent-container inspect remote-agent          # Check live state
scitex-agent-container check remote-agent.yaml       # Preflight checks
scitex-agent-container start --no-preflight ...      # Skip SSH checks
```

## Requirements on Remote Host

- `scitex-agent-container` installed (same version recommended)
- `tmux` (or `screen`) installed
- `claude` CLI installed
- SSH key-based auth configured

## SSH connection multiplexing (ControlMaster)

Every sac call that shells out to ssh — `sac host exec`, `sac host probe`,
dispatch fan-out, drift probes, OAuth send-preflight, ssh+curl turn
delivery — automatically prepends `-o ControlMaster=auto -o
ControlPersist=60s -o ControlPath=<dir>/%C`. Concurrent calls against the
same peer share one TCP+SSH master, which:

- respects per-user `MaxSessions` caps on HPC heads (Spartan: typically
  10) and `sshd_config` `MaxStartups`,
- avoids the per-call TCP/cipher handshake when sac fans out across
  several hosts in parallel, and
- sidesteps the apptainer `control socket dir is read-only` failure
  caused by the default `~/.ssh/sockets` ControlPath landing on the SIF
  overlay.

ControlPath resolution order: explicit caller arg → `$SAC_SSH_CONTROL_DIR`
env → `${TMPDIR:-/tmp}/.sac-ssh-cm`. The dir is `mkdir -p`'d on first
use; if the parent is read-only the function falls through to `[]` and
sac's argv is byte-identical to the pre-patch shape (degrade, don't
crash).

### Opt out

```
export SAC_SSH_CONTROL_MASTER=0   # or 'no' / 'false' / 'off'
```

Useful when ssh is itself proxied through a wrapper that breaks
ControlPath (rare).

### Helper for agent prompts

Agent prompts that shell out to ssh themselves shouldn't re-derive the
flags. Use:

```
ssh   $(sac host ssh-opts) myhost cmd
rsync -e "ssh $(sac host ssh-opts)" src/ myhost:dst/
```

`sac host ssh-opts` prints the flags shell-quoted; empty when the opt-out
env is set so the splat is a no-op either way. The Python helper is
`scitex_agent_container._state.host_config.ssh_control_options()` for
in-process callers.
