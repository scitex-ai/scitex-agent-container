"""Per-agent MCP config FILE — keep secret material out of the child argv.

THE DEFECT THIS MODULE CLOSES
=============================
``claude-agent-sdk`` serialises ``ClaudeAgentOptions.mcp_servers`` when it
builds the ``claude`` child's command line
(``_internal/transport/subprocess_cli.py``)::

    if isinstance(self._options.mcp_servers, dict):
        cmd.extend(["--mcp-config", json.dumps({"mcpServers": servers_for_cli})])

That JSON embeds **every server entry's ``env`` block**, values included. On
Linux ``/proc/<pid>/cmdline`` is world-readable while ``/proc/<pid>/environ``
is restricted to the owning uid — so serialising an env block into argv takes
a value the kernel was protecting and publishes it to every local user. A
plain ``ps -eo args`` on scitex-compute-04 (2026-08-14) printed a live
Telegram bot token exactly this way (card
``sac-bot-token-plaintext-in-process-argv-20260814``).

This is NOT one agent's or one secret's problem. Two independent mechanisms
put resolved literals into those env blocks for EVERY agent:

  * :func:`._sdk_channels.merge_home_mcp_servers` →
    :func:`._sdk_channels._resolve_env_refs_local` expands ``${CCT_BOT_TOKEN}``
    (and any other ``${VAR}``) out of ``$HOME/.mcp.json`` into a literal;
  * :func:`._mcp_spec_env.bake_spec_env_into_servers` bakes the whole
    ``SAC_SPEC_ENV_KEYS`` manifest into **every** stdio entry's ``env``.

So the exposed set is "every value of every spec-env key, plus every
``${VAR}`` an agent's ``.mcp.json`` references" — an open-ended list that no
per-secret patch can bound.

THE FIX
=======
The SDK's own ``else`` branch passes a ``str``/``Path`` through verbatim::

    cmd.extend(["--mcp-config", str(self._options.mcp_servers)])

and ``claude --mcp-config <path>`` loads a file in the ordinary ``.mcp.json``
shape. So writing the SAME assembled config to a per-agent file with mode
0600 and handing over the PATH changes nothing about what ``claude`` loads
while removing the argv exposure entirely — and it removes it for every
current and future secret KIND at once, because argv stops carrying values
at all.

WHY NOT RESOLVE-IN-THE-CHILD
============================
Letting the child re-resolve its own secret (the ``CCT_BOT_TOKEN_<SLOT>``
pool, so only the slot NAME travels) is the narrower and more elegant-looking
option, but it is wrong here on two counts. It addresses one secret KIND
while the mechanism leaks all of them; and the values are baked into the
``env`` block precisely BECAUSE inheritance is not reachable there — the MCP
stdio transport respawns a reconnecting server with a sanitised allowlist
(``HOME``/``LOGNAME``/``PATH``/``SHELL``/``TERM``/``USER``) plus the entry's
own ``env``, so a value that is not in the block is simply gone mid-session.
Removing it would re-open card
``sac-env-injection-lost-on-mcp-reconnect-20260721``. The file keeps the bake
intact and moves only the TRANSPORT.

IN-PROCESS SERVERS
==================
``type: "sdk"`` entries carry a live Python object (``instance``) and cannot
be serialised to a file at all; the SDK must keep receiving them as a dict.
sac never builds one today, so :func:`externalize_mcp_servers` declines the
rewrite when it sees one rather than crashing an agent that starts using
them. Nothing is lost by declining: an in-process server has no ``env`` block
to leak.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

#: Directory (under the in-container ``$HOME``) holding per-agent MCP configs.
#: Created 0700 — the file itself is 0600, the directory keeps the *names*
#: private too.
MCP_CONFIG_DIRNAME = ".sac/mcp"

#: Mode for the config file. It holds resolved secret literals, so it must be
#: readable by the owning uid only — the whole point of moving off argv.
MCP_CONFIG_FILE_MODE = 0o600
MCP_CONFIG_DIR_MODE = 0o700

#: ``type`` values naming an in-process (non-serialisable) MCP server.
_INPROCESS_TYPES = frozenset({"sdk"})

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


class McpConfigWriteError(RuntimeError):
    """No writable location was found for the per-agent MCP config file.

    Raised instead of silently falling back to the dict form: that fallback
    would put every server's env block back on the world-readable child argv,
    which is the defect this module exists to remove. A loud boot failure is
    strictly better than a quiet secret disclosure.
    """


def has_inprocess_servers(servers: Mapping[str, Any] | None) -> bool:
    """True when any entry is an in-process (``type: "sdk"``) server."""
    if not servers:
        return False
    return any(
        isinstance(entry, dict)
        and str(entry.get("type") or "").lower() in _INPROCESS_TYPES
        for entry in servers.values()
    )


def _slug(agent_name: str) -> str:
    """Filesystem-safe stem for ``agent_name`` (never empty)."""
    slug = _SLUG_RE.sub("-", str(agent_name or "").strip()).strip("-")
    return slug or "agent"


def _candidate_dirs() -> list[Path]:
    """Writable-directory candidates, most preferred first.

    ``$HOME`` is the agent's own (per-agent overlay) home inside the
    container, so a config there is already isolated to this agent; the
    tempdir is the fallback for a read-only or unset home.
    """
    out: list[Path] = []
    home = os.environ.get("HOME")
    if home:
        out.append(Path(home) / MCP_CONFIG_DIRNAME)
    out.append(Path(tempfile.gettempdir()) / "sac-mcp")
    return out


def write_mcp_config_file(
    agent_name: str,
    servers: Mapping[str, Any],
    *,
    dirs: list[Path] | None = None,
) -> str:
    """Write ``{"mcpServers": servers}`` to a 0600 per-agent file; return its path.

    The file is created with the restrictive mode BEFORE any content is
    written (``os.open`` with ``O_CREAT|O_EXCL``-free truncation plus an
    explicit ``chmod``), so a pre-existing world-readable file from an older
    sac is tightened rather than reused as-is.

    Raises :class:`McpConfigWriteError` when no candidate directory accepts
    the write.
    """
    payload = json.dumps({"mcpServers": dict(servers)}, indent=2) + "\n"
    name = f"{_slug(agent_name)}.mcp.json"
    errors: list[str] = []
    for directory in dirs if dirs is not None else _candidate_dirs():
        path = directory / name
        try:
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, MCP_CONFIG_DIR_MODE)
            fd = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                MCP_CONFIG_FILE_MODE,
            )
            try:
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
            # An inherited file predating this fix may be 0644; O_CREAT's mode
            # only applies on creation, so tighten unconditionally.
            os.chmod(path, MCP_CONFIG_FILE_MODE)
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            continue
        logger.debug(
            "mcp config externalised for agent %r: %d server(s) → %s (mode 0600)",
            agent_name,
            len(servers),
            path,
        )
        return str(path)
    raise McpConfigWriteError(
        f"cannot write the MCP config file for agent {agent_name!r}; tried: "
        + "; ".join(errors)
        + ". Refusing to pass the config as an inline --mcp-config JSON "
        "argument instead: that argument is visible to every local user via "
        "/proc/<pid>/cmdline and would disclose every secret in every MCP "
        "server's env block (card "
        "sac-bot-token-plaintext-in-process-argv-20260814). Fix: make $HOME "
        "or the temp dir writable inside the container, then restart."
    )


def externalize_mcp_servers(kwargs: dict, agent_name: str) -> bool:
    """Replace ``kwargs['mcp_servers']`` dict with a 0600 file PATH.

    The last step of ``build_sdk_options`` — it must run AFTER every entry is
    assembled and after the spec-env bake, so the file carries exactly what
    the inline JSON would have carried.

    Returns True when the rewrite happened. No-op (returns False) when there
    are no servers, when the value is already a path/string, or when any
    entry is an in-process ``type: "sdk"`` server (not serialisable, and
    carrying no env block to leak).
    """
    servers = kwargs.get("mcp_servers")
    if not servers or not isinstance(servers, dict):
        return False
    if has_inprocess_servers(servers):
        logger.debug(
            "mcp config NOT externalised for agent %r: an in-process "
            "(type: sdk) server is present, which cannot be serialised to a "
            "file. In-process servers carry no env block, so no secret is "
            "exposed by the inline form.",
            agent_name,
        )
        return False
    kwargs["mcp_servers"] = write_mcp_config_file(agent_name, servers)
    return True


def read_mcp_servers(value: Any) -> dict[str, Any]:
    """Return the effective ``mcpServers`` table behind ``value``.

    ``ClaudeAgentOptions.mcp_servers`` is a PATH after
    :func:`externalize_mcp_servers` runs, so anything that wants to inspect
    what ``claude`` will actually load — tests, diagnostics — must go through
    the file. Accepts either shape: a dict is returned as-is (in-process
    servers, or a pre-externalisation call), a path/str is read back.
    """
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    raw = json.loads(Path(str(value)).read_text(encoding="utf-8"))
    servers = raw.get("mcpServers") if isinstance(raw, dict) else None
    return dict(servers) if isinstance(servers, dict) else {}


__all__ = [
    "MCP_CONFIG_DIRNAME",
    "MCP_CONFIG_DIR_MODE",
    "MCP_CONFIG_FILE_MODE",
    "McpConfigWriteError",
    "externalize_mcp_servers",
    "has_inprocess_servers",
    "read_mcp_servers",
    "write_mcp_config_file",
]
