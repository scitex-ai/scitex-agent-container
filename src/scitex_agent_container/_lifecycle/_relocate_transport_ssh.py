"""The I/O half of the transport: move the bytes, and COUNT THEM ON THE FAR SIDE.

:mod:`_relocate_transport` decides what may travel and whether what landed is
what left. It owns no filesystem and no socket, deliberately, so every refusal
path is a unit test rather than a second machine. This module is the other half:
the adapters that list a directory, move bytes between two hosts, and measure the
result WHERE IT LANDED. The plumbing they run on lives in
:mod:`_relocate_shell` — one route, one place it can be wrong.

WHAT TRAVELS IS A SNAPSHOT, AND THE SNAPSHOT IS TAKEN FIRST. Before a byte moves,
:func:`snapshot_transcripts` records, per file, the offset of the LAST NEWLINE and
the number of complete lines at that instant. The copy carries exactly that many
bytes and arrival is checked against exactly those numbers. The source is then
free to grow underneath the transfer without changing the answer — which is the
whole point, because on 2026-08-12 it did: the file was 11,717 bytes longer when
it was read than when it was measured, on an identical line count, and the
relocation aborted 422 over bytes that were never lost.

WHY head -c PER FILE, AND NOT tar, scp OR rsync. tar was the earlier choice and
its argument still holds for names: an allowlist decided an EXACT set, and a glob
re-expanded by a shell at copy time would carry whatever matched a moment later.
What tar cannot do is carry PART of a file, and a whole file is by definition not
a snapshot. ``head -c <recorded-offset> -- <path>`` is the bounded read stated
directly, it is present everywhere in this fleet (GNU, busybox and BSD all have
it, unlike ``truncate``), it moves only the bytes that were promised, and the path
is built here rather than expanded anywhere, so nothing globs on either side.

THE COST IS ONE SSH PER FILE, AND IT IS PAID KNOWINGLY. tar was one connection for
the whole set; this is one per transcript, chained under ``&&`` in a single
host-side ``bash -c`` so the whole thing is still one ``host_exec`` and one exit
code. Measured before choosing it: the busiest project directory on this fleet
holds six transcripts, so the connection count is single digits. Truncating on the
TARGET instead would keep one connection, but it would ship the extra bytes and
then throw them away, and it would need ``truncate`` or a temp-file rewrite on
hosts that may have neither.

A PIPELINE'S EXIT CODE IS EVIDENCE, NEVER PROOF. ``head | ssh cat`` exiting 0 says
two processes exited 0; it says nothing about what is on the disk at the other
end. So the exit code is returned and reported, and the ANSWER comes from
:func:`measure_transcripts` run ON THE TARGET — ``wc -c`` and ``wc -l`` executed
by the target's own shell on the target's own files. Nothing here infers a
target-side count from a local ``stat``, which is the shortcut that makes a
truncated copy look verified.

BOTH SIDES ARE MEASURED BY THE SAME SCRIPT TEXT. ``wc -l`` counts newlines, so a
file with no trailing newline reads one short — identically on both hosts,
because the identical command runs on both. A comparison between two different
measuring methods produces mismatches that mean nothing and hides the ones that
mean something.
"""

from __future__ import annotations

import shlex
from typing import Callable, Sequence

from ._relocate_probe_ssh import RemoteRun, build_probe_argv
from ._relocate_shell import (
    Shell,
    marked,
    one_marked,
    quote,
    run_argv_on_host,
)
from ._relocate_transport import CREDENTIAL_BASENAMES, TranscriptFile

__all__ = [
    "MARK_ABSENT",
    "MARK_DIR",
    "MARK_ENTRY",
    "MARK_FILE",
    "MARK_MOVED",
    "build_copy_argv",
    "build_tree_copy_argv",
    "copy_transcripts",
    "copy_tree",
    "ensure_dir",
    "list_transcript_dir",
    "measure_transcripts",
    "move_dir_aside",
    "parse_measurements",
    "snapshot_transcripts",
]

MARK_DIR = "TX-DIR="
MARK_ENTRY = "TX-ENTRY="
MARK_FILE = "TX-FILE="
MARK_ABSENT = "TX-ABSENT="
MARK_MOVED = "TX-MOVED="

