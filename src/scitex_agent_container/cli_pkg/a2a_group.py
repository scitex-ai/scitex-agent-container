"""``sac a2a {serve,doctor,grant,revoke,grants}`` CLI subcommands — A2A protocol surface.

* :func:`a2a_serve` boots the stdlib HTTP A2A server for one or more
  agent YAMLs.
* :func:`a2a_doctor` probes an agent's AgentCard endpoint (as
  declared by ``spec.a2a`` in its YAML) and reports liveness +
  round-trip latency. Useful for ops or as ``spec.health.method:
  a2a-card`` from outside the agent process.
* :func:`a2a_grant` / :func:`a2a_revoke` / :func:`a2a_grants` are
  thin click wrappers over the cross-group ACL primitives in
  ``_state.state_db_nodes`` (``grant_send`` / ``revoke_send`` /
  ``list_comms_grants``). Operators previously had to drop into a
  Python REPL to amend the comms-grants table — a footgun
  (silently granting too much on wrong argument order). The CLI
  makes it auditable and validates the positional order at the
  Click layer.

Examples::

    sac a2a serve mock-echo.yaml --port 8888
    sac a2a serve mock-echo.yaml --handler claude_cli --port 8888
    sac a2a serve agents/*/*.yaml --port 9000
    sac a2a doctor mock-echo.yaml
    sac a2a doctor mock-echo.yaml --json
    sac a2a grant worker-a worker-b --note "ticket-123"
    sac a2a revoke worker-a worker-b
    sac a2a grants --json
"""

from __future__ import annotations

import json
import logging
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import click
import yaml

from scitex_agent_container.a2a import HANDLERS, serve


@click.group(name="a2a")
def a2a() -> None:
    """A2A protocol — generic agent-to-agent surface (no fleet deps)."""


@a2a.command("serve")
@click.argument(
    "agent_yamls",
    nargs=-1,
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Interface to bind. Use 0.0.0.0 to expose externally.",
)
@click.option(
    "--port",
    type=int,
    default=8888,
    show_default=True,
    help="TCP port for the A2A HTTP server.",
)
@click.option(
    "--handler",
    type=click.Choice(sorted(HANDLERS), case_sensitive=False),
    default="echo",
    show_default=True,
    help=(
        "Default JSON-RPC dispatcher (overridden per-agent by "
        "`spec.a2a.handler` in the yaml). 'echo' = canned reply, "
        "'claude_cli' = `claude --print`, 'exec' = $SAC_A2A_EXEC_COMMAND."
    ),
)
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Enable INFO-level logging on the server.",
)
def a2a_serve(
    agent_yamls: tuple[Path, ...],
    host: str,
    port: int,
    handler: str,
    verbose: bool,
) -> None:
    """Serve A2A endpoints for the given agent YAMLs (foreground).

    \b
    Example:
      $ sac a2a serve ~/.scitex/agent-container/agents/foo/foo.yaml
      $ sac a2a serve foo.yaml bar.yaml --port 8001
    """
    if verbose:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
        )
    serve(list(agent_yamls), host=host, port=port, handler=handler)


