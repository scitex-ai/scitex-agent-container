"""Build the ``to_home_layers`` :class:`MigrationPlan` — the missing half.

:mod:`._layers_migration_model` defines the plan's SHAPE and
:mod:`._layers_migration_apply` consumes it, but nothing ever filled one in.
That gap was invisible because the model's own docstring names a
``plan_migration`` that was never written, so the migration read as complete
while its entry point did not exist. This module is that entry point.

What a plan is derived FROM, and why it matters:

The declaration written into a spec is the cascade that spec resolves TODAY —
:func:`settings_layer_dirs` with the layers it actually contributes, not a
guess and not a constant. That is the whole zero-behaviour-change argument:
declaring what is already true cannot change what an agent inherits. Deriving
it instead from a fixed list, or from the layer NAMES without checking which
resolved to a real directory, would write declarations that differ from
today's behaviour on any host whose layout differs from this one.

Three outcomes per spec, kept apart on purpose:

* **writable** — a single-line insert, ready to apply.
* **refused** — no ``to_home:`` line to anchor to. Expected, named, counted,
  and NOT a failure: a human resolves it. (Explicitly not an error exit.)
* **unreadable** — the spec could not be loaded at all, so its layers could
  not be derived and no edit could be planned. This is NOT a refusal: a
  refusal is the editor declining a shape it knows it does not handle, while
  this never reached the editor. It makes the plan unsafe to apply, because a
  plan that cannot describe every spec does not describe the sweep.

A fourth, quieter outcome exists: a spec that ALREADY declares the key. It is
neither written nor refused, so it appears in ``edits`` carrying no
``new_text`` and no ``refusal`` — see :func:`already_declared`. This is what
makes the sweep re-runnable without either duplicating a key or reporting the
second run as a fleet of refusals.

``unreadable`` entries are formatted ``"<agent>: <reason>"`` so a report names
the spec AND why it could not be read; the agent name is everything before the
first ``": "``.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from ..config import load_config
from ..config._to_home_layers_line import insert_to_home_layers
from ._layers_migration_model import MigrationPlan, SpecEdit, count_added_lines

logger = logging.getLogger(__name__)

_NO_ANCHOR = "no 'to_home:' line to anchor the declaration to"
_RESOLVE_LOGGER = "scitex_agent_container.runtimes._to_home_resolve"
#: An unreadable spec's reason, capped. The full exception is logged; this is
#: the one-line form a report and a JSON field can carry without either
#: swallowing the useful half or spilling a 60-line validation dump per spec.
_REASON_CHARS = 240


def _reason(agent: str, exc: BaseException) -> str:
    """``"<agent>: <Type>: <message>"``, whitespace-collapsed and capped.

    Collapsed rather than first-line-only: ``load_config``'s validation error
    puts "Config validation failed for <path>:" on line one and the fields
    that are actually missing on the lines after, so taking line one alone
    reports that something is wrong while discarding what.
    """
    flat = " ".join(str(exc).split())
    if len(flat) > _REASON_CHARS:
        flat = flat[: _REASON_CHARS - 1] + "…"
    return f"{agent}: {type(exc).__name__}: {flat}"


@contextlib.contextmanager
def quiet_undeclared_warning():
    """Silence ``settings_layer_dirs``' per-agent "declares no layers" WARNING.

    That warning exists to make this migration visible, and it works: measured
    on this host it fires 101 times in one dry-run, once per undeclared spec.
    Inside THIS verb it is pure noise — the command's entire output is that
    same finding, counted, attributed and per-agent, and 101 copies on stderr
    bury the report they duplicate.

    Scoped as tightly as it can be: one named logger, restored in ``finally``,
    and never touched on any production path. Suppressing it anywhere else
    would hide the signal instead of rendering it.
    """
    log = logging.getLogger(_RESOLVE_LOGGER)
    previous = log.level
    log.setLevel(logging.ERROR)
    try:
        yield
    finally:
        log.setLevel(previous)


def fleet_spec_paths(specs_dir: "Path | None" = None) -> "list[Path]":
    """Every fleet spec, via the registry's own enumerator.

    Re-exported rather than re-implemented. ``_reconcile._pass`` already owns
    the answer to "which specs are the fleet", including the ``_``-prefix rule
    that keeps ``_shared`` / ``_template_*`` scaffolding out — measured on this
    host, a raw ``*/spec.yaml`` glob returns 107 where the fleet is 102. Two
    sweeps of one fleet must never disagree about which agents exist.
    """
    from .._reconcile._pass import fleet_spec_paths as _paths

    return _paths(specs_dir)


def resolved_layer_names(config) -> "list[str]":
    """The layer names that CONTRIBUTE to ``config`` today, in cascade order.

    A layer resolving to ``None`` contributes nothing — it is either absent on
    this host or collapsed as a duplicate of an earlier layer — so naming it in
    the declaration would claim an inheritance the agent does not have.
    """
    from ..runtimes._to_home_resolve import settings_layer_dirs

    return [name for name, path in settings_layer_dirs(config) if path is not None]


def already_declared(plan: MigrationPlan) -> "tuple[SpecEdit, ...]":
    """Specs that already declare the key: no write, and no refusal either.

    Separate from both other buckets because it is the only one that is
    finished. Folding it into ``refused`` would make a completed re-run look
    like a fleet needing manual attention; folding it into ``writable`` would
    have the apply rewrite files it does not need to touch.
    """
    return tuple(e for e in plan.edits if e.new_text is None and e.refusal is None)


def plan_spec(path: Path) -> "SpecEdit | str":
    """Plan ONE spec. Returns the edit, or an ``"<agent>: <reason>"`` string.

    The string return is the unreadable case. It is a distinct TYPE rather than
    a ``SpecEdit`` with an empty refusal so a caller cannot accidentally treat
    "could not read this spec" as "this spec needs no edit".
    """
    agent = path.parent.name
    # stx-allow: fallback (reason: this classifies a spec as UNREADABLE — the
    # honest third value. Enumerating exception types would promote any new
    # failure mode into a crash of the whole 102-spec sweep, which is the one
    # outcome a bulk operation must not have.)
    try:
        config = load_config(path)
        layers = tuple(resolved_layer_names(config))
        original = path.read_text()
    except Exception as exc:
        logger.error("migrate-layers: cannot read spec %s — %s", path, exc)
        return _reason(agent, exc)

    if getattr(config, "to_home_layers", None) is not None:
        return SpecEdit(agent=agent, path=path, layers=layers)

    new_text, changed = insert_to_home_layers(original, list(layers))
    if not changed:
        return SpecEdit(agent=agent, path=path, layers=layers, refusal=_NO_ANCHOR)
    return SpecEdit(
        agent=agent,
        path=path,
        layers=layers,
        new_text=new_text,
        lines_added=count_added_lines(original, new_text),
    )


def plan_migration(spec_paths: "list[Path] | None" = None) -> MigrationPlan:
    """What the sweep WOULD do, over every fleet spec. Reads only.

    ``spec_paths`` defaults to the fleet registry. Passing it explicitly is how
    a test points this at a corpus that is not the operator's live fleet.
    """
    paths = list(spec_paths) if spec_paths is not None else fleet_spec_paths()
    edits: list[SpecEdit] = []
    unreadable: list[str] = []
    for path in paths:
        outcome = plan_spec(path)
        if isinstance(outcome, str):
            unreadable.append(outcome)
        else:
            edits.append(outcome)
    return MigrationPlan(edits=tuple(edits), unreadable=tuple(unreadable))


__all__ = [
    "already_declared",
    "fleet_spec_paths",
    "plan_migration",
    "plan_spec",
    "quiet_undeclared_warning",
    "resolved_layer_names",
]