#: Moving a multi-megabyte transcript over a jump chain. Separate from the
#: measurement timeout because the two fail for different reasons and want
#: different patience.
DEFAULT_COPY_TIMEOUT_S = 900.0


def list_transcript_dir(
    shell: Shell,
    directory: str,
    *,
    exec_fn: Callable[..., dict] | None = None,
) -> tuple[str, ...] | None:
    """Every entry in ``directory`` on ``shell``, or ``None`` if it was not listed.

    ``ls -A`` rather than a glob, so DOTFILES ARE SEEN. That is deliberate and it
    is the point of :data:`.._relocate_transport.CREDENTIAL_BASENAMES`: a
    credential that is never listed cannot be refused BY NAME, and a filter whose
    input never contained the dangerous thing proves nothing about the filter.

    A missing directory returns an empty tuple — an OBSERVED "nothing there",
    which the plan turns into CODE_NOTHING_TO_CARRY. A listing that could not be
    taken returns ``None``, which refuses. Those are different answers, and the
    difference is the whole discipline.
    """
    body = (
        f"if [ -d {quote(directory)} ]; then\n"
        f"  printf '{MARK_DIR}yes\\n'\n"
        f"  ls -A -- {quote(directory)} | sed 's/^/{MARK_ENTRY}/'\n"
        f"else\n"
        f"  printf '{MARK_DIR}no\\n'\n"
        f"fi"
    )
    run = shell.run(body, exec_fn=exec_fn)
    exists = one_marked(run, MARK_DIR)
    if exists is None:
        return None
    if exists == "no":
        return ()
    return tuple(marked(run, MARK_ENTRY))


def measure_transcripts(
    shell: Shell,
    directory: str,
    names: Sequence[str],
    *,
    exec_fn: Callable[..., dict] | None = None,
) -> tuple[TranscriptFile, ...]:
    """Byte and line counts for ``names``, measured BY ``shell`` ON ``shell``.

    Returns one :class:`TranscriptFile` per name that EXISTS there. A name that is
    absent is simply not in the result, which is what makes
    :func:`.._relocate_transport.verify_arrival` report "absent on the target"
    rather than a size mismatch — the copy did not happen, versus it happened
    badly, and those call for different next moves.

    A file that exists but whose counts could not be taken comes back with
    ``None`` counts, i.e. NOT MEASURED, which verification treats as UNKNOWN.
    Counting it as zero would turn an unreadable file into an empty one.
    """
    if not names:
        return ()
    parts = [f"cd {quote(directory)} 2>/dev/null || exit 0"]
    for name in names:
        parts.append(
            f"if [ -f {quote(name)} ]; then\n"
            f"  __b=$(wc -c < {quote(name)} 2>/dev/null)\n"
            f"  __l=$(wc -l < {quote(name)} 2>/dev/null)\n"
            f'  printf \'{MARK_FILE}%s\\t%s\\t%s\\n\' {quote(name)} "$__b" "$__l"\n'
            f"else\n"
            f"  printf '{MARK_ABSENT}%s\\n' {quote(name)}\n"
            f"fi"
        )
    return parse_measurements(shell.run("\n".join(parts), exec_fn=exec_fn))


