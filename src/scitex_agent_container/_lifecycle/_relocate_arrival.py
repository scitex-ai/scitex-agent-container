"""The first thing a relocated agent is told — and the same message that proves it woke up.

The operator's requirement, 2026-08-11:「tgt agent には『あなたは <src> から <tgt>
に移動してきました；記憶はここで resume の id はこれです』みたいな説明もあった方が
良さそうです。というか、ハンドシェイクが必要なはずで」

An agent that resumes a carried transcript has a real problem no amount of
transcript fixes: its conversation ends on the source host, mid-work, and nothing
in it mentions that the machine underneath changed. Left unsaid, it will keep
referring to paths, ports and peers that were true where the conversation
happened and are not true where it is now — and it will do so confidently,
because from the inside nothing looks different. So the move is stated
explicitly, once, in the first turn after the resume.

THE BRIEF IS THE HANDSHAKE CHALLENGE, NOT A SECOND MESSAGE. It carries the
nonce and the proof-of-work question that :mod:`_relocate_handshake` evaluates,
so the agent's answer to "where are you and what do you see" IS the B->A leg
that gate requires. Two messages would have meant the explanation could be
delivered to an agent that never proved it could reply — which is the
"started, reported healthy, did nothing" shape with a friendly note attached.

WHY IT IS NOT DELIVERED VIA ``startup_prompts``. That is how a TWIN gets its
boot kick (:func:`._twin.build_twin_boot_kick`), and it works there because a
twin is a NEW agent whose spec is being written anyway. A relocation writes
NOTHING to a spec file — where an agent runs is an observation, and the spec is
the human-authored intent (operator, 2026-08-11:「設定ファイル、人が書くものは
ファイル、状態は db」). Editing ``startup_prompts`` to deliver this would put a
one-time operational message into a git-tracked document that exists in one copy
per machine, and it would still be there on the next boot, telling the agent it
just relocated months after it did.

WHAT THE PROOF-OF-WORK QUESTION IS FOR. An echo proves the message path. The
question must be something the agent can only answer by looking at the machine it
is now on, because the failure being ruled out is an agent whose loop runs and
whose tools do not. The caller supplies both the question and the answer it
computed independently; this module refuses to build a brief without them rather
than letting the gate be weakened by an omission.

Pure: text in, text out. No transport, no clock, no nonce generation.
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_HANDOVER_DOC",
    "build_arrival_brief",
]

#: The relocation handover document — the operator's own notes for this move,
#: pointed AT rather than inlined. The brief has to stay short enough to be read
#: in full by an agent resuming mid-task; a pointer that is followed when needed
#: beats a wall of context that is skimmed every time.
DEFAULT_HANDOVER_DOC = "~/.scitex/agent-container/runtime/relocation-plan-20260811.md"


def build_arrival_brief(
    *,
    agent: str,
    from_host: str,
    to_host: str,
    resume_session_id: str,
    nonce: str,
    question: str,
    handover_doc: str = DEFAULT_HANDOVER_DOC,
) -> str:
    """The message the relocated agent receives on the target, as its first new turn.

    ``resume_session_id`` is the uuid whose transcript was carried and seeded —
    the same id the target's ``session_id`` marker now holds. It is stated to the
    agent because it is the one handle that lets it verify its own continuity:
    an agent told "your memory was carried" and given no id cannot check the
    claim, and this is the exact claim that was false on 2026-08-07.

    ``nonce`` and ``question`` make the reply verifiable — see
    :func:`._relocate_handshake.evaluate_handshake`, which reads both back out of
    the answer. Both are required, and an empty one raises rather than silently
    producing a brief whose reply proves nothing.
    """
    if not agent:
        raise ValueError("build_arrival_brief needs the agent's name")
    if not from_host or not to_host:
        raise ValueError("build_arrival_brief needs both the source and target host")
    if from_host == to_host:
        raise ValueError(
            f"build_arrival_brief: {from_host!r} to itself is not a relocation; a brief "
            "announcing a move that did not happen would be a false statement to the agent"
        )
    if not resume_session_id:
        raise ValueError(
            "build_arrival_brief needs the resumed session id — an agent told its memory "
            "was carried, with no id to check that against, cannot verify the one claim "
            "that has silently been false before"
        )
    if not nonce:
        raise ValueError(
            "build_arrival_brief needs a nonce — without correlation, a reply left over "
            "from an earlier turn satisfies 'the agent answered'"
        )
    if not question:
        raise ValueError(
            "build_arrival_brief needs a proof-of-work question — an echo proves the "
            "message path, not the agent, and this message is the gate for the agent"
        )

    lines = [
        f"You are {agent}, and you have RELOCATED from {from_host} to {to_host}.",
        "",
        "You are the same agent — same identity, same cards, same work in flight. "
        "Only the machine underneath you changed.",
        "",
        "YOUR MEMORY WAS CARRIED.",
        f"  - This session resumes transcript {resume_session_id}, copied from "
        f"{from_host} and verified on {to_host} by byte and line count before you "
        "were started.",
        "  - So the conversation above this message happened on "
        f"{from_host}. Anything it says about paths, ports, mounts, peers or "
        f"running processes describes {from_host} and may not hold here. Re-check "
        "before you rely on it; do not assume a path that existed there exists here.",
        f"  - {from_host} has been stopped. You are the only instance of {agent}.",
        "",
        "WHERE YOUR STATE IS:",
        f"  - Handover document: {handover_doc}",
        "  - The scitex-todo card board is authoritative for what is in flight — "
        "read your card slice before resuming work, not the transcript's memory of it.",
        "",
        "REPLY NOW, in this exact shape — this reply is the handshake that "
        "completes the relocation, and until it is observed the move does not "
        "proceed:",
        f"  nonce={nonce}",
        f"  answer=<your answer to: {question}>",
        "",
        "Answer it by actually looking on this host. An echoed or guessed answer "
        "fails the check, and it fails it in the useful direction: it is how we "
        "catch an agent whose loop runs but whose tools cannot reach anything.",
    ]
    return "\n".join(lines)
