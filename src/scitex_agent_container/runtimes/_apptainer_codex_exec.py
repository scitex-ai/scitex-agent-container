"""In-container exec shim for the ``codex`` TUI (see ``_apptainer_inner_argv_codex``).

Runs INSIDE the agent container as the tail of the pane's ``bash -lc``: it
resolves the ``codex`` binary from the image's own venv, translates the MCP
server files the Claude TUI receives as ``--mcp-config`` flags into Codex's
``-c mcp_servers.*`` overrides, and ``execv``s the binary in place — so the
pane holds ``codex`` itself, not a python parent.

    python3 -m scitex_agent_container.runtimes._apptainer_codex_exec \\
        [--mcp-config PATH]... [--mcp-json JSON]... [--hooks-from SETTINGS] \\
        -- <codex args...>

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
import re
import sys
from pathlib import Path

from .._logging import get_logger

__all__ = [
    "adapt_hook_commands",
    "inherited_env_names",
    "main",
    "mcp_overrides",
    "resolve_codex_binary",
    "split_env_placeholders",
    "write_hooks_from",
]

#: Codex reads its hooks from this file under CODEX_HOME (measured in the
#: 0.147 binary: "hooks/hooks.json", "failed to serialize hooks.json").
HOOKS_FILENAME = "hooks.json"

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


_PLACEHOLDER = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_EMBEDDED = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def split_env_placeholders(env: dict) -> tuple[dict[str, str], list[str]]:
    """Claude's ``${VAR}`` env placeholders, the way Codex can honour them.

    Claude Code expands ``${VAR}`` in a ``.mcp.json`` ``env`` map from its own
    environment; Codex passes the text through literally, and the first live
    codex pane showed the cost (handyman-01, 2026-09-05 09:36 UTC): the
    telegrammer server refused to start with "unexpanded ${...}
    placeholder(s) in env: CCT_AGENT_ID=${CCT_AGENT_ID} ...".

    A value that is exactly ``${NAME}`` becomes an ``env_vars`` entry —
    Codex forwards that variable BY NAME from the pane's environment, so the
    value (often a token) never lands in the argv. A value with a placeholder
    embedded in more text is expanded here from the shim's own environment
    (``${NAME:-default}`` honoured); a plain value stays a plain ``env``
    entry.
    """
    literal: dict[str, str] = {}
    forwarded: list[str] = []
    for name, value in env.items():
        text = str(value)
        whole = _PLACEHOLDER.match(text)
        if whole:
            forwarded.append(whole.group(1))
            continue
        if "${" in text:
            text = _EMBEDDED.sub(
                lambda m: os.environ.get(m.group(1), m.group(2) or ""), text
            )
        literal[str(name)] = text
    return literal, forwarded


def _servers(document: object) -> dict[str, dict]:
    if not isinstance(document, dict):
        return {}
    servers = document.get("mcpServers", document)
    return (
        {str(name): entry for name, entry in servers.items() if isinstance(entry, dict)}
        if isinstance(servers, dict)
        else {}
    )


#: Prefixes of the fleet variables an MCP server may read from its inherited
#: environment. Claude Code hands its own environment to every stdio MCP
#: server it spawns; Codex passes ONLY the declared ``env`` plus the names in
#: ``env_vars``, so a server that reads an inherited variable dies. Measured
#: on the first codex pane (handyman-01, 2026-09-05 10:10 UTC): the
#: telegrammer server exited with "No database connection string. Set
#: SCITEX_STORE_DSN (the fleet-wide switch) or CCT_STORE_DSN" although
#: SCITEX_STORE_DSN was present in the pane. Names only ever reach the argv.
INHERITED_ENV_PREFIXES = ("SCITEX_", "CCT_", "SAC_")


def inherited_env_names(environ: dict | None = None) -> list[str]:
    """Fleet variable NAMES present in this pane, for Codex's ``env_vars``."""
    source = os.environ if environ is None else environ
    return sorted(name for name in source if name.startswith(INHERITED_ENV_PREFIXES))


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
            literal, forwarded = split_env_placeholders(entry.get("env") or {})
            if literal:
                flags += ["-c", f"{key}.env={_toml(literal)}"]
            # The declared placeholders PLUS the fleet variables this pane
            # carries, so a server that reads an inherited variable behaves as
            # it does under Claude Code (see INHERITED_ENV_PREFIXES).
            names = sorted(set(forwarded) | set(inherited_env_names()))
            if names:
                flags += ["-c", f"{key}.env_vars={_toml(names)}"]
    return flags


