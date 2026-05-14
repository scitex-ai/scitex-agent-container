#!/usr/bin/env bash
# Lesson 14 — Multi-host: spec.host / spec.hosts and `sac --on <peer>`.
#
# What problem does this solve?
#   You have agents that should run on a specific machine — a GPU box,
#   an HPC login node, a NAS that owns a dataset. You also want to
#   control those agents from your laptop without sshing into each
#   host manually.
#
# Failure mode if you skip this:
#   - You SSH around and run `sac` commands locally on each box,
#     and forget which agent is where.
#   - You start the same agent on two hosts because each `sac agents
#     list` only sees its own host. Set spec.host (singleton) or use
#     `sac registry reconcile` (fleet-wide).
#
# Two host-related fields in spec.yaml:
#
#   spec.host: gpu-box
#     # singleton: this agent runs on the peer named "gpu-box".
#     # `sac agents start` from any host dispatches there.
#
#   spec.hosts: [laptop, gpu-box, nas]
#     # multi-instance: one copy per host. `sac agents start` boots
#     # three apptainer instances, named e.g. my-agent@laptop.
#
# Where are peers defined?
#   ~/.scitex/agent-container/hosts.yaml (or per-project override).
#   Each peer entry has an ssh target and per-host paths. Inspect:
#     sac host list
#     sac host show gpu-box
#     sac host probe gpu-box       # round-trip ssh + sac --version
#
# Cross-host dispatch — the `--on` global flag:
#
#   sac --on gpu-box agents list
#     # → equivalent to: ssh gpu-box sac agents list
#     # but routes through the control-plane (sac listen) bearer-auth
#     # rather than a fresh ssh per call (faster, auditable).
#
#   sac --on gpu-box agents start my-agent
#   sac --on gpu-box agents tail  my-agent --json
#   sac --on gpu-box agents stop  my-agent
#
# Local agent-to-agent across hosts:
#   sac channel send <to-agent> "<msg>"
#     # routes via sac listen → A2A endpoint on the target host.
#
# Pure-apptainer equivalent:
#   None. apptainer is per-host. Multi-host is a sac concept built
#   on top of an ssh-resolved peer registry.
set -euo pipefail
APPLY="${1:-}"

PEER="${SAC_DEMO_PEER:-gpu-box}"

echo "── List configured peers ──"
echo '$ sac host list'
sac host list 2>/dev/null || echo "(sac not installed or no peers configured)"

echo
echo "── spec.yaml: pin an agent to one host ──"
cat <<YAML
spec:
  host: $PEER
  runtime: apptainer
  apptainer:
    image: ~/.scitex/agent-container/containers/sac-scitex.sif
YAML

echo
echo "── spec.yaml: one copy per host ──"
cat <<'YAML'
spec:
  hosts: [laptop, gpu-box, nas]
  runtime: apptainer
YAML

echo
echo "── Dispatch a command on a remote peer ──"
echo '$ sac --on '"$PEER"' agents list'
echo '  # → runs on '"$PEER"', output streamed back'
echo '$ sac --on '"$PEER"' agents start my-agent'
echo '$ sac --on '"$PEER"' agents tail  my-agent --json -n 5'

echo
echo "── Probe a peer (handy when --on hangs) ──"
echo '$ sac host probe '"$PEER"
echo '  # → ssh round-trip ms, sac --version on peer, agent count'

if [[ "$APPLY" == "--apply" ]]; then
    echo
    echo "── sac host probe $PEER (real) ──"
    sac host probe "$PEER" || true
fi

# EOF
