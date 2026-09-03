"""``sac a2a {serve,doctor,grant,unblock,block,revoke,grants,list,reachability}``.

The A2A protocol surface. This module holds the ``a2a`` group itself plus
the two protocol verbs; the rest live in sibling modules (this file sat
over the per-file cap) and are registered at the bottom, the same way
``_host_sync`` attaches to ``sac host``:

* :func:`a2a_serve` boots the stdlib HTTP A2A server for one or more
  agent YAMLs.
* :func:`a2a_doctor` probes an agent's AgentCard endpoint (as
  declared by ``spec.a2a`` in its YAML) and reports liveness +
  round-trip latency. Useful for ops or as ``spec.health.method:
  a2a-card`` from outside the agent process.
* ``grant`` / ``unblock`` / ``block`` / ``revoke`` / ``grants``
  (:mod:`._a2a_acl_cmds`) are thin click wrappers over the cross-group
  ACL primitives in ``_state.state_db_nodes``. Operators previously had
  to drop into a Python REPL to amend the comms-grants table — a footgun
  (silently granting too much on wrong argument order). The CLI makes it
  auditable and validates the positional order at the Click layer.
* ``list`` (:mod:`._a2a_list_cmd`) lists every peer registered on the
  local ``sac listen``.
* ``reachability`` (:mod:`._a2a_reachability`) probes the cross-host a2a
  transport to every peer, from this host.

Examples::

    sac a2a serve mock-echo.yaml --port 8888
    sac a2a serve mock-echo.yaml --handler claude_cli --port 8888
    sac a2a serve agents/*/*.yaml --port 9000
    sac a2a doctor mock-echo.yaml
    sac a2a doctor mock-echo.yaml --json
    sac a2a grant worker-a worker-b --note "ticket-123"
    sac a2a revoke worker-a worker-b
    sac a2a grants --json
    sac a2a reachability --all --json
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
# Verbs that live in sibling modules (this file sat over the per-file cap),
# registered here the way ``_host_sync`` attaches to ``sac host``. The verb
# names are re-exported so ``from .a2a_group import a2a_grant`` keeps
# resolving for any caller written against the pre-split layout.
# ---------------------------------------------------------------------------
from ._a2a_acl_cmds import (  # noqa: E402
    a2a_block,
    a2a_grant,
    a2a_grants,
    a2a_revoke,
    a2a_unblock,
)
from ._a2a_acl_cmds import register as _register_acl  # noqa: E402
from ._a2a_list_cmd import a2a_list  # noqa: E402
from ._a2a_list_cmd import register as _register_list  # noqa: E402
from ._a2a_reachability import a2a_reachability  # noqa: E402
from ._a2a_reachability import register as _register_reachability  # noqa: E402

_register_acl(a2a)
_register_list(a2a)
_register_reachability(a2a)

__all__ = [
    "a2a",
    "a2a_block",
    "a2a_doctor",
    "a2a_grant",
    "a2a_grants",
    "a2a_list",
    "a2a_reachability",
    "a2a_revoke",
    "a2a_serve",
    "a2a_unblock",
]
