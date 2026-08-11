"""The I/O half of the transport: move the bytes, and COUNT THEM ON THE FAR SIDE.

:mod:`_relocate_transport` decides what may travel and whether what landed is
what left. It owns no filesystem and no socket, deliberately, so every refusal
path is a unit test rather than a second machine. This module is the other half:
the adapters that list a directory, move bytes between two hosts, and measure the
result WHERE IT LANDED. The plumbing they run on lives in
:mod:`_relocate_shell` — one route, one place it can be wrong.

WHY tar-OVER-ssh, AND NOT scp OR rsync. The decisive reason is the allowlist.
:func:`.._relocate_transport.select_transferable` returns an EXACT set of names,
and ``tar -C <dir> -cf - -- <name> <name>`` takes exactly those names as
arguments. Nothing is expanded here and nothing is expanded on the far side, so
what travels is the set that was decided. A glob like ``*.jsonl`` would be
re-expanded by a shell at copy time, which turns an allowlist into "whatever
matched the pattern a moment later" — including anything created in between.
Two lesser reasons: rsync must exist on BOTH ends and does not on the fleet's
busybox targets, and scp would need either one connection per file or a remote
glob, which is the first problem again. tar is one connection, one exact file
list, and it rides the same ``-J`` jump chain every other sac ssh call uses.

A PIPELINE'S EXIT CODE IS EVIDENCE, NEVER PROOF. ``tar | ssh tar`` exiting 0 says
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
    "copy_transcripts",
    "ensure_dir",
    "list_transcript_dir",
    "measure_transcripts",
    "move_dir_aside",
    "parse_measurements",
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


def build_copy_argv(
    *,
    source: Shell,
    source_dir: str,
    target: Shell,
    target_dir: str,
    files: Sequence[str],
    peers=None,
) -> list[str]:
    """The argv the BARE HOST runs to move exactly ``files`` and nothing else.

    A pure function so the command itself can be asserted in a test — the thing
    most worth pinning is that the file list is passed as ARGUMENTS and that no
    glob character reaches either shell.

    ``set -o pipefail`` is why this is ``bash -c`` and not ``sh -c``: without it
    the pipeline reports only the LAST stage's status, so a source-side tar that
    failed outright would be masked by an ssh that cheerfully extracted nothing.
    The exit code is still only evidence — arrival is decided by counting on the
    target — but evidence that lies is worse than none.
    """
    if not files:
        raise ValueError(
            "build_copy_argv refuses an empty file list — a copy that transfers "
            "nothing and exits 0 is the failure shape this feature exists to prevent"
        )
    bad = [f for f in files if "/" in f or f in ("", ".", "..")]
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
    named_secrets = [f for f in files if f in CREDENTIAL_BASENAMES]
    if named_secrets:
        raise ValueError(
            f"build_copy_argv refuses to carry {named_secrets!r}: a credential is never "
            "copied — the target re-issues its own. Carrying one leaves two hosts holding "
            "one identity's secret, and the source's copy outliving the source"
        )

    produce: list[str] = ["tar", "-C", source_dir, "-cf", "-", "--", *files]
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


def copy_transcripts(
    *,
    source: Shell,
    source_dir: str,
    target: Shell,
    target_dir: str,
    files: Sequence[str],
    exec_fn: Callable[..., dict] | None = None,
    timeout_s: float = DEFAULT_COPY_TIMEOUT_S,
    peers=None,
) -> RemoteRun:
    """Stream ``files`` from ``source_dir`` into ``target_dir`` over one ssh.

    Returns the pipeline's :class:`RemoteRun`. THE CALLER MUST STILL VERIFY: this
    return value says two processes exited, and the only statement worth making
    is the one :func:`measure_transcripts` makes afterwards, on the target.
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
