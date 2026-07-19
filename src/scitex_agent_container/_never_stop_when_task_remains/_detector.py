"""Run the hook executable and classify the RESULT — never its contents.

OWNERSHIP BOUNDARY (agreed with scitex-cards)
---------------------------------------------
**scitex-cards ships the hook executable.** It emits the Claude Code
Stop-hook JSON itself — ``decision: "block"`` plus the ``reason`` that
becomes the agent's next instruction. It owns both ends of that contract.

**sac registers it and nothing else.** Settings materialisation, deployment
to every agent, the de-dupe algebra, the loop guard, fail-open.

So this module deliberately has **zero knowledge of scitex-cards' output
format**. It does not read ``items[]``, ``card_id``, ``next_action``, or
``idle_seconds``, and it does not parse numbered hint lines. An earlier
draft did all of that, which made cards' stdout an API they could not change
without breaking us — the exact coupling that was deleted in the other
direction when cards' bridge was killed for depending on sac. Removing their
dependency on us and then quietly building ours on them would have left the
same knot tied the other way round.

What this module DOES read is the **Claude Code hook protocol**, which is
owned by Claude Code and is the shared standard both sides already target:

* stdout carrying a hook-protocol object (it has ``decision``) → **passed
  through verbatim**.
* exit 0 → allow the stop.
* exit 2 **plus** a parseable verdict on stdout (it has ``runnable``) →
  block, with a reason composed from THAT PAYLOAD.
* exit 2 with nothing parseable on stdout → ``UNKNOWN``.
* anything else → ``UNKNOWN``.

A RESULT IS ONLY ADMISSIBLE IF WE CAN PARSE IT
----------------------------------------------
An exit code on its own is not an answer. Exit 2 is both "work remains" and
the universal CLI usage-error code, so a host whose scitex-cards predates
``may-stop`` exits 2 with click's ``No such command`` on stderr. Treating
that as an affirmative block turned "I could not determine whether you may
stop" into "you may NOT stop" — UNKNOWN collapsed into the blocking pole,
the one direction this gate must never fail in — and then handed the usage
text back to the agent as its next instruction.

So: only a payload we can actually read counts as a verdict, and only the
detector's own words may become a ``reason``. Raw stderr never does.

THREE STATES, NEVER TWO
-----------------------
``allow`` / ``block`` / ``unknown``. "The check said nothing is runnable"
and "we could not tell" are different facts and must not collapse into the
same pole. Only ``unknown`` fails open.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass

#: Nothing runnable — stopping is allowed. Definite.
ALLOW = "allow"
#: Work remains — the stop must be converted. Definite.
BLOCK = "block"
#: We COULD NOT TELL (missing / timed out / crashed / unexpected rc).
#: Absence of evidence is not evidence of absence. Fails open, but loudly.
UNKNOWN = "unknown"

#: The registered hook executable. Overridable via ``$SAC_MAY_STOP_CMD``:
#: an operator escape hatch AND the seam that lets a test point at a real
#: script. Defaults to what exists TODAY; when scitex-cards ships its own
#: hook executable this default moves to that command and nothing else here
#: changes, because we never depended on the shape of its output.
_DEFAULT_CMD = "scitex-cards may-stop"
_CMD_ENV = "SAC_MAY_STOP_CMD"

#: THE COMPATIBILITY FLOOR — the scitex-cards version that first provides
#: :data:`_DEFAULT_CMD`'s verb. Declared here so the skew is STATED rather
#: than discovered at runtime by an agent that cannot stop.
#:
#: Treat this as a signpost, not an oracle, and prefer the OBSERVED version
#: that :func:`_provenance` reports. It is contested: scitex-cards report the
#: verb shipped in 0.17.0, yet a clean non-editable install reporting 0.16.2
#: answers `may-stop` correctly — so at least one version STRING in this
#: ecosystem does not match the code behind it. That is exactly why the
#: failure log below prints what the binary ACTUALLY reports instead of
#: trusting this constant.
MIN_CARDS_VERSION = "0.17.0"

#: Seconds before we give up on the executable and fail open.
_TIMEOUT_S = 15.0

#: The key that marks stdout as a CLAUDE CODE hook-protocol payload.
_DECISION_KEY = "decision"

#: The key that marks stdout as the detector's own verdict — THE SHAPE WE
#: EXPECT. ``may-stop`` answers with one JSON line carrying ``runnable`` and,
#: when work remains, ``items[]``.
_RUNNABLE_KEY = "runnable"

#: Bounds on the composed reason, so a runaway board cannot produce a
#: megabyte-long instruction.
_MAX_REASON_ITEMS = 20
_MAX_ITEM_CHARS = 200


@dataclass(frozen=True)
class Verdict:
    """The classified result.

    ``payload`` is the hook JSON to emit verbatim when the executable
    produced one; ``reason`` is the opaque fallback text. ``detail`` carries
    the loud-log explanation for :data:`UNKNOWN`.
    """

    state: str
    payload: "dict | None" = None
    reason: str = ""
    detail: str = ""
    returncode: "int | None" = None

    def block_signature_source(self) -> str:
        """The opaque text the loop guard digests to detect "no progress".

        Deliberately the whole rendered block, not extracted card ids —
        extracting ids would mean knowing their format. Any change in what
        the executable says counts as progress.

        CAVEAT worth stating plainly: if the executable's ``reason`` embeds a
        value that moves every turn (an idle-seconds counter, a timestamp),
        this signature changes every turn and the loop guard will never trip.
        That is a property of the emitted text, so it is a contract note for
        whoever owns the executable — sac cannot fix it without parsing, and
        parsing is what this boundary exists to prevent.
        """
        if self.payload is not None:
            return json.dumps(self.payload, sort_keys=True)
        return self.reason


def detector_argv(agent: str) -> list[str]:
    """Build the command line, always naming the agent.

    The executable must be TOLD who to answer for; letting it infer identity
    is how a hook ends up reporting on the wrong agent's board.
    """
    base = (os.environ.get(_CMD_ENV) or "").strip() or _DEFAULT_CMD
    return [*shlex.split(base), "--agent", agent]


def _hook_json(text: str) -> "dict | None":
    """Return the hook-protocol JSON object on stdout, if there is one.

    Scans lines rather than assuming stdout is exactly one object, so a
    stray warning line does not cost us a valid decision. This reads the
    CLAUDE CODE protocol — not scitex-cards' schema — and forwards the
    object without inspecting its domain fields.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:  # stx-allow: fallback (reason: a noisy stdout line is not the payload; keep scanning)
            continue
        if isinstance(data, dict):
            return data
    return None


