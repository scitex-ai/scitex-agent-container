"""What Claude Code hooks are ACTUALLY on disk, measured where they are used.

Why a plain listing is the whole implementation
-----------------------------------------------
An agent's effective hook set is the union of TWO stacked mounts over
``/home/agent``:

1. ``<root>/runtime/<agent>/home`` (the workspace-home bind), and
2. ``<root>/containers/overlays/<agent>/upper/home/agent`` (the overlay upper,
   mounted ON TOP).

Every host-side proxy for that union has UNDERCOUNTED. Measured 2026-08-10 for
``scitex-agent-container`` on this laptop::

    runtime/<agent>/home/.claude/hooks/pre-tool-use     67   <- layer 1 only
    overlays/<agent>/upper/home/agent/.../pre-tool-use  71
    $HOME/.claude/hooks/pre-tool-use  (IN the container) 71   <- effective

and ``log_post_tool_use.sh``, which the layer-1 read called missing, is
present in the container. Reading one layer is not a cheaper way to get the
answer; it is a different answer.

So this module does NOT re-implement the union. From inside the container the
mount stack is already resolved by the kernel, and a plain ``os.listdir`` of
``$HOME/.claude/hooks/<event>/`` **is** the effective set. That is the entire
point: a guarantee must be measured where it is consumed.

The consequence, stated plainly because it decides where the gate lives: this
function is only truthful when it runs with the agent's own ``$HOME``. Run on
the bare host it measures the OPERATOR's ``~/.claude`` — the same undercount in
a new costume. :mod:`._floor` therefore reports "whose hooks did I just count"
as its own three-valued check rather than letting a host-side read pass for an
in-container one.

Which directories count as hook directories
-------------------------------------------
:data:`HOOK_EVENT_DIRS` is imported from
:data:`..runtimes._host_merge._HOOK_EVENT_SUBDIRS` — the repo's ONE enumeration
of the event-named subdirs Claude Code discovers hooks from. A second copy here
would be dead the moment it landed and would then drift into being cited as
authoritative.

Which ENTRIES count as hooks (same rules as the host-merge walk, so the two
cannot disagree about what a hook is): regular files only, skipping dot-entries
(``.old/``, ``.<script>.log`` run artifacts), directories (``__pycache__/``),
and ``*.md`` / ``*.log`` / ``.gitignore`` docs and markers. On the measurement
above that turns 71 raw ``ls`` entries into 69 actual hook scripts — the two
extra being ``__pycache__/`` and a README. Both numbers are "correct"; only one
of them is a count of hooks.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..runtimes._host_merge import _HOOK_EVENT_SUBDIRS

#: The event-named subdirs of ``~/.claude/hooks`` that hold hook scripts.
#: Re-exported (not re-declared) from the host-merge walk — see module docstring.
HOOK_EVENT_DIRS: "frozenset[str]" = _HOOK_EVENT_SUBDIRS

#: Non-hook entries inside an event dir (docs / markers), mirroring
#: ``_host_merge._iter_host_files``.
_NON_HOOK_SUFFIXES = (".md", ".log")
_NON_HOOK_NAMES = frozenset({".gitignore"})


@dataclass(frozen=True)
class HookInventory:
    """The hooks visible at one ``.claude/hooks`` root, per event directory.

    ``dirs`` holds only directories that EXIST and could be listed. The other
    two fields are what keep an absence honest, and they are deliberately not
    merged: ``missing_dirs`` is a definite "no hooks are armed for this event"
    (the parent was readable and the directory was not there), while
    ``unreadable_dirs`` is "I could not tell" (an ``OSError`` — permissions, a
    dead symlink, a mount that is not there yet). Collapsing the second into
    the first would manufacture a refusal out of a measurement nobody took.
    """

    root: Path
    dirs: "dict[str, list[str]]" = field(default_factory=dict)
    missing_dirs: "list[str]" = field(default_factory=list)
    unreadable_dirs: "dict[str, str]" = field(default_factory=dict)
    #: Set when the hooks ROOT itself could not be read at all — every
    #: per-directory answer is then UNKNOWN rather than empty.
    root_error: "str | None" = None

    @property
    def counts(self) -> "dict[str, int]":
        """``{event dir: number of hook scripts}`` for the readable dirs."""
        return {name: len(names) for name, names in self.dirs.items()}

    @property
    def total(self) -> int:
        """Total hook scripts across every readable event dir."""
        return sum(len(names) for names in self.dirs.values())

    def has(self, event_dir: str, script: str) -> "bool | None":
        """Is ``script`` armed under ``event_dir``? ``None`` when unknowable.

        ``True`` / ``False`` are measurements. ``None`` is returned when the
        root or that directory could not be read — the caller must not turn
        that into either verdict.
        """
        if self.root_error is not None:
            return None
        if event_dir in self.unreadable_dirs:
            return None
        if event_dir in self.dirs:
            return script in self.dirs[event_dir]
        # Directory absent from a readable root: the hook is definitely not
        # armed. Claude Code cannot load a hook from a directory that is not
        # there, so this is knowledge, not ignorance.
        return False

    def to_dict(self) -> dict:
        """JSON-friendly projection (the shape the CLI/MCP surfaces emit)."""
        return {
            "root": str(self.root),
            "dirs": {name: list(names) for name, names in sorted(self.dirs.items())},
            "counts": self.counts,
            "total": self.total,
            "missing_dirs": sorted(self.missing_dirs),
            "unreadable_dirs": dict(sorted(self.unreadable_dirs.items())),
            "root_error": self.root_error,
        }


def _is_hook_entry(entry: os.DirEntry) -> bool:
    """A regular, non-hidden, non-doc file — the host-merge walk's own rules."""
    name = entry.name
    if name.startswith("."):
        return False
    if name in _NON_HOOK_NAMES or name.endswith(_NON_HOOK_SUFFIXES):
        return False
    # follow_symlinks=True on purpose: the host-merge materialises hooks AS
    # SYMLINKS into the host ~/.claude, so a link is the normal shape of an
    # armed hook. A DEAD link resolves to not-a-file and is correctly excluded
    # — it cannot fire either.
    return entry.is_file()


