"""``sac agent send`` — resume an agent's session for one more turn.

Wraps ``claude --resume <session-id> -p "<prompt>"`` so the caller
doesn't need to know the session id or the workdir. Reads the
session id from ``~/.scitex/agent-container/runtime/<name>/session_id``
(persisted by the SDK runner) and ``cd``s into the agent's workdir
before shelling out so claude's per-project session lookup resolves.

v1 scope: bare-CLI passthrough. The follow-up implementation order in
``GITIGNORED/SAC_OROCHI_SCOPES.md`` then exposes this through
``sac listen`` + ``POST /agents/<name>/send``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import click

from .._runners._session_state import read_session_id, state_dir_for
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
        "Send a control key instead of a prompt (tmux-style, e.g. ``ESC``, "
        "``C-c``). Mutually exclusive with PROMPT."
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
    no_stream: bool,
    forward: tuple[str, ...],
) -> None:
    """Send a follow-up PROMPT (or control key) to an agent's existing
    Claude session.

    \b
    Examples:
      sac agent send coverage-runner "now bump the threshold to 95%"
      sac agent send coverage-runner --key ESC
      sac agent send coverage-runner -- --model opus --max-turns 3 "..."

    Anything after a literal ``--`` is forwarded verbatim to ``claude``
    (the raw escape hatch documented in SAC_OROCHI_SCOPES.md §1).
    """
    if key and prompt:
        raise click.UsageError("--key is mutually exclusive with PROMPT.")
    if not key and not prompt:
        raise click.UsageError("Either PROMPT or --key is required.")
    if key:
        # ESC / C-c → SIGINT to the runner pid. Other keys are reserved
        # for a future tty-bridge implementation.
        if key not in ("ESC", "C-c", "SIGINT"):
            raise click.UsageError(
                f"--key {key!r} not supported. Only ESC / C-c / SIGINT are "
                "wired (cancel current turn). Use a prompt otherwise."
            )
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
        return

    # Cross-host: when the agent's active state.db.instances row lives
    # on a peer, POST one turn to the peer's /v1/turn endpoint over the
    # ssh control plane and short-circuit before the local resume path.
    # Architectural choice: prompt-style sends go through A2A (HTTP) so
    # the running runner re-uses its in-process Claude session, not via
    # a separate `claude --resume` shellout that would race the runner.
    if _try_dispatch_remote_send(name, prompt):
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
