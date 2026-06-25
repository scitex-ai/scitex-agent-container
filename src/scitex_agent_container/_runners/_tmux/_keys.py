"""Named-key vocabulary for tmux ``send-keys`` passthrough.

``sac agents send <agent> --key <NAME>`` (and ``--keys "<NAME> <NAME>
..."``) routes the full key vocabulary tmux understands into the
agent's live tmux session, so arrows / Enter / Tab / digits land in
the TUI exactly as if typed at the keyboard. Delivery is already
``tmux send-keys`` underneath (see
:meth:`...tmux.TmuxManager.send_keys`); this module is the single
source of truth for *which* names are accepted and rejects anything
else fail-loud with the valid set listed (no surprise key silently
swallowed).

Two kinds of token reach :func:`validate_key`:

  * a NAMED key — one of the tmux keyword names below (``Enter``,
    ``Up``, ``BTab``, ``C-c``, ``M-x`` …). These pass through to
    ``send-keys`` verbatim; tmux itself interprets the keyword.
  * a LITERAL single character — a digit / letter / punctuation
    (``"1"``, ``"a"``, ``"/"``). tmux send-keys treats a non-keyword
    argument as raw input, so a single printable char is delivered
    as-is. We accept any single printable character so the operator
    can send ``1`` / ``2`` to a radio selector or ``y`` to a y/n
    prompt.

Anything else (an unknown multi-char word like ``"Retrun"``, an empty
string) is rejected by :func:`validate_key` raising
:class:`UnknownKeyError`, whose message lists the full named set so
the caller can self-correct.
"""

from __future__ import annotations

__all__ = [
    "NAMED_KEYS",
    "UnknownKeyError",
    "validate_key",
    "validate_keys",
    "parse_key_sequence",
]


# The named keys tmux ``send-keys`` understands as keyword arguments.
# Mirrors the vocabulary in tmux(1) "KEY BINDINGS" / "send-keys": the
# named special keys plus the C-/M- modifier prefixes. Held as a frozen
# set so the validation is a single O(1) membership test and the set
# cannot be mutated at runtime.
_SPECIAL_NAMES: frozenset[str] = frozenset(
    {
        # Submit / whitespace.
        "Enter",
        "Tab",
        "BTab",  # back-tab (Shift-Tab)
        "Space",
        # Editing.
        "BSpace",  # backspace
        "DC",  # delete-character (forward delete)
        "IC",  # insert-character
        "Escape",
        # Navigation.
        "Up",
        "Down",
        "Left",
        "Right",
        "Home",
        "End",
        "PageUp",
        "PPage",  # alias tmux accepts for PageUp
        "PageDown",
        "NPage",  # alias tmux accepts for PageDown
        # Function keys F1..F12.
        "F1",
        "F2",
        "F3",
        "F4",
        "F5",
        "F6",
        "F7",
        "F8",
        "F9",
        "F10",
        "F11",
        "F12",
    }
)

# Aliases the operator may type that map onto a canonical tmux name.
# ``ESC`` and ``C-c`` are the historical cancel-key spellings; keep
# them working so the existing vocabulary is a strict superset.
_ALIASES: dict[str, str] = {
    "ESC": "Escape",
}

# Public view of the accepted named keys (canonical names + aliases),
# used for the fail-loud error message.
NAMED_KEYS: frozenset[str] = _SPECIAL_NAMES | frozenset(_ALIASES)


class UnknownKeyError(ValueError):
    """Raised when a key name is neither a known tmux keyword nor a
    single literal printable character.

    Subclasses :class:`ValueError` so the CLI (``click``) and the MCP
    layer surface it as an input error. The message always lists the
    valid named set so the caller can self-correct without guessing.
    """


def _is_literal_char(token: str) -> bool:
    """Return True for a single printable character (digit / letter /
    punctuation) tmux delivers as raw input.

    A single printable char is the literal-passthrough case: ``"1"``,
    ``"a"``, ``"/"``. Whitespace is excluded — ``Space`` /
    ``Enter`` / ``Tab`` are the named-key spellings for those.
    """
    return len(token) == 1 and token.isprintable() and not token.isspace()


def _modifier_key(token: str) -> bool:
    """Return True for a ``C-<x>`` / ``M-<x>`` / ``S-<x>`` modifier combo.

    tmux accepts control (``C-``), meta/alt (``M-``) and shift
    (``S-``) prefixes on a single following character or a named key
    (e.g. ``C-c``, ``M-x``, ``C-Left``, ``S-Up``). We accept the
    prefix when the remainder is itself a single literal char or a
    known special name, so the full modifier vocabulary is reachable
    without hand-enumerating every combination.
    """
    for prefix in ("C-", "M-", "S-"):
        if token.startswith(prefix) and len(token) > len(prefix):
            rest = token[len(prefix) :]
            if _is_literal_char(rest) or rest in _SPECIAL_NAMES:
                return True
    return False


def validate_key(token: str) -> str:
    """Validate one key token and return the tmux ``send-keys`` argument.

    Accepts, in order:

      * an alias (``ESC`` → ``Escape``) — returns the canonical name;
      * a known special name (``Enter``, ``Up``, ``BTab`` …) verbatim;
      * a ``C-``/``M-``/``S-`` modifier combo (``C-c``, ``M-x``) verbatim;
      * a single printable literal char (``"1"``, ``"a"``) verbatim.

    Raises:
        UnknownKeyError: when ``token`` matches none of the above. The
            message lists the accepted named set so the caller can fix
            the call without guessing.
    """
    if token in _ALIASES:
        return _ALIASES[token]
    if token in _SPECIAL_NAMES:
        return token
    if _modifier_key(token):
        return token
    if _is_literal_char(token):
        return token
    raise UnknownKeyError(
        f"unsupported key {token!r}. Accepted: a single printable "
        f"character (e.g. '1', 'a', '/'), a C-/M-/S- modifier combo "
        f"(e.g. 'C-c', 'M-x'), or one of the named keys "
        f"{sorted(NAMED_KEYS)}."
    )


def validate_keys(tokens: list[str]) -> list[str]:
    """Validate a list of key tokens, returning the tmux arguments.

    Fails loud on the FIRST invalid token (no partial send): the whole
    sequence is rejected so the operator never gets a half-delivered
    key stream. Returns the validated/canonicalised tokens in order.

    Raises:
        UnknownKeyError: on the first unrecognised token.
        ValueError: when ``tokens`` is empty (nothing to send).
    """
    if not tokens:
        raise ValueError("no keys to send (empty sequence)")
    return [validate_key(t) for t in tokens]


def parse_key_sequence(spec: str) -> list[str]:
    """Split a whitespace-separated ``--keys`` spec into tokens.

    ``"Up Up Enter"`` → ``["Up", "Up", "Enter"]``. Whitespace-only
    input yields an empty list (the caller / :func:`validate_keys`
    then rejects it loud). This is deliberately simple — a single
    literal *space* is the named key ``Space``, so there is no need to
    preserve embedded spaces as data.
    """
    return spec.split()
