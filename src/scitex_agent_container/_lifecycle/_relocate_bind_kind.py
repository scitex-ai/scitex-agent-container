"""A missing path is not one problem — it is three, and they need opposite fixes.

``binds_exist_on_target`` used to answer with a list of paths and one sentence:
"remove or re-point these binds in the spec". That sentence is right for exactly
one of the shapes a missing bind actually takes, and misleading for the others.

Measured across the fleet's specs on 2026-08-11: fifteen agents bind paths that
do not exist on scitex-compute-04, and every one of those specs is CORRECT for
the machine it currently pins. Nine are Spartan agents binding
``/data/gpfs/projects/punim0264/...`` — shared cluster storage that has no
counterpart on a workstation and cannot be moved to one. Six are laptop agents
binding a dataset under the agent's own project root and a local ``scitex-clew``
checkout — data that exists on exactly one machine because that machine made it.

Those look identical when printed as "path not found", and the operator's action
differs completely:

    host infra    provision or mount it on the target. It may be immovable, in
                  which case the answer is a different target, not a fix.
    agent-local   it exists only where the agent has been living. MOVE IT WITH
                  THE AGENT (a relocation carries the spec and the transcript and
                  nothing else), or re-point the bind, or — if it is a checkout —
                  clone it there.
    credential    NEVER copy it. Provision the target's own, or bind the account
                  file that host already holds.

The credential case is the one worth the separate kind. "This path is missing;
copy it with the agent" is a correct-sounding instruction that, applied to
``~/.ssh`` or an ``accounts/*/.credentials.json``, means copying key material
between machines. A hint that suggests it is worse than a hint that says nothing.

PURE AND PATH-SHAPE ONLY. No stat, no git, no network — classification runs
against paths that by definition do NOT exist on the target, and half the time
do not exist on the machine doing the classifying either. Where the shape cannot
settle it, the answer is :data:`KIND_UNCLASSIFIED`, which states both
possibilities rather than picking the likelier one. A confident wrong category
sends the operator to provision a directory that should have travelled.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "ACTION_CARRY",
    "ACTION_DECIDE",
    "ACTION_PROVISION",
    "KIND_AGENT_LOCAL",
    "KIND_CREDENTIAL",
    "KIND_HOST_INFRA",
    "KIND_UNCLASSIFIED",
    "BindPath",
    "classify_bind",
    "classify_binds",
    "group_by_action",
]

#: What the operator has to DO. Ordered as the plan orders them: things the
#: target needs first, things that must travel second.
ACTION_PROVISION: Final = "provision on the target"
ACTION_CARRY: Final = "must travel with the agent"
ACTION_DECIDE: Final = "decide: provision or carry"

KIND_CREDENTIAL: Final = "credential"
KIND_AGENT_LOCAL: Final = "agent-local"
KIND_HOST_INFRA: Final = "host-infra"
KIND_UNCLASSIFIED: Final = "unclassified"

#: Path components that mean "this is key material". Matched as whole components
#: (or the file's own name), never as substrings: a directory called
#: ``ssh-notes`` is not ``.ssh``, and a project named ``accounts-service`` is not
#: an account store.
_CREDENTIAL_COMPONENTS: Final = frozenset(
    {
        ".ssh",
        ".gnupg",
        ".aws",
        ".azure",
        ".docker",
        "accounts",
        "gh",
        "secrets",
    }
)
_CREDENTIAL_NAMES: Final = frozenset(
    {
        ".credentials.json",
        "credentials.json",
        ".pgpass",
        ".netrc",
        ".env",
        ".envrc",
        "id_rsa",
        "id_ed25519",
    }
)

#: First components that mean a mounted filesystem the HOST provides. These are
#: the roots that exist because a machine was set up that way, not because an
#: agent wrote something.
_INFRA_ROOTS: Final = frozenset(
    {
        "data",
        "gpfs",
        "mnt",
        "media",
        "scratch",
        "nfs",
        "net",
        "srv",
        "share",
        "shared",
        "opt",
        "usr",
        "var",
        "etc",
        "proc",
        "sys",
        "dev",
        "run",
    }
)

#: Components that mean "the agent's own material" wherever they appear. A
#: ``dataset`` directory under ``.scitex`` is the 2026-08-11 laptop case
#: verbatim, and it can sit under a home OR under cluster storage.
_AGENT_DATA_COMPONENTS: Final = frozenset({"dataset", "datasets"})


@dataclass(frozen=True)
class BindPath:
    """One missing bind source, with what it IS and what to do about it."""

    path: str
    kind: str
    action: str
    #: Why it was put in this category — the evidence, so a wrong call is
    #: arguable rather than mysterious.
    because: str
    #: The concrete thing to do, naming the path and the host.
    fix: str


def _parts(path: str) -> tuple[str, ...]:
    return tuple(p for p in path.strip().split("/") if p and p != ".")


def _under(path: str, root: str) -> bool:
    """True when ``path`` IS ``root`` or lies beneath it, component-wise.

    Compared as components rather than with ``startswith``, which would call
    ``/home/ywatanabe/proj-old`` a child of ``/home/ywatanabe/proj``.
    """
    if not root:
        return False
    p, r = _parts(path), _parts(root)
    return len(p) >= len(r) and p[: len(r)] == r


def classify_bind(path: str, *, workdir: str = "", from_host: str = "") -> BindPath:
    """Decide what a missing bind source is, from its shape alone.

    ``workdir`` is the agent's own working directory as the spec declares it. It
    is what separates "the agent's material" from "the machine's": anything at or
    beneath the workdir was made by this agent, and anything beside it — same
    parent — belongs to the same person's project tree on the same machine, which
    is equally absent on a target that has never hosted them.

    The order of the rules is load-bearing. Credentials are decided FIRST, so an
    ``accounts/`` directory under the agent's own home is never labelled
    agent-local and never attracts "move it with the agent".
    """
    where = from_host or "the source"
    parts = _parts(path)
    name = parts[-1] if parts else ""

    if name in _CREDENTIAL_NAMES or (set(parts) & _CREDENTIAL_COMPONENTS):
        return BindPath(
            path=path,
            kind=KIND_CREDENTIAL,
            action=ACTION_PROVISION,
            because="the path names key material (an ssh/gh/account/secret location)",
            fix=(
                f"provision the TARGET's own copy of {path} there — do NOT copy it "
                f"from {where}. Credential material must not travel between hosts; "
                "if the target already holds an equivalent account file, re-point "
                "the bind at it instead"
            ),
        )

    if set(parts) & _AGENT_DATA_COMPONENTS:
        return BindPath(
            path=path,
            kind=KIND_AGENT_LOCAL,
            action=ACTION_CARRY,
            because="the path holds a dataset directory, which exists where it was made",
            fix=(
                f"move {path} to the target with the agent, or re-point the bind at "
                "a copy that already exists there. A relocation carries the spec and "
                "the transcript and nothing else, so this data does not follow by itself"
            ),
        )

    if workdir and (_under(path, workdir) or _beside(path, workdir)):
        rel = "under" if _under(path, workdir) else "beside"
        return BindPath(
            path=path,
            kind=KIND_AGENT_LOCAL,
            action=ACTION_CARRY,
            because=f"the path sits {rel} the agent's own workdir ({workdir})",
            fix=(
                f"move {path} to the target with the agent, or — if it is a git "
                f"checkout — clone it there. It exists on {where} because the agent "
                "works there; nothing on the target creates it"
            ),
        )

    if parts and parts[0] in _INFRA_ROOTS:
        return BindPath(
            path=path,
            kind=KIND_HOST_INFRA,
            action=ACTION_PROVISION,
            because=f"the path is under /{parts[0]}, a filesystem the host provides",
            fix=(
                f"provision or mount {path} on the target. If that filesystem does "
                "not exist there at all (shared cluster storage usually does not), "
                "this spec belongs on a host that has it — re-pointing the bind is a "
                "different agent, not a relocated one"
            ),
        )

    return BindPath(
        path=path,
        kind=KIND_UNCLASSIFIED,
        action=ACTION_DECIDE,
        because="the path's shape does not say whether it is host infrastructure or the agent's own material",
        fix=(
            f"look at {path} on {where} and decide: if the host provides it, "
            "provision it on the target; if the agent made it, move it with the "
            "agent or re-point the bind. This preflight will not guess between the two"
        ),
    )


def _beside(path: str, workdir: str) -> bool:
    """True when ``path`` shares the workdir's parent — the same project tree.

    A sibling of the workdir is not host infrastructure by construction: it is
    one directory in the same person's project root on the same machine. That is
    exactly the Spartan shape (``.../ywatanabe/scitex-clew/src`` next to
    ``.../ywatanabe/paper-scitex-clew``), and treating it as a mount to provision
    would send the operator to the wrong place.
    """
    parent = "/".join(_parts(workdir)[:-1])
    if not parent:
        return False
    return _under(path, "/" + parent) and not _under(path, workdir)


def classify_binds(
    paths: tuple[str, ...] | list[str], *, workdir: str = "", from_host: str = ""
) -> tuple[BindPath, ...]:
    """Classify every missing path, in the order given."""
    return tuple(
        classify_bind(p, workdir=workdir, from_host=from_host) for p in paths
    )


def group_by_action(classified: tuple[BindPath, ...]) -> tuple[tuple[str, tuple[BindPath, ...]], ...]:
    """Bucket by action, in the order the operator works: provision, carry, decide.

    Returned as pairs rather than a dict so the ORDER is part of the value. The
    operator asked for failures ordered by what he has to do about them, and a
    dict hands that ordering to whatever iterates it next.
    """
    order = (ACTION_PROVISION, ACTION_CARRY, ACTION_DECIDE)
    out = []
    for action in order:
        members = tuple(b for b in classified if b.action == action)
        if members:
            out.append((action, members))
    return tuple(out)
