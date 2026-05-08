"""``sac dev`` noun-group — developer/maintainer plumbing.

Currently exposes:

* ``upload-apikey-from-credentials-to-github`` — sync local Anthropic auth env vars into
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


def _gh_set_variable(repo: str, name: str, value: str) -> None:
    """Create-or-update a repo Actions variable.

    Variables are world-readable to anyone with repo read; we only
    push a SHA256 hash of the secret value here, never anything that
    could be reversed back to the token. Used as a public fingerprint
    so :func:`upload_apikey_from_credentials_to_github` can show
    whether the GitHub-side secret matches the local one.
    """
    # ``gh variable set`` is idempotent: creates or updates.
    proc = subprocess.run(
        ["gh", "variable", "set", name, "-R", repo, "--body", value],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        raise click.ClickException(
            f"gh variable set {name} failed: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )


def _gh_get_variable(repo: str, name: str) -> str | None:
    """Return the variable value, or None if absent."""
    proc = subprocess.run(
        ["gh", "api", f"repos/{repo}/actions/variables/{name}"],
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout).get("value")
    except json.JSONDecodeError:
        return None


def _sha256(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _classify_token(value: str) -> str:
    """Return a short label ('oauth' / 'api-key' / 'unknown') for the report."""
    if value.startswith("sk-ant-oat"):
        return "oauth"
    if value.startswith("sk-ant-api"):
        return "api-key"
    return "unknown"


def _format_age(updated_iso: str | None) -> str:
    """Render a GitHub-secret ``updated_at`` timestamp as a human age.

    Picks the largest unit that gives a readable number: seconds for
    very fresh rotations, then minutes, hours, days. Mirrors the
    `git relative_date` style — a single value + unit, no compound
    "1d 4h" form.
    """
    if not updated_iso:
        return "?"
    dt = datetime.fromisoformat(updated_iso.replace("Z", "+00:00"))
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}min"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _read_credentials_oauth_token(path: Path) -> str:
    """Return ``claudeAiOauth.accessToken`` from ``path``.

    Mirrors the bash bridge in
    ``~/.dotfiles/src/.bash.d/secrets/010_scitex/01_agent-container.src``
    (``jq -r '.claudeAiOauth.accessToken // empty'``) so that calling
    ``sac dev extract-apikey-from-credentials`` from a shell startup file produces
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


@dev_group.command(name="extract-apikey-from-credentials")
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
def extract_apikey_from_credentials(path: Path | None, as_export: bool) -> None:
    """Print the Anthropic OAuth access token from ~/.claude/.credentials.json.

    \b
    Replaces the legacy ``jq -r '.claudeAiOauth.accessToken'`` snippet
    in the bash bridge so shell startup doesn't depend on jq.
    Anthropic Pro/Max users get an OAuth bearer here (``sk-ant-oat-*``);
    pay-per-token operators should use a real API key (``sk-ant-api-*``)
    via ``SAC_ANTHROPIC_API_KEY`` instead — this command is OAuth-only.

    \b
    Examples:
      $ sac dev extract-apikey-from-credentials
      $ eval "$(sac dev extract-apikey-from-credentials --export)"
    """
    token = _read_credentials_oauth_token(path or _CREDENTIALS_PATH)
    if as_export:
        click.echo(f"export SAC_ANTHROPIC_API_KEY={token}")
    else:
        click.echo(token)


@dev_group.command(name="upload-apikey-from-credentials-to-github")
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
def upload_apikey_from_credentials_to_github(dry_run: bool, yes: bool) -> None:
    """Sync local Anthropic auth env vars into this repo's GitHub Actions secrets.

    \b
    Reads ``SAC_ANTHROPIC_API_KEY`` from the local environment (falling
    back to ``~/.claude/.credentials.json`` via the same logic as
    ``sac dev extract-apikey-from-credentials``) and pushes it to the GitHub secret
    of the same name on the current repo. The runner inside CI detects
    OAuth (``sk-ant-oat*``) vs api-key (``sk-ant-api*``) by prefix.

    \b
    Slot:
      SAC_ANTHROPIC_API_KEY  (single slot; runner detects oauth vs api-key by prefix)

    \b
    Examples:
      $ sac dev upload-apikey-from-credentials-to-github --dry-run
      $ sac dev upload-apikey-from-credentials-to-github --yes
    """
    if shutil.which("gh") is None:
        raise click.ClickException("'gh' CLI not found on PATH")

    local, source = _resolve_local_token()

    repo = _detect_repo()
    target_slot = _ANTHROPIC_SLOT
    sha_var = f"{target_slot}_SHA256"
    kind = _classify_token(local)
    remote = _gh_list_secrets(repo)
    local_sha = _sha256(local)
    remote_sha = _gh_get_variable(repo, sha_var)

    click.echo(f"repo:        {repo}")
    click.echo(f"source:      {source}")
    # Show enough of the token to spot-check at a glance (prefix + a
    # tail fingerprint) without leaking the secret outright. Pattern
    # is the same as `gh secret list` and `git ls-remote` truncations.
    head = local[:24] if len(local) > 32 else local[: max(1, len(local) // 2)]
    tail = local[-4:] if len(local) > 32 else ""
    masked = f"{head}…{tail}" if tail else head
    click.echo(f"local token: {kind} (length={len(local)}, value={masked})")
    click.echo(f"local sha256: {local_sha}")
    click.echo(f"target slot: {target_slot}")
    if target_slot in remote:
        click.echo(
            f"remote slot: present (last updated {_format_age(remote[target_slot])} ago)"
        )
    else:
        click.echo("remote slot: missing")
    if remote_sha is None:
        click.echo(f"remote sha256: <not yet published as `{sha_var}` repo variable>")
        click.echo("match:       unknown (rotate with --yes to publish the hash)")
    else:
        click.echo(f"remote sha256: {remote_sha}")
        match = "yes" if remote_sha == local_sha else "NO — local differs from remote"
        click.echo(f"match:       {match}")

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
    # Publish the SHA256 as a public repo variable so future invocations
    # can detect drift between local and remote without a CI roundtrip.
    # Hash is irreversible; only the fingerprint is exposed.
    _gh_set_variable(repo, sha_var, local_sha)
    click.echo(f"rotated {target_slot} on {repo} (sha256 sidecar: {sha_var})")


__all__ = ["dev_group"]
