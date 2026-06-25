"""``sac agent send`` — resume an agent's session for one more turn.

Wraps ``claude --resume <session-id> -p "<prompt>"`` so the caller
doesn't need to know the session id or the workdir. Reads the
session id from ``~/.scitex/agent-container/runtime/<name>/session_id``
(persisted by the SDK runner) and ``cd``s into the agent's workdir
before shelling out so claude's per-project session lookup resolves.

v1 scope: bare-CLI passthrough. The follow-up implementation order in
``GITIGNORED/SAC_OROCHI_SCOPES.md`` then exposes this through
``sac listen`` + ``POST /agents/<name>/send``.

For programmatic callers that need a structured ``{status,
response_text, response_metadata}`` payload (e.g. the MCP
``agent_send`` tool), use
:func:`scitex_agent_container.cli_pkg._send.send_to_agent` — the
library-facing sibling helper that returns a dict instead of writing
to stdout.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import click

from .._runners._session_state import read_session_id, state_dir_for
from .._runners._tmux.multiplexer import get_multiplexer
from ..config import load_config
from ..config._resolve import resolve_config
from ._helpers import agent_name_complete


class _RemoteA2APortMissingError(click.ClickException):
    """The remote agent's state.db row has no a2a_port recorded.

    Raised by :func:`_try_dispatch_remote_send` when an agent is active
    on a peer but never registered an A2A port (e.g. ``runtime`` doesn't
    expose ``/v1/turn``, or the runner hasn't claimed a port yet). The
    typed exception makes the failure mode explicit so callers can
    distinguish it from a generic network error and so tests can pin
    on the class rather than a message substring.
    """


def _send_via_host_listen(
    *,
    name: str,
    prompt: str,
    model: str | None,
    max_turns: int | None,
) -> None:
    """In-SIF send proxy — POST /agents/<name>/send → outcome JSON.

    PR-3 Checkpoint 3 — the path the ``sac agents send <name>
    <prompt>`` CLI takes when running inside an apptainer SIF. The
    ``--key`` (SIGINT) path is excluded by the call site because
    it needs local pid access; prompts route through the host
    listen so the running agent's in-process SDK session handles
    the turn end-to-end.

    The host's lineage-scoped ACL gate denies cross-lineage sends
    with ``kind=acl_deny`` + exit 5; other failures map per the
    standard outcome table.
    """
    import sys as _sys

    from .._lifecycle._in_sif_http_client import (
        HostListenTransportError,
        host_listen_call,
    )
    from .._lifecycle._in_sif_outcome import (
        build_outcome,
        outcome_to_stdout_json,
        transport_outcome,
    )

    body: dict = {"prompt": prompt}
    if model is not None:
        body["model"] = model
    if max_turns is not None:
        body["max_turns"] = max_turns
    try:
        status, resp = host_listen_call("POST", f"/agents/{name}/send", body=body)
        outcome = build_outcome(http_status=status, body=resp)
    except HostListenTransportError as exc:
        outcome = transport_outcome(str(exc), url=exc.url)
    _sys.stdout.write(outcome_to_stdout_json(outcome))
    _sys.exit(outcome.exit_code)


def _try_dispatch_remote_send(name: str, prompt: str) -> bool:
    """POST a turn to ``name`` on a remote peer via /v1/turn.

    Looks up the agent's active row in ``state.db.instances``; when
    ``host != current_host``, builds ``ssh://<host>:<a2a_port>/v1/turn``
    and lets :func:`post_turn_to_url` dispatch it through the ssh
    control plane (the same path ``sac peer post-turn`` already uses
    for ssh-as-transport).

    Returns:
        * ``True`` when the dispatch happened (reply printed to stdout).
        * ``False`` when no remote row exists (caller proceeds local).

    Raises:
        _RemoteA2APortMissingError: When the row has no ``a2a_port``
            recorded. This is a sharp failure surface — without a port
            we cannot reach the peer's /v1/turn, and silently falling
            back to local would mis-target the prompt (run claude with
            no session on the lead).
        click.ClickException: When the underlying HTTP / ssh call
            fails. We wrap the PeerError so the user sees the same
            error shape they get from ``sac peer post-turn``.
    """
    from .._network.peer import PeerError, post_turn_to_url
    from .lifecycle._dispatch import lookup_remote_peer

    found = lookup_remote_peer(name)
    if found is None:
        return False
    peer, row = found
    a2a_port = row.get("a2a_port")
    if not isinstance(a2a_port, int) or a2a_port <= 0:
        raise _RemoteA2APortMissingError(
            f"agent {name!r} is active on peer {peer!r} but state.db "
            f"records no a2a_port for it (a2a_port={a2a_port!r}). The "
            f"remote agent did not register an A2A port; cannot send. "
            f"Restart the agent on the peer with spec.a2a.port set, or "
            f"shell into the peer and run `sac agent send {name} ...` "
            f"directly."
        )
    url = f"ssh://{peer}:{a2a_port}/v1/turn"
    click.echo(f"# send {name}: POST {url}", err=True)
    try:
        reply = post_turn_to_url(url, prompt)
    except PeerError as exc:
        raise click.ClickException(f"remote send failed: {exc}") from exc
    click.echo(reply)
    return True


def _try_dispatch_local_send(name: str, prompt: str) -> bool:
    """POST a turn to a LOCAL agent via its loopback /v1/turn endpoint.

    Symmetric to :func:`_try_dispatch_remote_send`: when ``name`` has an
    active ``state.db.instances`` row on the *current* host with a bound
    ``a2a_port``, build ``http://127.0.0.1:<port>/v1/turn`` and POST one
    turn so the running SDK runner re-uses its in-process Claude session.

    This is the fix for the local mis-target diagnosed 2026-05-22: an
    apptainer agent's SDK session lives inside the container's
    ``~/.claude/projects/`` store, NOT on the host, so a host-side
    ``claude --resume <sid>`` cannot see it and exits 1 with "No
    conversation found". The HTTP path reaches the live in-container
    session instead.

    Returns:
        * ``True`` when the dispatch happened (reply printed to stdout).
        * ``False`` when there is no active local row, or the row exists
          but records no ``a2a_port`` (a non-A2A runtime) — the caller
          then falls through to the ``claude --resume`` host shellout,
          which only makes sense for a non-containerized host-side agent.

    Raises:
        click.ClickException: When the underlying HTTP call fails. We
            wrap the PeerError so the user sees the same error shape they
            get from ``sac peer post-turn``.
    """
    from .._network.peer import PeerError, post_turn_to_url
    from .._state.state_db import _resolve_host, list_active_instances

    current_host = _resolve_host(None)
    rows = list_active_instances()
    matching = [
        r
        for r in rows
        if r.get("name") == name and str(r.get("host") or "") == current_host
    ]
    if not matching:
        return False
    a2a_port = matching[0].get("a2a_port")
    if not isinstance(a2a_port, int) or a2a_port <= 0:
        # No bound A2A port: this is a non-A2A runtime. Let the caller
        # fall through to the host-side claude --resume path.
        return False
    url = f"http://127.0.0.1:{a2a_port}/v1/turn"
    click.echo(f"# send {name}: POST {url}", err=True)
    try:
        reply = post_turn_to_url(url, prompt)
    except PeerError as exc:
        raise click.ClickException(f"local send failed: {exc}") from exc
    click.echo(reply)
    return True


def _find_claude_binary() -> str:
    """Locate the ``claude`` CLI binary, preferring the SDK's bundled
    copy under ``/opt/venv-sac/...`` (when running inside the sac
    apptainer image) and falling back to ``$PATH``."""
    bundled = (
        "/opt/venv-sac/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude"
    )
    if os.path.isfile(bundled) and os.access(bundled, os.X_OK):
        return bundled
    found = shutil.which("claude")
    if found:
        return found
    raise click.ClickException(
        "claude binary not found on PATH and no bundled SDK copy at "
        f"{bundled}. Install claude-agent-sdk or put claude on PATH."
    )


# Cancel keys keep their historical interrupt semantics: instead of
# being typed into the TUI they SIGINT the runner pid, cancelling the
# current turn. Every OTHER key goes through tmux send-keys.
_CANCEL_KEYS = frozenset({"ESC", "C-c", "SIGINT"})


def _interrupt_via_sigint(name: str) -> None:
    """SIGINT the agent's recorded runner pid (cancel the current turn).

    The historical ESC / C-c / SIGINT semantics: cancel the turn rather
    than typing the literal key into the TUI. Fails loud when no pid
    file exists (agent not running) or the kill fails.
    """
    import signal as _signal

    state_dir = state_dir_for(name)
    pid_file = state_dir / "pid"
    if not pid_file.is_file():
        raise click.ClickException(
            f"No pid file at {pid_file} — agent {name!r} not running."
        )
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, _signal.SIGINT)
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"# interrupt {name}: SIGINT → pid={pid}", err=True)


def _send_keys_to_session(name: str, tmux_keys: list[str]) -> None:
    """Deliver validated tmux key names into the agent's tmux session.

    Resolves the agent's ``screen_name`` (its tmux session) from its
    config and routes the keys through the configured multiplexer's
    ``send_keys`` — the SAME send-keys primitive the TUI runner uses,
    so Enter / arrows / Tab / digits land in the live TUI.
    """
    spec_path = resolve_config(name)
    cfg = load_config(spec_path)
    mux = get_multiplexer(cfg)
    if not mux.exists(cfg.screen_name):
        raise click.ClickException(
            f"No tmux session {cfg.screen_name!r} for agent {name!r} "
            "— is it running under the tmux/TUI runtime?"
        )
    mux.send_keys(cfg.screen_name, *tmux_keys)
    click.echo(
        f"# keys {name}: {' '.join(tmux_keys)} → tmux {cfg.screen_name}",
        err=True,
    )


def _deliver_keys(name: str, *, key: str | None, keys: str | None) -> None:
    """Route ``--key`` / ``--keys`` to SIGINT (cancel) or tmux send-keys.

    A single ``--key`` that is one of :data:`_CANCEL_KEYS` keeps the
    historical interrupt path (SIGINT the runner pid). Everything else
    — a non-cancel ``--key`` or any ``--keys`` sequence — is validated
    against the tmux key vocabulary and delivered to the agent's tmux
    session. Validation fails loud (lists the valid set) BEFORE any
    partial send.
    """
    from .._runners._tmux._keys import (
        UnknownKeyError,
        parse_key_sequence,
        validate_keys,
    )

    if key is not None and key in _CANCEL_KEYS:
        _interrupt_via_sigint(name)
        return

    tokens = [key] if key is not None else parse_key_sequence(keys or "")
    try:
        tmux_keys = validate_keys(tokens)
    except (UnknownKeyError, ValueError) as exc:
        raise click.UsageError(str(exc)) from exc
    _send_keys_to_session(name, tmux_keys)


@click.command(name="send")
@click.argument("name", shell_complete=agent_name_complete)
@click.argument("prompt", required=False)
@click.option(
    "--model",
    default=None,
    help="Override the model for this turn only (e.g. ``opus``, ``sonnet``).",
)
@click.option(
    "--max-turns",
    type=int,
    default=None,
    help="Cap autonomous turns within this send. Default: claude's own default.",
)
@click.option(
    "--key",
    default=None,
    help=(
        "Send one key instead of a prompt (tmux-style, e.g. ``Enter``, "
        "``Up``, ``Tab``, ``1``, ``ESC``, ``C-c``). Cancel keys "
        "(ESC / C-c / SIGINT) interrupt the turn; every other key is "
        "delivered to the agent's tmux session. Mutually exclusive with "
        "PROMPT and --keys."
    ),
)
@click.option(
    "--keys",
    "keys",
    default=None,
    help=(
        "Send a whitespace-separated SEQUENCE of keys to the agent's "
        'tmux session (e.g. ``--keys "Up Up Enter"`` or ``--keys "2 '
        'Enter"``). Mutually exclusive with PROMPT and --key.'
    ),
)
@click.option(
    "--no-stream",
    is_flag=True,
    default=False,
    help="Buffer the response and print at the end instead of streaming.",
)
@click.argument("forward", nargs=-1, type=click.UNPROCESSED)
def send(
    name: str,
    prompt: str | None,
    model: str | None,
    max_turns: int | None,
    key: str | None,
    keys: str | None,
    no_stream: bool,
    forward: tuple[str, ...],
) -> None:
    """Send a follow-up PROMPT (or key/keys) to an agent's existing
    Claude session.

    \b
    Examples:
      sac agent send coverage-runner "now bump the threshold to 95%"
      sac agent send coverage-runner --key ESC
      sac agent send coverage-runner --key Enter
      sac agent send coverage-runner --keys "Up Up Enter"
      sac agent send coverage-runner -- --model opus --max-turns 3 "..."

    ``--key`` / ``--keys`` deliver tmux key names (Enter, Up, Down,
    Left, Right, Tab, BTab, Space, Home, End, PageUp, PageDown,
    Escape, C-c, …) plus literal chars/digits straight into the
    agent's tmux session. The cancel keys (ESC / C-c / SIGINT) instead
    interrupt the current turn via SIGINT.

    Anything after a literal ``--`` is forwarded verbatim to ``claude``
    (the raw escape hatch documented in SAC_OROCHI_SCOPES.md §1).
    """
    _passed = [v for v in (prompt, key, keys) if v]
    if len(_passed) > 1:
        raise click.UsageError(
            "PROMPT, --key and --keys are mutually exclusive."
        )
    if not _passed:
        raise click.UsageError("Either PROMPT, --key or --keys is required.")

    # PR-3 — in-SIF auto-fallback. When inside an apptainer SIF and
    # sending a PROMPT (the ``--key`` SIGINT path needs local pid
    # access and is excluded), auto-proxy to ``POST /agents/<name>/send``
    # on the host listen. The host's existing lineage-scoped ACL gate
    # (already wired into node_message_send + the per-agent send
    # surface) enforces caller permission. Outcome JSON + exit code
    # follow the same Checkpoint 2 contract as the other in-SIF verbs.
    from .._lifecycle._in_sif_broker import is_in_sif

    if is_in_sif() and prompt and not key and not keys:
        _send_via_host_listen(
            name=name,
            prompt=prompt,
            model=model,
            max_turns=max_turns,
        )
        return  # noreturn — _send_via_host_listen sys.exits
    if key or keys:
        _deliver_keys(name, key=key, keys=keys)
        return

    # Cross-host: when the agent's active state.db.instances row lives
    # on a peer, POST one turn to the peer's /v1/turn endpoint over the
    # ssh control plane and short-circuit before the local resume path.
    # Architectural choice: prompt-style sends go through A2A (HTTP) so
    # the running runner re-uses its in-process Claude session, not via
    # a separate `claude --resume` shellout that would race the runner.
    if _try_dispatch_remote_send(name, prompt):
        return

    # Local A2A: when the agent is running on THIS host with a bound
    # a2a_port, POST one turn to its loopback /v1/turn so the live
    # in-container SDK session handles it. A host-side `claude --resume`
    # cannot see a containerized agent's session (it lives inside the
    # container's ~/.claude/projects/ store), so the HTTP path is the
    # only correct local delivery for an apptainer runtime. Falls
    # through to claude --resume only when no a2a_port is recorded
    # (non-A2A, host-side runtime).
    if _try_dispatch_local_send(name, prompt):
        return

    spec_path = resolve_config(name)
    cfg = load_config(spec_path)
    state_dir = state_dir_for(name)
    sid = read_session_id(state_dir)
    if not sid:
        raise click.ClickException(
            f"No session_id recorded for agent {name!r} at "
            f"{state_dir / 'session_id'}. Has the agent run at least once?"
        )

    workdir = cfg.expanded_workdir or os.getcwd()
    claude_bin = _find_claude_binary()

    argv = [claude_bin, "--resume", sid, "-p", prompt]
    if model:
        argv += ["--model", model]
    if max_turns is not None:
        argv += ["--max-turns", str(max_turns)]
    if not no_stream:
        argv += ["--output-format", "stream-json", "--include-partial-messages"]
    if forward:
        argv += list(forward)

    click.echo(
        f"# resume {name}: session={sid[:8]}… workdir={workdir}",
        err=True,
    )
    try:
        rc = subprocess.call(argv, cwd=workdir)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    sys.exit(rc)
