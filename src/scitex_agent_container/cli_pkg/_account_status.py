"""Helpers for ``sac accounts status`` — one-shot quota snapshot.

Split out of ``account_group.py`` so the click command stays thin and so
that the core helpers (``_collect_status``, ``_format_status_prose``,
``_collect_status_remote``) can be unit-tested directly without going
through ``CliRunner``.

Design rules:

* No silent fallback.  Every recoverable failure raises
  :class:`StatusError` carrying a human-readable message; the CLI layer
  converts that into an exit-1 with the message echoed to stderr.

* Remote dispatch (``--host``) reuses
  :func:`scitex_agent_container._state.host_config.build_ssh_argv` so
  ``env_preamble`` is honored (Spartan needs Lmod before ``sac`` is on
  $PATH).

* Test-injection seams are spelled as ``fetch_fn``/``meta_fn`` keyword
  arguments so the helpers can be exercised without monkeypatching the
  underlying production functions (see STX-NM002).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


class StatusError(RuntimeError):
    """Raised when a quota status snapshot cannot be produced.

    Carrying a dedicated exception (rather than a bare ``RuntimeError``)
    lets the CLI layer distinguish "expected, render to the user as a
    one-line error and exit 1" from genuinely unexpected bugs. No silent
    fallback — every caller must either propagate or convert to a loud
    failure.
    """


def collect_status(
    home: Path | None = None,
    *,
    fetch_fn: Callable[..., dict[str, Any]] | None = None,
    meta_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Gather a one-shot quota snapshot for the local lead.

    Args:
        home: Optional override for the user home directory.
        fetch_fn: Callable returning the quota dict shape produced by
            :func:`scitex_agent_container._account.claude_usage.fetch_usage`.
            Test-injection seam; defaults to the real fetcher.
        meta_fn: Callable returning the safe credentials-metadata dict
            shape produced by
            :func:`scitex_agent_container._account.credentials.read_credentials_metadata`.
            Test-injection seam.

    Returns:
        Flat dict with keys: ``used_pct_5h``, ``used_pct_7d``,
        ``email_address``, ``rate_limit_tier``, ``fetched_at``,
        ``from_cache``.

    Raises:
        StatusError: When the credentials file is missing, the network
            fetch failed, or the API returned an error payload. The
            error message is suitable for direct display to the user.
    """
    _home = Path(home) if home is not None else Path.home()

    creds_path = _home / ".claude" / ".credentials.json"
    if not creds_path.is_file():
        raise StatusError(f"no claude credentials at {creds_path}")

    if fetch_fn is None:
        from .._account.claude_usage import fetch_usage as _fetch_usage

        fetch_fn = _fetch_usage
    if meta_fn is None:
        from .._account.credentials import (
            read_credentials_metadata as _read_credentials_metadata,
        )

        meta_fn = _read_credentials_metadata

    usage = fetch_fn(home=_home)
    err = usage.get("error")
    if err:
        raise StatusError(f"could not fetch quota: {err}")

    meta = meta_fn(home=_home)

    return {
        "used_pct_5h": usage.get("used_pct_5h"),
        "used_pct_7d": usage.get("used_pct_7d"),
        "email_address": meta.get("email_address"),
        "rate_limit_tier": meta.get("rate_limit_tier"),
        "fetched_at": usage.get("fetched_at"),
        "from_cache": bool(usage.get("from_cache")),
    }


def format_status_prose(snapshot: dict[str, Any]) -> str:
    """Render the ``collect_status`` dict as three human-readable lines."""

    def _pct(value: Any) -> str:
        if value is None:
            return "  -.--%"
        return f"{float(value):5.1f}%"

    def _str(value: Any) -> str:
        if value is None:
            return "-"
        return str(value)

    return (
        f"5h usage:  {_pct(snapshot.get('used_pct_5h'))}\n"
        f"7d usage:  {_pct(snapshot.get('used_pct_7d'))}\n"
        f"account:   {_str(snapshot.get('email_address'))} "
        f"(tier: {_str(snapshot.get('rate_limit_tier'))})"
    )


def build_remote_status_argv(host: str) -> list[str]:
    """Return the ssh argv that runs ``sac accounts status --json`` on host.

    Splits out of :func:`collect_status_remote` so a test can assert
    the argv shape (and that ``build_ssh_argv`` was reached with the
    right peer name) without spawning a subprocess.

    Raises:
        StatusError: when ``host`` is not declared as a peer in
            ``~/.scitex/agent-container/config.yaml``.
    """
    from .._state.host_config import build_ssh_argv
    from .._state.host_config import load as _load_host_config

    config = _load_host_config()
    if host not in config.peers:
        raise StatusError(f"no such peer {host!r}; run `sac host list`")

    return build_ssh_argv(
        host,
        ["sac", "accounts", "status", "--json"],
        config.peers,
    )


def collect_status_remote(
    host: str,
    *,
    run_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """ssh to ``host``, run ``sac accounts status --json``, parse output.

    Args:
        host: Peer name from ``~/.scitex/agent-container/config.yaml``.
        run_fn: Test-injection seam for ``subprocess.run``. Must accept
            the same ``capture_output=True, text=True, check=False``
            kwargs and return an object with ``.returncode``, ``.stdout``,
            ``.stderr`` attributes.

    Returns:
        The remote snapshot dict (same shape as :func:`collect_status`).

    Raises:
        StatusError: on unknown peer, ssh non-zero exit, unparseable
            output, or non-object payload.
    """
    import json as _json

    argv = build_remote_status_argv(host)

    if run_fn is None:
        import subprocess

        run_fn = subprocess.run

    proc = run_fn(argv, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        raise StatusError(
            f"remote `sac accounts status` on {host!r} exited "
            f"{proc.returncode}: {stderr}"
        )

    try:
        snapshot = _json.loads(proc.stdout)
    except _json.JSONDecodeError as exc:
        raise StatusError(f"could not parse JSON from {host!r}: {exc}") from exc

    if not isinstance(snapshot, dict):
        raise StatusError(
            f"remote `sac accounts status --json` on {host!r} returned "
            f"non-object payload"
        )

    return snapshot
