"""Where this agent came from, written where the agent itself will find it.

THE OPERATOR'S RULING, 2026-08-12, on being asked how a relocation should merge a
``memory/`` directory that may have diverged on both hosts:

    「エージェントがどこから来たのかっていうのが分かって、そこを調査することが
    できるならば、全く問題ない」

    — if the agent can tell where it came from and go and investigate there,
      there is no problem at all.

He called the merge policy 枝葉, a leaf detail. THE REQUIREMENT THAT MATTERS IS
PROVENANCE. So this module writes the record: which host, which absolute path,
when, what travelled, and — the half that is usually missing — WHAT DID NOT, with
the reason for each. Nothing is ever deleted on the source, so every refused entry
named here is still sitting where it was named, ready to be fetched by hand.

WHY THE REFUSALS ARE THE IMPORTANT HALF. A relocation that carried nine tenths of
an agent and said so is recoverable in a minute; one that carried nine tenths
silently is discovered weeks later by someone who assumed it moved everything.
That is exactly how ``memory/`` went missing: the refusal existed, as one line in
a run log that nobody keeps. Here it lands in a file on the target, beside the
conversation, where the agent that lost the memory is the one who reads it.

WHY MARKDOWN AND NOT JSON. The reader is an agent with a filesystem and an
operator over its shoulder, not a parser. It sits in the project directory, which
the runner scans for ``*.jsonl`` and ignores everything else, so a ``.md`` there
is inert.

Pure: strings and numbers in, one document out. The writing is somebody else's
job, so every line of this can be asserted without a second machine.
"""

from __future__ import annotations

import time
from typing import Final, Sequence

__all__ = [
    "PROVENANCE_FILENAME",
    "render_provenance",
]

#: Named so an agent can be told to read it, and so a later relocation's listing
#: shows it by a name that explains itself.
PROVENANCE_FILENAME: Final = "RELOCATED-FROM.md"


def _rule(text: str) -> str:
    return f"- {text}"


def render_provenance(
    *,
    agent: str,
    from_host: str,
    source_dir: str,
    to_host: str,
    target_dir: str,
    when: float,
    session: str = "",
    transcripts: Sequence[tuple[str, int | None, int | None]] = (),
    directories: Sequence[str] = (),
    refused: Sequence[tuple[str, str]] = (),
    displaced_to: str = "",
) -> str:
    """The record, as the target will hold it.

    ``when`` is unix time, passed in rather than read here — this module owns no
    clock, for the same reason every other decision module in this feature does
    not. Both the epoch and a readable UTC rendering are printed: the first is
    what survives comparison across hosts whose clocks differ, the second is what
    a human reads. The operator's ruling on skew was 「そんなシビアじゃない」 —
    plain unix time is enough — so nothing here tries to correct for it, and the
    raw number is shown so anyone who cares can see it.

    ``transcripts`` are ``(name, bytes, lines)`` AS CARRIED — the snapshot
    numbers, which is what the target actually holds — so a reader comparing the
    two hosts by hand knows what the target's file is supposed to weigh.
    """
    stamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(when))
    lines: list[str] = [
        f"# {agent} was relocated here",
        "",
        f"This agent moved from **{from_host}** to **{to_host}** on {stamp}",
        f"(unix {when:.0f}).",
        "",
        "## Where it came from",
        "",
        _rule(f"source host: `{from_host}`"),
        _rule(f"source path: `{source_dir}`"),
        _rule(f"this path:   `{target_dir}`"),
    ]
    if session:
        lines.append(_rule(f"session resumed: `{session}`"))
    lines += [
        "",
        "NOTHING WAS DELETED ON THE SOURCE. Everything listed below — carried or "
        "not — is still at the source path above, on that host, exactly as it was. "
        "If something is missing here, go and look there.",
        "",
        "## What was carried",
        "",
    ]
    if transcripts:
        lines.append("Transcripts (bytes / lines as carried):")
        lines.append("")
        for name, byte_count, line_count in transcripts:
            lines.append(_rule(f"`{name}` — {byte_count} bytes / {line_count} lines"))
        lines.append("")
        lines.append(
            "Each transcript was carried up to the byte offset of its LAST COMPLETE "
            "LINE at the moment the copy began, so a record that was still being "
            "written when the source stopped is the one thing that may be missing "
            "from the end."
        )
        lines.append("")
    if directories:
        lines.append("Directories, carried whole:")
        lines.append("")
        for name in directories:
            lines.append(_rule(f"`{name}/`"))
        lines.append("")
    if not transcripts and not directories:
        lines.append("Nothing. That is worth reading twice.")
        lines.append("")

    lines += ["## What was NOT carried", ""]
    if refused:
        lines.append(
            "These were seen in the source directory and deliberately left there:"
        )
        lines.append("")
        for name, reason in refused:
            lines.append(_rule(f"`{name}` — {reason}"))
    else:
        lines.append(
            "Nothing was refused: every entry in the source directory travelled."
        )
    lines.append("")

    if displaced_to:
        lines += [
            "## What was already here",
            "",
            "This host already held a directory at the path above. It was MOVED "
            "ASIDE, never overwritten and never deleted:",
            "",
            _rule(f"`{displaced_to}`"),
            "",
            "If that older copy holds memory or transcripts worth keeping, merging "
            "it is a deliberate act — read both and decide. The source's copy won "
            "here by default, which is the operator's rule "
            "(「ソースの方が普通大切だろう」), not a judgement about the contents.",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"