@a2a.command("doctor")
@click.argument(
    "agent_yaml",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--host",
    default=None,
    help="Override host from spec.a2a.host. Default: read from YAML.",
)
@click.option(
    "--port",
    type=int,
    default=None,
    help="Override port from spec.a2a.port. Default: read from YAML.",
)
@click.option(
    "--timeout",
    type=float,
    default=5.0,
    show_default=True,
    help="HTTP timeout in seconds.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit a JSON envelope instead of human text.",
)
def a2a_doctor(
    agent_yaml: Path,
    host: str | None,
    port: int | None,
    timeout: float,
    as_json: bool,
) -> None:
    """Probe an agent's A2A AgentCard endpoint and report health.

    \b
    Example:
      $ sac a2a doctor ~/.scitex/agent-container/agents/foo/foo.yaml
      $ sac a2a doctor foo.yaml --json
    """
    v3 = yaml.safe_load(agent_yaml.read_text()) or {}
    # Dir-as-SSoT: agent identifier is the parent dir's name (the yaml itself
    # is always called spec.yaml). Fall back to metadata.name (legacy) and
    # then to the file stem only if the yaml lives directly at a search
    # root rather than in its own subdir.
    if agent_yaml.parent.name and agent_yaml.stem in ("spec",):
        name = agent_yaml.parent.name
    else:
        name = (v3.get("metadata") or {}).get("name") or agent_yaml.stem
    a2a_block = (v3.get("spec") or {}).get("a2a") or {}

    eff_host = host or str(a2a_block.get("host", "127.0.0.1"))
    eff_port = port or a2a_block.get("port")
    if eff_port is None:
        result = {
            "ok": False,
            "agent": name,
            "error": "spec.a2a.port not set in YAML and --port not given",
        }
        _emit(result, as_json)
        sys.exit(2)

    # Canonical A2A v1 well-known path is ``agent-card.json`` (matches
    # ``a2a/_server.py`` + ADR-0004). The pre-v1 ``agent.json`` spelling
    # is no longer served by sac.
    url = f"http://{eff_host}:{int(eff_port)}/agents/{name}/.well-known/agent-card.json"
    t0 = time.time()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = json.loads(resp.read())
        elapsed_ms = int((time.time() - t0) * 1000)
        if not isinstance(body, dict) or body.get("name") != name:
            result = {
                "ok": False,
                "agent": name,
                "url": url,
                "elapsed_ms": elapsed_ms,
                "error": (
                    f"AgentCard name mismatch (expected {name!r}, "
                    f"got {body.get('name') if isinstance(body, dict) else '?'})"
                ),
            }
            _emit(result, as_json)
            sys.exit(1)
        result = {
            "ok": True,
            "agent": name,
            "url": url,
            "elapsed_ms": elapsed_ms,
            "card_url": body.get("url"),
        }
        _emit(result, as_json)
    except (
        urllib.error.HTTPError
    ) as exc:  # stx-allow: fallback (reason: expected failure — see inline comment)
        _emit(
            {
                "ok": False,
                "agent": name,
                "url": url,
                "error": f"HTTP {exc.code}: {exc.reason}",
            },
            as_json,
        )
        sys.exit(1)
    except (
        urllib.error.URLError,
        OSError,
        json.JSONDecodeError,
    ) as exc:  # stx-allow: fallback (reason: malformed JSON tolerated)
        _emit(
            {
                "ok": False,
                "agent": name,
                "url": url,
                "error": f"{type(exc).__name__}: {exc}",
            },
            as_json,
        )
        sys.exit(1)


def _emit(result: dict, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(result, ensure_ascii=False))
        return
    if result.get("ok"):
        click.echo(
            f"[{result['agent']}] healthy ({result['elapsed_ms']} ms) "
            f"at {result['url']}"
        )
    else:
        url = result.get("url") or "(no URL)"
        click.echo(
            f"[{result['agent']}] unhealthy at {url}: {result['error']}",
            err=True,
        )


# ---------------------------------------------------------------------------
# Cross-group ACL verbs — thin wrappers over state_db_nodes primitives.
#
# Imports happen inside the callback (not at module import) to keep the
# Click cold-start cheap: ``sac --help`` and tab-completion press should
# never load SQLite. The same lazy pattern used by ``host_group`` /
# ``peer_group`` for state_db consumers.
# ---------------------------------------------------------------------------


def _do_unblock(sender: str, target: str, note: str | None) -> None:
    """Shared implementation for both ``grant`` (legacy) and ``unblock``.

    Task #27 PR B: dispatches via
    :func:`_a2a_acl_dispatch.dispatch_acl_decision` which routes
    in-SIF → host listen HTTP (so the write lands on the host's
    state.db) and bare-host → local DB helpers directly.
    """
    from ._a2a_acl_dispatch import dispatch_acl_decision

    try:
        result = dispatch_acl_decision(
            "unblock", sender=sender, target=target, note=note
        )
    except click.ClickException:
        raise
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        raise SystemExit(2) from exc
    except Exception as exc:
        # In-SIF broker raised an AclBrokerError (or transport
        # error). Surface as a clean ClickException so the
        # operator sees a single-line stderr instead of a
        # traceback.
        raise click.ClickException(str(exc)) from exc
    from ._helpers import console

    extras = []
    if result.get("unblocked"):
        extras.append("removed block")
    if result.get("cleared_pending"):
        extras.append("cleared pending prompt")
    tail = f" [dim]({'; '.join(extras)})[/dim]" if extras else ""
    console.print(f"[green]ok[/green]  unblocked  {sender}  ->  {target}{tail}")


