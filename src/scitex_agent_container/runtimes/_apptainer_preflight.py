"""Static $HOME-visibility preflight for the apptainer runtime.

See ``docs/design/2026-05-13-isolation-hardening.md`` (D2 + D4).

The script is intentionally a single module-level constant so its
sha256 is stable; Clew's verification chain attests the exact bytes
that ran inside the container. Per-operator generation is rejected
(see ADR §D4 "Considered and rejected: bind-aware preflight").

Invariants checked:

1. uid != 0 — sac never runs the agent as root.
2. ``$HOME`` is EMPTY — under ``--containall`` apptainer doesn't
   auto-bind the host home, but it still scaffolds ``$HOME`` as an
   empty directory from the inherited passwd entry. Any *content*
   under ``$HOME`` means either ``--containall`` isn't in effect or
   an operator-declared bind brought host files in.

Bind targets that mirror host home paths (e.g.
``/home/$USER/proj/...``) populate ``$HOME`` with host content and
trip check 2. D4 requires bind targets to use container-canonical
roots (``/srv/``, ``/work/``, ``/opt/``, ``/data/``) so ``$HOME``
stays empty.
"""

from __future__ import annotations

# Exit codes are stable so external verifiers can map them to causes:
#   11 — running as root inside the container
#   12 — host content visible under $HOME inside the container
PREFLIGHT_SCRIPT = (
    "set -eu\n"
    'test "$(id -u)" != "0" || '
    '{ echo "ERROR[sac-preflight]: running as root inside container — refuse"'
    " >&2; exit 11; }\n"
    '{ [ ! -d "$HOME" ] || [ -z "$(ls -A "$HOME" 2>/dev/null)" ]; } || '
    '{ echo "ERROR[sac-preflight]: host content visible under \\$HOME —'
    ' isolation breach (D4: bind targets MUST be /srv/, /work/, /opt/, /data/)"'
    " >&2; exit 12; }"
)


__all__ = ["PREFLIGHT_SCRIPT"]
