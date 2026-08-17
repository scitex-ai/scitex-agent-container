"""``sac host`` noun group — local host identity + peer routing (F-CS12).

Phase 1 surface (read-only inspection):

  * ``sac host list`` — local host row + configured peers with their ssh routes
  * ``sac host add / remove / set`` — peer CRUD against config.yaml

Phase 2 (deferred): ``sac host probe <peer>``, ``sac host exec
<peer> -- <args>``, the ``--on <peer>`` global flag, and fold-ins
of ``sac network probe`` / ``sac installation boot``.
"""

from __future__ import annotations

import json
import subprocess
import sys

import click

from .._state._peer_resolve import peers_with_registry
from .._state.host_config import (
    Config,
    build_ssh_argv,
    load,
    ssh_control_options_str,
)
from ._helpers import _json_flag, console
from ._host_list_cmd import host_list, register_list_command
from ._host_validate_cmd import host_validate, register_validate_command

# ``host_list`` / ``host_validate`` are re-exported because callers import them
# FROM THIS MODULE (tests/scitex_agent_container/_state/test_host_config.py and
# cli_pkg/test_host_group.py). Moving the leaves out without these two names
# broke those imports — the extraction preserved the API I had in mind
# (host_group / split_on_flag / dispatch_remote) rather than the one that was
# actually imported. Keep them until the callers are migrated deliberately.
__all__ = [
    "dispatch_remote",
    "host_exec",
    "host_group",
    "host_list",
    "host_probe",
    "host_validate",
    "split_on_flag",
]


@click.group(
    "host",
    context_settings={"help_option_names": ["-h", "--help"]},
)
def host_group() -> None:
    """Local host identity and peer routing for sac.

    \b
    Examples:
      $ sac host list
    """


# The ``list`` and ``validate`` leaves live in their own modules to keep this
# orchestrator under the per-file line cap; they attach at import time (same
# pattern as ``_account_list_cmd.py``). This module keeps one cohesive
# responsibility: reaching peers (split_on_flag / dispatch_remote / exec /
# probe / ssh-opts / peer CRUD).
register_list_command(host_group)
register_validate_command(host_group)


def _unknown_peer_error(peer: str, cfg: Config) -> str:
    """The message for a peer neither config.yaml nor the registry knows.

    Names BOTH sources, because "not defined in config.yaml" was actively
    misleading once the registry became a route source: on a host with no
    config.yaml at all it pointed the operator at a file that does not
    exist and never mentioned the registry that lists the host they asked
    for. Ends with the one command that enumerates every name that WOULD
    have worked.
    """
    where = str(cfg.source_path) if cfg.source_path else "config.yaml"
    return (
        f"error: peer '{peer}' is not defined in {where} and is not a "
        f"routable host in the scitex-dev registry (a registry host is "
        f"routable when it declares an ssh_alias).\n"
        f"Add it under peers: in config.yaml or to the registry, then "
        f"re-run. See: sac host list"
    )


def split_on_flag(argv: list[str]) -> tuple[str | None, list[str]]:
    """If ``argv`` contains ``--on PEER`` (or ``--on=PEER``), strip the
    flag and return ``(peer, remaining_argv)``. Otherwise ``(None, argv)``.

    Used by the entry-point shim before Click parses anything: when the
    user typed ``sac --on spartan agent list``, we want ``agent list``
    to run on spartan with no further sac-side processing. Click
    normally consumes ``--on`` during parsing of ``main``, but the
    flag must be honoured BEFORE the subcommand is dispatched.
    """
    out: list[str] = []
    peer: str | None = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--on":
            if i + 1 >= len(argv):
                raise click.UsageError("--on requires a peer name")
            peer = argv[i + 1]
            i += 2
            continue
        if tok.startswith("--on="):
            peer = tok.split("=", 1)[1]
            i += 1
            continue
        out.append(tok)
        i += 1
    return peer, out