@a2a.command("grant")
@click.argument("sender")
@click.argument("target")
@click.option(
    "--note",
    default=None,
    help="Free-form audit annotation (e.g. ticket / handoff that authorised this).",
)
def a2a_grant(sender: str, target: str, note: str | None) -> None:
    """Grant ``SENDER`` permission to send messages to ``TARGET``.

    Legacy alias of ``sac a2a unblock <SENDER> <TARGET>``. Writes the
    ``comms_grants`` row, removes any ``comms_blocks`` row, and
    clears the pending-prompt row for the pair. Re-granting an
    already-granted pair is a no-op on the timestamp.

    Argument order matters: ``SENDER → TARGET`` is directional. To
    allow bidirectional cross-group traffic, run the command twice.

    \b
    Example:
      $ sac a2a grant worker-a worker-b
      $ sac a2a grant worker-a worker-b --note "ticket-PA-512"
    """
    _do_unblock(sender, target, note)


@a2a.command("unblock")
@click.argument("sender")
@click.argument("target")
@click.option(
    "--note",
    default=None,
    help="Free-form audit annotation (e.g. the approval-prompt msg_id this responds to).",
)
def a2a_unblock(sender: str, target: str, note: str | None) -> None:
    """UNBLOCK ``SENDER`` — allow this sender's future messages to ``TARGET``.

    Task #27 receiver-facing verb. Embedded in the approve-prompt
    push the receiver sees on a denied cross-group send. Writes the
    ``comms_grants`` row, removes any ``comms_blocks`` row, and
    clears the pending-prompt row. The sender's original denied
    message is NOT replayed — they resend if needed.

    \b
    Example:
      $ sac a2a unblock worker-a lead
      $ sac a2a unblock worker-a lead --note "prompt msg_id abc123"
    """
    _do_unblock(sender, target, note)


@a2a.command("block")
@click.argument("sender")
@click.argument("target")
@click.option(
    "--note",
    default=None,
    help="Free-form audit annotation (e.g. the approval-prompt msg_id this responds to).",
)
def a2a_block(sender: str, target: str, note: str | None) -> None:
    """BLOCK ``SENDER`` — silently drop this sender's future attempts to ``TARGET``.

    Task #27 receiver-facing verb. Embedded in the approve-prompt
    push as the silence-this-sender alternative to ``unblock``.
    Writes the ``comms_blocks`` row, clears the pending-prompt row.
    Future sends from ``SENDER`` to ``TARGET`` are silently dropped
    by :func:`_listen._acl.check_send_acl` (no receiver push, no
    approve-prompt re-fire). The sender still gets a 403 — they
    learn their send did not land — but the receiver sees nothing.

    Idempotent: re-blocking is a no-op on the existing row's
    timestamp. Block precedence: if the pair also has a grant,
    BLOCK wins.

    \b
    Example:
      $ sac a2a block worker-a lead
      $ sac a2a block worker-a lead --note "prompt msg_id abc123"
    """
    from ._a2a_acl_dispatch import dispatch_acl_decision

    try:
        result = dispatch_acl_decision("block", sender=sender, target=target, note=note)
    except click.ClickException:
        raise
    except ValueError as exc:
        click.echo(f"error: {exc}", err=True)
        raise SystemExit(2) from exc
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    from ._helpers import console

    tail = (
        " [dim](cleared pending prompt)[/dim]" if result.get("cleared_pending") else ""
    )
    console.print(f"[yellow]ok[/yellow]  blocked  {sender}  ->  {target}{tail}")


@a2a.command("revoke")
@click.argument("sender")
@click.argument("target")
def a2a_revoke(sender: str, target: str) -> None:
    """Revoke ``SENDER``'s permission to send messages to ``TARGET``.

    Thin wrapper over ``_state.state_db_nodes.revoke_send`` — removes
    the single ``sender → target`` row in ``comms_grants``. No
    confirmation prompt: the operation is narrow (one row, one
    direction) and idempotent — revoking a non-existent grant prints
    ``no-op`` and exits 0.

    \b
    Example:
      $ sac a2a revoke worker-a worker-b
    """
    if not sender or not target:
        click.echo(
            "error: SENDER and TARGET must both be non-empty",
            err=True,
        )
        raise SystemExit(2)
    from .._state.state_db_nodes import revoke_send
    from ._helpers import console

    removed = revoke_send(sender=sender, target=target)
    if removed:
        console.print(f"[green]ok[/green]  revoked  {sender}  ->  {target}")
    else:
        console.print(f"[dim]no-op[/dim]  no grant  {sender}  ->  {target}")


