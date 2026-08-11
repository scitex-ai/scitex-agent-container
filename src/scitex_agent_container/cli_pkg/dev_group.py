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

No-mocks seams (PA-306): ``_load_scitex_git`` is a real callable that
returns a backend exposing ``format_age``, ``get_variable``,
``list_secrets``, ``set_secret_with_sha_sidecar``, ``sha256_hex``.
Tests swap it for a hand-rolled real fake (same pattern as
``image_group._load_apptainer``). ``_credentials_path()`` is a
function (not a module-level constant) so it picks up the current
``$HOME`` at call time, letting tests redirect ``HOME`` to ``tmp_path``
without monkeypatching module globals.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import click

from .._env import getenv as _sac_env
from ._helpers import HelpRecursiveGroup


def _load_scitex_git() -> Any | None:
    """Return the ``scitex_git`` module (or a test-installed fake), or None.

    Lazy import seam — kept as a module-level callable so tests can
    swap it for a real-callable returning a hand-rolled fake (mirrors
    ``image_group._load_apptainer``). Production code calls this once
    per command invocation; the cost is one ``importlib`` lookup.
    """
    # scitex-git ships in the [dev] extra (see pyproject.toml). The
    # ``sac dev …`` commands need it for the gh-secret/variable wrappers
    # and sha256 sidecar; raise a clean message if a runtime install
    # without [dev] tries to invoke them.
    from scitex_dev import try_import_optional

    return try_import_optional("scitex_git", extra="dev", pkg="scitex-agent-container")


def _require_scitex_git() -> Any:
    """Return the loaded scitex-git backend or raise a clean ClickException."""
    backend = _load_scitex_git()
    if backend is None:
        raise click.ClickException(
            "`sac dev` needs the [dev] extra. Install with: "
            "pip install -e '.[dev]' (or pip install scitex-git>=0.1.3)."
        )
    return backend


def _credentials_path() -> Path:
    """Return ``~/.claude/.credentials.json`` resolved against the current ``$HOME``.

    A function (not a module-level constant) so it picks up the
    current ``$HOME`` env var on every call — lets tests redirect
    ``HOME`` to a tmpdir without monkeypatching module globals.
    """
    return Path.home() / ".claude" / ".credentials.json"


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


def _classify_token(value: str) -> str:
    """Return a short label ('oauth' / 'api-key' / 'unknown') for the report."""
    if value.startswith("sk-ant-oat"):
        return "oauth"
    if value.startswith("sk-ant-api"):
        return "api-key"
    return "unknown"


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
    val = _sac_env("ANTHROPIC_API_KEY")
    if val:
        return val, "env:SAC_ANTHROPIC_API_KEY"
    creds = _credentials_path()
    if creds.is_file():
        return _read_credentials_oauth_token(creds), str(creds)
    raise click.ClickException(
        "no Anthropic auth found — set SAC_ANTHROPIC_API_KEY or run "
        "'claude /login' so ~/.claude/.credentials.json exists"
    )


@click.group(name="dev", cls=HelpRecursiveGroup)
def dev_group() -> None:
    """Developer / maintainer plumbing (CI secrets, scheduled jobs, etc.)."""


# Federated scheduled-job subcommands (`sac dev {cron,systemd}`, derived
# from `_dev_jobs.GROUP_KINDS`) delegate to scitex-dev's ecosystem
# aggregator. Kept in their own module to hold this file under the
# per-file line cap; attached at import time.
from ._dev_jobs import register_dev_jobs_commands

register_dev_jobs_commands(dev_group)

# The one-time canonical-name cutover. Deliberately NOT a verb inside a
# kind group: it has no counterpart in scitex-dev's grammar and retires
# once every host is migrated, so exposing it as a kind verb would
# advertise a verb the ecosystem does not serve.
from ._dev_jobs_migrate import register_migrate_command

