"""``sac dev`` noun-group — developer/maintainer plumbing.

Currently exposes:

* ``rotate-github-secrets`` — sync local Anthropic auth env vars into
  this repo's GitHub Actions secret slots so CI runs against the same
  credentials the operator has locally. Honours the OAuth-vs-API
  prefix split (``sk-ant-oat-*`` → ``…_API_KEY_OAUTH`` slot;
  ``sk-ant-api-*`` → ``…_API_KEY`` slot).

Design constraints:

* Read-only by default — refuses to mutate without ``--yes/-y``
  (audit-cli §2; the rotate is destructive on the GitHub side).
* GitHub returns secret names + ``updated_at`` only — never values.
  "Inconsistent" therefore means *slot present but older than ~1 day*
  or *slot missing entirely*. Operators rotating mid-day can force
  with ``--force``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import click

from ._helpers import HelpRecursiveGroup

_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"

# Single GH Actions secret slot, matching the canonical local env var
# name. The runner inside CI (`provision_anthropic_auth`) auto-detects
# whether the value is OAuth (`sk-ant-oat*`) or an API key
# (`sk-ant-api*`) by prefix and routes accordingly, so we don't need a
# slot-per-form split in the workflow.
_ANTHROPIC_SLOT = "SAC_ANTHROPIC_API_KEY"


def _detect_repo() -> str:
    """Return ``owner/repo`` from the local git remote.

    Falls back to raising :class:`click.ClickException` so the message
    surfaces in the CLI rather than a stack trace.
    """
    try:
        out = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise click.ClickException(
            "could not read 'git remote get-url origin' — run inside the repo"
        ) from exc
    # Accept both git@github.com:owner/repo[.git] and https://github.com/owner/repo[.git]
    if out.startswith("git@"):
        path = out.split(":", 1)[1]
    elif "://" in out:
        path = out.split("://", 1)[1].split("/", 1)[1]
    else:
        path = out
    return path[:-4] if path.endswith(".git") else path


def _gh_list_secrets(repo: str) -> dict[str, str]:
    """Return ``{name: updated_at_iso}`` for the repo's Actions secrets."""
    raw = subprocess.check_output(
        [
            "gh",
            "api",
            f"repos/{repo}/actions/secrets",
            "--paginate",
        ],
        text=True,
    )
    payload = json.loads(raw)
    return {s["name"]: s["updated_at"] for s in payload.get("secrets", [])}


