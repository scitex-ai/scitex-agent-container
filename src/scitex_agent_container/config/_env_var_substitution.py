"""Tiny env-var-reference parser for ``spec.model.<label>.api_key``.

ADR-0018 §"`api_key` field semantics" defines three accepted forms
for ``spec.model.<label>.api_key``:

1. ``$VAR`` or ``${VAR}`` — env-var reference. Resolved at agent
   START to the host env var's value. Fail loud at start if the
   env var is undefined.

2. Literal value (``sk-ant-...``) — accepted but warned at start
   ("secrets in spec.yaml is anti-pattern; spec.yaml syncs via
   dotfiles git").

3. Omitted (empty string) — falls back to the provider registry's
   ``auth_token_env`` (the PR #244 mechanism).

PR A (this module) only needs to RECOGNIZE the shape so the parser
can store the raw value and the validator can warn on literal
secrets. The actual env-var resolution at agent START lands in PR B
(see ADR-0018 §"Implementation outline" — runtime fallback
dispatcher). Splitting recognition out here keeps the parser pure
(no env access at parse time, important when the validator runs on
a different host than where the agent will eventually launch — env
vars belong to the target host, not the controller).

The format is intentionally a STRICT SUBSET of POSIX shell variable
syntax: only ``$VAR`` and ``${VAR}`` with valid identifier characters
are env refs. Numeric (``$1``), empty-braces (``${}``), bare ``$``,
and any other shell-y form is treated as a LITERAL — operators who
really want a literal that starts with ``$`` get the expected
"this is a literal" semantics rather than a confusing parse error.

A valid identifier follows the C / POSIX rule: starts with a letter
or underscore, continues with letters / digits / underscores. We do
NOT accept lowercase env names — convention across the fleet is
ALL_CAPS env vars, and matching anything looser would shadow normal
secret-looking literals (``$superkey``) into accidental env refs.
"""

from __future__ import annotations

import re

# POSIX-shell-conforming identifier (uppercase / underscore / digit,
# starting with letter or underscore). Pinned uppercase-only because
# every fleet env var the runtime reads (``ANTHROPIC_API_KEY``,
# ``DEEPSEEK_API_KEY``, ``XIAOMI_API_KEY``, ``SAC_*``) is uppercase, and
# the broader pattern would silently route a typo'd-lowercase literal
# like ``$superkey`` through the env-var path → at-start KeyError on a
# value the operator typed as a literal secret. Fail loud at validate
# time on the misspelling instead.
#
# Operators who genuinely need a lowercase env var (vanishingly rare —
# I cannot find one in the current fleet config) get a clear miss here
# and can rename the env var to uppercase; that's a one-line workaround
# for an edge case we explicitly do not want to expand the format to
# cover.
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


def resolve_env_var_ref(raw: str) -> tuple[str, str | None]:
    """Classify ``raw`` as an env-var reference, a literal, or empty.

    Returns ``(kind, var_name)`` where:

    * ``kind == "ref"`` — ``raw`` is ``$VAR`` or ``${VAR}`` and
      ``VAR`` is a valid POSIX-style uppercase identifier. ``var_name``
      is the bare identifier (no ``$`` / ``${}``). The CALLER (PR B
      runtime, not this module) does the actual ``os.environ`` lookup
      at agent START — splitting recognition from resolution lets the
      controller host validate a spec without needing the target host's
      env vars set.

    * ``kind == "literal"`` — ``raw`` is a non-empty string that
      isn't an env-ref. The PR B runtime emits a stderr warning at
      start ("secrets in spec.yaml is anti-pattern; spec.yaml syncs
      via dotfiles git") and uses ``raw`` verbatim as the API key.
      ``var_name`` is ``None``.

    * ``kind == "empty"`` — ``raw`` is the empty string (the parsed
      sentinel for "field omitted"). The PR B runtime falls back to
      the provider registry's ``auth_token_env``. ``var_name`` is
      ``None``.

    Edge cases (documented choices, NOT silently coerced):

    * ``"$"`` alone — literal. There's no identifier to resolve.
    * ``"${}"`` empty braces — literal. Same reason.
    * ``"$1"`` numeric — literal. POSIX positional params don't
      apply to env vars; treating this as a literal matches operator
      intent (probably a copy-pasted token).
    * ``"$lowercase"`` — literal. See module docstring on the
      uppercase-only choice.
    * ``"${MIXED_case}"`` — literal. Same reason.

    Non-string input (None, int, ...) is treated as ``empty`` —
    matches the parser's "stay non-raising; validator surfaces shape
    errors" contract. The validator separately rejects non-string
    ``api_key`` against the raw block.
    """
    if not isinstance(raw, str) or raw == "":
        return ("empty", None)
    if raw.startswith("${") and raw.endswith("}"):
        inner = raw[2:-1]
        if _ENV_NAME_RE.match(inner):
            return ("ref", inner)
        return ("literal", None)
    if raw.startswith("$"):
        inner = raw[1:]
        if _ENV_NAME_RE.match(inner):
            return ("ref", inner)
        return ("literal", None)
    return ("literal", None)


def is_env_var_ref(raw: str) -> bool:
    """Return ``True`` iff ``raw`` parses as an env-var reference.

    Convenience wrapper around :func:`resolve_env_var_ref` for sites
    that don't need the bare identifier — currently the validator's
    "literal secret in spec.yaml" warning path.
    """
    kind, _ = resolve_env_var_ref(raw)
    return kind == "ref"


__all__ = ["resolve_env_var_ref", "is_env_var_ref"]
