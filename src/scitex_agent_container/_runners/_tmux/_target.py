"""``exact_target`` — render a tmux ``-t`` session target that matches EXACTLY.

THE HAZARD (live incident 2026-08-14, card
``sac-tmux-prefix-match-false-alive-20260814``): tmux's ``-t`` resolves a
session name by PREFIX when no exact match exists. ``tmux has-session -t
tui-scitex-cards`` matched the SIBLING session ``tui-scitex-cards-gui``, so
``TmuxManager.exists`` reported the cards agent alive off the GUI agent's
pane, ``sac agents start scitex-cards`` no-op'd having launched nothing, and
a stop/restart would have prefix-match KILLED the innocent sibling. Every
``-t`` that passes a bare session name carries this bug; this helper is the
one place that closes it.

THE FORM (measured against a real tmux 3.4 server, one PASS/FAIL per
subcommand — see ``tests/.../test_tmux_exact_target.py`` which re-proves it
in CI): a leading ``=`` forces exact session-name matching, but the BARE
``=name`` form is NOT uniformly accepted — target-pane subcommands
(``capture-pane``, ``send-keys``) reject it with ``can't find pane: =name``
while ``has-session`` / ``kill-session`` / ``list-panes`` accept it. The
UNIVERSAL form is ``=name:`` — the trailing colon marks the target as a
session (active window, active pane), which every subcommand parses:
``has-session``, ``kill-session``, ``rename-session``, ``capture-pane``,
``send-keys``, ``list-panes``, ``display``, ``resize-window``. So this
helper emits ``=name:``, not ``=name``.

GUARDS — inputs that must NOT be blindly wrapped:

* ``%5`` / ``@2`` / ``$3`` — pane / window / session IDs are already exact
  by construction; prefixing would corrupt them.
* ``=...`` — already exact; never double-prefix.
* ``name:0`` / ``name:0.1`` — the caller supplied a window/pane part; the
  ``=`` prefix still pins the SESSION portion (``=name:0``), and appending
  another colon would change the meaning.
* ``""`` — tmux reads an empty ``-t`` as "the current session"; wrapping it
  would turn a deliberate default into a parse error.
"""

from __future__ import annotations

__all__ = ["exact_target"]

#: First characters that mark a target as already exact: ``=`` (explicit
#: exact-match prefix), ``$`` (session id), ``@`` (window id), ``%`` (pane id).
_ALREADY_EXACT_PREFIXES = ("=", "$", "@", "%")


def exact_target(session_name: str) -> str:
    """Return ``session_name`` as an EXACT-match tmux ``-t`` target.

    ``"tui-foo"`` becomes ``"=tui-foo:"`` — exact session match, universal
    across tmux subcommands (see module docstring for the measured why).
    Targets that are already exact (ids, ``=``-prefixed) and the empty
    string pass through untouched; a target carrying its own ``:window``
    part keeps it (``"tui-foo:0"`` → ``"=tui-foo:0"``).
    """
    if not session_name:
        return session_name
    if session_name.startswith(_ALREADY_EXACT_PREFIXES):
        return session_name
    if ":" in session_name:
        return f"={session_name}"
    return f"={session_name}:"
