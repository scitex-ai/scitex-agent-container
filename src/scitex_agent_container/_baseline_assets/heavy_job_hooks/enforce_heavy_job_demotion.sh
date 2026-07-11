#!/bin/bash
# -*- coding: utf-8 -*-
# Timestamp: "2026-07-11 (proj-scitex-agent-container)"
# File: ~/.claude/hooks/pre-tool-use/enforce_heavy_job_demotion.sh
#
# Description: PreToolUse hook for Bash. BLOCKS (exit 2) obviously-HEAVY
# commands (container image builds, mksquashfs, mass compression,
# archive creation, high `-j` parallel builds) launched WITHOUT a
# `nice`/`ionice` prefix, with an EDUCATIONAL message carrying the
# corrected command (`nice -n 19 ionice -c 2 -n 7 <cmd>`), the
# remote-first advice (Spartan / dedicated build host), and the
# bypasses. Demoted invocations and light usage (docker ps, tar xf,
# make -j2, --version) pass untouched.
#
# WHY (P1 incident 2026-07-10, incident-local-heavy-build)
# --------------------------------------------------------
# A full SIF rebake ran at NORMAL priority on the operator's already-
# loaded shared interactive host — load spiked 27 -> 50+ and his
# session starved. Fix #1 made `sac image build` self-demote (PR #605);
# THIS hook guards the general PATTERN: any heavy job an agent launches
# by hand must self-demote or move to a remote/dedicated host. IO
# demotion is best-effort lowest (-c 2 -n 7), NOT idle (-c 3): an
# idle-class build starved and died at the mksquashfs stage the same
# night (rationale lives with the constants in
# heavy_job_demotion_policy.py and _build_priority.py).
#
# This wrapper owns --self-test, the cheap keyword fast-path, and the
# bypasses; the decision logic lives in the sibling
# heavy_job_demotion_core.py + heavy_job_demotion_policy.py (heavy
# classes + educational error catalogue are documented there and in
# README.md). Deploy all three files together into
# $HOME/.claude/hooks/pre-tool-use/. If the core is missing the hook
# FAILS OPEN (warn + allow): a broken hook must never brick the agent.
#
# Knobs:
#   SAC_HEAVY_JOB_GUARD_DISABLE  standing opt-out for dedicated build
#                                hosts (any value other than empty/"0")
#   SAC_HEAVY_JOB_JOBS_MAX       -j parallelism threshold (default 4)
#   SAC_HEAVY_JOB_EXTRA_DENY     comma/space list of extra denied cmds
# Bypass (rare — operator-supervised):
#   SAC_HEAVY_JOB_ALLOW=1        env escape
#   `# hook-bypass: heavy-job`   inline marker

set -u

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_PATH="$THIS_DIR/.$(basename "$0").log"
CORE="$THIS_DIR/heavy_job_demotion_core.py"

# Cheap fast-path pre-filter (this hook runs on EVERY Bash call): only
# commands containing a candidate heavy keyword spawn the python core.
# Keep this a SUPERSET of the policy's denied names — a miss here means
# a silent allow. The self-test's block cases cover every class, so
# drift between this regex and the policy fails the self-test.
HEAVY_HINT_RE='\b(mksquashfs|unsquashfs|apptainer|singularity|docker|docker-compose|podman|buildah|nerdctl|tar|xz|unxz|pixz|pigz|unpigz|pbzip2|zstd|unzstd|pzstd|lrzip|lzma|plzip|lz4|unlz4|zip|7z|7za|7zr|make|gmake|ninja|cargo|cmake|ctest|bazel|mvn|gradle|gcc|clang|rustc|nvcc|sac)\b|g\+\+|clang\+\+'

