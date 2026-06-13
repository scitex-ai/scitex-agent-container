"""Fleet-default bind helpers.

Two classes of fleet-wide bind live here today:

* **P3a-2 single-shared-store** — every agent's apptainer container
  mounts the host's ``~/.scitex/todo/`` so scitex-todo's precedence-4
  user-scope store resolves to the SAME global ``tasks.yaml``
  fleet-wide. Operator directive
  ``feedback_scitex_todo_single_shared_store``
  (lead-learnings/22, P3a unlock). Lead a2a
  ``214dd26d3fd24e088c75a34329895fa4``. The dotfiles
  ``_base/spec.yaml`` carries the explicit bind line for immediate
  coverage; this module makes the bind survive spec churn — every
  sac-launched agent gets the default bind even if its spec doesn't
  carry the explicit line.

* **2026-06-13 SAC overlay stopgap** — bind the host's working
  ``scitex_agent_container`` source over the in-SIF install so
  agents pick up new CLI surface (e.g., ``sac pytest spartan run``
  from PR #375) WITHOUT a 30-minute SIF rebuild. Read-only because
  the host-side tree is the source of truth; nothing inside the
  container should mutate it. Lead a2a ``b6f3916cdf3544a9`` opened
  this as the fast-path for the spartan-pytest hook rollout.
  Removable: delete the overlay entry once a SIF rebuild folds the
  new package version back into the canonical install.

Mechanism — see :func:`apply_default_binds`:
  * The list of default binds is :data:`_FLEET_DEFAULT_BINDS` —
    extend cautiously, every entry adds a host directory bind
    to every agent.
  * An EXPLICIT ``spec.apptainer.binds`` entry to the SAME
    destination path REPLACES the default (operator override
    wins; we de-dupe by destination, not by full string).
  * Missing host source dir → SKIP that default silently. The
    operator may not have a ``~/.scitex/todo/`` yet (clean
    install, fresh laptop), or a fresh deploy host may not have
    the canonical ``~/proj/scitex-agent-container/`` checkout —
    we don't create either from sac code.

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
    # 2026-06-13 STOPGAP (lead a2a b6f3916c) — bind the host's working
    # ``scitex_agent_container`` source over the in-SIF install so
    # agents pick up new CLI surface (e.g., ``sac pytest spartan run``
    # from PR #375) WITHOUT a 30-minute SIF rebuild. Read-only because
    # the host-side tree is the source of truth; nothing inside the
    # container should mutate it.
    #
    # Removable: delete this entry once a SIF rebuild folds the new
    # package version back into the canonical install. The
    # ``default_binds_for_host`` skip-if-missing filter makes the
    # entry a no-op on hosts that don't carry the canonical repo
    # path (e.g., a fresh deploy box). Per-agent spec overrides via
    # ``apptainer.binds`` for the SAME destination still win
    # through ``apply_default_binds``'s de-dup-by-destination merge.
    #
    # Pinned to python3.12 because every SAC SIF def
    # (apptainer-base.def + apptainer-scitex.def) uses ``/opt/venv-sac``
    # with Python 3.12 today; the bind silently skips if a future SIF
    # moves to 3.13 (the destination dir won't exist inside that SIF,
    # apptainer surfaces a benign warning) — operator notices and
    # either updates the entry or drops it after the SIF refresh.
    "~/proj/scitex-agent-container/src/scitex_agent_container"
    ":/opt/venv-sac/lib/python3.12/site-packages/scitex_agent_container:ro",
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
