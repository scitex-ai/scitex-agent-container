"""Error class for the declared Claude-Code-hook floor.

Sibling in spirit to ``runtimes._to_home_errors.UndeclaredToHomeLayers``: that
one refuses a spec that says NOTHING about what gets merged into it; this one
refuses a container in which what the spec DID say is demonstrably not true.
Kept out of ``_to_home_errors`` because that module is scoped to the ``to_home``
materialization pipeline, and this refusal happens later and elsewhere — inside
the running container, after materialization is over.
"""

from __future__ import annotations


class MissingRequiredHooks(RuntimeError):
    """A hook the spec declares REQUIRED is not armed in this container.

    Raised on the container's own startup path (see :mod:`._gate`), before the
    agent runner is ``exec``'d, so a refusal never yields a live agent running
    without the guard it was promised. ``--allow-missing-hooks`` /
    ``SAC_ALLOW_MISSING_HOOKS=1`` starts anyway and logs the bypass at ERROR
    naming exactly which hooks were missing.

    NEVER raised for an UNKNOWN measurement. "I could not read the hooks
    directory" and "the hook is not there" are different findings, and turning
    the first into this exception would ground an agent on an unreadable mount
    — the mirror image of the silent pass this class exists to prevent.
    """


__all__ = ["MissingRequiredHooks"]
