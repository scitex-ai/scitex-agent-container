"""Parsers for the spec's DECLARATION fields.

Two top-level keys answer the same kind of question — "what does this spec
STATE about what reaches the agent, instead of leaving it to be discovered on
disk" — so they are parsed together:

* ``to_home_layers`` — which ``to_home`` cascade layers get merged in;
* ``required_claude_hooks`` — which Claude Code hooks must be armed once that
  merge has happened.

Both share one rule that is the point of both fields: **a malformed
declaration RAISES, it never degrades to "absent".** Returning ``None`` for an
unusable value would make a broken declaration indistinguishable from no
declaration, so the spec would silently fall back to inheriting/requiring
everything while its author believed it had stated something. That is the exact
class of surprise these fields exist to remove, so it cannot be how they fail.

``_parse_to_home_layers`` moved here from ``config._loaders`` (which was 515
lines, over the 512-line cap) and is re-exported from there under its original
private name, so existing imports resolve to the same object.
"""

from __future__ import annotations


def parse_to_home_layers(value: object) -> "list[str] | None":
    """Normalise ``spec.to_home_layers`` to a list of names, or ``None``.

    ``None``/absent keeps the implicit cascade. A string is accepted as a
    one-element list, because a single-layer declaration is the common case and
    writing it as a bare scalar in YAML is the obvious thing to do.

    Any other type RAISES — see the module docstring.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ValueError(
        f"spec.to_home_layers must be a list of layer names (or a single name), "
        f"got {type(value).__name__}: {value!r}. Valid names: "
        f"user-shared, project-shared, per-agent. Omit the key entirely to "
        f"inherit the implicit cascade."
    )


def _shape_error(detail: str) -> ValueError:
    return ValueError(
        f"spec.required_claude_hooks {detail}. Expected a mapping of Claude "
        "Code hook EVENT DIRECTORY to a list of script NAMES, e.g.\n"
        "  required_claude_hooks:\n"
        "    pre-tool-use:\n"
        "      - enforce_git_dash_C.sh\n"
        "    post-tool-use:\n"
        "      - log_post_tool_use.sh\n"
        "Omit the key entirely to declare (and enforce) nothing."
    )


def parse_required_claude_hooks(value: object) -> "dict[str, list[str]] | None":
    """Normalise ``spec.required_claude_hooks`` to ``{event: [names]}`` or ``None``.

    A bare string under an event dir is accepted as a one-element list, for the
    same reason ``to_home_layers`` accepts a scalar: a single required hook is
    the common case.

    The EVENT-DIRECTORY NAMES are deliberately NOT validated here. A misspelt
    dir is a real defect, but it is one the report names precisely
    (``required_hooks_declared`` fails with the valid set) — and rejecting it at
    LOAD time would make an agent with a typo unloadable by every sac verb,
    including the one that would tell you about the typo.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _shape_error(f"must be a mapping, got {type(value).__name__}: {value!r}")
    out: "dict[str, list[str]]" = {}
    for raw_dir, raw_names in value.items():
        event_dir = str(raw_dir).strip()
        if not event_dir:
            raise _shape_error("has an empty event-directory key")
        if isinstance(raw_names, str):
            names = [raw_names.strip()] if raw_names.strip() else []
        elif isinstance(raw_names, (list, tuple)):
            names = [str(n).strip() for n in raw_names if str(n).strip()]
        else:
            raise _shape_error(
                f"entry {event_dir!r} must be a list of script names (or a "
                f"single name), got {type(raw_names).__name__}: {raw_names!r}"
            )
        out[event_dir] = sorted(set(names))
    return out


__all__ = ["parse_required_claude_hooks", "parse_to_home_layers"]