def dispatch_remote(peer: str, argv: list[str], ssh_argv0: str = "sac") -> int:
    """Run ``sac <argv>`` on ``peer`` via ssh; return the remote exit code.

    Used when the entry point detects ``--on PEER`` in sys.argv. The
    remote command is always ``sac <argv>`` — orchestrators rely on
    that prefix so peers can be set up to alias ``sac`` to the right
    binary path on hosts where it isn't on $PATH.

    Special case ``agents start``: the verbatim pass-through would record
    the new instance ONLY in the REMOTE peer's local registry, leaving the
    dispatching (lead) host's cross-host ``instances`` table unaware of the
    override-host placement (issue #192 — clew restarted via
    ``sac --on spartan-bm001 agents start`` was invisible to the lead).
    For that verb we delegate to :func:`._on_start_propagate.propagate_remote_start`,
    which runs the remote start with ``--json --no-redispatch`` and writes
    a lead-side row capturing the ACTUAL override host + bound port +
    ``remote=True``.
    """
    cfg = load()
    peers = peers_with_registry(cfg.peers)
    if peer not in peers:
        click.echo(_unknown_peer_error(peer, cfg), err=True)
        return 2
    from ._on_start_propagate import is_agents_start_argv, propagate_remote_start

    if is_agents_start_argv(argv):
        return propagate_remote_start(peer, argv, ssh_argv0=ssh_argv0)
    ssh_argv = build_ssh_argv(peer, [ssh_argv0, *argv], peers)
    proc = subprocess.run(ssh_argv)
    return proc.returncode


@host_group.command(
    "exec",
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
    },
)
@click.argument("peer", required=True)
@click.argument("argv", nargs=-1, type=click.UNPROCESSED)
def host_exec(peer: str, argv: tuple[str, ...]) -> None:
    """Run a command on PEER over ssh, multi-hop aware.

    \b
    Example:
      $ sac host exec spartan -- agent list --json
      $ sac host exec bm198 -- sac db export --since 2026-05-01

    PEER may be a ``peers:`` entry in config.yaml or any host in the
    scitex-dev registry that declares an ``ssh_alias``; config.yaml wins
    when both define it. The peer's ``via:`` chain renders into ssh's
    ``-J`` flag automatically; sac never opens a port.

    ON A TTY stdio is INHERITED so long-running output streams live. OFF a
    TTY it is CAPTURED and re-emitted through click — see
    :func:`_run_peer_command`, which explains why that difference is not
    cosmetic.
    """
    cfg = load()
    peers = peers_with_registry(cfg.peers)
    if peer not in peers:
        click.echo(_unknown_peer_error(peer, cfg), err=True)
        raise SystemExit(2)
    if not argv:
        click.echo(
            "error: no command supplied. Try: sac host exec PEER -- <cmd>", err=True
        )
        raise SystemExit(2)
    ssh_argv = build_ssh_argv(peer, list(argv), peers)
    raise SystemExit(_run_peer_command(ssh_argv))


def _run_peer_command(ssh_argv: list[str]) -> int:
    """Run the ssh child, capturing its output when nothing is watching a TTY.

    WHY THIS IS NOT COSMETIC — measured 2026-08-17, twice, by two agents.

    The straightforward ``subprocess.run(ssh_argv)`` inherits the OS-LEVEL
    file descriptors, so the child writes past any IN-PROCESS capture. That is
    exactly right for a human at a terminal: output streams as it arrives. It
    is silently wrong for every programmatic caller, because the MCP surface
    invokes this command through Click's ``CliRunner``, which captures
    Python-level streams only. The ssh child's bytes go to the real fd and the
    caller receives EMPTY stdout and EMPTY stderr with only an exit code.

    What that produced in practice:

      * ``sac host exec <peer> -- agents list`` returned ``exit 127`` with BOTH
        streams empty. 127 is command-not-found — the non-login ssh shell
        lacked the peer's venv on PATH — but the stderr that would have said so
        was gone. A bare 127 is indistinguishable from a dozen other failures.
      * Worse, a command containing ``hostname`` returned ``exit_code 0`` with
        empty stdout. ``hostname`` cannot print nothing, so the command had not
        run — and the tool reported SUCCESS. An empty stdout with a zero exit
        reads as "ran fine, found nothing", and the agent that hit it nearly
        published "0 runtime homes on that host" as a finding. It would have
        corroborated a WRONG hypothesis of mine with a fabricated measurement:
        two agents agreeing, from one broken instrument, about something
        neither had measured.

    So the rule this encodes: a transport must never render "I could not run
    it" identically to "it ran and produced nothing". Capturing off-TTY and
    re-emitting through click makes the child's own words survive to whatever
    is actually reading.
    """
    if sys.stdout.isatty():
        # A human is watching: inherit, so output streams as it arrives.
        return subprocess.run(ssh_argv).returncode

    proc = subprocess.run(ssh_argv, capture_output=True, text=True)
    if proc.stdout:
        click.echo(proc.stdout, nl=False)
    if proc.stderr:
        click.echo(proc.stderr, nl=False, err=True)
    return proc.returncode