def snapshot_transcripts(
    shell: Shell,
    directory: str,
    names: Sequence[str],
    *,
    exec_fn: Callable[..., dict] | None = None,
) -> tuple[TranscriptFile, ...]:
    """The PREFIX of each file that will travel: whole lines only, measured once.

    Returns the same :class:`TranscriptFile` shape as :func:`measure_transcripts`,
    but the counts describe the SNAPSHOT rather than the file: ``byte_count`` is
    the offset of the last newline and ``line_count`` the number of complete lines
    at the instant this ran. Those two numbers are the contract for everything
    downstream — the copy is bounded by the first and arrival is checked against
    both, so a source that grows afterwards does not change the answer.

    The offset is computed as ``head -n <complete-lines> | wc -c``, which is the
    definition rather than an approximation of it: the bytes of the first L lines
    ARE the bytes up to and including the last newline. It is POSIX, it needs no
    ``stat`` spelling, and it gives the same answer on every host in the fleet.
    Deliberately NOT ``wc -c`` minus a guess: cutting anywhere but a newline hands
    the target a torn final record, which is malformed JSON inside a file that
    still parses as JSONL everywhere else.

    A file whose line count could not be taken comes back with ``line_count=None``
    — NOT MEASURED, which verification treats as UNKNOWN and which
    :func:`build_copy_argv` refuses to carry.
    """
    if not names:
        return ()
    parts = [f"cd {quote(directory)} 2>/dev/null || exit 0"]
    for name in names:
        parts.append(
            f"if [ -f {quote(name)} ]; then\n"
            f"  __l=$(wc -l < {quote(name)} 2>/dev/null)\n"
            f'  __b=$(head -n "$__l" < {quote(name)} 2>/dev/null | wc -c)\n'
            f'  printf \'{MARK_FILE}%s\\t%s\\t%s\\n\' {quote(name)} "$__b" "$__l"\n'
            f"else\n"
            f"  printf '{MARK_ABSENT}%s\\n' {quote(name)}\n"
            f"fi"
        )
    return parse_measurements(shell.run("\n".join(parts), exec_fn=exec_fn))


def parse_measurements(run: RemoteRun) -> tuple[TranscriptFile, ...]:
    """Read ``TX-FILE=<name>\\t<bytes>\\t<lines>`` lines out of a run.

    Split out so the parse is testable against captured output with no host at
    all — the same seam the probe adapter uses.
    """
    files: list[TranscriptFile] = []
    for raw in marked(run, MARK_FILE):
        parts = raw.split("\t")
        name = parts[0].strip() if parts else ""
        if not name:
            continue
        files.append(
            TranscriptFile(
                name=name,
                byte_count=_int_or_none(parts[1] if len(parts) > 1 else ""),
                line_count=_int_or_none(parts[2] if len(parts) > 2 else ""),
            )
        )
    return tuple(files)


def _int_or_none(raw: str) -> int | None:
    text = raw.strip()
    return int(text) if text.isdigit() else None


def ensure_dir(
    shell: Shell,
    directory: str,
    *,
    exec_fn: Callable[..., dict] | None = None,
) -> bool | None:
    """``mkdir -p`` the destination, then CONFIRM it is a directory.

    Three-valued for the usual reason: ``mkdir`` exiting 0 and the directory
    existing are two claims, and the second is the one the copy depends on. The
    confirmation is a separate ``[ -d ]`` in the same round trip.
    """
    body = (
        f"mkdir -p {quote(directory)} 2>/dev/null\n"
        f"if [ -d {quote(directory)} ]; then printf '{MARK_DIR}yes\\n'; "
        f"else printf '{MARK_DIR}no\\n'; fi"
    )
    answer = one_marked(shell.run(body, exec_fn=exec_fn), MARK_DIR)
    return None if answer is None else answer == "yes"


def move_dir_aside(
    shell: Shell,
    directory: str,
    destination: str,
    *,
    exec_fn: Callable[..., dict] | None = None,
) -> bool | None:
    """Move ``directory`` to ``destination``. NEVER a delete, on either host.

    ``True`` when the move happened and the old path is gone, ``False`` when it
    did not, ``None`` when it could not be told. A directory that was not there
    in the first place is ``True`` with nothing moved: the postcondition the
    caller wants is "that path is clear", and it already is.

    ``mv`` rather than ``cp`` + ``rm``, so there is no window in which two copies
    exist and one of them is being deleted.
    """
    if not destination:
        raise ValueError(
            "move_dir_aside needs somewhere to move it TO — never a delete"
        )
    body = (
        f"if [ ! -e {quote(directory)} ]; then printf '{MARK_MOVED}vacuous\\n'; exit 0; fi\n"
        f'mkdir -p "$(dirname {quote(destination)})" 2>/dev/null\n'
        f"if mv {quote(directory)} {quote(destination)} 2>/dev/null && "
        f"[ ! -e {quote(directory)} ] && [ -e {quote(destination)} ]; then\n"
        f"  printf '{MARK_MOVED}yes\\n'\n"
        f"else\n"
        f"  printf '{MARK_MOVED}no\\n'\n"
        f"fi"
    )
    answer = one_marked(shell.run(body, exec_fn=exec_fn), MARK_MOVED)
    return None if answer is None else answer in ("yes", "vacuous")


