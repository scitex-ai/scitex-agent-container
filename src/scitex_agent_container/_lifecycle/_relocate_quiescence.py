"""Has the transcript stopped CHANGING? A different question from "did the agent stop?".

THE 2026-08-12 FAILURE, IN ONE SENTENCE: a relocation stopped the agent, verified
the stop, copied the transcript, and aborted 422 because the file had grown
11,717 bytes on an IDENTICAL line count between the measurement and the read.

Nothing was wrong with the stop. :mod:`_relocate_liveness` asked tmux and got the
strongest answer available — *"tmux on ywata-note-win answered and has NO session
tui-… — positive evidence of absence, from tmux's own bookkeeping"* — and that
answer was true. A TMUX SESSION DISAPPEARING IS NOT THE SAME EVENT AS THE PROCESS
EXITING AND RELEASING ITS FILE DESCRIPTOR. The dying process finished flushing its
last line after the session was gone, so the source was MEASURED at one instant
and READ at another with nothing guaranteeing it was unchanged in between.

So SOURCE_STOP now waits for the file itself, not only for tmux. Two consecutive
readings of ``(size, mtime)`` for every ``*.jsonl`` in the source's project
directory must be IDENTICAL before the phase reports success. A flush in flight
changes the size; a same-size rewrite changes the mtime; a new session file
appearing changes the set. Any of the three restarts the wait.

WHY NOT AN OPEN-DESCRIPTOR CHECK, WHICH WOULD BE STRONGER. It needs a tool that is
not on every host in this fleet (``lsof``/``fuser`` are absent on the busybox
targets), and where it exists it needs privileges to see another user's
descriptors — so its unavailable case is UNKNOWN on hosts where the cheap check
would have answered plainly. Two readings need nothing that is not already there.

THE WAIT IS BOUNDED AND A TIMEOUT IS UNKNOWN, NEVER SUCCESS. A file that is still
moving at the deadline is reported with the name of what was moving and by how
much, because "not quiescent" without the evidence is not something anyone can
act on — and because the alternative, proceeding anyway, is the exact bug above.

The sampling touches a host; the comparison and the verdict are pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Final, Sequence

from ._relocate_shell import Shell, marked, one_marked, quote

__all__ = [
    "MARK_QUIET",
    "MARK_QUIET_DIR",
    "QUIESCENCE_INTERVAL_S",
    "QUIESCENCE_TIMEOUT_S",
    "FileState",
    "Quiescence",
    "await_quiescence",
    "describe_change",
    "sample_transcripts",
]

MARK_QUIET_DIR = "TX-QDIR="
MARK_QUIET = "TX-QUIET="

#: Seconds between the two readings that must agree. Long enough that a shutdown
#: flush cannot span both and still look stable, short enough that it costs a
#: healthy relocation one interval and no more.
QUIESCENCE_INTERVAL_S: Final = 2.0

#: The whole wait, bounded — about fifteen readings. Long enough for a process
#: that is slow to let go of its descriptor, short enough that a wedged host
#: fails the phase promptly instead of sitting in front of the copy's much
#: larger budget.
QUIESCENCE_TIMEOUT_S: Final = 30.0


@dataclass(frozen=True)
class FileState:
    """One file at one INSTANT — enough to tell whether it has moved since.

    ``byte_count`` is ``None`` for NOT MEASURED, and a sample containing one
    refuses: two unmeasured readings compare equal, which would manufacture
    exactly the false "it has settled" this module exists to prevent.

    ``mtime`` is ``None`` when the host's ``stat`` answered neither the GNU nor
    the BSD spelling. That is not fatal — the size is the signal for an
    append-only jsonl, and the verdict says out loud that only the size was
    compared — but it is recorded rather than silently treated as equal.
    """

    name: str
    byte_count: int | None = None
    mtime: str | None = None


@dataclass(frozen=True)
class Quiescence:
    """Whether the transcript has stopped changing. Three-valued, no ``__bool__``.

    ``settled=None`` is a wait that ran out or a reading that could not be taken.
    It refuses as firmly as a failure: the next phase reads these files.
    """

    settled: bool | None
    detail: str
    hint: str = ""

    def __post_init__(self) -> None:
        if self.settled not in (True, False, None):
            raise ValueError(
                f"Quiescence.settled must be True/False/None, got {self.settled!r}"
            )
        if not self.detail:
            raise ValueError("Quiescence.detail must be non-empty")
        if self.settled is not True and not self.hint:
            raise ValueError(
                "Quiescence: a wait that did not settle must say what to do next"
            )


def sample_transcripts(
    shell: Shell,
    directory: str,
    *,
    exec_fn: Callable[..., dict] | None = None,
) -> tuple[FileState, ...] | None:
    """Size and mtime for every ``*.jsonl`` in ``directory``, at one instant.

    ``None`` means the script produced no answer at all — not measured, which
    refuses. A directory that does not exist is an OBSERVED empty sample: there
    is no transcript there to still be moving.

    Sorted by name so two readings of the same unchanged directory compare equal
    regardless of the order the far-side shell happened to emit them in.

    A glob is used here, unlike the copy, and deliberately: this asks "did
    ANYTHING in the directory change", so a file that appeared between the two
    readings must be seen. The allowlist governs what TRAVELS, which is a
    different question and a different function.
    """
    body = (
        f"if [ -d {quote(directory)} ]; then\n"
        f"  printf '{MARK_QUIET_DIR}yes\\n'\n"
        f"  cd {quote(directory)} || exit 0\n"
        f"  for __f in *.jsonl; do\n"
        f'    [ -f "$__f" ] || continue\n'
        f'    __b=$(wc -c < "$__f" 2>/dev/null)\n'
        f'    __m=$(stat -c %Y "$__f" 2>/dev/null || stat -f %m "$__f" 2>/dev/null)\n'
        f"    printf '{MARK_QUIET}%s\\t%s\\t%s\\n' \"$__f\" \"$__b\" \"$__m\"\n"
        f"  done\n"
        f"else\n"
        f"  printf '{MARK_QUIET_DIR}no\\n'\n"
        f"fi"
    )
    run = shell.run(body, exec_fn=exec_fn)
    if one_marked(run, MARK_QUIET_DIR) is None:
        return None
    states: list[FileState] = []
    for raw in marked(run, MARK_QUIET):
        parts = raw.split("\t")
        name = parts[0].strip() if parts else ""
        if not name:
            continue
        states.append(
            FileState(
                name=name,
                byte_count=_int_or_none(parts[1] if len(parts) > 1 else ""),
                mtime=(parts[2].strip() if len(parts) > 2 and parts[2].strip() else None),
            )
        )
    return tuple(sorted(states, key=lambda f: f.name))


def _int_or_none(raw: str) -> int | None:
    text = raw.strip()
    return int(text) if text.isdigit() else None


def describe_change(
    before: Sequence[FileState], after: Sequence[FileState]
) -> str:
    """What moved between two readings, in words a refusal can print.

    Pure. Named per file and per direction, because "the transcript was still
    changing" without the numbers is the same unhelpful sentence the 422 that
    prompted this module produced.
    """
    was = {f.name: f for f in before}
    now = {f.name: f for f in after}
    notes: list[str] = []
    for name in sorted(set(now) - set(was)):
        notes.append(f"{name} appeared")
    for name in sorted(set(was) - set(now)):
        notes.append(f"{name} disappeared")
    for name in sorted(set(was) & set(now)):
        old, new = was[name], now[name]
        if old.byte_count != new.byte_count:
            grew = (new.byte_count or 0) - (old.byte_count or 0)
            notes.append(
                f"{name} went from {old.byte_count} to {new.byte_count} bytes ({grew:+d})"
            )
        elif old.mtime != new.mtime:
            notes.append(
                f"{name} was rewritten in place at the same size "
                f"(mtime {old.mtime} -> {new.mtime})"
            )
    return "; ".join(notes)


def _unmeasured(sample: Sequence[FileState]) -> tuple[str, ...]:
    return tuple(f.name for f in sample if f.byte_count is None)


def _mtime_note(sample: Sequence[FileState]) -> str:
    blind = [f.name for f in sample if f.mtime is None]
    if not blind:
        return ""
    return (
        f" (size only for {', '.join(blind)} — this host's stat answered no mtime, "
        "so a same-size rewrite there would not have been seen)"
    )


def _not_measured(directory: str, why: str) -> Quiescence:
    return Quiescence(
        settled=None,
        detail=f"whether {directory} has stopped changing was not measured: {why}",
        hint=(
            "read the source's transcript directory before copying. A stop that was "
            "confirmed by tmux is not evidence that the process has finished writing "
            "— the two are different events, ~11.7 KB apart on 2026-08-12"
        ),
    )


def await_quiescence(
    shell: Shell,
    directory: str,
    *,
    exec_fn: Callable[..., dict] | None = None,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    interval_s: float = QUIESCENCE_INTERVAL_S,
    timeout_s: float = QUIESCENCE_TIMEOUT_S,
) -> Quiescence:
    """Wait until two consecutive readings of ``directory`` agree, or give up saying so.

    ``now`` and ``sleep`` are injected for the usual reason: the wait is real
    wall-clock time on a real relocation, and a test that had to spend it would
    either be slow or would not be written.

    Returns ``settled=True`` only on two IDENTICAL readings. A deadline reached
    while the file is still moving returns ``settled=None`` naming what moved —
    never ``True``, and never ``False``, because "it is still being written" is
    an unfinished measurement rather than a verdict about the host.
    """
    first = sample_transcripts(shell, directory, exec_fn=exec_fn)
    if first is None:
        return _not_measured(directory, "the sampling script gave no answer")
    blind = _unmeasured(first)
    if blind:
        return _not_measured(directory, f"no byte count for {', '.join(blind)}")

    deadline = now() + timeout_s
    while True:
        sleep(interval_s)
        second = sample_transcripts(shell, directory, exec_fn=exec_fn)
        if second is None:
            return _not_measured(directory, "a follow-up reading gave no answer")
        blind = _unmeasured(second)
        if blind:
            return _not_measured(directory, f"no byte count for {', '.join(blind)}")

        if second == first:
            return Quiescence(
                settled=True,
                detail=(
                    f"{len(second)} transcript(s) in {directory} read identically "
                    f"twice, {interval_s:.0f}s apart{_mtime_note(second)}"
                ),
            )

        changed = describe_change(first, second)
        first = second
        if now() >= deadline:
            return Quiescence(
                settled=None,
                detail=(
                    f"{directory} was STILL CHANGING at the {timeout_s:.0f}s "
                    f"quiescence deadline: {changed}"
                ),
                hint=(
                    "something is still writing to the transcript after the agent was "
                    "reported stopped. Find it and let it finish (or stop it) before "
                    "copying — this is the 2026-08-12 abort, where a shutdown flush "
                    "landed between the measurement and the read"
                ),
            )
