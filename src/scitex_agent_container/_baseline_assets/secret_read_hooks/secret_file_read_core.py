#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/_baseline_assets/secret_read_hooks/secret_file_read_core.py
"""Refuse tool calls that would read secret material from disk.

Claude Code submits a PreToolUse JSON envelope on stdin. This engine decides
whether the call would put the CONTENTS of a secret file into the session,
whose transcript is durable:

* ``Read`` / ``NotebookRead`` of a secret path;
* ``Grep`` rooted at secret material, or whose ``glob`` targets it;
* ``Glob`` whose pattern targets secret files;
* ``Bash`` commands whose reading programs or ``<`` redirections name a
  secret path (see ``secret_file_read_bash.py``).

The path catalogue and normalisation live in ``secret_file_read_paths.py``.
Exit status: 0 = allow, 2 = deny (reason on stderr). A payload that does
not parse fails OPEN, like every other sac PreToolUse guard.

Operator override: ``SAC_ALLOW_SECRET_FILE_READ=1`` in the HOOK process
environment (the harness launch environment -- a Bash tool call cannot set
it), the same ``SAC_ALLOW_<RULE>=1`` convention as
``deny_swallowed_exit_code.sh``. Every override is logged. Deliberately
there is NO inline ``# hook-bypass`` marker: an agent can type a marker.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from secret_file_read_bash import hits_for_bash  # noqa: E402
from secret_file_read_paths import Hit, find_hits  # noqa: E402

try:
    import scitex_logging as slogging

    _CONSOLE = slogging.getPlainConsole(__name__)

    def _emit(line: str) -> None:
        _CONSOLE.emit(line)

except ImportError:  # standalone copy on an agent $HOME without sac installed

    def _emit(line: str) -> None:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


DENY = 2
ALLOW = 0

OVERRIDE_ENV = "SAC_ALLOW_SECRET_FILE_READ"
LOG_ENV = "SCITEX_AGENT_CONTAINER_HOOK_LOG_PATH"
HOOK_NAME = "deny_secret_file_read.sh"


def _join_glob(root: str, glob: str, *, basename_anywhere: bool) -> str:
    if glob.startswith(("/", "~", "$HOME", "${HOME}")):
        return glob
    if basename_anywhere and "/" not in glob:
        glob = "**/" + glob  # ripgrep --glob without a slash matches at any depth
    return f"{root.rstrip('/')}/{glob}"


def hits_for_tool(tool: str, tool_input: dict, cwd: str) -> list[Hit]:
    """Secret targets of a native (non-Bash) file tool call."""
    if tool == "Read":
        return find_hits([tool_input.get("file_path", "")], cwd, recursive=False)
    if tool == "NotebookRead":
        return find_hits([tool_input.get("notebook_path", "")], cwd, recursive=False)
    if tool == "Grep":
        root = tool_input.get("path") or cwd
        hits = find_hits([root], cwd, recursive=True)
        glob = tool_input.get("glob")
        if not hits and isinstance(glob, str) and glob:
            joined = _join_glob(root, glob, basename_anywhere=True)
            hits = find_hits([joined], cwd, recursive=False)
        return hits
    if tool == "Glob":
        pattern = tool_input.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            return []
        root = tool_input.get("path") or cwd
        joined = _join_glob(root, pattern, basename_anywhere=False)
        return find_hits([joined], cwd, recursive=False)
    return []


def decide(payload: dict) -> list[Hit]:
    """Every secret target the tool call in ``payload`` would read."""
    tool = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return []
    cwd = payload.get("cwd") or os.getcwd()
    if tool == "Bash":
        command = tool_input.get("command") or ""
        return hits_for_bash(command, cwd) if isinstance(command, str) else []
    return hits_for_tool(tool, tool_input, cwd)


DENY_MESSAGE = """\
BLOCKED by {hook}: this {tool} call would read secret material:
{targets}

Agents do not read credentials from files. Whatever a tool reads lands in
the session transcript, which is durable and leaves this machine.

Do this instead:
  * Credentials reach agents as ENVIRONMENT VARIABLES, materialised by sac's
    payload system from the operator's secret pool when the agent starts.
    Check whether the one you need is set WITHOUT printing its value:
        ~/.claude/hooks/lib/env-check VARNAME
  * If it is not set, ask the operator to add it to your agent's secret
    payload. Do not look for another route to the file.