def hooks_root(home: "str | Path | None" = None) -> Path:
    """The ``.claude/hooks`` root for ``home`` (default: this process's ``$HOME``).

    ``Path.home()`` reads ``$HOME`` on POSIX, which is what makes the container
    measure itself and makes a test hermetic without a single mock: point
    ``$HOME`` at a real tmp tree and the real function reads it.
    """
    base = Path(home).expanduser() if home is not None else Path.home()
    return base / ".claude" / "hooks"


def inventory_hooks(*, home: "str | Path | None" = None) -> HookInventory:
    """List the hooks visible at ``<home>/.claude/hooks`` — never raises.

    Every failure is REPORTED (``root_error`` / ``unreadable_dirs``), never
    swallowed into an empty result: an empty inventory and an unreadable one
    look identical to a caller and mean opposite things.
    """
    root = hooks_root(home)
    try:
        root_exists = root.is_dir()
    except OSError as exc:
        return HookInventory(root=root, root_error=f"{type(exc).__name__}: {exc}")
    if not root_exists:
        # A readable parent with no hooks/ dir is knowledge ("nothing armed"),
        # but we cannot distinguish that from "this is not the agent's home"
        # here — so say what we saw and let ._floor decide, with the
        # measurement-site check beside it.
        return HookInventory(
            root=root,
            root_error=f"no such directory: {root}",
        )

    dirs: "dict[str, list[str]]" = {}
    missing: "list[str]" = []
    unreadable: "dict[str, str]" = {}
    for name in sorted(HOOK_EVENT_DIRS):
        target = root / name
        try:
            if not target.is_dir():
                missing.append(name)
                continue
            with os.scandir(target) as entries:
                dirs[name] = sorted(e.name for e in entries if _is_hook_entry(e))
        except OSError as exc:
            unreadable[name] = f"{type(exc).__name__}: {exc}"
    return HookInventory(
        root=root, dirs=dirs, missing_dirs=missing, unreadable_dirs=unreadable
    )


__all__ = ["HOOK_EVENT_DIRS", "HookInventory", "hooks_root", "inventory_hooks"]
