"""P3a-2 default-bind helpers — single-shared-store fleet wiring.

Operator directive ``feedback_scitex_todo_single_shared_store``
(lead-learnings/22, P3a unlock). Lead a2a
``214dd26d3fd24e088c75a34329895fa4`` — every agent's apptainer
container mounts the host's ``~/.scitex/todo/`` so scitex-todo's
precedence-4 user-scope store resolves to the SAME global
``tasks.yaml`` fleet-wide. The dotfiles ``_base/spec.yaml`` carries
the explicit bind line for immediate coverage; THIS module makes
the bind survive spec churn — every sac-launched agent gets the
default bind even if its spec doesn't carry the explicit line.

Mechanism — see :func:`apply_default_binds`:
  * The list of default binds is :data:`_FLEET_DEFAULT_BINDS` —
    extend cautiously, every entry adds a host directory bind
    to every agent.
  * An EXPLICIT ``spec.apptainer.binds`` entry to the SAME
    destination path REPLACES the default (operator override
    wins; we de-dupe by destination, not by full string).
  * Missing host source dir → SKIP that default silently. The
    operator may not have a ``~/.scitex/todo/`` yet (clean
    install, fresh laptop); we don't create it from sac code
    because the operator's todo init is a separate workflow
    that owns the layout.

This module is intentionally tiny so the sites that consume the
default-bind list (``_apptainer_runtime.py``) stay under the
512-line module limit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

__all__ = [
    "apply_default_binds",
    "default_binds_for_host",
]


# Fleet-wide default binds. Each entry is the string form
# ``host:container[:mode]`` apptainer's ``--bind`` consumes.
# ``~`` is expanded against the host's ``$HOME`` at resolution time.
_FLEET_DEFAULT_BINDS: tuple[str, ...] = (
    # P3a-2 — scitex-todo single shared store (operator directive
    # feedback_scitex_todo_single_shared_store).
    "~/.scitex/todo:/home/agent/.scitex/todo:rw",
)


def _bind_destination(bind_str: str) -> str:
    """Return the container-side destination path of a bind string.

    Accepts ``host:container`` and ``host:container:mode`` shapes
    (the only two apptainer ``--bind`` consumes). Falls back to
    the whole string for a malformed entry so the caller's de-dup
    set still gets a stable key.
    """
    if ":" not in bind_str:
        return bind_str
    _, _, rest = bind_str.partition(":")
    return rest.split(":", 1)[0]


def default_binds_for_host() -> tuple[str, ...]:
    """Return the fleet-default binds whose host source EXISTS today.

    Walks :data:`_FLEET_DEFAULT_BINDS`, expands ``~`` against the
    operator's ``$HOME``, and FILTERS each entry by whether the
    host-side source path resolves to an existing directory. Missing
    host source = the default skips silently — sac does NOT mkdir on
    the host (the bound layout's ownership lives with whoever owns
    the source tree, e.g. scitex-todo for ``~/.scitex/todo/``).

    The returned tuple uses the ORIGINAL (un-expanded) ``~`` form so
    the caller can hand it directly to apptainer's ``--bind``, which
    expands ``~`` itself per its own resolution rules.
    """
    out: list[str] = []
    for bind_str in _FLEET_DEFAULT_BINDS:
        if ":" not in bind_str:
            continue
        host_src, _, _ = bind_str.partition(":")
        if Path(host_src).expanduser().is_dir():
            out.append(bind_str)
    return tuple(out)


def apply_default_binds(spec_binds: Iterable[str]) -> list[str]:
    """Merge fleet-default binds with the spec's explicit binds.

    Returns a list of bind strings (apptainer ``--bind`` ready) with
    fleet defaults PREPENDED and any explicit spec entry to the SAME
    destination path overriding the default (de-dup by destination —
    the operator's spec is the operator's last word).

    The fleet defaults are filtered by host-source existence via
    :func:`default_binds_for_host` BEFORE merge, so a missing
    ``~/.scitex/todo/`` (operator hasn't initialised the store)
    produces NO bind, NO crash, no surprise mount.
    """
    spec_binds_list = list(spec_binds)
    spec_destinations = {_bind_destination(b) for b in spec_binds_list}
    defaults_that_apply = [
        b
        for b in default_binds_for_host()
        if _bind_destination(b) not in spec_destinations
    ]
    return defaults_that_apply + spec_binds_list