def _refuse_bad_snapshots(files: Sequence[TranscriptFile]) -> None:
    """Every reason a named snapshot must not be carried, raised before anything runs."""
    if not files:
        raise ValueError(
            "build_copy_argv refuses an empty file list — a copy that transfers "
            "nothing and exits 0 is the failure shape this feature exists to prevent"
        )
    bad = [f.name for f in files if "/" in f.name or f.name in ("", ".", "..")]
    if bad:
        raise ValueError(
            f"build_copy_argv takes bare file names, got {bad!r}. A path component here "
            "would place the file outside the directory the allowlist was applied to"
        )
    # The credential refusal is repeated HERE, at the only place bytes actually
    # move, rather than trusted to hold upstream. `select_transferable` already
    # declines these, but this function is generic — it will carry any named file
    # — and a transport that would copy a credential if asked is one that
    # eventually does.
    named_secrets = [f.name for f in files if f.name in CREDENTIAL_BASENAMES]
    if named_secrets:
        raise ValueError(
            f"build_copy_argv refuses to carry {named_secrets!r}: a credential is never "
            "copied — the target re-issues its own. Carrying one leaves two hosts holding "
            "one identity's secret, and the source's copy outliving the source"
        )
    unbounded = [f.name for f in files if f.byte_count is None or f.byte_count < 0]
    if unbounded:
        raise ValueError(
            f"build_copy_argv refuses to carry {unbounded!r} with no recorded byte "
            "offset. The snapshot IS the bound: copying 'however much is there now' "
            "restores the race the offset was introduced to remove, and the resulting "
            "mismatch would be reported against a source that had already moved"
        )


def build_copy_argv(
    *,
    source: Shell,
    source_dir: str,
    target: Shell,
    target_dir: str,
    files: Sequence[TranscriptFile],
    peers=None,
) -> list[str]:
    """The argv the BARE HOST runs to move exactly these SNAPSHOTS and nothing else.

    ``files`` are snapshots from :func:`snapshot_transcripts`, not bare names:
    each carries the byte offset that bounds its read. A pure function so the
    command itself can be asserted in a test — the two things most worth pinning
    are that no glob character reaches either shell and that every stage is
    bounded by a number recorded before the copy began.

    ``set -o pipefail`` is why this is ``bash -c`` and not ``sh -c``: without it
    a pipeline reports only the LAST stage's status, so a source-side read that
    failed outright would be masked by an ssh that cheerfully wrote nothing. The
    stages are chained with ``&&`` so the first failure stops the rest. The exit
    code is still only evidence — arrival is decided by counting on the target —
    but evidence that lies is worse than none.
    """
    _refuse_bad_snapshots(files)

    stages: list[str] = []
    for snapshot in files:
        read = [
            "head",
            "-c",
            str(int(snapshot.byte_count or 0)),
            "--",
            f"{source_dir.rstrip('/')}/{snapshot.name}",
        ]
        produce = (
            read
            if source.is_local
            else build_probe_argv(source.host, source.script(shlex.join(read)), peers)
        )
        landing = f"{target_dir.rstrip('/')}/{snapshot.name}"
        write = (
            f"mkdir -p {quote(target_dir)} && cat > {quote(landing)} && "
            f"printf '{MARK_FILE}%s\\n' {quote(snapshot.name)}"
        )
        consume = build_probe_argv(target.host, target.script(write), peers)
        stages.append(f"{shlex.join(produce)} | {shlex.join(consume)}")

    pipeline = (
        "set -o pipefail; "
        + " && ".join(stages)
        + f" && printf '{MARK_DIR}yes\\n'"
    )
    return ["bash", "-c", pipeline]