@dataclass
class _Run:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    failure: str = ""


def _invoke(argv: list[str]) -> _Run:
    """Run the executable, turning every failure mode into a ``_Run``."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=_TIMEOUT_S, check=False
        )
    except FileNotFoundError:
        return _Run(
            returncode=-1,
            failure=f"hook executable not found: {argv[0]!r} is not on PATH",
        )
    except subprocess.TimeoutExpired:
        return _Run(
            returncode=-1,
            failure=f"hook executable timed out after {_TIMEOUT_S:.0f}s",
        )
    except OSError as exc:  # stx-allow: fallback (reason: spawn failure must fail open, not crash the agent's turn)
        return _Run(returncode=-1, failure=f"hook executable could not be run: {exc}")
    return _Run(
        returncode=proc.returncode, stdout=proc.stdout or "", stderr=proc.stderr or ""
    )


#: Bound on the version probe. It runs only on a FAILURE path, never on the
#: hot path.
#:
#: MEASURED, not guessed: ``scitex-cards --version`` takes 3.3-4.1s cold on a
#: loaded host, because the CLI does import-time work before click's eager
#: ``--version`` short-circuits. An earlier 5s budget looked generous and
#: silently timed out under load, reporting ``version: unknown`` for a binary
#: that answers perfectly well — a diagnostic that lies about the one fact it
#: exists to establish is worse than no diagnostic at all.
_VERSION_TIMEOUT_S = 10.0

#: A version-shaped token, e.g. the ``0.16.2`` in "scitex-cards, version
#: 0.16.2". We extract ONLY this rather than quoting the banner, so a CLI that
#: answers ``--version`` with a usage error cannot smuggle its prose into a
#: message that reaches the agent.
_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)?[0-9A-Za-z.+-]*")


def _reported_version(exe: str) -> str:
    """What ``exe`` claims its version is, or ``"unknown"``. Never raises."""
    try:
        proc = subprocess.run(
            [exe, "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_S,
            check=False,
        )
    except (
        OSError,
        subprocess.SubprocessError,
    ):  # stx-allow: fallback (reason: a diagnostic must never become a second failure)
        return "unknown"
    match = _VERSION_RE.search(f"{proc.stdout or ''}\n{proc.stderr or ''}")
    return match.group(0) if match else "unknown"


def _provenance(argv: list[str]) -> str:
    """Name WHICH BINARY answered, where it lives, and what version it claims.

    ``Error: No such command 'may-stop'`` says a verb is missing but NOT WHICH
    SIDE is stale — the caller naming a verb that never existed, or a callee
    too old to have it. Three agents investigated that one string on the night
    this was written, and one concluded the caller was wrong and filed a card
    proposing we change our default command; the truth was the opposite. The
    error names the verb but never the version that lacks it.

    So whenever we cannot get an answer, we say exactly who we asked. That
    turns a multi-agent investigation into one line of log.
    """
    exe = argv[0] if argv else ""
    resolved = shutil.which(exe) if exe else None
    return (
        f"asked: {shlex.join(argv)}; resolved: {resolved or 'NOT FOUND on PATH'}; "
        f"reports version: {_reported_version(exe) if exe else 'unknown'}; "
        f"this command needs scitex-cards >= {MIN_CARDS_VERSION}"
    )


#: Why an exit 2 carrying no parseable verdict is UNKNOWN. Names the
#: overwhelmingly likely cause, because it is the fleet's STEADY STATE rather
#: than an edge case: a host whose scitex-cards predates the verb answers with
#: click's usage error — which also exits 2.
#:
#: The child's own output is deliberately NOT quoted here. This text reaches
#: the agent and the operator, and CLI error text read back as an instruction
#: is precisely the defect this guard exists to prevent. The provenance line
#: appended by :func:`_provenance` is more actionable than the bytes anyway:
#: it names WHICH binary answered and WHAT version it claims to be.
_UNREADABLE_EXIT_TWO = (
    "hook executable exited 2 but printed no verdict we could parse on "
    "stdout, so we could not tell whether work remains. The usual cause is "
    "that this host's scitex-cards predates the `may-stop` verb, whose usage "
    "error also exits 2."
)


def _compose_reason(payload: dict, agent: str) -> str:
    """Build the agent's next instruction from the DETECTOR-AUTHORED verdict.

    Sourced strictly from the parsed stdout payload — never from stderr. The
    ``reason`` is handed back to Claude as its next instruction, so anything
    that reaches it must be something the detector deliberately said. stderr
    is an unstructured channel that carries deprecation notices, library
    warnings, and (the bug this replaced) CLI usage errors; forwarding it
    verbatim is how "No such command" became an agent's marching orders.

    Deliberately omits volatile fields such as ``idle_seconds``: the loop
    guard digests this text to detect "no progress", so a value that moves
    every turn would mean the guard could never trip.
    """
    lines: list[str] = []
    items = payload.get("items")
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            parts = [
                str(item[key]).strip()
                for key in ("card_id", "reason", "next_action")
                if isinstance(item.get(key), (str, int, float))
                and str(item[key]).strip()
            ]
            if parts:
                lines.append(" — ".join(parts)[:_MAX_ITEM_CHARS])
            if len(lines) >= _MAX_REASON_ITEMS:
                break

    who = agent or "this agent"
    if not lines:
        return (
            f"The runnable-work check reports that work remains on {who}'s "
            "board, but listed no items. Do not stop — inspect your board and "
            "take the next item."
        )
    numbered = "\n".join(f"{n}. {text}" for n, text in enumerate(lines, 1))
    return (
        f"{len(lines)} runnable item(s) on {who}'s board — an agent does not "
        f"stop while the board holds work:\n{numbered}"
    )


def probe(agent: str) -> Verdict:
    """Run the hook executable for ``agent`` and classify. Never raises.

    EXIT 2 IS A BORROWED SIGNAL — GATE IT ON THE PAYLOAD
    ----------------------------------------------------
    The detector signals "work remains" with exit 2, which is also the
    UNIVERSAL usage-error code (click, argparse, most CLIs). So any missing,
    renamed, or not-yet-shipped verb IMPERSONATES A POSITIVE RESULT: the
    process exits 2 and we would read a failure to answer as an affirmative
    "you may not stop". We therefore keep accepting 2 for compatibility but
    require a parseable verdict on stdout alongside it.

    If the protocol ever gains a distinct code, a value outside the
    conventional range (e.g. 10) would remove the ambiguity at the source and
    is the recommended direction — but the exit codes are scitex-cards' to
    choose, so this side does not change them unilaterally.

    THE GENERAL RULE: a hook that calls a NEW verb needs a guarded fallback,
    because THE FLEET ALWAYS RUNS OLDER THAN THE PUBLISHED VERSION. ``may-stop``
    was published and correct; the deployed SIF simply predated it, which made
    the missing-verb path the steady state rather than an edge case. The verb
    is not the lesson — the skew is, and it recurs on every rename and every
    new verb. Note the shape of the trap: the caller was RIGHT, and the error
    text still read as if it were wrong, which is why :func:`_provenance`
    reports who was actually asked.
    """
    if not agent:
        return Verdict(
            state=UNKNOWN,
            detail=(
                "could not resolve this agent's identity from the environment "
                "(no SCITEX_CARDS_AGENT_ID / SCITEX_TODO_AGENT_ID / SAC_NAME); "
                "refusing to guess it from the working directory"
            ),
        )

    argv = detector_argv(agent)
    run = _invoke(argv)

    if run.failure:
        return Verdict(
            state=UNKNOWN,
            detail=f"{run.failure}. {_provenance(argv)}",
            returncode=run.returncode,
        )

    payload = _hook_json(run.stdout)

    # (1) The executable spoke the CLAUDE CODE hook protocol outright. Forward
    # it untouched — the decision is theirs to make and ours to deliver.
    if payload is not None and _DECISION_KEY in payload:
        state = BLOCK if payload.get(_DECISION_KEY) == "block" else ALLOW
        return Verdict(state=state, payload=payload, returncode=run.returncode)

    # (2) exit 0 — a definite "nothing runnable".
    if run.returncode == 0:
        return Verdict(state=ALLOW, returncode=0)

    # (3) exit 2 — "work remains", but ONLY when a verdict we can PARSE came
    # with it. An exit code alone is not an answer: click exits 2 for a usage
    # error too, so on any host whose scitex-cards predates `may-stop`,
    # "work remains" and "No such command" are literally the same number.
    if run.returncode == 2:
        if payload is None or _RUNNABLE_KEY not in payload:
            return Verdict(
                state=UNKNOWN,
                detail=f"{_UNREADABLE_EXIT_TWO} {_provenance(argv)}",
                returncode=2,
            )
        if not payload.get(_RUNNABLE_KEY):
            # They answered, and the answer was "nothing runnable".
            return Verdict(state=ALLOW, returncode=2)
        return Verdict(
            state=BLOCK,
            payload={"decision": "block", "reason": _compose_reason(payload, agent)},
            returncode=2,
        )

    # (4) Any other exit code is UNKNOWN by contract. Their output is not
    # quoted here either, for the reason given at _UNREADABLE_EXIT_TWO.
    return Verdict(
        state=UNKNOWN,
        detail=(
            f"hook executable exited {run.returncode} (expected 0 or 2) and "
            f"printed no verdict we could parse. {_provenance(argv)}"
        ),
        returncode=run.returncode,
    )


__all__ = ["ALLOW", "BLOCK", "UNKNOWN", "Verdict", "detector_argv", "probe"]