Still allowed: listing a directory (`ls ~/.ssh`), public keys (`*.pub`),
`~/.ssh/known_hosts`, `~/.ssh/config`, `gpg --list-keys`, CA bundles.
"""


def _log(line: str) -> None:
    default = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), f".{HOOK_NAME}.log"
    )
    path = os.environ.get(LOG_ENV) or default
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(f"[{stamp}] {line}\n")
    except OSError as exc:
        sys.stderr.write(f"{HOOK_NAME}: could not write log {path}: {exc}\n")


def main(stdin: str) -> int:
    try:
        payload = json.loads(stdin)
    except (TypeError, ValueError):
        return ALLOW
    if not isinstance(payload, dict):
        return ALLOW
    hits = decide(payload)
    if not hits:
        return ALLOW
    tool = payload.get("tool_name") or "tool"
    # Paths and labels only -- never the command text, which may carry values.
    summary = "; ".join(f"{h.path} [{h.label}]" for h in hits)
    if os.environ.get(OVERRIDE_ENV) == "1":
        _log(f"OVERRIDE {OVERRIDE_ENV}=1 :: {tool} :: {summary}")
        sys.stderr.write(
            f"{HOOK_NAME}: operator override {OVERRIDE_ENV}=1 permitted a "
            f"secret read ({tool}: {summary}); logged.\n"
        )
        return ALLOW
    _log(f"BLOCK :: {tool} :: {summary}")
    targets = "\n".join(f"  - {h.path}  ({h.label})" for h in hits)
    sys.stderr.write(DENY_MESSAGE.format(hook=HOOK_NAME, tool=tool, targets=targets))
    return DENY


# ----------------------------------------------------------------------
# Self-test: measured deny/allow pairs against a throwaway HOME holding
# only fake files. Runnable at any time:  bash deny_secret_file_read.sh --self-test
# ----------------------------------------------------------------------
def _self_test() -> int:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="sac-secret-hook-") as tmp:
        home = os.path.join(tmp, "home")
        cwd = os.path.join(home, "work")
        os.makedirs(os.path.join(home, ".ssh"))
        os.makedirs(cwd)
        fake_key = os.path.join(home, ".ssh", "id_ed25519")
        with open(fake_key, "w", encoding="utf-8") as stream:
            stream.write("FAKE-NOT-A-KEY\n")
        os.symlink(fake_key, os.path.join(cwd, "innocent.txt"))
        os.environ["HOME"] = home
        os.environ.pop(OVERRIDE_ENV, None)
        os.environ[LOG_ENV] = os.path.join(tmp, "hook.log")

        def bash(cmd: str) -> dict:
            return {"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": cwd}

        def read(path: str) -> dict:
            return {"tool_name": "Read", "tool_input": {"file_path": path}, "cwd": cwd}

        def tool(name: str, **kw: str) -> dict:
            return {"tool_name": name, "tool_input": kw, "cwd": cwd}

        cases = [
            ("Read ssh key", read("~/.ssh/id_ed25519"), True),
            ("Read via ..", read(f"{cwd}/../.ssh/id_rsa"), True),
            ("Read via symlink", read(f"{cwd}/innocent.txt"), True),
            ("Read pw payload", read("~/.pw/github.gpg"), True),
            ("Read token store", read("~/.bash.d/secrets/access_tokens/gh"), True),
            ("Read gh hosts", read("~/.config/gh/hosts.yml"), True),
            ("Read pem", read("/srv/tls/server.pem"), True),
            ("Bash cat key", bash("cat ~/.ssh/id_rsa"), True),
            ("Bash redirect", bash("wc -c < $HOME/.netrc"), True),
            ("Bash glob", bash("head ~/.ssh/id_*"), True),
            ("Bash cd then cat", bash("cd ~/.ssh && cat id_rsa"), True),
            ("Bash gpg -d", bash("gpg -d ~/.pw/x.gpg"), True),
            ("Bash tar dir", bash("tar czf /tmp/x.tgz ~/.gnupg"), True),
            ("Bash bash -c", bash("bash -c 'cat ~/.git-credentials'"), True),
            ("Grep ssh dir", tool("Grep", pattern="x", path="~/.ssh"), True),
            (
                "Glob tokens",
                tool("Glob", pattern="*.github-token", path="~/.bash.d/secrets"),
                True,
            ),
            ("ls ssh dir", bash("ls -la ~/.ssh"), False),
            ("cat pubkey", bash("cat ~/.ssh/id_ed25519.pub"), False),
            ("cat ssh config", bash("cat ~/.ssh/config"), False),
            ("gpg list keys", bash("gpg --list-keys"), False),
            ("Read CA bundle", read("/etc/ssl/certs/ISRG_Root_X1.pem"), False),
            ("Read source", read("~/work/main.py"), False),
            ("Glob all py", tool("Glob", pattern="**/*.py"), False),
            ("echo path", bash("echo ~/.ssh/id_rsa"), False),
        ]
        failures = 0
        for desc, payload, want_deny in cases:
            denied = bool(decide(payload))
            ok = denied == want_deny
            failures += not ok
            verdict = "deny" if denied else "allow"
            _emit(f"  {'PASS' if ok else 'FAIL'} ({verdict}) {desc}")
        _emit(f"pass={len(cases) - failures} fail={failures}")
        return 0 if failures == 0 else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        raise SystemExit(_self_test())
    raise SystemExit(main(sys.stdin.read()))

# EOF
