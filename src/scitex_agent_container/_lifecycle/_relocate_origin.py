"""Where the agent CAME FROM, written down so a bad move is recoverable by hand.

The operator's item #10: the target records where its source was, so that when
something goes wrong the memory, the temp files and the unfinished git work can
be recovered — agentically, by whoever picks it up, without a human who happens
to remember.

WHY THIS IS NOT THE RESIDENCY HISTORY. :mod:`_residency` answers "which host was
this agent on at time T", which is an ATTRIBUTION question about rows already
written. This answers a RECOVERY question — "the move went wrong, what is left
behind on the old machine and where exactly" — and the two need different
fields. Residency needs a host and two timestamps. Recovery needs paths: the
workdir, the state dir, the transcript, and every repo that had work in it. A
host name alone sends the reader to a machine with no idea where to look.

WHAT IS WORTH RECORDING IS WHAT IS NOT COPIED. The relocation carries the spec
and (when the transport runs) the transcript. Everything else stays on the
source: scratch files, a half-finished branch, a stash, an unpushed commit,
whatever the agent had open. Those are precisely what a recovery needs and
precisely what nothing else in sac points at, so this record names them
individually rather than pointing at a home directory and wishing the reader
luck.

UNCOMMITTED WORK IS RECORDED EVEN THOUGH PREFLIGHT REFUSES ON IT. The check in
:mod:`_relocate_preflight` fails a relocation that would strand uncommitted work,
so in the ordinary case this list is empty. It is recorded anyway for the case
that matters: a relocation run with the check overridden, or work that appeared
between the check and the move. A recovery record that only describes tidy
departures is a recovery record for the case nobody needs it.

AN EMPTY RECORD IS REFUSED. A record naming no host and no path is not a
recovery aid; it is a row that makes the state db look like it answered. So
:class:`OriginRecord` validates at construction, where the mistake is visible,
rather than letting an empty one be discovered by someone who needed it.

Pure: no clock, no filesystem, no git. The observations are gathered elsewhere
and passed in, so the record is testable without a repo and without a move.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "OriginRecord",
    "RepoWork",
    "recovery_lines",
]


@dataclass(frozen=True)
class RepoWork:
    """One repository on the source and what was left un-saved in it.

    ``uncommitted`` and ``unpushed`` are counts, ``None`` for NOT MEASURED. Zero
    and unmeasured are different answers and the difference decides whether a
    reader has to go and look: 0 says the repo was clean, ``None`` says nobody
    asked.
    """

    path: str
    branch: str = ""
    uncommitted: int | None = None
    unpushed: int | None = None

    def __post_init__(self) -> None:
        if not self.path:
            raise ValueError(
                "RepoWork.path must be non-empty — a repo with no path is not findable"
            )
        for name in ("uncommitted", "unpushed"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"RepoWork.{name} must be >= 0 or None, got {value}")

    @property
    def has_work(self) -> bool | None:
        """True if anything is un-saved, ``None`` if neither count was measured."""
        if self.uncommitted is None and self.unpushed is None:
            return None
        return bool(self.uncommitted or self.unpushed)


@dataclass(frozen=True)
class OriginRecord:
    """Everything a recovery needs to reach back into the source host.

    Written at DONE and RETAINED (item #9): the fact of a migration is never
    discarded, so this stays in the state db after the relocation succeeds, not
    only while it is in flight. A record deleted on success is a record that
    exists exactly when it is not needed.
    """

    agent: str
    from_host: str
    to_host: str
    at: float
    #: The agent's working directory on the SOURCE.
    workdir: str = ""
    #: Where the source kept session markers, pidfiles and scratch state.
    state_dir: str = ""
    #: The session the agent was living in when it moved.
    session_uuid: str = ""
    #: The transcript file on the SOURCE, whether or not it was carried.
    transcript_path: str = ""
    #: Whether the transcript was verified on the target — three-valued, and the
    #: unknown is the case a recovery cares about most.
    transcript_carried: bool | None = None
    repos: tuple[RepoWork, ...] = field(default_factory=tuple)
    #: Anything else worth pointing at, in the recorder's own words.
    notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.agent:
            raise ValueError("OriginRecord.agent must be non-empty")
        if not self.from_host:
            raise ValueError(
                "OriginRecord.from_host must be non-empty — a recovery record that does "
                "not name the machine to go back to is not one"
            )
        if not self.to_host:
            raise ValueError("OriginRecord.to_host must be non-empty")
        if self.from_host == self.to_host:
            raise ValueError(
                f"OriginRecord: from_host and to_host are both {self.from_host!r}; "
                "nothing moved, so there is nothing to recover from"
            )
        if self.transcript_carried not in (True, False, None):
            raise ValueError(
                f"OriginRecord.transcript_carried must be True/False/None, got {self.transcript_carried!r}"
            )
        if not (self.workdir or self.state_dir or self.transcript_path or self.repos):
            raise ValueError(
                "OriginRecord names no workdir, state dir, transcript or repo — a record "
                f"that only says {self.from_host!r} sends a recovery to a machine with no "
                "idea where to look. Record at least one path"
            )

    @property
    def repos_with_work(self) -> tuple[RepoWork, ...]:
        """Repos known to hold un-saved work. Unmeasured repos are NOT included.

        They are surfaced separately by :attr:`repos_unmeasured`, because "clean"
        and "not looked at" must not share a bucket in a record whose whole job
        is telling someone where to look.
        """
        return tuple(r for r in self.repos if r.has_work is True)

    @property
    def repos_unmeasured(self) -> tuple[RepoWork, ...]:
        return tuple(r for r in self.repos if r.has_work is None)


def recovery_lines(record: OriginRecord) -> list[str]:
    """The record rendered as instructions someone can follow.

    Written as "go here and look at this", not as a field dump, because the
    reader of this record is by definition dealing with a relocation that went
    wrong and should not also have to work out what the fields mean.

    Pure strings out; the caller decides where they are printed or stored.
    """
    lines = [
        f"ORIGIN  {record.agent} came from {record.from_host} (moved to {record.to_host} at {record.at:.0f})",
        f"  ssh {record.from_host}   # everything below is a path ON THAT HOST",
    ]
    if record.workdir:
        lines.append(f"  workdir         {record.workdir}")
    if record.state_dir:
        lines.append(f"  state dir       {record.state_dir}")
    if record.session_uuid:
        lines.append(f"  session         {record.session_uuid}")
    if record.transcript_path:
        carried = {
            True: "verified on the target",
            False: "NOT carried — this file is the only copy",
            None: "UNKNOWN whether it arrived — check the target before deleting anything here",
        }[record.transcript_carried]
        lines.append(f"  transcript      {record.transcript_path}  ({carried})")

    dirty = record.repos_with_work
    if dirty:
        lines.append("  UNSAVED WORK left on the source:")
        for repo in dirty:
            counts = []
            if repo.uncommitted:
                counts.append(f"{repo.uncommitted} uncommitted")
            if repo.unpushed:
                counts.append(f"{repo.unpushed} unpushed")
            branch = f" on {repo.branch}" if repo.branch else ""
            lines.append(f"    {repo.path}{branch}: {', '.join(counts)}")

    unmeasured = record.repos_unmeasured
    if unmeasured:
        lines.append("  NOT MEASURED (go and look — this is not 'clean'):")
        for repo in unmeasured:
            lines.append(f"    {repo.path}")

    for note in record.notes:
        lines.append(f"  note            {note}")
    return lines
