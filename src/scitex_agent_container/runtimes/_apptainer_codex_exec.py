"""In-container exec shim for the ``codex`` TUI (see ``_apptainer_inner_argv_codex``).

Runs INSIDE the agent container as the tail of the pane's ``bash -lc``: it
resolves the ``codex`` binary from the image's own venv, translates the MCP
server files the Claude TUI receives as ``--mcp-config`` flags into Codex's
``-c mcp_servers.*`` overrides, and ``execv``s the binary in place — so the
pane holds ``codex`` itself, not a python parent.

    python3 -m scitex_agent_container.runtimes._apptainer_codex_exec \\
        [--mcp-config PATH]... [--mcp-json JSON]... -- <codex args...>

Why here and not on the host: ``~/.mcp.json`` and the inline channel JSON
exist only in the container, and ``codex_cli_bin.bundled_codex_path()`` on
the host names the HOST venv, which is not the path inside the image.

Codex's MCP shape (``codex mcp add`` writes the same keys):
``mcp_servers.<name>.command`` / ``.args`` / ``.env`` for stdio servers,
``mcp_servers.<name>.url`` (+ ``.bearer_token_env_var``) for streamable
HTTP. Claude's ``.mcp.json`` uses ``mcpServers.<name>.{command,args,env}``
and ``{type: "http", url, headers}``; the translation below covers both.
Diagnostics go through sac's logger (the house rule for ``src/``); they land
on the pane's stderr, which the boot-stderr log captures.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .._logging import get_logger

__all__ = ["main", "mcp_overrides", "resolve_codex_binary"]

_log = get_logger(__name__)

#: An operator-set absolute path wins; the bundled binary is the default.
CODEX_BIN_ENV = "SAC_CODEX_BIN"


def resolve_codex_binary() -> str:
    override = os.environ.get(CODEX_BIN_ENV, "").strip()
    if override:
        return override
    try:
        from codex_cli_bin import bundled_codex_path

        return str(bundled_codex_path())
    except Exception:  # noqa: BLE001 — the fallback names the gap loudly below
        return "codex"


def _toml(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    if isinstance(value, dict):
        return (
            "{ "
            + ", ".join(f"{json.dumps(str(k))} = {_toml(v)}" for k, v in value.items())
            + " }"
        )
    return json.dumps(str(value))


def _servers(document: object) -> dict[str, dict]:
    if not isinstance(document, dict):
        return {}
    servers = document.get("mcpServers", document)
    return (
        {str(name): entry for name, entry in servers.items() if isinstance(entry, dict)}
        if isinstance(servers, dict)
        else {}
    )


def mcp_overrides(documents: list[object]) -> list[str]:
    """``-c mcp_servers.<name>.<field>=<toml>`` flags for every server given."""
    flags: list[str] = []
    for document in documents:
        for name, entry in _servers(document).items():
            key = f"mcp_servers.{name}"
            url = entry.get("url")
            if url:
                flags += ["-c", f"{key}.url={_toml(url)}"]
                headers = entry.get("headers") or {}
                token_env = entry.get("bearer_token_env_var")
                if token_env:
                    flags += ["-c", f"{key}.bearer_token_env_var={_toml(token_env)}"]
                elif headers:
                    flags += ["-c", f"{key}.http_headers={_toml(headers)}"]
                continue
            command = entry.get("command")
            if not command:
                continue
            flags += ["-c", f"{key}.command={_toml(command)}"]
            args = entry.get("args") or []
            if args:
                flags += ["-c", f"{key}.args={_toml(list(args))}"]
            env = entry.get("env") or {}
            if env:
                flags += ["-c", f"{key}.env={_toml(dict(env))}"]
    return flags


def _load_documents(paths: list[str], inline: list[str]) -> list[object]:
    documents: list[object] = []
    for path in paths:
        p = Path(path)
        if not p.is_file():
            _log.warning("codex-exec: mcp config %s is absent; skipping", path)
            continue
        try:
            documents.append(json.loads(p.read_text(encoding="utf-8")))
        except ValueError as exc:
            _log.warning(
                "codex-exec: mcp config %s is not JSON (%s); skipping", path, exc
            )
    for text in inline:
        try:
            documents.append(json.loads(text))
        except ValueError as exc:
            _log.warning("codex-exec: inline mcp json is not JSON (%s); skipping", exc)
    return documents


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    paths: list[str] = []
    inline: list[str] = []
    while args and args[0] != "--":
        flag = args.pop(0)
        if flag == "--mcp-config" and args:
            paths.append(args.pop(0))
        elif flag == "--mcp-json" and args:
            inline.append(args.pop(0))
        else:
            _log.error("codex-exec: unexpected argument %r", flag)
            return 2
    if args and args[0] == "--":
        args.pop(0)
    binary = resolve_codex_binary()
    codex_argv = [binary, *args, *mcp_overrides(_load_documents(paths, inline))]
    try:
        os.execv(binary, codex_argv)
    except OSError as exc:
        _log.error(
            "codex-exec: cannot exec %r: %s. Set %s to the codex binary, or install "
            "openai-codex-cli-bin in the image venv.",
            binary,
            exc,
            CODEX_BIN_ENV,
        )
        return 127
    return 0  # pragma: no cover — execv does not return


if __name__ == "__main__":
    raise SystemExit(main())
