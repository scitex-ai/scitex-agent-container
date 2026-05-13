"""``sac channel`` — agent-to-agent messaging on this host.

Step 3 of SAC_OROCHI_SCOPES.md §6. Local-only routing in v1: each
``sac channel send`` POSTs to the local ``sac listen`` at
``http://127.0.0.1:7878/v1/sac/agents/<to>/send`` with the message
wrapped in a ``<channel source="sac" from="<from>">…</channel>`` tag so
the receiving agent reads it as channel input (per claude's channel
protocol — see ~/.claude/skills/claude-code-official/03_runtime_03_channels.md).

Cross-host routing (sac → orochi → peer sac listen) is step 5; this
layer is the surface orochi will sit behind.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import click

from .._listen.tokens import default_token_path, read_token
from ._helpers import HelpRecursiveGroup, agent_name_complete


def _default_listen_url() -> str:
    return os.environ.get("SAC_LISTEN_URL", "http://127.0.0.1:7878")


def _post(url: str, body: dict, token: str, timeout: float = 60.0) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


@click.group(name="channel", cls=HelpRecursiveGroup)
def channel_group() -> None:
    """Agent-to-agent messaging (local v1; orochi routes cross-host)."""


@channel_group.command("send")
@click.argument("to_agent", shell_complete=agent_name_complete)
@click.argument("message")
@click.option(
    "--from",
    "from_agent",
    default="cli",
    help="Sender identity stamped on the channel tag.",
)
@click.option(
    "--listen-url",
    default=None,
    help="Override the sac listen URL. Default: $SAC_LISTEN_URL or 127.0.0.1:7878.",
)
@click.option(
    "--token-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Bearer-token file. Default: ~/.scitex/agent-container/tokens/listen-<host>.token.",
)
def send(
    to_agent: str,
    message: str,
    from_agent: str,
    listen_url: str | None,
    token_file: Path | None,
) -> None:
    """Deliver MESSAGE to TO_AGENT via local sac listen.

    \b
    Examples:
      sac channel send coverage-runner "found 3 untested branches in foo.py"
      sac channel send coverage-runner "..." --from quality-orchestrator
    """
    tok_path = token_file or default_token_path()
    token = read_token(tok_path)
    if not token:
        raise click.ClickException(
            f"No sac-listen token at {tok_path}. Run `sac listen` once to "
            "auto-generate it, or pass --token-file explicitly."
        )
    base = listen_url or _default_listen_url()
    url = f"{base.rstrip('/')}/v1/sac/agents/{to_agent}/send"

    # Wrap in the channel-tag shape claude consumes natively.
    wrapped = f'<channel source="sac" from="{from_agent}">{message}</channel>'
    try:
        result = _post(url, {"type": "prompt", "prompt": wrapped}, token=token)
    except urllib.error.HTTPError as exc:
        raise click.ClickException(
            f"sac listen returned {exc.code}: {exc.read().decode('utf-8', 'replace')}"
        )
    except urllib.error.URLError as exc:
        raise click.ClickException(
            f"sac listen at {base} unreachable: {exc.reason}. "
            "Is `sac listen` running on this host?"
        )

    click.echo(json.dumps(result, indent=2))
