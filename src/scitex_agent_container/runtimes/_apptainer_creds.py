"""Per-agent OAuth credential resolution for the apptainer runtime.

Extracted from ``_apptainer_runtime.py`` (512-line cap) — mirrors the
existing helper-module split (``_apptainer_build``,
``_apptainer_listen_env``, ``_apptainer_iso_flags``).

The single public entry point :func:`resolve_cred_file` decides WHICH
host-side ``.credentials.json`` gets bound into an agent container:

* ``spec.claude.account`` empty → the host's live
  ``~/.claude/.credentials.json`` (shared OAuth — current default).
* ``spec.claude.account`` set → a FROZEN BOOT-COPY of that saved
  account's snapshot, copied into the agent's own state dir so two
  agents pinned to two accounts never fight one mount, and a host
  ``/login`` never moves a pinned agent.

The copy is bound ``:rw`` by the caller so the in-container Claude CLI
can refresh the OAuth ``accessToken`` (~1h cadence) on the agent's
private copy. Changing ``spec.claude.account`` only takes effect on the
next ``sac agent restart`` (the copy happens at start).

Fail-soft: any resolution / copy hiccup degrades to the host live file
(with a best-effort warning) so a stale spec never wedges a start.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..config import AgentConfig


def resolve_cred_file(config: AgentConfig, state_dir: Path) -> Path | None:
    """Return the host-side ``.credentials.json`` to bind for ``config``.

    Returns ``None`` only when the chosen file does not exist (caller
    skips the bind). See module docstring for the full decision table.
    """
    host_cred = Path.home() / ".claude" / ".credentials.json"
    acct = getattr(getattr(config, "claude", None), "account", "") or ""
    if not acct:
        return host_cred

    # stx-allow: fallback (reason: account-snapshot copy is the pinning
    # mechanism; any resolution/copy hiccup degrades to the host live
    # file so a start is never wedged — the warning surfaces the gap.)
    try:
        from .._state.account_store import _store_path

        store = _store_path(None, Path.home())
        snapshot = store / acct / ".credentials.json"
        if not snapshot.is_file():
            import warnings

            warnings.warn(
                f"spec.claude.account='{acct}' has no snapshot at "
                f"{snapshot}; falling back to host "
                "~/.claude/.credentials.json. Create it with "
                f"`sac account save {acct}` on the credential-holding "
                "host, then restart this agent.",
                stacklevel=2,
            )
            return host_cred

        dest = Path(state_dir).expanduser() / "claude" / ".credentials.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(snapshot, dest)
        return dest
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return host_cred