# ------------------------------------------------------------------
# Self-test
# ------------------------------------------------------------------
if [[ "${1:-}" == "--self-test" ]]; then
    echo "=== Self-test: $(basename "$0") ==="
    pass=0
    fail=0

    # Deterministic: scrub every knob; each case sets its own via env args.
    unset SAC_HEAVY_JOB_ALLOW SAC_HEAVY_JOB_GUARD_DISABLE \
        SAC_HEAVY_JOB_JOBS_MAX SAC_HEAVY_JOB_EXTRA_DENY 2>/dev/null || true

    mk_json() {
        printf '{"tool_name":"Bash","tool_input":{"command":%s},"cwd":"/tmp"}' \
            "$(printf '%s' "$1" | python3 -c 'import sys,json;print(json.dumps(sys.stdin.read()))')"
    }

    run() {
        local desc="$1" cmd="$2" want="$3" rc json
        shift 3
        json=$(mk_json "$cmd")
        if [[ $# -gt 0 ]]; then
            printf '%s' "$json" | env "$@" "$0" >/dev/null 2>&1
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

    run_msg() {
        local desc="$1" cmd="$2" needle="$3" json err
        shift 3
        json=$(mk_json "$cmd")
        err=$(printf '%s' "$json" | env "$@" "$0" 2>&1 >/dev/null)
        if printf '%s' "$err" | grep -qF -- "$needle"; then
            echo "  PASS (msg) $desc"
            pass=$((pass + 1))
        else
            echo "  FAIL msg missing '$needle': $desc"
            fail=$((fail + 1))
        fi
    }

    # --- allow: everyday light commands (fast-path or core) ---
    run "plain ls" "ls -la" 0
    run "git status" "git -C /repo status" 0
    run "docker ps" "docker ps -a" 0
    run "docker compose up" "docker compose up -d" 0
    run "apptainer exec" "apptainer exec img.sif hostname" 0
    run "tar extract" "tar xzf release.tgz" 0
    run "tar list" "tar tf release.tgz" 0
    run "zip single file" "zip out.zip notes.txt" 0
    run "7z extract" "7z x archive.7z" 0
    run "serial make" "make" 0
    run "low-parallel make" "make -j2" 0
    run "make -j at threshold" "make -j 4" 0
    run "xz version introspection" "xz --version" 0
    run "sac CLI plumbing" "sac agents list" 0
    run "sac image build (self-demotes)" "sac image build base -y" 0
    run "echo mentioning a heavy word" 'echo "run mksquashfs later"' 0
    run "heredoc body is data" \
        $'cat <<EOF > build.sh\nmksquashfs a b\nEOF\nls' 0

    # --- allow: already-demoted heavy commands ---
    run "full demotion prefix" \
        "nice -n 19 ionice -c 2 -n 7 tar czf big.tgz data/" 0
    run "nice alone counts" "nice -n 19 xz -9 huge.log" 0
    run "ionice alone counts" "ionice -c 2 -n 7 mksquashfs root out.sqfs" 0
    run "demoted apptainer build" \
        "nice -n 19 ionice -c 2 -n 7 apptainer build out.sif r.def" 0
    run "demoted bash -c payload inherits" \
        "nice -n 19 bash -c 'mksquashfs a b'" 0

    # --- block: each heavy class, undemoted ---
    run "mksquashfs" "mksquashfs squashfs-root out.squashfs" 2
    run "apptainer build" "apptainer build out.sif recipe.def" 2
    run "docker build" "docker build -t img ." 2
    run "docker buildx build" "docker buildx build --platform amd64 ." 2
    run "podman build" "podman build ." 2
    run "docker-compose build" "docker-compose build" 2
    run "tar create compressed" "tar czf big.tgz data/" 2
    run "tar create dash form" "tar -cJf big.txz data/" 2
    run "xz compress" "xz -9 huge.log" 2
    run "zstd compress" "zstd -19 big.bin" 2
    run "pigz compress" "pigz big.tar" 2
    run "zip recursive" "zip -r out.zip data/" 2
    run "7z add" "7z a out.7z data/" 2
    run "make high -j" "make -j8" 2
    run "make bare -j (unlimited)" "make -j" 2
    run "ninja high -j" "ninja -j 16" 2
    run "cargo high -j" "cargo build -j12" 2
    run "dynamic -j treated as max" 'make -j$(nproc)' 2
    run "sac image build --no-nice" "sac image build base -y --no-nice" 2
    run "SAC_BUILD_NO_NICE=1 assign prefix" \
        "SAC_BUILD_NO_NICE=1 sac image build base -y" 2
    run "chained: second segment heavy" "ls -la && mksquashfs a b" 2
    run "piped: tar create into xz" "tar cf - data/ | xz > out.txz" 2
    run "bash -c heavy payload" "bash -c 'mksquashfs a b'" 2
    run "xargs into compressor" "fd -e log | xargs xz" 2
    run "eval heavy payload" 'eval "xz -9 huge.log"' 2

    # --- educational message content ---
    run_msg "message carries corrected prefix" "mksquashfs a b" \
        "nice -n 19 ionice -c 2 -n 7"
    run_msg "message explains not-idle-class rationale" "mksquashfs a b" \
        "NOT idle (-c 3)"
    run_msg "message advises remote-first" "xz -9 huge.log" "Spartan"
    run_msg "message names sac self-demoting build" \
        "apptainer build out.sif r.def" "sac image build"
    run_msg "message names inline bypass" "pigz big.tar" \
        "hook-bypass: heavy-job"
    run_msg "no-nice block educates dedicated-host knob" \
        "sac image build base -y --no-nice" "SAC_HEAVY_JOB_GUARD_DISABLE"

    # --- knobs + bypasses ---
    run "env bypass" "mksquashfs a b" 0 "SAC_HEAVY_JOB_ALLOW=1"
    run "inline marker bypass" \
        "mksquashfs a b # hook-bypass: heavy-job" 0
    run "dedicated-host disable knob" "mksquashfs a b" 0 \
        "SAC_HEAVY_JOB_GUARD_DISABLE=1"
    run "disable knob value 0 stays active" "mksquashfs a b" 2 \
        "SAC_HEAVY_JOB_GUARD_DISABLE=0"
    run "jobs threshold raised allows -j8" "make -j8" 0 \
        "SAC_HEAVY_JOB_JOBS_MAX=8"
    run "extra-deny extends classes" "rsync -a big/ dest:/data/" 2 \
        "SAC_HEAVY_JOB_EXTRA_DENY=rsync"
    run "extra-deny leaves others alone" "rsync -a big/ dest:/data/" 0

    # --- pass-through: non-Bash / bad payload / empty ---
    run "empty command" "" 0
    rc=0
    echo '{"tool_name":"Edit","tool_input":{}}' | "$0" >/dev/null 2>&1 || rc=$?
    if [[ "$rc" == "0" ]]; then
        echo "  PASS (0) non-Bash tool"
        pass=$((pass + 1))
    else
        echo "  FAIL got $rc want 0: non-Bash tool"
        fail=$((fail + 1))
    fi
    rc=0
    printf 'this is not json but mentions mksquashfs' | "$0" >/dev/null 2>&1 || rc=$?
    if [[ "$rc" == "0" ]]; then
        echo "  PASS (0) invalid JSON -> fail-open"
        pass=$((pass + 1))
    else
        echo "  FAIL got $rc want 0: invalid JSON"
        fail=$((fail + 1))
    fi

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
[[ "${SAC_HEAVY_JOB_ALLOW:-}" == "1" ]] && exit 0

# ------------------------------------------------------------------
# Read input; string-marker bypass; cheap keyword fast-path
# ------------------------------------------------------------------
INPUT="$(cat)"
printf '%s' "$INPUT" | grep -qF 'hook-bypass: heavy-job' && exit 0
if [[ -z "${SAC_HEAVY_JOB_EXTRA_DENY:-}" ]]; then
    printf '%s' "$INPUT" | grep -qE "$HEAVY_HINT_RE" || exit 0
fi

# ------------------------------------------------------------------
# Delegate to the decision core. FAIL OPEN if the core did not deploy —
# a broken hook must never brick the agent.
# ------------------------------------------------------------------
if [[ ! -f "$CORE" ]]; then
    echo "warn(enforce_heavy_job_demotion): core missing at $CORE; fail-open (allowing)." >&2
    exit 0
fi
printf '%s' "$INPUT" | LOG_PATH="$LOG_PATH" python3 "$CORE"
exit $?

# EOF