@host_group.command("probe")
@click.argument("peer", required=True)
@click.option("--timeout", type=int, default=10, help="ssh ConnectTimeout seconds.")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def host_probe(ctx: click.Context, peer: str, timeout: int, as_json: bool) -> None:
    """One ssh round-trip to PEER; report reachability + remote canonical hostname.

    Cheap liveness check used by orchestrators (and eventually by
    F-CS14's pull cron). Runs ``sac host list --json`` on the remote
    so the report contains the peer's reported canonical name (from
    the local block) plus a measured round-trip duration.

    \b
    Example:
      $ sac host probe spartan-bm198
      $ sac host probe nas --timeout 10 --json
    """
    import time

    cfg = load()
    peers = peers_with_registry(cfg.peers)
    if peer not in peers:
        msg = _unknown_peer_error(peer, cfg)
        if _json_flag(ctx, as_json):
            click.echo(json.dumps({"peer": peer, "reachable": False, "error": msg}))
        else:
            click.echo(f"[red]error:[/red] {msg}", err=True)
        raise SystemExit(2)

    remote_argv = ["sac", "host", "list", "--json"]
    ssh_argv = build_ssh_argv(
        peer,
        remote_argv,
        peers,
        extra_opts=["-o", f"ConnectTimeout={timeout}"],
    )
    started = time.monotonic()
    proc = subprocess.run(ssh_argv, capture_output=True, text=True)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    reachable = proc.returncode == 0
    remote_canonical: str | None = None
    if reachable:
        try:
            remote_canonical = json.loads(proc.stdout).get("local", {}).get("name")
        except (
            ValueError,
            KeyError,
        ):  # stx-allow: fallback (reason: malformed remote output tolerated)
            remote_canonical = None
    payload = {
        "peer": peer,
        "reachable": reachable,
        "elapsed_ms": elapsed_ms,
        "remote_canonical": remote_canonical,
        "exit_code": proc.returncode,
        "stderr": proc.stderr.strip() if proc.stderr else "",
    }
    if _json_flag(ctx, as_json):
        click.echo(json.dumps(payload, indent=2))
    elif reachable:
        console.print(
            f"[green]ok[/green]  {peer}  {elapsed_ms}ms  remote={remote_canonical}"
        )
    else:
        console.print(f"[red]unreachable[/red]  {peer}  exit={proc.returncode}")
        if proc.stderr.strip():
            console.print(f"[dim]{proc.stderr.strip()[:200]}[/dim]")
    if not reachable:
        raise SystemExit(1)


@host_group.command("ssh-opts")
def host_ssh_opts() -> None:
    """Print sac's ssh ControlMaster options as a shell-quoted string.

    Splat this into an ad-hoc ssh / scp / rsync command so multi-host
    automation reuses sac's existing multiplexed master per peer
    instead of opening a fresh connection per call. The output is
    shell-safe — just pass it through ``$(...)``::

        ssh $(sac host ssh-opts) myhost cmd
        rsync -e "ssh $(sac host ssh-opts)" src/ myhost:dst/
        parallel ssh $(sac host ssh-opts) myhost ::: cmd1 cmd2 cmd3

    Prints an empty line when multiplexing is opted out
    (``SAC_SSH_CONTROL_MASTER=0``) so the splat is a no-op. The
    underlying flags are documented under
    :func:`scitex_agent_container._state.host_config.ssh_control_options`.
    """
    click.echo(ssh_control_options_str())


