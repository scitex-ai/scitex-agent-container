#!/usr/bin/env python3
"""ENGINE for the heavy-job demotion guard hook.

Reads a Claude Code PreToolUse payload (JSON) on stdin. When the Bash
command contains a known-HEAVY job (image/SIF build, mksquashfs, mass
compression, archive creation, high ``-j`` parallel build — classes in
the sibling ``heavy_job_demotion_policy.py``) that is NOT wrapped in a
``nice``/``ionice`` prefix, it exits 2 with an EDUCATIONAL stderr
message carrying the corrected command
(``nice -n 19 ionice -c 2 -n 7 <cmd>``), the remote-first advice, and
the bypasses. Demoted invocations, light invocations (``docker ps``,
``tar xf``, ``make -j2``, ``--version``), and everything unrecognised
pass untouched. ``$SAC_HEAVY_JOB_GUARD_DISABLE`` opts a dedicated
build host out entirely.

Driven by ``enforce_heavy_job_demotion.sh`` (which owns ``--self-test``,
the cheap keyword fast-path, and the bypasses); deployed side-by-side
with the policy module into ``$HOME/.claude/hooks/pre-tool-use/``.

Parsing model (a guardrail for cooperative agents, NOT a security
boundary — same conventions as the sibling ``hpc_login_whitelist_core``,
whose battle-tested segment splitter is reused verbatim): the command
string is split quote-aware into top-level simple-command segments
(``&&`` ``||`` ``|`` ``;`` ``&`` newline), with heredoc bodies swallowed
as data (writing a build script via ``cat <<EOF`` must not be judged).
Each segment's wrapper chain (``sudo``/``env``/``timeout``/``xargs``/…)
is unwrapped; seeing ``nice`` or ``ionice`` in that chain marks the
segment DEMOTED (priority is inherited by every descendant) and allows
it. ``bash -c``/``eval`` payloads are recursed into. Command/process
substitution (``$(...)``, ``<(...)``) is NOT descended into; a dynamic
``-j$(nproc)`` is treated as maximal parallelism. Fail-open everywhere:
unparseable payloads and non-Bash tools always pass — a broken hook
must never brick the agent.
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
import heavy_job_demotion_policy as policy  # noqa: E402

SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
KEYWORD_SKIP = {
    "do", "then", "else", "!", "{", "(", ")", "}", "if", "elif", "while",
    "until",
}
KEYWORD_ALLOW_SEG = {"fi", "done", "esac", "for", "case"}
# Wrappers unwrapped off the front of a segment. Seeing nice/ionice here
# means the target (and every process it forks) runs demoted — allow.
DEMOTION_WRAPPERS = {"nice", "ionice"}
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
_LIGHT_FLAGS = {"--version", "-V", "--help", "-h"}


def _split_segments(s: str) -> "list[str]":
    """Quote-aware split into top-level simple-command segments.

    Copied VERBATIM from the sibling ``hpc_login_whitelist_core.py``
    (the reference implementation for this hook family) so both hooks
    judge identical segment boundaries. Separators: ``&&`` ``||`` ``|``
    ``;`` ``&`` and newline. Heredoc bodies (``<<[-]WORD``) are
    swallowed as DATA. ``2>&1``/``>&2``/``&>`` are redirections, not
    separators. Backslash escapes the next char.
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


def _unwrap(toks: "list[str]") -> "tuple[list[str], bool]":
    """Strip wrapper commands off the front; report whether the chain
    contained a demotion wrapper (``nice``/``ionice``)."""
    demoted = False
    while toks:
        word = os.path.basename(toks[0]).strip("()")
        if word not in WRAPPER_VALUE_FLAGS:
            break
        if word in DEMOTION_WRAPPERS:
            demoted = True
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
    return toks, demoted


def _subcommands(rest: "list[str]", limit: int = 2) -> "list[str]":
    """First ``limit`` non-flag args (naive: a value-taking global flag's
    value can be miscounted — acceptable for a cooperative guardrail)."""
    subs: "list[str]" = []
    for t in rest:
        if t == "--":
            break
        if t.startswith("-"):
            continue
        subs.append(t)
        if len(subs) >= limit:
            break
    return subs


