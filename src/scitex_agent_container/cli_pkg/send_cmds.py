"""``sac agent send`` — resume an agent's session for one more turn.

Wraps ``claude --resume <session-id> -p "<prompt>"`` so the caller
doesn't need to know the session id or the workdir. Reads the
session id from ``~/.scitex/agent-container/runtime/<name>/session_id``
(persisted by the SDK runner) and ``cd``s into the agent's workdir
before shelling out so claude's per-project session lookup resolves.

v1 scope: bare-CLI passthrough. The follow-up implementation order in
``GITIGNORED/SAC_OROCHI_SCOPES.md`` then exposes this through
``sac listen`` + ``POST /v1/agents/<name>/send``.
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