register_migrate_command(dev_group)


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
    token = _read_credentials_oauth_token(path or _credentials_path())
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
    backend = _require_scitex_git()
    if shutil.which("gh") is None:
        raise click.ClickException("'gh' CLI not found on PATH")

    local, source = _resolve_local_token()

    repo = _detect_repo()
    target_slot = _ANTHROPIC_SLOT
    sha_var = f"{target_slot}_SHA256"
    kind = _classify_token(local)
    remote = backend.list_secrets(repo)
    local_sha = backend.sha256_hex(local)
    remote_sha = backend.get_variable(repo, sha_var)

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
            f"remote slot: present (last updated {backend.format_age(remote[target_slot])} ago)"
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

    # Publish secret + SHA256 sidecar in one call. The sidecar is a
    # public repo variable so future invocations can detect drift
    # without a CI roundtrip. Hash is irreversible; only the
    # fingerprint is exposed.
    backend.set_secret_with_sha_sidecar(repo, target_slot, local)
    click.echo(f"rotated {target_slot} on {repo} (sha256 sidecar: {sha_var})")


_CREDENTIALS_SLOT = "SAC_CLAUDE_CODE_CREDENTIALS_JSON"


@dev_group.command(name="upload-credentials-to-github")
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print what would be uploaded without calling 'gh secret set'.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Confirm the upload. Required when not in --dry-run.",
)
def upload_credentials_to_github(dry_run: bool, yes: bool) -> None:
    """Push the FULL ~/.claude/.credentials.json as a GitHub Actions secret.

    \b
    The bare ``ANTHROPIC_API_KEY`` env path does NOT work for Pro/Max
    OAuth tokens (Anthropic rejects ``sk-ant-oat*`` bearers passed
    that way). The working pattern — proven by newb's CI — is to
    upload the entire credentials.json (with its real ``refreshToken``)
    as a secret named ``SAC_CLAUDE_CODE_CREDENTIALS_JSON``, and have the
    workflow materialise it back to ``~/.claude/.credentials.json``
    before launching the agent. ``container.py`` then bind-mounts the
    file into the container, the SDK reads it, and the OAuth flat-rate
    path works.

    \b
    Refresh after every ``claude /login`` (the access token rotates).

    \b
    Slot:
      SAC_CLAUDE_CODE_CREDENTIALS_JSON  (full file content, including refresh_token)

    \b
    Examples:
      $ sac dev upload-credentials-to-github --dry-run
      $ sac dev upload-credentials-to-github --yes
    """
    backend = _require_scitex_git()
    if shutil.which("gh") is None:
        raise click.ClickException("'gh' CLI not found on PATH")

    creds = _credentials_path()
    if not creds.is_file():
        raise click.ClickException(f"{creds} not found — run `claude /login` first.")
    content = creds.read_text()

    # Quick sanity: the file should parse as JSON with the expected
    # OAuth shape, otherwise we'd silently push a bogus secret.
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"{creds} is not valid JSON: {exc}") from exc
    if "claudeAiOauth" not in payload:
        raise click.ClickException(f"{creds} has no .claudeAiOauth key — wrong format?")

    repo = _detect_repo()
    sha_var = f"{_CREDENTIALS_SLOT}_SHA256"
    remote = backend.list_secrets(repo)
    local_sha = backend.sha256_hex(content)
    remote_sha = backend.get_variable(repo, sha_var)

    click.echo(f"repo:        {repo}")
    click.echo(f"source:      {creds}")
    click.echo(f"local size:  {len(content)} bytes")
    click.echo(f"local sha256: {local_sha}")
    click.echo(f"target slot: {_CREDENTIALS_SLOT}")
    if _CREDENTIALS_SLOT in remote:
        click.echo(
            f"remote slot: present (last updated "
            f"{backend.format_age(remote[_CREDENTIALS_SLOT])} ago)"
        )
    else:
        click.echo("remote slot: missing")
    if remote_sha is None:
        click.echo(f"remote sha256: <not yet published as `{sha_var}` repo variable>")
        click.echo("match:       unknown (upload with --yes to publish the hash)")
    else:
        click.echo(f"remote sha256: {remote_sha}")
        match = "yes" if remote_sha == local_sha else "NO — local differs from remote"
        click.echo(f"match:       {match}")

    if dry_run:
        click.echo("[dry-run] would push the credentials.json content above.")
        return

    if not yes:
        click.echo(
            "Refusing to upload without --yes/-y (this overwrites the GitHub secret).",
            err=True,
        )
        raise SystemExit(2)

    backend.set_secret_with_sha_sidecar(repo, _CREDENTIALS_SLOT, content)
    click.echo(f"uploaded {_CREDENTIALS_SLOT} on {repo} (sha256 sidecar: {sha_var})")


__all__ = ["dev_group"]