def build_tree_copy_argv(
    *,
    source: Shell,
    source_dir: str,
    target: Shell,
    target_dir: str,
    names: Sequence[str],
    peers=None,
) -> list[str]:
    """The argv that carries WHOLE entries — a directory, or a file in full.

    THE SNAPSHOT DOES NOT APPLY HERE, and that is the distinction between this
    and :func:`build_copy_argv`. A transcript is an append-only log being written
    by a process that may still be flushing, so it is carried up to a recorded
    offset. A spec directory and a ``memory/`` directory are neither append-only
    nor open: there is no last-newline to cut at, and half of one is not a
    shorter version of it. They travel whole, by tar.

    tar's original argument still holds for these: ``tar -C <dir> -cf - -- <name>``
    takes exactly the named entries, recursing into a directory as ONE name, and
    nothing is re-expanded by a shell on either side. A glob would be re-expanded
    at copy time, which turns an allowlist into "whatever matched a moment later".
    """
    if not names:
        raise ValueError(
            "build_tree_copy_argv refuses an empty list — a copy that transfers "
            "nothing and exits 0 is the failure shape this feature exists to prevent"
        )
    bad = [n for n in names if "/" in n or n in ("", ".", "..")]
    if bad:
        raise ValueError(
            f"build_tree_copy_argv takes bare entry names, got {bad!r}. A path component "
            "here would place the copy outside the directory the allowlist was applied to"
        )
    named_secrets = [n for n in names if n in CREDENTIAL_BASENAMES]
    if named_secrets:
        raise ValueError(
            f"build_tree_copy_argv refuses to carry {named_secrets!r}: a credential is "
            "never copied — the target re-issues its own"
        )

    produce: list[str] = ["tar", "-C", source_dir, "-cf", "-", "--", *names]
    if not source.is_local:
        produce = build_probe_argv(
            source.host, source.script(shlex.join(produce)), peers
        )
    extract = (
        f"mkdir -p {quote(target_dir)} && tar -C {quote(target_dir)} -xf - && "
        f"printf '{MARK_DIR}yes\\n'"
    )
    consume = build_probe_argv(target.host, target.script(extract), peers)
    pipeline = f"set -o pipefail; {shlex.join(produce)} | {shlex.join(consume)}"
    return ["bash", "-c", pipeline]


def copy_tree(
    *,
    source: Shell,
    source_dir: str,
    target: Shell,
    target_dir: str,
    names: Sequence[str],
    exec_fn: Callable[..., dict] | None = None,
    timeout_s: float = DEFAULT_COPY_TIMEOUT_S,
    peers=None,
) -> RemoteRun:
    """Carry whole entries between two hosts over one ssh. Caller must verify.

    Used for the spec directory and for ``memory/``. Arrival is confirmed by
    listing and sizing the tree on BOTH hosts (:func:`.._relocate_target_ssh.
    list_tree`), never by this return value.
    """
    argv = build_tree_copy_argv(
        source=source,
        source_dir=source_dir,
        target=target,
        target_dir=target_dir,
        names=names,
        peers=peers,
    )
    return run_argv_on_host(
        argv,
        exec_fn=exec_fn,
        timeout_s=timeout_s,
        what=f"the host, copying {source.host}:{source_dir} -> {target.host}:{target_dir}",
    )


def copy_transcripts(
    *,
    source: Shell,
    source_dir: str,
    target: Shell,
    target_dir: str,
    files: Sequence[TranscriptFile],
    exec_fn: Callable[..., dict] | None = None,
    timeout_s: float = DEFAULT_COPY_TIMEOUT_S,
    peers=None,
) -> RemoteRun:
    """Stream each snapshot from ``source_dir`` into ``target_dir``, bounded by its offset.

    Returns the pipeline's :class:`RemoteRun`. THE CALLER MUST STILL VERIFY: this
    return value says some processes exited, and the only statement worth making
    is the one :func:`measure_transcripts` makes afterwards, on the target,
    against the snapshot numbers.
    """
    argv = build_copy_argv(
        source=source,
        source_dir=source_dir,
        target=target,
        target_dir=target_dir,
        files=files,
        peers=peers,
    )
    return run_argv_on_host(
        argv,
        exec_fn=exec_fn,
        timeout_s=timeout_s,
        what=f"the host, copying {source.host}:{source_dir} -> {target.host}:{target_dir}",
    )
