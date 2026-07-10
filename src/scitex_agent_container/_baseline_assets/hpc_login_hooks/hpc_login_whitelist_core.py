#!/usr/bin/env python3
"""ENGINE for the HPC login-node command whitelist hook.

Reads a Claude Code PreToolUse payload (JSON) on stdin. On an HPC LOGIN
node (hostname matches ``$SAC_HPC_LOGIN_NODE_PATTERN``, default
``spartan-login``) it enforces the whitelist defined in the sibling
``hpc_login_whitelist_policy.py``: whitelisted (control-plane) commands
exit 0; everything else exits 2 with an EDUCATIONAL stderr message that
names the right route for that command class (``sbatch`` /
``srun --overlap`` / ``module load`` / ``scitex-hpc`` permanent session).
Off login nodes the hook is a strict no-op — zero risk to the rest of
the fleet.

Driven by ``enforce_hpc_login_node_whitelist.sh`` (which owns the
``--self-test`` mode and the bypasses); deployed side-by-side with the
policy module into ``$HOME/.claude/hooks/pre-tool-use/``.

Parsing model (a guardrail for cooperative agents, NOT a security
boundary): the command string is split quote-aware into top-level
simple-command segments (``&&`` ``||`` ``|`` ``;`` ``&`` newline), with
heredoc bodies swallowed as data (writing an #SBATCH script via
``cat <<EOF > job.sh`` must not have the script body judged). Each
segment's first real command is resolved through env-assignment
prefixes, shell keywords, and light wrappers (``timeout``/``nice``/
``xargs``/…); ``bash -c``/``eval`` payloads are recursed into. Command
and process substitution (``$(...)``, ``<(...)``) are NOT descended into
— same precision as the sibling hooks; the documented bypasses cover
deliberate use.

Fail-open everywhere introspection can break (hostname resolution, a bad
gate regex, an unparseable payload): a broken hook must never brick the
agent — it warns on stderr and allows.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

# The policy module lives next to this file both in-repo and once deployed
# into $HOME/.claude/hooks/pre-tool-use/ (not a package) — import by path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hpc_login_whitelist_policy as policy  # noqa: E402

SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
KEYWORD_SKIP = {
    "do", "then", "else", "!", "{", "(", ")", "}", "if", "elif", "while",
    "until",
}
KEYWORD_ALLOW_SEG = {"fi", "done", "esac", "for", "case"}
WRAPPER_VALUE_FLAGS = {
    "sudo": {"-u", "-g"},
    "env": {"-u", "-S", "-C", "--unset", "--split-string", "--chdir"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p"},
    "time": set(),
    "command": set(),
    "nohup": set(),
    "setsid": set(),
    "builtin": set(),
    "exec": set(),
    "stdbuf": {"-i", "-o", "-e"},
    "timeout": {"-k", "--kill-after", "-s", "--signal"},
    "xargs": {
        "-a", "-d", "-E", "-e", "-I", "-i", "-L", "-l", "-n", "-P", "-s",
        "--arg-file", "--delimiter", "--max-args", "--max-procs",
        "--max-chars", "--replace",
    },
}
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_REDIR_RE = re.compile(r"^[0-9]*[<>]")
_DURATION_RE = re.compile(r"^\d+(\.\d+)?[smhd]?$")
_SCRIPT_SUFFIX_RE = re.compile(r"\.(sh|bash|zsh|py|pl|rb|R)$")


def _warn(msg: str) -> None:
    sys.stderr.write("warn(enforce_hpc_login_node_whitelist): %s\n" % msg)


def _resolve_hostname() -> "str | None":
    """Hostname, honouring the test seam; ``None`` = introspection failed."""
    override = os.environ.get("SAC_HPC_LOGIN_TEST_HOSTNAME", "")
    if override == "__fail__":
        return None
    if override:
        return override
    try:
        import socket

        return socket.gethostname() or None
    except Exception:
        return None


def _split_segments(s: str) -> "list[str]":
    """Quote-aware split into top-level simple-command segments.

    Separators: ``&&`` ``||`` ``|`` ``;`` ``&`` and newline. Heredoc
    bodies (``<<[-]WORD``) are swallowed as DATA. ``2>&1``/``>&2``/``&>``
    are redirections, not separators. Backslash escapes the next char.
    """
    segs: "list[str]" = []
    buf: "list[str]" = []
    i, n, quote = 0, len(s), None
    heredocs: "list[str]" = []
    while i < n:
        c = s[i]
        if quote:
            if c == "\\" and quote == '"' and i + 1 < n:
                buf.append(c)
                buf.append(s[i + 1])
                i += 2
                continue
            buf.append(c)
            if c == quote:
                quote = None
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            buf.append(c)
            buf.append(s[i + 1])
            i += 2
            continue
        if c in ("'", '"'):
            quote = c
            buf.append(c)
            i += 1
            continue
        if c == "<" and i + 1 < n and s[i + 1] == "<":
            j = i + 2
            if j < n and s[j] == "-":
                j += 1
            while j < n and s[j] in " \t":
                j += 1
            qd = None
            if j < n and s[j] in ("'", '"'):
                qd = s[j]
                j += 1
            k = j
            while k < n and (s[k].isalnum() or s[k] == "_"):
                k += 1
            word = s[j:k]
            if word:
                heredocs.append(word)
            buf.append(s[i:k])
            i = k
            if qd and i < n and s[i] == qd:
                i += 1
            continue
        if c == "\n":
            segs.append("".join(buf))
            buf = []
            i += 1
            while heredocs:
                term = heredocs.pop(0)
                while i < n:
                    j = s.find("\n", i)
                    line = s[i:j] if j != -1 else s[i:]
                    i = (j + 1) if j != -1 else n
                    if line.strip() == term:
                        break
            continue
        if c == "&" and i + 1 < n and s[i + 1] == "&":
            segs.append("".join(buf))
            buf = []
            i += 2
            continue
        if c == "|" and i + 1 < n and s[i + 1] == "|":
            segs.append("".join(buf))
            buf = []
            i += 2
            continue
        if c == "&" and buf and buf[-1] == ">":
            buf.append(c)  # 2>&1 / >&2
            i += 1
            continue
        if c == "&" and i + 1 < n and s[i + 1] == ">":
            buf.append(c)  # &> redirect
            i += 1
            continue
        if c in ("|", ";", "&"):
            segs.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    segs.append("".join(buf))
    return [x for x in (seg.strip() for seg in segs) if x]


def _tokens(seg: str) -> "list[str]":
    try:
        return shlex.split(seg, posix=True)
    except ValueError:
        return seg.split()


def _classify(word: str, raw: str) -> str:
    for name, group in policy.CLASS_SETS.items():
        if word in group:
            return name
    if word.startswith("python"):
        return "interpreter"
    if "/" in raw or _SCRIPT_SUFFIX_RE.search(word):
        return "script"
    return "default"


def _git_subcommand(rest: "list[str]") -> "str | None":
    j = 0
    while j < len(rest):
        t = rest[j]
        if t in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            j += 2
            continue
        if t.startswith("-"):
            j += 1
            continue
        return t
    return None


def _unwrap(toks: "list[str]") -> "list[str]":
    """Strip light wrapper commands (timeout/nice/xargs/…) off the front."""
    while toks:
        word = os.path.basename(toks[0]).strip("()")
        if word not in WRAPPER_VALUE_FLAGS:
            break
        vflags = WRAPPER_VALUE_FLAGS[word]
        j = 1
        while j < len(toks):
            t = toks[j]
            if word == "env" and _ASSIGN_RE.match(t):
                j += 1
                continue
            if t.startswith("-"):
                j += 2 if (t in vflags and j + 1 < len(toks)) else 1
                continue
            break
        if word == "timeout" and j < len(toks) and _DURATION_RE.match(toks[j]):
            j += 1
        toks = toks[j:]
    return toks


def _judge_command(toks, seg, depth):
    """``None`` (allowed) or ``(offending_word, class)`` for one command."""
    toks = _unwrap(toks)
    if not toks:
        return None
    raw = toks[0]
    word = os.path.basename(raw).strip("()")
    if word == "eval":
        return _judge_pipeline(" ".join(toks[1:]), depth + 1)
    if word in SHELLS:
        for j in range(1, len(toks)):
            t = toks[j]
            if t == "--":
                break
            if t.startswith("-") and "c" in t[1:]:
                if j + 1 < len(toks):
                    return _judge_pipeline(toks[j + 1], depth + 1)
                return None
            if not t.startswith("-"):
                return (raw, "script")
        return (raw, "script")
    if word.startswith("python"):
        args = toks[1:]
        if args and args[0] in ("--version", "-V", "-VV"):
            return None
        if "-c" in args:
            if len(seg) <= policy.pyc_max():
                return None
            return (raw, "pyc_too_long")
        return (raw, "interpreter")
    if word == "git":
        sub = _git_subcommand(toks[1:])
        if sub in policy.GIT_HEAVY:
            return ("git %s" % sub, "git_heavy")
        return None
    if word in policy.ALLOW:
        return None
    return (raw, _classify(word, raw))


def _judge_segment(seg, depth):
    toks = _tokens(seg)
    i = 0
    while i < len(toks):
        t = toks[i].lstrip("(").rstrip(")")
        if not t:
            i += 1
            continue
        if t.startswith("#"):
            return None
        if _ASSIGN_RE.match(t):
            i += 1
            continue
        if t in KEYWORD_ALLOW_SEG:
            return None
        if t in KEYWORD_SKIP:
            i += 1
            continue
        if _REDIR_RE.match(t):
            i += 1
            continue
        break
    if i >= len(toks):
        return None
    return _judge_command(toks[i:], seg, depth)


def _judge_pipeline(s, depth=0):
    if depth > 4:
        return None
    for seg in _split_segments(s):
        verdict = _judge_segment(seg, depth)
        if verdict is not None:
            return verdict
    return None


def _log_block(hostname, cls, word, cmd):
    log_path = os.environ.get("LOG_PATH", "")
    if not log_path:
        return
    try:
        import datetime

        ts = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        with open(log_path, "a") as fh:
            fh.write(
                "[%s] BLOCK hpc-login :: host=%s class=%s word=%s :: %s\n"
                % (ts, hostname, cls, word, cmd.replace("\n", "\\n"))
            )
    except Exception:
        pass


def main() -> int:
    # Gate: only on an HPC login node; fail-open on any introspection error.
    pattern = (os.environ.get(policy.PAT_ENV, policy.DEFAULT_PATTERN) or "").strip()
    if not pattern:
        return 0
    hostname = _resolve_hostname()
    if hostname is None:
        _warn("hostname introspection failed; fail-open (allowing).")
        return 0
    try:
        if re.search(pattern, hostname, re.IGNORECASE) is None:
            return 0
    except re.error:
        _warn(
            "invalid regex in $%s=%r; fail-open (allowing)."
            % (policy.PAT_ENV, pattern)
        )
        return 0

    # Payload (fail-open on anything unparseable).
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    if data.get("tool_name") != "Bash":
        return 0
    cmd = (data.get("tool_input", {}) or {}).get("command", "") or ""
    if not cmd.strip():
        return 0

    policy.extend_allow_from_env()
    verdict = _judge_pipeline(cmd)
    if verdict is None:
        return 0
    bad_word, cls = verdict
    _log_block(hostname, cls, bad_word, cmd)
    sys.stderr.write(policy.block_message(bad_word, cls, hostname, pattern))
    return 2


if __name__ == "__main__":
    sys.exit(main())

# EOF