# WSL → fleet-hub layered probe (folded from ``sac network probe``).
# Kept under ``host`` since it's a reachability check — same family
# as ``host probe PEER`` but targeted at the hub rather than a peer.
from .probe_cmds import probe_network as _probe_hub_impl  # noqa: E402

host_group.add_command(
    click.Command(
        name="probe-hub",
        callback=_probe_hub_impl.callback,
        params=list(_probe_hub_impl.params),
        help=_probe_hub_impl.help,
        short_help="WSL → fleet-hub layered probe (DNS, gateway, TCP, HTTPS).",
        epilog=_probe_hub_impl.epilog,
    )
)


# Peer CRUD verbs (add / remove / set). Split into ``_host_crud`` so
# ``host_group`` stays under the project line-budget; registered here
# so they show up in ``sac host --help`` alongside ``list`` / ``probe``.
from ._host_crud import register as _register_host_crud  # noqa: E402

_register_host_crud(host_group)


# ``sac host sync`` / ``sac host push-config`` — the one-way channels
# (code and generated client config, centre -> remote), each with a
# read-only ``--check`` drift detector. Split out like the CRUD verbs:
# this file is at the 512-line ceiling.
from ._host_push_config import register as _register_host_push_config  # noqa: E402
from ._host_sync import register as _register_host_sync  # noqa: E402

_register_host_push_config(host_group)
_register_host_sync(host_group)


# WI-4 Q4(b) — peer bearer-token registry. ``sac host add-peer`` /
# ``list-peers`` / ``remove-peer`` manage the
# ``peer-tokens/<peer-host>.token`` files the cross-host forwarder
# consults to authenticate at a destination ``sac listen``. Each
# entry is per-host scoped, so leaking one host's token compromises
# only that host (the per-host blast radius the lead asked for).


@host_group.command("add-peer")
@click.argument("peer_host")
@click.argument("token")
def host_add_peer(peer_host: str, token: str) -> None:
    """Register a peer host's listen bearer for cross-host forwarding.

    Writes ``~/.scitex/agent-container/peer-tokens/<PEER_HOST>.token``
    mode 0600. The cross-host forwarder reads this file when
    forwarding ``message:send`` to ``<PEER_HOST>``.

    \b
    Examples:
      sac host add-peer host-a $(ssh host-a 'cat ~/.scitex/agent-container/tokens/listen-host-a.token')
      sac host add-peer head-spartan AAAAA-bearer-from-spartan-BBBBB
    """
    from .._listen.peer_tokens import write_peer_token

    if not peer_host:
        raise click.UsageError("PEER_HOST must be non-empty")
    if not token:
        raise click.UsageError("TOKEN must be non-empty")
    dst = write_peer_token(peer_host=peer_host, token=token)
    console.print(f"[green]ok[/green]  wrote {dst}")


@host_group.command("list-peers")
def host_list_peers() -> None:
    """List the peer hosts that have a registered listen bearer.

    Token values are NEVER printed — only the peer-host names. To
    see the on-disk file paths run
    ``ls -la ~/.scitex/agent-container/peer-tokens/``.
    """
    from .._listen.peer_tokens import default_peer_tokens_dir, list_peer_hosts

    tdir = default_peer_tokens_dir()
    hosts = list_peer_hosts()
    if not hosts:
        console.print(
            f"no peer tokens registered (dir: {tdir}). "
            "Add one with: sac host add-peer <host> <token>"
        )
        return
    console.print(f"peer-tokens dir: {tdir}")
    for h in hosts:
        console.print(f"  {h}")


@host_group.command("remove-peer")
@click.argument("peer_host")
def host_remove_peer(peer_host: str) -> None:
    """Remove a peer host's listen bearer from the registry.

    Idempotent — removing an absent peer is a no-op (returns 0).
    """
    from .._listen.peer_tokens import default_peer_tokens_dir

    if not peer_host:
        raise click.UsageError("PEER_HOST must be non-empty")
    path = default_peer_tokens_dir() / f"{peer_host}.token"
    if not path.exists():
        console.print(f"no peer token to remove at {path}")
        return
    path.unlink()
    console.print(f"[green]ok[/green]  removed {path}")
