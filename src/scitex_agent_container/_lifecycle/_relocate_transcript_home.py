"""A containerised agent's ``$HOME`` is not the ssh user's ``$HOME``, and the transcript follows the container's.

:mod:`_relocate_transport_paths` derives ``<home>/.claude/projects/<encoded>``
from an OBSERVED ``$HOME``. Ask the target host for ``$HOME`` over ssh and it
answers with the ssh user's — ``/home/ywatanabe`` on this fleet. Ask the AGENT
and the answer is ``/home/agent``, inside a SIF, which is a path that does not
exist on the host at all. Neither is where the bytes have to land.

WHAT THE BYTES ACTUALLY NEED. The agent writes its transcript to
``$HOME/.claude/projects/`` INSIDE the container. Whether that write reaches a
durable host path, and which one, is decided entirely by the spec: a bind
mapping some host directory onto the container's home (or onto a subpath of it),
or, with no such bind, the apptainer OVERLAY's upper layer. So the host-side
answer is a property of the SPEC, not of the host, and it is derived here rather
than probed — a probe would have to guess which of the two mechanisms is in play.

MEASURED ON THIS FLEET, 2026-08-09, and the reason this module exists: an agent's
transcript landed in ``overlays/<agent>/upper/home/agent/.claude/projects/`` and
NOT in ``runtime/<agent>/home/``, which is the tree that looks like it should
hold it and only ever receives the boot seed. A relocation that carried the
runtime tree would have carried the agent's config and left its conversation
behind — present, intact, and on the wrong machine. The canary that found this
now carries an explicit leaf bind for exactly that reason, and both shapes are
handled here because both are live in the fleet today.

BIND ORDER IS PRECEDENCE, AND THE LAST ONE WINS. Apptainer applies binds in
order, so a later bind over the same container path shadows an earlier one; the
scan therefore walks the list and keeps the LAST match rather than the first.
Getting this backwards would name a host directory that the running agent does
not in fact write to, which is the same invisible-success failure by another
route.

A SUBPATH BIND IS ACCEPTED AND CONVERTED, NOT REJECTED. Binding
``…/home/.claude/projects`` onto ``/home/agent/.claude/projects`` is the shape
that demonstrably works (a bind OVER ``/home/agent`` itself loses to apptainer's
own ``--home`` flag — measured the same day, writes kept going to the overlay).
The container home it implies is the host path with ``/.claude/projects``
removed, and that is what is returned, so
:func:`.._relocate_transport_paths.derive_target_dir` keeps its single
``<home>/.claude/projects/<encoded>`` rule instead of growing a special case.

UNRESOLVABLE IS ``None``. There is no default and no "probably the overlay":
a wrong answer here writes a real transcript to a real directory that nothing
ever reads.

Pure: a parsed spec in, a path out. No filesystem, no ssh.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "CODE_FROM_BIND",
    "CODE_FROM_OVERLAY",
    "CODE_UNKNOWN",
    "CODE_UNCONTAINED",
    "TranscriptHome",
    "transcript_home_from_spec",
]

#: A bind maps a host directory onto the container's home (or a subpath of it).
CODE_FROM_BIND: Final = 200
#: No such bind; the writes land in the apptainer overlay's upper layer.
CODE_FROM_OVERLAY: Final = 201
#: The agent does not run in a container, so the host's own $HOME is the answer.
CODE_UNCONTAINED: Final = 202
#: Neither mechanism could be established. NOT a guess.
CODE_UNKNOWN: Final = 503

#: The container-side home every sac agent runs under (``--home /home/agent``).
CONTAINER_HOME: Final = "/home/agent"

#: Container paths that imply the home, longest first so the most specific bind
#: is recognised before the coarser one that is a prefix of it.
_HOME_SUFFIXES: Final[tuple[tuple[str, str], ...]] = (
    (f"{CONTAINER_HOME}/.claude/projects", "/.claude/projects"),
    (f"{CONTAINER_HOME}/.claude", "/.claude"),
    (CONTAINER_HOME, ""),
)


@dataclass(frozen=True)
class TranscriptHome:
    """The HOST-side directory that backs the agent's container ``$HOME``.

    ``path`` is ``None`` when it could not be established, and there is no
    ``__bool__`` — the rest of the relocate machinery refuses to let an
    undetermined value read as an answer, and this one decides where a
    conversation is written.
    """

    path: str | None
    code: int
    reason: str
    hint: str = ""

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("TranscriptHome.reason must be non-empty")
        if self.path is None and self.code != CODE_UNKNOWN:
            raise ValueError(
                f"TranscriptHome: an unresolved home must carry CODE_UNKNOWN, got {self.code}"
            )
        if self.path is None and not self.hint:
            raise ValueError(
                "TranscriptHome: an unresolved home must say what to measure next"
            )


def _body(spec: dict) -> dict:
    inner = spec.get("spec")
    return inner if isinstance(inner, dict) else spec


def _binds(spec: dict) -> list[str]:
    apptainer = _body(spec).get("apptainer")
    if not isinstance(apptainer, dict):
        return []
    binds = apptainer.get("binds")
    return [b for b in binds if isinstance(b, str)] if isinstance(binds, list) else []


def _overlay_dir(spec: dict) -> str:
    """The ``--overlay <dir>`` from ``raw_args``, or ``""``.

    Read out of ``raw_args`` rather than the ``apptainer.overlay`` field because
    that is where every live spec actually carries it — the typed field is empty
    on the specs in this fleet, and reading the empty one would report "no
    overlay" for an agent that has one.
    """
    apptainer = _body(spec).get("apptainer")
    if not isinstance(apptainer, dict):
        return ""
    typed = apptainer.get("overlay")
    if isinstance(typed, str) and typed.strip():
        return typed.strip().rstrip("/")
    raw = apptainer.get("raw_args")
    if not isinstance(raw, list):
        return ""
    args = [a for a in raw if isinstance(a, str)]
    for index, arg in enumerate(args):
        if arg == "--overlay" and index + 1 < len(args):
            return args[index + 1].strip().rstrip("/")
    return ""


def transcript_home_from_spec(
    spec: dict, *, ssh_home: str | None = None
) -> TranscriptHome:
    """Where this agent's ``~/.claude`` lands on the HOST, per its spec.

    ``ssh_home`` is the host's own ``$HOME`` as OBSERVED (never assumed), used
    only for an agent that runs no container at all. Passing it for a
    containerised agent changes nothing: the bind and overlay branches are
    checked first, because for those the ssh user's home is simply the wrong
    answer rather than a fallback.
    """
    binds = _binds(spec)
    found: tuple[str, str] | None = None
    for entry in binds:
        parts = entry.split(":")
        if len(parts) < 2:
            continue
        host_side, container_side = parts[0].strip(), parts[1].strip()
        if not host_side or not container_side:
            continue
        container_side = container_side.rstrip("/")
        for candidate, suffix in _HOME_SUFFIXES:
            if container_side != candidate:
                continue
            host_side = host_side.rstrip("/")
            if suffix and not host_side.endswith(suffix):
                # The bind's two halves disagree about their own shape. Naming
                # it is worth more than silently trimming something else off.
                return TranscriptHome(
                    path=None,
                    code=CODE_UNKNOWN,
                    reason=(
                        f"the bind {entry!r} maps onto {container_side}, so its host side "
                        f"should end in {suffix!r} and does not"
                    ),
                    hint=(
                        "fix the bind or say explicitly where the transcript lands; a "
                        "home guessed from a mismatched pair would name a directory the "
                        "agent never writes to"
                    ),
                )
            home = host_side[: -len(suffix)] if suffix else host_side
            found = (home.rstrip("/"), entry)
            break
    if found is not None:
        home, entry = found
        return TranscriptHome(
            path=home,
            code=CODE_FROM_BIND,
            reason=f"the spec binds {entry} — the container's home is {home} on the host",
        )

    overlay = _overlay_dir(spec)
    if overlay:
        home = f"{overlay}/upper{CONTAINER_HOME}"
        return TranscriptHome(
            path=home,
            code=CODE_FROM_OVERLAY,
            reason=(
                f"no bind covers the container's home, so writes land in the overlay's "
                f"upper layer at {home} (measured on this fleet 2026-08-09)"
            ),
        )

    runtime = str(_body(spec).get("runtime") or "").strip().lower()
    if runtime in ("", "none", "local") and not binds:
        if ssh_home:
            return TranscriptHome(
                path=ssh_home.rstrip("/"),
                code=CODE_UNCONTAINED,
                reason=f"no container in this spec; the host's own $HOME {ssh_home} is the home",
            )
        return TranscriptHome(
            path=None,
            code=CODE_UNKNOWN,
            reason="no container in this spec and the host's $HOME was not observed",
            hint="probe the host for $HOME; this host's is not evidence about that one's",
        )

    return TranscriptHome(
        path=None,
        code=CODE_UNKNOWN,
        reason=(
            "the spec has neither a bind covering the container's home nor an "
            "--overlay, so where the transcript is written durably is undetermined"
        ),
        hint=(
            "add the bind the agent actually uses, or state the host-side transcript "
            "root explicitly. Copying to a guessed directory produces an agent that "
            "starts, reports healthy, and has no memory — the 2026-08-07 failure"
        ),
    )