@a2a.command("grants")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit a JSON array instead of a rich table (scripting-friendly).",
)
def a2a_grants(as_json: bool) -> None:
    """List every row in the ``comms_grants`` table.

    Thin wrapper over ``_state.state_db_nodes.list_comms_grants``.
    Rows are emitted in insertion order with their audit ``note`` (if
    any). Empty table renders as ``(no grants)`` in rich mode and
    ``[]`` in JSON mode.

    \b
    Example:
      $ sac a2a grants
      $ sac a2a grants --json | jq '.[] | select(.sender == "worker-a")'
    """
    from .._state.state_db_nodes import list_comms_grants

    rows = list_comms_grants()
    if as_json:
        click.echo(json.dumps(rows, ensure_ascii=False))
        return
    from ._helpers import console

    if not rows:
        console.print("[dim](no grants)[/dim]")
        return
    from rich.table import Table

    table = Table(show_header=True, header_style="bold")
    table.add_column("sender")
    table.add_column("target")
    table.add_column("created_at", justify="right")
    table.add_column("note", overflow="fold")
    for r in rows:
        table.add_row(
            str(r["sender"]),
            str(r["target"]),
            f"{r['created_at']:.0f}",
            r["note"] if r["note"] is not None else "",
        )
    console.print(table)


@a2a.command("list")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit a JSON array instead of a rich table (scripting-friendly).",
)
@click.option(
    "--url",
    "base_url",
    default=None,
    help=(
        "Listen base URL. Default: $SAC_LISTEN_BASE_URL or "
        "http://127.0.0.1:7878."
    ),
)
def a2a_list(as_json: bool, base_url: str | None) -> None:
    """List every peer registered on the local ``sac listen`` (the a2a registry).

    Queries ``GET /agents`` on the local listen server -- the SAME source
    the ``a2a_peers`` MCP tool reads. Shows container agents (Registry)
    AND self-registered comms-nodes: any process that holds the sac MCP
    and self-registers at startup (e.g. ``sac mcp channel --name lead``).

    Fail-loud: aborts with a clear message if no listen bearer token is
    found or the listen server is unreachable -- no silent empty result.

    \b
    Example:
      $ sac a2a list
      $ sac a2a list --json | jq '.[] | select(.kind == "comms-node")'
    """
    import os

    from .._listen.tokens import default_token_path, read_token

    url = base_url or os.environ.get(
        "SAC_LISTEN_BASE_URL", "http://127.0.0.1:7878"
    )
    token = os.environ.get("SAC_LISTEN_BEARER")
    if not token:
        token = read_token(default_token_path())
    if not token:
        raise SystemExit(
            "sac a2a list: no listen bearer token found "
            f"($SAC_LISTEN_BEARER unset and {default_token_path()} absent). "
            "Is `sac listen` running on this host?"
        )

    req = urllib.request.Request(url.rstrip("/") + "/agents")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=6.0) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"sac a2a list: cannot reach listen at {url}: {exc}"
        ) from exc

    agents = payload.get("agents", [])
    if as_json:
        click.echo(json.dumps(agents, ensure_ascii=False))
        return

    from ._helpers import console

    if not agents:
        console.print("[dim](no a2a peers)[/dim]")
        return

    from rich.table import Table

    table = Table(show_header=True, header_style="bold")
    table.add_column("name")
    table.add_column("kind")
    table.add_column("host")
    table.add_column("a2a_port", justify="right")
    table.add_column("turn_url", overflow="fold")
    for a in agents:
        port = a.get("a2a_port")
        table.add_row(
            str(a.get("name", "")),
            str(a.get("kind", "agent")),
            str(a.get("host", "")),
            "" if port is None else str(port),
            str(a.get("turn_url") or ""),
        )
    console.print(table)
