"""Fleet-default bind helpers.

Two classes of fleet-wide bind live here today:

* **P3a-2 single-shared-store** — every agent's apptainer container
  mounts the host's ``~/.scitex/todo/`` so scitex-todo's precedence-4
  user-scope store resolves to the SAME global ``tasks.yaml``
  fleet-wide. Operator directive
  ``feedback_scitex_todo_single_shared_store``
  (lead-learnings/22, P3a unlock). Lead a2a
  ``214dd26d3fd24e088c75a34329895fa4``. This module is the SOLE
  source of the bind — no fleet ``_shared/spec.yaml`` carries an
  explicit ``~/.scitex/todo:`` line (lead audit 2026-06-13 a2a
  ``f33cbc78c2074594b513439d93748810``), so the helper here is what
  every sac-launched agent picks up at boot.

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
    # HAZARD — a dev-source bind MUST target a destination that EXISTS
    # in the SIF. ``default_binds_for_host`` filters ONLY by host-source
    # existence; it cannot see inside the SIF. apptainer normally
    # auto-creates a missing bind destination, BUT under ``--containall``
    # + a directory overlay that is slow to mount (host contention) the
    # auto-create loses the race and apptainer FATALs the WHOLE boot
    # ("destination ... doesn't exist in container") — a silent-looking,
    # NON-DETERMINISTIC death (empty pane, session vanishes at t=0).
    #
    # A second bind to ``/opt/venv-agent/lib/.../scitex_agent_container``
    # used to live here (2026-06-15) to shadow a broken stub that an
    # OLDER SIF build shipped at that path. The canonical sac-base.sif
    # now installs sac under ``/opt/venv-sac`` ONLY — there is NO
    # ``/opt/venv-agent`` in the SIF at all (verified by probe) — so that
    # bind targeted a nonexistent destination and FATAL-killed every boot
    # whose overlay was contended (proj-paper-scitex-clew died instantly
    # 3× while neurovista, winning the same race, came up). Removed
    # 2026-06-23. The ``/opt/venv-sac`` bind above already covers the
    # canonical install. If a future SIF reintroduces a second venv
    # prefix, add its bind ONLY after confirming the destination dir
    # exists in that SIF.
    #
    # Operator handoff path (card sac-bind-host-tmp-emacs-handoff) — under
    # ``--containall`` the host ``/tmp`` is isolated, so agents cannot read
    # the UI debug + screenshot context the operator hands over at
    # ``/tmp/emacs-claude-code/`` (``Element_Debug_Info_*.txt`` etc.), and
    # ``ssh ywata-note-win`` from inside the container is refused. Bind the
    # handoff dir READ-ONLY so an agent reads the file at the SAME path the
    # operator names, with no manual copy-into-home step. Destination is a
    # ``/tmp`` tmpfs path (writable under --containall), so apptainer's
    # bind-dest auto-create cannot lose the overlay race the HAZARD above
    # describes. ``default_binds_for_host`` skips it silently on hosts/times
    # where the source dir does not exist (remote hosts, fresh boot before
    # emacs writes) — no FATAL, no surprise mount.
    "/tmp/emacs-claude-code:/tmp/emacs-claude-code:ro",
    # GENERAL HOST-/tmp HANDOFF — generalise the narrow emacs entry above
    # into an operator->agent file-handoff channel: anything the operator
    # drops in host ``/tmp`` becomes readable in-container at
    # ``/tmp/host/...``. READ-ONLY because host ``/tmp`` holds other
    # processes' tempfiles + live sockets — an agent must NEVER clobber it.
    # Destination ``/tmp/host`` is a SUBPATH under the container's writable
    # ``/tmp`` tmpfs (writable under --containall), so apptainer's bind-dest
    # auto-create cannot lose the overlay race the HAZARD above describes
    # (same reasoning as the emacs entry). Host ``/tmp`` ALWAYS exists, so
    # this default always applies. Do NOT bind over ``/tmp`` ITSELF — that
    # would clobber the container's relocated scratch, the ``/tmp/sac-claude``
    # credentials bind, and the nested-apptainer cache; mounting at the
    # ``/tmp/host`` subpath sits harmlessly inside the tmpfs.
    "/tmp:/tmp/host:ro",
    # PERSISTENT TESTMON CACHE — survive the fresh-git-worktree churn the
    # develop-pin hook forces. Every commit lands in a NEW worktree, so a
    # worktree-local ``.testmondata`` is always cold and pytest re-runs the
    # full ~2500-test suite (~2h). A peer package (scitex-dev) is building a
    # pre-commit-hook wrapper that points testmon's data file at
    # ``$SCITEX_TESTMON_CACHE_ROOT`` (sac injects that env var to the
    # container-side path ``/home/agent/.cache/scitex-testmon`` — see
    # ``_apptainer_listen_env.listen_env_flags``). Binding the host's
    # ``~/.cache/scitex-testmon`` to that container path ``rw`` lets the
    # cache PERSIST across worktree churn so only impacted tests re-run.
    # ``rw`` because testmon must WRITE the updated cache after each run.
    # The ``default_binds_for_host`` skip-if-missing filter means a missing
    # host dir is a silent no-op (NO bind, NO crash) — so sac must NOT
    # mkdir it; the operator/infra creates ``~/.cache/scitex-testmon`` on
    # the host separately (non-container local dev falls back to the same
    # ``~/.cache/scitex-testmon`` path the wrapper reads directly).
    "~/.cache/scitex-testmon:/home/agent/.cache/scitex-testmon:rw",
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

    The returned tuple uses the EXPANDED absolute host path —
    apptainer's ``--bind`` does NOT expand ``~`` (it resolves it as a
    literal dir relative to CWD, causing a FATAL mount failure), so we
    expand against ``$HOME`` here before handing it to ``--bind``.
    """
    out: list[str] = []
    for bind_str in _FLEET_DEFAULT_BINDS:
        if ":" not in bind_str:
            continue
        host_src, _, rest = bind_str.partition(":")
        expanded = Path(host_src).expanduser()
        if expanded.is_dir():
            # Return the EXPANDED absolute host path. apptainer's
            # ``--bind`` does NOT expand ``~`` (it treats it as a
            # literal dir relative to CWD -> FATAL mount failure), so
            # we must hand it an absolute source. Bug fix 2026-06-13:
            # the literal ``~/.scitex/todo`` form broke every agent's
            # boot on restart.
            out.append(f"{expanded}:{rest}")
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
