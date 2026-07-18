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

* stdout that parses as a JSON object → **passed through verbatim**.
* exit 0 with nothing usable → allow the stop.
* exit 2 with nothing usable → block, with the executable's **raw stderr**
  as an opaque reason string. We do not interpret that text, only forward
  it. (This is the transitional path: today only ``scitex-cards may-stop``
  exists, which signals via exit codes rather than emitting hook JSON.)
* anything else → ``UNKNOWN``.

THREE STATES, NEVER TWO
-----------------------
``allow`` / ``block`` / ``unknown``. "The check said nothing is runnable"
and "we could not tell" are different facts and must not collapse into the
same pole. Only ``unknown`` fails open.
"""

from __future__ import annotations

import json
import os
import shlex
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

#: Seconds before we give up on the executable and fail open.
_TIMEOUT_S = 15.0


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


def probe(agent: str) -> Verdict:
    """Run the hook executable for ``agent`` and classify. Never raises."""
    if not agent:
        return Verdict(
            state=UNKNOWN,
            detail=(
                "could not resolve this agent's identity from the environment "
                "(no SCITEX_CARDS_AGENT_ID / SCITEX_TODO_AGENT_ID / SAC_NAME); "
                "refusing to guess it from the working directory"
            ),
        )

    run = _invoke(detector_argv(agent))

    if run.failure:
        return Verdict(state=UNKNOWN, detail=run.failure, returncode=run.returncode)

    payload = _hook_json(run.stdout)
    if payload is not None:
        # The executable spoke the hook protocol. Forward it untouched — the
        # decision is entirely theirs to make and ours to deliver.
        state = BLOCK if payload.get("decision") == "block" else ALLOW
        return Verdict(state=state, payload=payload, returncode=run.returncode)

    if run.returncode == 0:
        return Verdict(state=ALLOW, returncode=0)

    if run.returncode == 2:
        # Transitional exit-code signalling. stderr is forwarded as opaque
        # text — we never interpret it, so its format stays theirs.
        reason = (run.stderr or "").strip()
        return Verdict(
            state=BLOCK,
            reason=reason
            or (
                "The runnable-work check reported that work remains, but said "
                "nothing further. Do not stop — inspect your board and take "
                "the next item."
            ),
            returncode=2,
        )

    tail = (run.stderr or run.stdout or "").strip().splitlines()
    return Verdict(
        state=UNKNOWN,
        detail=(
            f"hook executable exited {run.returncode} (expected 0 or 2)"
            + (f": {tail[-1][:300]}" if tail else "")
        ),
        returncode=run.returncode,
    )


__all__ = ["ALLOW", "BLOCK", "UNKNOWN", "Verdict", "detector_argv", "probe"]
