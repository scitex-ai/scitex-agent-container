"""apptainer is the container engine. There is no second one to choose.

WHY THIS EXISTS — operator ruling 2026-08-14, verbatim intent:

  「runtime を選べること自体を廃止、apptainer 一本化」
  「迷わないということです。うちはかつ丼だけ出す店です、方式でいいと思います」

The ABOLISHED thing is the CHOICE, not merely the alternatives. What the
choice cost was not a wrong engine — it was doubt: a reader of a spec could
not tell from the spec whether that agent was contained. The value bought
back is containment as a DEFAULT GUARANTEE — by default nothing leaks out —
and one fewer field to be wrong about.

WHAT THE FIELD ACTUALLY WAS. ``spec.container.runtime`` was declared by 105
of 105 live specs on head-mba, every one of them spelling ``none`` — the
value meaning "no container engine". Every one of those agents ran inside
apptainer regardless, because NO launch path ever read the field: it was
parsed into ``ContainerSpec.runtime`` and consulted by nothing. So the field
was not a stale option, it was a stale LIE, and the whole fleet told it.
(The engine actually dispatched is resolved by ``spec.runtime`` — the
HARNESS launch-mode axis — via ``runtimes._apptainer_runtime``; the two
fields shared a name and nothing else.)

WHY REMOVAL IS LOUD AND NOT SILENT. Deleting the vocabulary while leaving
``spec.container`` tolerant of unknown keys would make a stale spec load
clean and mean nothing, which is the same doubt in a new place. So presence
of the key is a hard load error naming the one-line fix — the same posture
``spec.access`` and ``apptainer.container_workdir`` took when they were
removed. There is deliberately NO accept-and-ignore branch and no
deprecation window: a migration phase is a second thing to reason about,
and the fix is deleting one line.

THE CHECK KEYS ON PRESENCE, NOT TRUTHINESS. The check this replaces read::

    if cr and cr not in VALID_CONTAINER_RUNTIMES:

so ``runtime:`` written with an empty or null value passed unexamined. That
shape is how a removed field survives a migration sweep unnoticed: the
sweep greps for values, the spec carries the key, and the two never meet.
A key the author WROTE is a key the author must delete.
"""

from __future__ import annotations

__all__ = ["CONTAINER_ENGINE", "container_runtime_removed_error"]

#: The one engine every sac agent runs inside. Not a default and not a
#: preference — the containment guarantee itself, named once so the
#: invariant test has something to assert against.
CONTAINER_ENGINE = "apptainer"

_REMOVED_MESSAGE = (
    "spec.container.runtime has been REMOVED — "
    f"{CONTAINER_ENGINE} is the only container engine, so there is "
    "nothing left to select. No launch path ever read this field, so "
    "deleting it changes nothing except what the spec claims about "
    "itself.\n"
    "\n"
    "  DELETE the INDENTED one, under `container:`:\n"
    "\n"
    "      spec:\n"
    "        container:\n"
    "          runtime: ...      <-- DELETE THIS LINE\n"
    "\n"
    "  KEEP the one directly under `spec:` — it is a DIFFERENT, LIVE "
    "field (the harness launch mode, e.g. `tui`) that merely shares the "
    "name:\n"
    "\n"
    "      spec:\n"
    "        runtime: tui        <-- LEAVE THIS ALONE\n"
    "\n"
    "  So do not act on a bare `grep runtime:` — check the indentation "
    "first."
)
# The message names no other engine ON PURPOSE — not even to say one is
# gone. A removal notice that lists alternatives has rebuilt the menu it
# was meant to close, and the reader now has a new thing to wonder about.
#
# BUT IT MUST NAME THE NESTING, and that is not stylistic. Measured
# 2026-08-20 across the 122 live specs on compute-04:
#
#     with `spec.runtime` (LIVE)       122 / 122
#     with `spec.container.runtime`      0 / 122
#
# So EVERY spec a reader could be holding contains a `runtime:` line that
# must NOT be deleted, and the one this error is about is absent from all
# of them. The earlier wording said only "DELETE the `runtime:` line from
# the `container:` block". A reader who greps `runtime:` — the obvious
# move, since the message quotes the leaf key — finds exactly one hit,
# the live one, in 122 cases out of 122.
#
# That is not hypothetical. On 2026-08-20 the operator hit this error on a
# host whose checkout still carried the removed key, ran the grep, and was
# one keystroke from deleting `runtime: tui` from a working agent. The
# error text was the whole cause: it was accurate about the field and
# ambiguous about WHICH LINE, and ambiguity in a remedy is executed, not
# puzzled over.
#
# A removal notice is an instruction someone will follow literally. It has
# to be safe to follow literally on a spec that does NOT have the field,
# because that is the spec most readers are looking at.


def container_runtime_removed_error(spec: object) -> list[str]:
    """One error iff the spec still WRITES ``spec.container.runtime``.

    Returns a list (0 or 1 entries) to compose with ``validate_raw``'s
    error accumulation. A non-mapping ``container:`` block yields no
    error here — "container must be a mapping" is a shape diagnostic
    that belongs to the parser, and reporting a removed field inside a
    block that is not even a mapping would name the wrong problem.
    """
    if not isinstance(spec, dict):
        return []
    container = spec.get("container")
    if not isinstance(container, dict) or "runtime" not in container:
        return []
    return [_REMOVED_MESSAGE]
