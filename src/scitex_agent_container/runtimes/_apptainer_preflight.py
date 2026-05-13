"""Static D5 preflight for the apptainer runtime.

See ``docs/adr/0001-isolation-hardening.md`` (D2 → D5) +
``docs/isolation.md``.

The script is a single module-level constant so its sha256 is stable;
Clew's verification chain attests the exact bytes that ran inside the
container. Per-operator generation is rejected (see ADR §D4
"Considered and rejected: bind-aware preflight").

Invariants checked:

1. **uid != 0**, OR ``/proc/self/uid_map`` confirms userns-fakeroot
   (host uid is non-zero in the map). sac never runs the agent as
   real root; fakeroot=true is an explicit opt-in (see
   ``apptainer.fakeroot``).
2. **``$HOME`` == ``/home/agent``** — sac auto-injects
   ``--home /home/agent`` so the in-container HOME is canonical and
   operator-independent. Any other value means an operator override
   slipped in or ``relaxed: true`` is in effect; the preflight rejects
   both because they break the verification chain.

The "no host leak" property falls out of ``--containall`` + canonical
HOME + the declared ``binds:`` (reviewable on the AgentCard).
"""

from __future__ import annotations

# Exit codes are stable so external verifiers can map them to causes:
#   11 — running as real root inside the container (not fakeroot)
#   12 — $HOME drifted off /home/agent (non-canonical isolation)
PREFLIGHT_SCRIPT = (
    "set -eu\n"
    # D5 §1: uid != 0, or fakeroot (userns map shows host uid != 0).
    'if [ "$(id -u)" = "0" ]; then\n'
    "  if ! { [ -r /proc/self/uid_map ] && "
    "awk '$1==0 && $2!=0 {found=1} END {exit !found}' /proc/self/uid_map; }; then\n"
    '    echo "ERROR[sac-preflight]: running as real root (no userns fakeroot) — refuse" >&2\n'
    "    exit 11\n"
    "  fi\n"
    "fi\n"
    # D5 §2: canonical HOME.
    'test "$HOME" = "/home/agent" || '
    '{ echo "ERROR[sac-preflight]: \\$HOME=$HOME (expected /home/agent) — non-canonical isolation"'
    " >&2; exit 12; }"
)


__all__ = ["PREFLIGHT_SCRIPT"]