#: Hook commands whose stdout carries ``updatedInput`` without an explicit
#: ``permissionDecision`` — Claude Code implies allow, Codex refuses. Measured
#: 2026-09-05: the rtk command-rewrite hook is the one such hook in the fleet.
_OUTPUT_ADAPTED_COMMANDS = ("rtk hook claude",)
_OUTPUT_ADAPTER = "python3 -m scitex_agent_container.runtimes._codex_hook_output"


def adapt_hook_commands(hooks: dict) -> dict:
    """Pipe the hooks Codex would misread through the output adapter.

    Only the commands listed in ``_OUTPUT_ADAPTED_COMMANDS`` are wrapped;
    every other hook runs exactly as it does under Claude Code.
    """
    adapted: dict = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            adapted[event] = groups
            continue
        new_groups = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                new_groups.append(group)
                continue
            new_hooks = []
            for hook in group["hooks"]:
                command = hook.get("command") if isinstance(hook, dict) else None
                if (
                    isinstance(command, str)
                    and command.strip() in _OUTPUT_ADAPTED_COMMANDS
                ):
                    hook = {**hook, "command": f"{command} | {_OUTPUT_ADAPTER}"}
                new_hooks.append(hook)
            new_groups.append({**group, "hooks": new_hooks})
        adapted[event] = new_groups
    return adapted


def write_hooks_from(settings_path: str, codex_home: str) -> Path | None:
    """Copy the Claude ``settings.json`` hooks block into ``$CODEX_HOME/hooks.json``.

    Returns the file written, or ``None`` when there was nothing to copy
    (no settings file, no ``hooks`` block, or no CODEX_HOME). The settings
    are the source of truth: the file is rewritten on every boot so a hook
    added to the fleet reaches the codex pane at its next restart, the same
    way it reaches the Claude pane. Codex's engine reads this shape — event
    name -> [{matcher, hooks: [{type: "command", command, timeout}]}] — and
    skips the hook TYPES it does not run yet (prompt / agent / async) with a
    named diagnostic rather than failing, so the block is copied whole.
    """
    if not settings_path or not codex_home:
        return None
    source = Path(settings_path)
    if not source.is_file():
        _log.warning(
            "codex-exec: settings %s is absent; no hooks copied", settings_path
        )
        return None
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except ValueError as exc:
        _log.warning("codex-exec: settings %s is not JSON (%s)", settings_path, exc)
        return None
    hooks = document.get("hooks") if isinstance(document, dict) else None
    if not isinstance(hooks, dict) or not hooks:
        return None
    hooks = adapt_hook_commands(hooks)
    target = Path(codex_home) / HOOKS_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"hooks": hooks}, indent=2) + "\n", encoding="utf-8")
    return target


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
    hooks_from = ""
    while args and args[0] != "--":
        flag = args.pop(0)
        if flag == "--mcp-config" and args:
            paths.append(args.pop(0))
        elif flag == "--mcp-json" and args:
            inline.append(args.pop(0))
        elif flag == "--hooks-from" and args:
            hooks_from = args.pop(0)
        else:
            _log.error("codex-exec: unexpected argument %r", flag)
            return 2
    if args and args[0] == "--":
        args.pop(0)
    binary = resolve_codex_binary()
    if hooks_from:
        write_hooks_from(hooks_from, os.environ.get("CODEX_HOME", ""))
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