def _gh_set_secret(repo: str, name: str, value: str) -> None:
    proc = subprocess.run(
        ["gh", "secret", "set", name, "-R", repo, "--body", "-"],
        input=value,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise click.ClickException(
            f"gh secret set {name} failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )


def _classify_token(value: str) -> str:
    """Return a short label ('oauth' / 'api-key' / 'unknown') for the report."""
    if value.startswith("sk-ant-oat"):
        return "oauth"
    if value.startswith("sk-ant-api"):
        return "api-key"
    return "unknown"


def _age_days(updated_iso: str | None) -> float | None:
    if not updated_iso:
        return None
    dt = datetime.fromisoformat(updated_iso.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0


def _read_credentials_oauth_token(path: Path) -> str:
    """Return ``claudeAiOauth.accessToken`` from ``path``.

    Mirrors the bash bridge in
    ``~/.dotfiles/src/.bash.d/secrets/010_scitex/01_agent-container.src``
    (``jq -r '.claudeAiOauth.accessToken // empty'``) so that calling
    ``sac dev credential2apikey`` from a shell startup file produces
    the same value the legacy jq snippet did, without depending on jq.
    """
    if not path.is_file():
        raise click.ClickException(f"credentials file not found: {path}")
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"could not parse {path}: {exc}") from exc
    token = (payload.get("claudeAiOauth") or {}).get("accessToken")
    if not token:
        raise click.ClickException(
            f"{path} has no .claudeAiOauth.accessToken — run 'claude /login'?"
        )
    return token


def _resolve_local_token() -> tuple[str, str]:
    """Return (token, source) using the same precedence as the bash bridge.

    Order:
      1. ``SAC_ANTHROPIC_API_KEY`` env var (the canonical handoff name)
      2. OAuth ``accessToken`` in ``~/.claude/.credentials.json``
    """
    val = os.environ.get("SAC_ANTHROPIC_API_KEY")
    if val:
        return val, "env:SAC_ANTHROPIC_API_KEY"
    if _CREDENTIALS_PATH.is_file():
        return _read_credentials_oauth_token(_CREDENTIALS_PATH), str(_CREDENTIALS_PATH)
    raise click.ClickException(
        "no Anthropic auth found — set SAC_ANTHROPIC_API_KEY or run "
        "'claude /login' so ~/.claude/.credentials.json exists"
    )


@click.group(name="dev", cls=HelpRecursiveGroup)
def dev_group() -> None:
    """Developer / maintainer plumbing (CI secrets, etc.)."""


@dev_group.command(name="credential2apikey")
@click.option(
    "--path",
    "path",
    type=click.Path(path_type=Path),
    default=None,
    help="Override credentials file path (default: ~/.claude/.credentials.json).",
)
@click.option(
    "--export",
    "as_export",
    is_flag=True,
    default=False,
    help="Print 'export SAC_ANTHROPIC_API_KEY=...' shell snippet instead of the bare token.",
)
def credential2apikey(path: Path | None, as_export: bool) -> None:
    """Print the Anthropic OAuth access token from ~/.claude/.credentials.json.

    \b
    Replaces the legacy ``jq -r '.claudeAiOauth.accessToken'`` snippet
    in the bash bridge so shell startup doesn't depend on jq.
    Anthropic Pro/Max users get an OAuth bearer here (``sk-ant-oat-*``);
    pay-per-token operators should use a real API key (``sk-ant-api-*``)
    via ``SAC_ANTHROPIC_API_KEY`` instead — this command is OAuth-only.

    \b
    Examples:
      $ sac dev credential2apikey
      $ eval "$(sac dev credential2apikey --export)"
    """
    token = _read_credentials_oauth_token(path or _CREDENTIALS_PATH)
    if as_export:
        click.echo(f"export SAC_ANTHROPIC_API_KEY={token}")
    else:
        click.echo(token)


@dev_group.command(name="rotate-github-secrets")
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print what would be rotated without calling 'gh secret set'.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Confirm the rotation. Required when not in --dry-run.",
)
@click.option(
    "--force",
    "force",
    is_flag=True,
    default=False,
    help="Rotate even when the GitHub slot was updated within the last day.",
)
def rotate_github_secrets(dry_run: bool, yes: bool, force: bool) -> None:
    """Sync local Anthropic auth env vars into this repo's GitHub Actions secrets.

    \b
    Reads ``SAC_ANTHROPIC_API_KEY`` from the local environment (falling
    back to ``~/.claude/.credentials.json`` via the same logic as
    ``sac dev credential2apikey``) and pushes it to the GitHub secret
    of the same name on the current repo. The runner inside CI detects
    OAuth (``sk-ant-oat*``) vs api-key (``sk-ant-api*``) by prefix.

    \b
    Slot:
      SAC_ANTHROPIC_API_KEY  (single slot; runner detects oauth vs api-key by prefix)

    \b
    Examples:
      $ sac dev rotate-github-secrets --dry-run
      $ sac dev rotate-github-secrets --yes
    """
    if shutil.which("gh") is None:
        raise click.ClickException("'gh' CLI not found on PATH")

    local, source = _resolve_local_token()

    repo = _detect_repo()
    target_slot = _ANTHROPIC_SLOT
    kind = _classify_token(local)
    remote = _gh_list_secrets(repo)
    remote_age = _age_days(remote.get(target_slot))

    click.echo(f"repo:        {repo}")
    click.echo(f"source:      {source}")
    click.echo(f"local token: {kind} (length={len(local)}, prefix={local[:11]}...)")
    click.echo(f"target slot: {target_slot}")
    if target_slot in remote:
        age = "?" if remote_age is None else f"{remote_age:.1f} d"
        click.echo(f"remote slot: present (last updated {age} ago)")
    else:
        click.echo("remote slot: missing")

    fresh = remote_age is not None and remote_age < 1.0
    if fresh and not force:
        click.echo(
            "slot was updated within the last day — pass --force to override.",
            err=True,
        )
        if not dry_run:
            raise SystemExit(0)

    if dry_run:
        click.echo("[dry-run] would push local value to the slot above.")
        return

    if not yes:
        click.echo(
            "Refusing to rotate without --yes/-y (this overwrites the GitHub secret).",
            err=True,
        )
        raise SystemExit(2)

    _gh_set_secret(repo, target_slot, local)
    click.echo(f"rotated {target_slot} on {repo}")


__all__ = ["dev_group"]
