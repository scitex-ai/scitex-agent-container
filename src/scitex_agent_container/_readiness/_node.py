"""Would an agent started on THIS host actually be able to do anything?

Every existing readiness surface answers INSTALLATION questions — is sac
present, is the listen daemon up, are there specs on disk. On 2026-08-23
scitex-compute-01 answered YES to all of them and was still incapable of
running a useful agent:

    sac installed        0.26.2, editable, current
    sac listen           alive since boot, uptime 1d21h
    host uptime          1d21h, no crash, no reboot
    agents DEFINED       121
    egress               github / astral / pypi all 200 in ~0.1s
    SIF images           4, dated that day
    agents RUNNING       0

An agent started there came up with NO MCP SERVERS AT ALL and no error. The
operator found out because a colleague stopped answering on Telegram and he
asked a human question: is it down, or is it dead?

THE CHECK THIS MODULE MAKES IS DELIBERATELY NOT "IS SAC INSTALLED".
It is: **how many declared MCP servers would an agent started here actually
get, and which declarations cannot be honoured?** That is the outcome an
operator cares about, and it is the only question that would have failed on
compute-01 while every installation check passed.

THREE FAILURES IT CATCHES, all measured on real hosts that night:

  1. NO to_home BASELINE.
     ``agents/_shared/to_home/`` is the tree copied into every agent's home;
     it supplies ``.mcp.json``. Absent on compute-01 AND on spartan — so this
     is not one mis-set-up machine, it is a setup step nobody owns. An agent
     starts, heartbeats, registers, and holds zero tools. Nothing errors.

  2. A DANGLING SYMLINK IN THE BASELINE.
     The baseline carries ``.claude/skills`` as a symlink. Copy it to a host
     whose target does not exist and every deploy dies with
     ``DanglingToHomeSymlinkError``. That error is excellent — it names the
     path, what it resolved to, and the remedies — but it fires at agent
     START, which is far too late and only for the person starting an agent.

  3. A DECLARATION THAT CANNOT BE HONOURED.
     A declared server whose command does not exist on this host. The
     measured instance pointed at a repo checkout that had never been cloned
     here; another pointed at ``/usr/bin/true``. Both READ as configured.

Failure 3 is the one worth the most care, because a false declaration is
worse than a missing feature. An agent read ``command: /usr/bin/true`` for
its Telegram server, correctly inferred "someone decided I do not get
Telegram", and TOLD THE OPERATOR SO. There had been no such decision — only
an unprovisioned host. A stub does not merely fail to work; it makes its
reader infer a decision nobody made, and that inference reaches humans.

So a declared-but-unservable channel is reported here as BROKEN, never as
absent, and never silently.

WHAT THE NUMBER IS, EXACTLY. It is the BASELINE tool count: what an agent with
no per-agent ``.mcp.json`` would get. In principle an agent shipping its own
config has more, so in principle this is a FLOOR rather than a total.

In practice, on compute-04 measured 2026-08-23, that principle describes a
case which does not occur:

    real agent dirs                    116
      with their own .mcp.json           5     each 23 bytes, {"mcpServers": {}}
      relying on the baseline only     111

Not one agent declares a single server of its own, so the baseline is the
floor AND the ceiling, and whatever it declares is exactly what every agent on
the host gets. That is why an absent baseline is not a degradation — it is
total, for every agent at once, silently.

The floor framing is kept anyway, because it is the honest general statement
and this measurement covers one host on one day. An instrument that quietly
over-claims is the failure this module exists to catch; building one here
would be a poor joke.

SCOPE, AND WHY IT IS THIS NARROW (operator ruling, 2026-08-23 07:41Z).
Five distinct gaps were found on real hosts that night and the first instinct
was to answer all five with one check. That instinct is the bug repeating at a
larger scale, so the boundary is drawn by OWNERSHIP OF THE ARTEFACT:

    IN  — the to_home baseline, .mcp.json, and the symlinks inside them.
          sac writes these, sac copies them into an agent home, and sac is
          what raises DanglingToHomeSymlinkError over them. sac can be wrong
          about them, so sac must be able to check them.

    OUT — reachability. Whether a host can ssh anywhere, whether a proxy
          binary exists in this container, whether a machine is on the VPN.
          That is scitex-net's, and it is not a near-miss: it is measured
          FROM A SEAT, and a seat-dependent answer folded in here would make
          this verdict mean different things on different machines.

    OUT — the node inventory. Which hosts should exist at all belongs to
          scitex-dev's registry. This module assesses the host it is given
          and never claims the set of hosts is complete.

IT MUST RUN ON THE HOST IT JUDGES, and that is not a deployment detail — it is
what the check MEANS. Every question here is about paths on a particular
filesystem, so pointing this at another machine's ``.mcp.json`` from your own
box measures YOUR disk and reports the result under THEIR name. (Nearly done
while building this: compute-01's config was pulled to compute-04 to
"validate against real data", which would have checked compute-04's
filesystem throughout and passed for the wrong reason.) There is therefore no
remote mode and no ``--host`` flag; a fleet sweep runs ``sac doctor --node``
over ssh ON each host and collects the JSON.

PURE AND INJECTABLE. Every path is a parameter, so a caller (or a test) can
point this at a real directory tree it built itself rather than at the live
host. There is nothing to patch and no collaborator to fake.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "NodeReadiness",
    "ServerVerdict",
    "assess_node_readiness",
    "node_readiness_for_this_host",
]

#: A declared server whose command resolves to this is a placeholder, not an
#: implementation. Seen deployed on a real host, and the reason an agent told
#: the operator Telegram had been disabled for it.
_STUB_COMMANDS = ("/usr/bin/true", "/bin/true", "true")


@dataclass(frozen=True)
class ServerVerdict:
    """One declared MCP server, and whether this host can actually serve it."""

    name: str
    #: "servable" | "stub" | "command-missing" | "malformed"
    state: str
    #: The command as declared. Never a secret: commands are paths, and env
    #: values (which DO hold tokens) are deliberately not read here.
    command: str = ""
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.state == "servable"


@dataclass(frozen=True)
class NodeReadiness:
    """Whether this host can produce a working agent, and what is missing.

    ``verdict`` is three-valued on purpose. "unknown" exists so that a host we
    could not inspect is never folded into the passing column — the failure
    this check exists to catch is invisibility, so treating unmeasured as
    healthy would reproduce it.
    """

    #: "ready" | "crippled" | "cannot-deploy" | "unknown"
    verdict: str
    baseline_dir: str = ""
    #: Servers an agent started here would actually get.
    usable_servers: tuple[str, ...] = ()
    #: Declared servers this host cannot honour — the important list.
    broken_servers: tuple[ServerVerdict, ...] = ()
    #: Symlinks in the baseline whose target does not exist here. Any entry
    #: means agent deploys FAIL, so this outranks a tool shortfall.
    dangling_links: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def tool_count(self) -> int:
        """How many MCP servers an agent started here would get. THE number."""
        return len(self.usable_servers)

    @property
    def is_alarming(self) -> bool:
        """True unless this host would produce a fully-equipped agent.

        "unknown" counts as alarming, matching every other sac check: an
        invariant we could not assert is not an invariant that held. That is
        the whole failure mode here — compute-01 was never measured, and
        unmeasured was silently read as fine.
        """
        return self.verdict != "ready"

    def to_dict(self) -> dict:
        """JSON shape, mirroring the other doctor checks."""
        return {
            "verdict": self.verdict,
            "baseline_dir": self.baseline_dir,
            "tool_count": self.tool_count,
            "usable_servers": list(self.usable_servers),
            "broken_servers": [
                {
                    "name": v.name,
                    "state": v.state,
                    "command": v.command,
                    "detail": v.detail,
                }
                for v in self.broken_servers
            ],
            "dangling_links": list(self.dangling_links),
            "missing": list(self.missing),
            "notes": list(self.notes),
        }


def _scan_dangling(root: Path) -> list[str]:
    """Symlinks under ``root`` whose target does not exist.

    Walked eagerly rather than lazily: a single dangling link anywhere in the
    baseline aborts every agent deploy on the host, so finding one late is the
    same as not finding it.
    """
    dangling: list[str] = []
    if not root.is_dir():
        return dangling
    for dirpath, dirnames, filenames in os.walk(root):
        for name in list(dirnames) + list(filenames):
            p = Path(dirpath) / name
            if p.is_symlink() and not p.exists():
                try:
                    target = os.readlink(p)
                except OSError:  # stx-allow: fallback (unreadable link is still dangling)
                    target = "<unreadable>"
                dangling.append(f"{p.relative_to(root)} -> {target}")
    return dangling


def _judge_server(name: str, spec: object) -> ServerVerdict:
    """Can this host actually serve the named MCP server?

    Only ``command`` is inspected. Env is deliberately NOT read: it carries
    bot tokens, and a readiness check must never be a reason to open a
    secret. A token that is present-but-wrong is not detectable here and is
    not claimed to be — that failure only surfaces on a real send.
    """
    if not isinstance(spec, dict):
        return ServerVerdict(name=name, state="malformed", detail="entry is not an object")
    command = str(spec.get("command", "") or "")
    if not command:
        return ServerVerdict(name=name, state="malformed", detail="no command declared")
    if command in _STUB_COMMANDS:
        return ServerVerdict(
            name=name,
            state="stub",
            command=command,
            detail=(
                "declared but wired to a no-op; reads as a deliberate decision "
                "to disable this channel when none was made"
            ),
        )
    # A bare name is resolved from PATH at launch; only absolute paths can be
    # checked here, and claiming otherwise would be a check that cannot fail.
    if command.startswith("/") and not Path(command).exists():
        return ServerVerdict(
            name=name, state="command-missing", command=command, detail="command does not exist on this host"
        )
    args = spec.get("args")
    if isinstance(args, list):
        for arg in args:
            text = str(arg)
            if text.startswith("/") and text.endswith(".ts") and not Path(text).exists():
                return ServerVerdict(
                    name=name,
                    state="command-missing",
                    command=command,
                    detail=f"script argument does not exist on this host: {text}",
                )
    return ServerVerdict(name=name, state="servable", command=command)


def assess_node_readiness(baseline_dir: Path | str) -> NodeReadiness:
    """Answer: would an agent started on this host have working tools?

    ``baseline_dir`` is the ``agents/_shared/to_home`` tree. Passed in rather
    than resolved internally so this stays pure and a caller can point it at
    any tree — which is also how it is tested, against real directories built
    for the purpose.
    """
    root = Path(baseline_dir)
    if not root.is_dir():
        return NodeReadiness(
            verdict="cannot-deploy",
            baseline_dir=str(root),
            missing=("to_home baseline",),
            notes=(
                "No baseline tree: every agent started here gets a home with no "
                ".mcp.json and therefore no tools, and nothing reports an error.",
            ),
        )

    dangling = tuple(sorted(_scan_dangling(root)))

    mcp_path = root / ".mcp.json"
    if not mcp_path.is_file():
        return NodeReadiness(
            verdict="cannot-deploy",
            baseline_dir=str(root),
            missing=(".mcp.json in baseline",),
            dangling_links=dangling,
            notes=("Baseline exists but declares no MCP servers.",),
        )

    # stx-allow: fallback (a malformed baseline is a real host state and must
    # be reported, not raised — the caller is a doctor, not a deploy)
    try:
        doc = json.loads(mcp_path.read_text())
        servers = doc.get("mcpServers") or {}
        if not isinstance(servers, dict):
            raise ValueError("mcpServers is not an object")
    except Exception as exc:  # stx-allow: fallback (see comment above)
        return NodeReadiness(
            verdict="cannot-deploy",
            baseline_dir=str(root),
            missing=("parseable .mcp.json",),
            dangling_links=dangling,
            notes=(f"Baseline .mcp.json could not be read: {type(exc).__name__}: {exc}",),
        )

    verdicts = [_judge_server(name, spec) for name, spec in sorted(servers.items())]
    usable = tuple(v.name for v in verdicts if v.usable)
    broken = tuple(v for v in verdicts if not v.usable)

    # A dangling link outranks a tool shortfall: deploys ABORT, so the agent
    # does not merely lose tools, it never starts.
    if dangling:
        verdict = "cannot-deploy"
    elif not usable:
        verdict = "cannot-deploy"
    elif broken:
        verdict = "crippled"
    else:
        verdict = "ready"

    missing: list[str] = []
    if dangling:
        missing.append("resolvable symlink targets")
    missing.extend(f"servable: {v.name}" for v in broken)

    return NodeReadiness(
        verdict=verdict,
        baseline_dir=str(root),
        usable_servers=usable,
        broken_servers=broken,
        dangling_links=dangling,
        missing=tuple(missing),
    )


def _resolve_host_baseline() -> Path | None:
    """The baseline a REAL deploy on this host would read.

    Imported lazily and delegated rather than reimplemented: a readiness check
    that resolved the path its own way could pass while every deploy failed,
    which is precisely the wrong-vantage error this module exists to catch.
    """
    from ..runtimes._to_home_resolve import _user_baseline_to_home_dir

    return _user_baseline_to_home_dir()


def _vantage_note() -> str:
    """Name the filesystem that was searched, always.

    Run inside a container, ``~`` is the container's home and NOT the host's,
    so "no baseline" measured in here is not a statement about the host. An
    absent-result that does not name its vantage is how a wrong-seat
    measurement gets reported as a fact about someone else's machine — the
    same error this check exists to stop, one level up.
    """
    override = (os.environ.get("SAC_USER_TO_HOME_BASELINE", "") or "").strip()
    where = f"HOME={Path('~').expanduser()}"
    if override:
        where += f", SAC_USER_TO_HOME_BASELINE={override}"
    return f"Searched from {where} — inside a container this is the CONTAINER's home, not the host's."


def node_readiness_for_this_host(
    baseline_resolver: Callable[[], Path | None] | None = None,
) -> NodeReadiness:
    """Assess the host this process is running on.

    ``baseline_resolver`` is the single impure edge, injected so a caller or a
    test supplies its own without patching anything. It defaults to the
    resolver the deploy path itself uses.
    """
    resolve = baseline_resolver or _resolve_host_baseline
    # stx-allow: fallback (a resolver that raises is an UNKNOWN host, not a
    # healthy one — reporting it as ready is the defect being fixed)
    try:
        baseline = resolve()
    except Exception as exc:  # stx-allow: fallback (see comment above)
        return NodeReadiness(
            verdict="unknown",
            notes=(f"Baseline directory could not be resolved: {type(exc).__name__}: {exc}",),
        )
    if baseline is None:
        return NodeReadiness(
            verdict="cannot-deploy",
            missing=("to_home baseline",),
            notes=(
                "No shared to_home baseline, so an agent started here gets a "
                "home with no .mcp.json and therefore no tools. Nothing "
                "errors; the agent registers and heartbeats normally.",
                _vantage_note(),
            ),
        )
    return assess_node_readiness(baseline)
