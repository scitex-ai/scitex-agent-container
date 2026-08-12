#!/bin/bash
# -*- coding: utf-8 -*-
# Timestamp: "2026-07-06 (proj-scitex-agent-container)"
# File: ~/.claude/hooks/pre-tool-use/enforce_commit_author_allowlist.sh
#
# Description: PreToolUse hook for Bash. On `git commit` / `git push`,
# resolves the EFFECTIVE commit-author email and BLOCKS (exit 2) when it
# is not on the CLA allowlist, with an actionable, in-place fix so the
# identity is corrected BEFORE the push — never after CI.
#
# WHY (evidenced incident, scitex-hpc 2026-07-05)
# ------------------------------------------------
# An agent's PR went fully GREEN on real CI (audit / docs / import-smoke /
# pytest 3.11-3.13) but the REQUIRED `CLAssistant` check FAILED and blocked
# the merge: the commits were authored `agent@scitex-hpc`, which maps to no
# GitHub account, and the CLA allowlist rejected it. Force-push is
# hook-blocked, so the ONLY remedy was re-creating the whole tree as a fresh
# commit authored by the allowlisted identity on a NEW branch — pure,
# avoidable rework discovered only AFTER a full CI cycle.
#
# Root cause: the container's git author is meant to default to the
# CLA-allowlisted `Yusuke Watanabe <ywatanabe@scitex.ai>` (= GitHub
# `ywatanabe1989`), pinned via `SAC_GIT_AUTHOR_EMAIL` (direnv `.envrc`) ->
# `GIT_AUTHOR_EMAIL` (apptainer alias step). That pin can silently fail
# (direnv never fired / var empty), leaving `GIT_AUTHOR_EMAIL` unset so git
# synthesizes `user@hostname`; or a prompt-level `git config user.email`
# override moves the author away. Nothing caught it until CLAssistant, after
# CI. This hook is the fail-loud backstop that catches it at commit/push
# time, in-place.
#
# The CLA maps AUTHOR -> GitHub account by email, then checks that account
# against `.github/workflows/cla.yml`'s `allowlist: bot*,ywatanabe1989`. So
# what matters is whether the email is VERIFIED on `ywatanabe1989` -- not the
# local-part. Two are: `ywatanabe@scitex.ai` (the operator's own, human work)
# and `agent@scitex.ai` (the AGENT identity; operator-approved + verified on
# the same account, 2026-08-12). Both pass CLAssistant and differ only in who
# `git log` says did the work. ANY OTHER author (`agent@<host>`, a synthesized
# `user@hostname`, a stray override) maps to no allowlisted account and is the
# bug this hook exists to catch, so the default stays tight at those two;
# extend it for the rare non-default-but-allowlisted identity (LLEmacs / a
# bot) via the env var below rather than loosening the default.
#
# Policy:
#   - `git commit`, effective author NOT allowlisted             -> BLOCK
#   - `git push`, ANY unpushed commit's author NOT allowlisted   -> BLOCK
#   - author IS allowlisted (exact / glob / env-extension)       -> allow
#   - read-only / non-commit-push git (status, log, add, ...)    -> allow
#   - non-git / non-Bash / not-in-a-repo                         -> allow
#
# Allowlist (case-insensitive email match):
#   - built-in exact:  ywatanabe@scitex.ai   (= GitHub ywatanabe1989, human)
#   - built-in exact:  agent@scitex.ai       (= GitHub ywatanabe1989, agents)
#   - built-in glob:   *[bot]@users.noreply.github.com   (bot* authors)
#   - env extension:   CC_CLA_ALLOWED_EMAILS="a@x,b@y"   (comma/space list)
#
# Detection of `git -C <path>` mirrors the sibling git hooks so a command
# issued from one cwd that mutates ANOTHER repo is judged against the repo
# it actually touches.
#
# Bypass (rare -- operator-supervised):
#   - Inline marker:  `# hook-bypass: cla-author`
#   - Env variable:   CC_ALLOW_CLA_AUTHOR=1

set -u

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Shell-local path; exported to the inline python body below as the
# namespaced SCITEX_AGENT_CONTAINER_HOOK_LOG_PATH. Writer and reader are
# both in THIS file, so the contract cannot drift. Unset/empty => no audit
# log; the block decision itself is unaffected.
LOG_PATH="$THIS_DIR/.$(basename "$0").log"

