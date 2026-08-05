"""Host names that are DESIGNED to point at a different machine later.

``nas`` is not a typo for ``nas-03``. It is a name the operator's numbering
scheme deliberately re-points as hardware is replaced — ``nas-01`` →
``nas-02`` → ``nas-03`` → … — so it resolves correctly every single time and
still addresses the wrong machine the day the next generation lands. Nothing
errors, because the name is valid; only the referent moved. (Operator,
2026-08-05: *"nas-03 が正しいです … 010203 とだんだん数字が増えていく"*.)

That makes a moving alias fine as a typing convenience and unsafe as an
IDENTITY. sac keys real decisions on peer names — where a credential is
pushed, which machine a dispatch lands on, which row in the cross-host
registry a heartbeat belongs to — and every one of those silently follows the
alias to the new machine.

So sac refuses a moving alias wherever a name is used as an identity, and the
refusal names the stable replacement. It stays usable in ``~/.ssh/config``,
where "the current NAS" is exactly what a human means when they type it.

The registry below is the single place that knows which names move. Adding
``nas-04`` later means editing ``MOVING_ALIASES`` here and nowhere else.
"""

from __future__ import annotations

# alias → the stable, generation-pinned name to key on instead.
MOVING_ALIASES: dict[str, str] = {
    "nas": "nas-03",
}


class MovingAliasError(KeyError):
    """Raised when a moving alias is used where an identity is required.

    Subclasses ``KeyError`` so existing ``except KeyError`` handlers around
    peer lookup keep working — the change is the message they now carry, not
    the type they catch. ``__str__`` is overridden because ``KeyError`` repr's
    its argument, which would wrap a multi-sentence hint in quotes and escape
    the newlines out of it.
    """

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


def stable_name_for(name: str) -> str | None:
    """Return the pinned name that ``name`` should be replaced by.

    ``None`` when ``name`` is not a known moving alias — which covers both
    "this name is stable" and "nobody has registered it as moving yet". Those
    are genuinely different, but sac treats them the same on purpose: refusing
    a name merely because it is unrecognised would break every legitimate peer.
    """
    return MOVING_ALIASES.get(name)


def moving_alias_hint(name: str, *, context: str = "") -> str | None:
    """Return the actionable refusal message for ``name``, else ``None``.

    ``None`` means "not a moving alias, carry on" — callers branch on it
    rather than catching an exception, so the check reads as a guard at the
    top of a function instead of a control-flow surprise.

    ``context`` names what was being attempted ("peer key in config.yaml",
    "ssh dispatch target"), so the same registry produces a message that fits
    the caller instead of a generic one the reader has to translate.
    """
    stable = stable_name_for(name)
    if stable is None:
        return None
    where = f" as a {context}" if context else ""
    return (
        f"'{name}' is a MOVING ALIAS and must not be used{where}: it is "
        f"re-pointed to a different machine as hardware is replaced "
        f"({name}-01 → {name}-02 → {stable} → …), so it keeps resolving "
        f"while silently addressing another host. Use '{stable}' instead. "
        f"If the current machine has been replaced, update MOVING_ALIASES in "
        f"scitex_agent_container/_state/moving_alias.py to the new generation "
        f"— that is the one place sac reads it from."
    )


__all__ = [
    "MOVING_ALIASES",
    "MovingAliasError",
    "moving_alias_hint",
    "stable_name_for",
]