def _parse_jobs(rest: "list[str]") -> "int | None":
    """Parallelism from ``-j``/``--jobs``. ``None`` = flag absent;
    ``-1`` = maximal (bare ``-j`` or a dynamic ``-j$(nproc)``)."""
    for i, t in enumerate(rest):
        if t in ("-j", "--jobs"):
            nxt = rest[i + 1] if i + 1 < len(rest) else None
            return int(nxt) if nxt and nxt.isdigit() else -1
        if t.startswith("-j") and t != "-j":
            val = t[2:]
            return int(val) if val.isdigit() else -1
        if t.startswith("--jobs="):
            val = t.split("=", 1)[1]
            return int(val) if val.isdigit() else -1
    return None


def _tar_creates(rest: "list[str]") -> bool:
    """``True`` for tar CREATE invocations (old-style ``tar czf …``,
    ``-czf``, ``--create``); extraction/list stay allowed."""
    if not rest:
        return False
    first = rest[0]
    if not first.startswith("-") and "c" in first:
        return True  # old-style bundled flags: tar czf ...
    for t in rest:
        if t == "--create":
            return True
        if t.startswith("-") and not t.startswith("--") and "c" in t[1:]:
            return True
    return False


def _judge_word(word, rest, assigns):
    """``None`` (allowed) or ``(offending_word, class)`` for one command."""
    if word == "sac":
        subs = _subcommands(rest, 2)
        no_nice_assign = any(
            a.split("=", 1)[0] == "SAC_BUILD_NO_NICE"
            and a.split("=", 1)[1] not in ("", "0")
            for a in assigns
        )
        if subs == ["image", "build"] and ("--no-nice" in rest or no_nice_assign):
            return ("sac image build --no-nice", "sac_no_nice")
        return None  # sac image build self-demotes (PR #605); rest is CLI plumbing
    if word in policy.ALWAYS_HEAVY:
        return (word, policy.ALWAYS_HEAVY[word])
    if word in policy.IMAGE_BUILD_SUBCOMMANDS:
        subs = tuple(_subcommands(rest, 2))
        for shape in policy.IMAGE_BUILD_SUBCOMMANDS[word]:
            if subs[: len(shape)] == shape:
                return ("%s %s" % (word, " ".join(shape)), "image_build")
        return None
    if word in policy.COMPRESSORS:
        if rest and all(t in _LIGHT_FLAGS for t in rest):
            return None  # pure --version/--help introspection
        return (word, "compress")
    if word == "tar":
        return (word, "archive") if _tar_creates(rest) else None
    if word == "zip":
        return (word, "archive") if "-r" in rest else None
    if word in policy.SEVEN_ZIP:
        subs = _subcommands(rest, 1)
        return (word, "archive") if subs[:1] == ["a"] else None
    if word in policy.PARALLEL_BUILDERS:
        jobs = _parse_jobs(rest)
        if jobs is not None and (jobs < 0 or jobs > policy.jobs_max()):
            return ("%s -j" % word, "parallel_build")
        return None
    if word in policy.EXTRA_DENY:
        return (word, "extra")
    return None


def _judge_command(toks, assigns, depth):
    toks, demoted = _unwrap(toks)
    if demoted or not toks:
        return None  # nice/ionice in the chain: every descendant inherits it
    word = os.path.basename(toks[0]).strip("()")
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
                break  # a script file: opaque — a deny-list cannot judge it
        return None
    return _judge_word(word, toks[1:], assigns)


def _judge_segment(seg, depth):
    toks = _tokens(seg)
    assigns: "list[str]" = []
    i = 0
    while i < len(toks):
        t = toks[i].lstrip("(").rstrip(")")
        if not t:
            i += 1
            continue
        if t.startswith("#"):
            return None
        if _ASSIGN_RE.match(t):
            assigns.append(t)
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
    return _judge_command(toks[i:], assigns, depth)


def _judge_pipeline(s, depth=0):
    if depth > 4:
        return None
    for seg in _split_segments(s):
        verdict = _judge_segment(seg, depth)
        if verdict is not None:
            return verdict
    return None


def _log_block(cls, word, cmd):
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
                "[%s] BLOCK heavy-job :: class=%s word=%s :: %s\n"
                % (ts, cls, word, cmd.replace("\n", "\\n"))
            )
    except Exception:
        pass


def main() -> int:
    # Standing opt-out for dedicated build hosts.
    if policy.guard_disabled():
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

    policy.extend_deny_from_env()
    verdict = _judge_pipeline(cmd)
    if verdict is None:
        return 0
    bad_word, cls = verdict
    _log_block(cls, bad_word, cmd)
    sys.stderr.write(policy.block_message(bad_word, cls))
    return 2


if __name__ == "__main__":
    sys.exit(main())

# EOF