# ------------------------------------------------------------------
# Self-test
# ------------------------------------------------------------------
if [[ "${1:-}" == "--self-test" ]]; then
    echo "=== Self-test: $(basename "$0") ==="
    pass=0
    fail=0

    # Deterministic: the hook resolves `git var GIT_AUTHOR_IDENT`, which
    # honours GIT_AUTHOR_EMAIL from the ENV over repo config. Unset the
    # inherited identity/extension vars so each case controls its own.
    unset GIT_AUTHOR_EMAIL GIT_AUTHOR_NAME GIT_COMMITTER_EMAIL \
        GIT_COMMITTER_NAME CC_CLA_ALLOWED_EMAILS CC_ALLOW_CLA_AUTHOR 2>/dev/null || true

    tmpdir=$(mktemp -d)
    trap 'rm -rf "$tmpdir"' EXIT

    good_repo="$tmpdir/good"     # config author = allowlisted (human)
    agent_repo="$tmpdir/agent"   # config author = allowlisted (agent)
    bad_repo="$tmpdir/bad"       # config author = NOT allowlisted
    (
        mkdir -p "$good_repo" && cd "$good_repo" || exit
        git init -q -b feature/x
        git config user.email "ywatanabe@scitex.ai"
        git config user.name "Yusuke Watanabe"
        git commit --allow-empty -q -m seed

        mkdir -p "$agent_repo" && cd "$agent_repo" || exit
        git init -q -b feature/x
        git config user.email "agent@scitex.ai"
        git config user.name "scitex-agent-container"
        git commit --allow-empty -q -m seed

        mkdir -p "$bad_repo" && cd "$bad_repo" || exit
        git init -q -b feature/x
        git config user.email "agent@scitex-hpc"
        git config user.name "agent"
        git commit --allow-empty -q -m seed
    ) >/dev/null 2>&1

    run() {
        local desc="$1" cmd="$2" cwd="$3" want="$4" envset="${5:-}" rc json
        json=$(printf '{"tool_name":"Bash","tool_input":{"command":%s},"cwd":%s}' \
            "$(printf '%s' "$cmd" | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))')" \
            "$(printf '%s' "$cwd" | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))')")
        if [[ -n "$envset" ]]; then
            printf '%s' "$json" | env $envset "$0" >/dev/null 2>&1
        else
            printf '%s' "$json" | "$0" >/dev/null 2>&1
        fi
        rc=$?
        if [[ "$rc" == "$want" ]]; then
            echo "  PASS ($rc) $desc"
            pass=$((pass + 1))
        else
            echo "  FAIL got $rc want $want: $desc -- cmd: $cmd"
            fail=$((fail + 1))
        fi
    }

    # --- commit: allowlisted config author -> ALLOW ---
    run "commit good-config author" "git commit -m x" "$good_repo" 0
    run "commit good, -C form" "git -C $good_repo commit -m x" "$tmpdir" 0

    # --- the AGENT identity is allowlisted too (2026-08-12) ---
    run "commit agent-identity author" "git commit -m x" "$agent_repo" 0
    run "push agent-identity commit" "git push origin feature/x" "$agent_repo" 0
    run "agent identity is case-insensitive" \
        "git -c user.email=Agent@SciTeX.ai commit -m x" "$bad_repo" 0

    # --- commit: non-allowlisted config author -> BLOCK ---
    run "commit bad-config author" "git commit -m x" "$bad_repo" 2
    run "commit bad, -am" "git commit -am x" "$bad_repo" 2
    run "commit bad, add && commit" "git add -A && git commit -m x" "$bad_repo" 2
    run "commit bad, -C form" "git -C $bad_repo commit -m x" "$tmpdir" 2

    # --- inline overrides win, respected by the guard ---
    run "bad repo but GIT_AUTHOR_EMAIL=good inline" \
        "GIT_AUTHOR_EMAIL=ywatanabe@scitex.ai git commit -m x" "$bad_repo" 0
    run "good repo but --author=bad inline" \
        "git commit -m x --author='Nobody <nobody@nowhere.invalid>'" "$good_repo" 2
    run "bad repo but -c user.email=good inline" \
        "git -c user.email=ywatanabe@scitex.ai commit -m x" "$bad_repo" 0

    # --- push: judges the authors of the unpushed commits ---
    run "push bad-author commit" "git push origin feature/x" "$bad_repo" 2
    run "push good-author commit" "git push origin feature/x" "$good_repo" 0

    # --- env extension allowlists an extra identity ---
    run "commit bad author, extended via env" "git commit -m x" "$bad_repo" 0 \
        "CC_CLA_ALLOWED_EMAILS=agent@scitex-hpc"

    # --- bypasses ---
    run "marker bypass" "git commit -m x # hook-bypass: cla-author" "$bad_repo" 0
    run "env bypass" "git commit -m x" "$bad_repo" 0 "CC_ALLOW_CLA_AUTHOR=1"

    # --- non-commit/push git in a bad repo -> ALLOW (read-only) ---
    run "status in bad repo" "git status" "$bad_repo" 0
    run "log in bad repo" "git log --oneline" "$bad_repo" 0
    run "add in bad repo" "git add -A" "$bad_repo" 0

    # --- non-git / non-repo / non-Bash -> ALLOW ---
    run "non-git command" "ls -la" "$bad_repo" 0
    run "commit outside any repo" "git commit -m x" "$tmpdir" 0
    rc=0
    echo '{"tool_name":"Edit","tool_input":{}}' | "$0" >/dev/null 2>&1 || rc=$?
    if [[ "$rc" == "0" ]]; then
        echo "  PASS (0) non-Bash tool"
        pass=$((pass + 1))
    else
        echo "  FAIL got $rc want 0: non-Bash tool"
        fail=$((fail + 1))
    fi

    # --- quoted git token in an rg pattern must NOT trip the gate ---
    run "quoted git commit in rg pattern" \
        "rg -N 'git commit|git push' $tmpdir" "$good_repo" 0

    echo "pass=$pass fail=$fail"
    [[ $fail -eq 0 ]] && exit 0 || exit 1
fi

# ------------------------------------------------------------------
# Enablement switch (project-switch helper, like the sibling hooks)
# ------------------------------------------------------------------
HELPER_SCRIPT="$(dirname "$THIS_DIR")/project-switch/hook_switch_helper.sh"
if [[ -f "$HELPER_SCRIPT" ]]; then
    # shellcheck source=/dev/null
    source "$HELPER_SCRIPT"
    if declare -f check_hook_enabled_or_exit >/dev/null 2>&1; then
        check_hook_enabled_or_exit "$(basename "$0")"
    fi
fi

# ------------------------------------------------------------------
# Env-var escape
# ------------------------------------------------------------------
[[ "${CC_ALLOW_CLA_AUTHOR:-}" == "1" ]] && exit 0

# ------------------------------------------------------------------
# Read input; string-marker bypass
# ------------------------------------------------------------------
INPUT="$(cat)"
printf '%s' "$INPUT" | grep -qF 'hook-bypass: cla-author' && exit 0

# ------------------------------------------------------------------
# Main decision. The Python body is captured via a QUOTED heredoc so it
# may contain arbitrary single/double quotes (the block message + git
# fix commands need both); it is passed to `python3 -c`. INPUT arrives on
# stdin, SCITEX_AGENT_CONTAINER_HOOK_LOG_PATH via the environment.
# ------------------------------------------------------------------
PYCODE=$(cat <<'PYEOF'
import json, sys, re, os, shlex, subprocess, fnmatch


def sh(*args):
    try:
        r = subprocess.run(list(args), capture_output=True, text=True)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception:
        return 1, "", ""


try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
if d.get("tool_name", "") != "Bash":
    sys.exit(0)
cmd = (d.get("tool_input", {}) or {}).get("command", "") or ""
cwd = d.get("cwd", "") or ""
if not cmd or not cwd or not os.path.isdir(cwd):
    sys.exit(0)


# Quote-aware split: a `git commit` token inside a quoted rg/grep pattern
# must NOT trip the gate. Mirrors the sibling git hooks.
def _split_top_level(s):
    segs, buf, i, n, q = [], [], 0, len(s), None
    while i < n:
        c = s[i]
        if q:
            buf.append(c)
            if c == q:
                q = None
            i += 1
            continue
        if c == "'" or c == '"':
            q = c
            buf.append(c)
            i += 1
            continue
        if c == "&" and i + 1 < n and s[i + 1] == "&":
            segs.append("".join(buf)); buf = []; i += 2; continue
        if c == "|" and i + 1 < n and s[i + 1] == "|":
            segs.append("".join(buf)); buf = []; i += 2; continue
        if c == "|" or c == ";":
            segs.append("".join(buf)); buf = []; i += 1; continue
        buf.append(c)
        i += 1
    segs.append("".join(buf))
    return segs


def _git_subcmd_and_C(seg):
    """(subcommand, git_C_target) if the segment's first real command is
    git, else (None, None)."""
    try:
        toks = shlex.split(seg, posix=True)
    except ValueError:
        toks = seg.split()
    i = 0
    while i < len(toks):
        t = toks[i]
        if "=" in t and not t.startswith("-"):
            i += 1
            continue
        if t in ("sudo", "env", "nice", "time", "command"):
            i += 1
            continue
        break
    if i >= len(toks) or not (toks[i] == "git" or toks[i].endswith("/git")):
        return None, None
    rest = toks[i + 1:]
    target = None
    j = 0
    while j < len(rest):
        t = rest[j]
        if t == "-C" and j + 1 < len(rest):
            target = rest[j + 1]; j += 2; continue
        if t.startswith("-C") and len(t) > 2:
            target = t[2:]; j += 1; continue
        if t == "-c" and j + 1 < len(rest):
            j += 2
            continue
        if t.startswith("-"):
            j += 1
            continue
        return t, target
    return None, target


mode_commit = False
mode_push = False
git_c = None
for seg in _split_top_level(cmd):
    sub, tgt = _git_subcmd_and_C(seg)
    if sub == "commit":
        mode_commit = True
        if tgt and not git_c:
            git_c = tgt
    elif sub == "push":
        mode_push = True
        if tgt and not git_c:
            git_c = tgt
if not (mode_commit or mode_push):
    sys.exit(0)

# Resolve the target repo (honour `git -C <path>`).
repo = git_c or cwd
if repo and not os.path.isabs(repo):
    repo = os.path.join(cwd, repo)
repo = os.path.expanduser(repo)
if not os.path.isdir(repo):
    repo = cwd
if sh("git", "-C", repo, "rev-parse", "--git-dir")[0] != 0:
    sys.exit(0)

# Allowlist (case-insensitive).
ALLOWED_EXACT = {"ywatanabe@scitex.ai", "agent@scitex.ai"}
ALLOWED_GLOBS = ["*[[]bot[]]@users.noreply.github.com"]
for e in re.split(r"[,\s]+", os.environ.get("CC_CLA_ALLOWED_EMAILS", "")):
    e = e.strip().lower()
    if e:
        ALLOWED_EXACT.add(e)


def allowed(email):
    e = (email or "").strip().lower()
    if not e:
        return False
    if e in ALLOWED_EXACT:
        return True
    return any(fnmatch.fnmatch(e, g) for g in ALLOWED_GLOBS)


def email_of(ident):
    m = re.search(r"<([^>]*)>", ident or "")
    return m.group(1).strip() if m else ""


# authors :: list of (email, source)
authors = []

# Inline overrides (mirror git's resolution precedence).
inline_email, inline_src = None, None
m = re.search(r'''(?:^|[\s;&|(])GIT_AUTHOR_EMAIL=("[^"]*"|'[^']*'|\S+)''', cmd)
if m:
    inline_email = m.group(1).strip("\"'")
    inline_src = "inline GIT_AUTHOR_EMAIL="
m = re.search(r'''-c\s+user\.email=("[^"]*"|'[^']*'|\S+)''', cmd)
if m:
    inline_email = m.group(1).strip("\"'")
    inline_src = "inline -c user.email="
if mode_commit:
    m = re.search(r'''--author[=\s]+("[^"]*"|'[^']*'|\S+)''', cmd)
    if m:
        val = m.group(1).strip("\"'")
        em = email_of(val) or (val if "@" in val else "")
        if em:
            inline_email = em
            inline_src = "inline --author flag"

if mode_commit:
    if inline_email:
        authors.append((inline_email, inline_src))
    else:
        ident = sh("git", "-C", repo, "var", "GIT_AUTHOR_IDENT")[1]
        env_ae = os.environ.get("GIT_AUTHOR_EMAIL", "").strip()
        cfg_ae = sh("git", "-C", repo, "config", "user.email")[1].strip()
        if env_ae:
            src = "GIT_AUTHOR_EMAIL env"
        elif cfg_ae:
            src = "user.email config"
        else:
            src = "git default (no identity set -> synthesized user@hostname)"
        authors.append((email_of(ident), src))

if mode_push:
    # Authors of the commits this push would publish: prefer the tracked
    # upstream range; else exclude known integration refs; else HEAD.
    emails = []
    rc, up, _ = sh("git", "-C", repo, "rev-parse", "--abbrev-ref",
                   "--symbolic-full-name", "@{upstream}")
    if rc == 0 and up:
        rc, out, _ = sh("git", "-C", repo, "log", "--format=%ae", up + "..HEAD")
        emails = [x for x in out.splitlines() if x.strip()] if rc == 0 else []
    else:
        excludes = []
        for ref in ("origin/develop", "origin/main", "origin/master"):
            if sh("git", "-C", repo, "rev-parse", "--verify", "--quiet", ref)[0] == 0:
                excludes.append("^" + ref)
        if excludes:
            rc, out, _ = sh("git", "-C", repo, "log", "--format=%ae", "HEAD", *excludes)
            emails = [x for x in out.splitlines() if x.strip()] if rc == 0 else []
        else:
            rc, out, _ = sh("git", "-C", repo, "log", "-1", "--format=%ae", "HEAD")
            emails = [out.strip()] if rc == 0 and out.strip() else []
    seen = set()
    for em in emails:
        if em not in seen:
            seen.add(em)
            authors.append((em, "pushed commit author"))

violations = [(em, src) for (em, src) in authors if not allowed(em)]
if not violations:
    sys.exit(0)

# Block + log.
log_path = os.environ.get("SCITEX_AGENT_CONTAINER_HOOK_LOG_PATH", "")
if log_path:
    try:
        import datetime
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(log_path, "a") as fh:
            fh.write("[%s] BLOCK cla-author :: repo=%s :: %s :: %s\n"
                     % (ts, repo, [v[0] for v in violations], cmd))
    except Exception:
        pass

bad_email = violations[0][0] or "<empty>"
bad_src = violations[0][1]

if bad_src.startswith("GIT_AUTHOR_EMAIL env") or bad_src.startswith("inline GIT_AUTHOR_EMAIL"):
    fix = (
        "A GIT_AUTHOR_EMAIL is set to a NON-allowlisted value (it beats\n"
        "  git config). Re-point it, then re-create the commit:\n"
        "    export GIT_AUTHOR_EMAIL=agent@scitex.ai\n"
        "    export GIT_AUTHOR_NAME=\"${SAC_NAME:-agent}\"\n"
        "    git -C %s commit --amend --reset-author --no-edit" % repo
    )
elif "pushed commit author" in bad_src:
    fix = (
        "One or more commits you are pushing are authored by a\n"
        "  non-allowlisted identity. Fix identity, then rewrite them\n"
        "  (topic branch only -- never a shared branch):\n"
        "    git -C %s config user.email agent@scitex.ai\n"
        "    git -C %s config user.name  \"${SAC_NAME:-agent}\"\n"
        "    # last commit:\n"
        "    git -C %s commit --amend --reset-author --no-edit\n"
        "    # or a range: git -C %s rebase --exec \\\n"
        "        'git commit --amend --reset-author --no-edit' <base>" % (repo, repo, repo, repo)
    )
else:
    fix = (
        "Set the CLA-allowlisted AGENT identity, then re-create the commit:\n"
        "    git -C %s config user.email agent@scitex.ai\n"
        "    git -C %s config user.name  \"${SAC_NAME:-agent}\"\n"
        "    git -C %s commit --amend --reset-author --no-edit   # if already committed"
        % (repo, repo, repo)
    )

msg = (
    "BLOCKED by enforce_commit_author_allowlist.sh: commit author\n"
    "'%s' is NOT CLA-allowlisted (source: %s).\n"
    "\n"
    ">>> This is a LOCAL IDENTITY problem -- NOT a failure of the CLAssistant\n"
    ">>> bot. The bot is fine; your commit author simply maps to no allowlisted\n"
    ">>> GitHub account.\n"
    "\n"
    "The required 'CLAssistant' check maps each commit author to a GitHub\n"
    "account via an allowlist (bot*, ywatanabe1989 -> agent@scitex.ai for\n"
    "agents / ywatanabe@scitex.ai for the operator's own commits). An\n"
    "author that maps to no allowlisted account makes CLAssistant FAIL and\n"
    "BLOCK THE MERGE -- and it only surfaces AFTER CI has already gone green.\n"
    "Because force-push is hook-blocked, the sole remedy then is re-creating\n"
    "the tree on a new branch (avoidable rework -- incident: scitex-hpc\n"
    "2026-07-05). Fix it HERE, before pushing.\n"
    "\n"
    "FIX\n"
    "---\n"
    "  %s\n"
    "\n"
    "If '%s' is a DIFFERENT but legitimately allowlisted identity\n"
    "(e.g. LLEmacs / a bot), extend the allowlist instead:\n"
    "  export CC_CLA_ALLOWED_EMAILS='%s'\n"
    "Bypass (operator-supervised only): append '# hook-bypass: cla-author'\n"
    % (bad_email, bad_src, fix, bad_email, bad_email)
)

sys.stderr.write(msg)
sys.exit(2)
PYEOF
)

printf '%s' "$INPUT" | SCITEX_AGENT_CONTAINER_HOOK_LOG_PATH="$LOG_PATH" python3 -c "$PYCODE"
exit $?

# EOF
