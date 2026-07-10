#!/bin/bash
# -*- coding: utf-8 -*-
# Timestamp: "2026-07-10 (proj-scitex-agent-container)"
# File: ~/.claude/hooks/pre-tool-use/enforce_hpc_login_node_whitelist.sh
#
# Description: PreToolUse hook for Bash. On an HPC LOGIN node it enforces a
# WHITELIST of login-safe (control-plane) commands and BLOCKS (exit 2)
# everything else with an EDUCATIONAL message that names the right route
# (sbatch / srun --overlap / module load / scitex-hpc permanent session).
# Inactive on every other host: the gate is a hostname match against
# $SAC_HPC_LOGIN_NODE_PATTERN (default: spartan-login) — zero risk to the
# rest of the fleet.
#
# WHY (operator directive 2026-07-10, his own design)
# ----------------------------------------------------
# Agents run ON spartan-login* (unimelb Spartan HPC). Login nodes are the
# cluster's SHARED front door; heavy CPU/RAM/IO there degrades every user's
# session and draws admin complaints (prior incidents: 2026-06-09 du/find
# GPFS scan, 2026-07-01 TeX compile — see the sibling
# deny_heavy_spartan_login.sh, which guards `ssh spartan ...` issued FROM
# other hosts; THIS hook guards execution ON the node itself). Operator
# wording: "login nodeでやってよいコマンドだけ並べて、whitelistで
# filteringするのが良い。error messageにフィードバックとして、slurm使え
# とか、module load使えとか、srun overlapとか、scitex-hpc permanent使え
# とか言ったり" and "hookで if node == spartan-login-node みたいに条件分岐".
#
# This wrapper owns --self-test and the bypasses; the decision logic lives
# in the sibling hpc_login_whitelist_core.py + hpc_login_whitelist_policy.py
# (whitelist rationale + the per-class educational error catalogue are
# documented there and in README.md). Deploy all three files together into
# $HOME/.claude/hooks/pre-tool-use/. If the core is missing the hook
# FAILS OPEN (warn + allow): a broken hook must never brick the agent.
#
# Knobs:
#   SAC_HPC_LOGIN_NODE_PATTERN   gate regex vs hostname (default spartan-login;
#                                empty string disables the hook entirely)
#   SAC_HPC_LOGIN_EXTRA_ALLOW    comma/space list of extra whitelisted cmds
#   SAC_HPC_LOGIN_PYC_MAX        python -c size guard, chars (default 500)
#   SAC_HPC_LOGIN_TEST_HOSTNAME  test seam: hostname override ("__fail__"
#                                simulates introspection failure)
# Bypass (rare — operator-supervised):
#   SAC_HPC_LOGIN_ALLOW=1        env escape
#   `# hook-bypass: hpc-login`   inline marker

set -u

THIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_PATH="$THIS_DIR/.$(basename "$0").log"
CORE="$THIS_DIR/hpc_login_whitelist_core.py"

# ------------------------------------------------------------------
# Self-test
# ------------------------------------------------------------------
if [[ "${1:-}" == "--self-test" ]]; then
    echo "=== Self-test: $(basename "$0") ==="
    pass=0
    fail=0

    # Deterministic: each case controls its own gate/knobs via env args.
    unset SAC_HPC_LOGIN_ALLOW SAC_HPC_LOGIN_NODE_PATTERN \
        SAC_HPC_LOGIN_TEST_HOSTNAME SAC_HPC_LOGIN_EXTRA_ALLOW \
        SAC_HPC_LOGIN_PYC_MAX 2>/dev/null || true

    ON="SAC_HPC_LOGIN_TEST_HOSTNAME=spartan-login1.hpc.unimelb.edu.au"
    OFF="SAC_HPC_LOGIN_TEST_HOSTNAME=ywata-note-win"

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

    # --- gate: only active when hostname matches the pattern ---
    run "off-host: pytest allowed" "pytest tests/" 0 "$OFF"
    run "empty pattern disables hook" "pytest tests/" 0 "$ON" "SAC_HPC_LOGIN_NODE_PATTERN="
    run "custom pattern gates other cluster" "pytest tests/" 2 \
        "SAC_HPC_LOGIN_TEST_HOSTNAME=mylogin01.example.edu" "SAC_HPC_LOGIN_NODE_PATTERN=mylogin"
    run "hostname introspection failure -> fail-open" "pytest tests/" 0 \
        "SAC_HPC_LOGIN_TEST_HOSTNAME=__fail__"
    run "bad gate regex -> fail-open" "pytest tests/" 0 "$ON" "SAC_HPC_LOGIN_NODE_PATTERN=("

    # --- allow: SLURM control plane (srun payload runs on COMPUTE) ---
    run "squeue" "squeue --me" 0 "$ON"
    run "sbatch submit" "sbatch job.sh" 0 "$ON"
    run "srun --overlap payload" "srun --overlap --jobid 12345 pytest tests/" 0 "$ON"
    run "salloc interactive" "salloc -n1 --time=1:00:00" 0 "$ON"
    run "scontrol" "scontrol show job 12345" 0 "$ON"

    # --- allow: modules / transfer / git / plumbing / fleet CLIs ---
    run "module load" "module load GCC/12.3.0" 0 "$ON"
    run "ml shorthand" "ml Python/3.11.5" 0 "$ON"
    run "ssh out" "ssh other-host 'hostname'" 0 "$ON"
    run "rsync small" "rsync -av results/ dest:/data/" 0 "$ON"
    run "git status" "git status" 0 "$ON"
    run "git -C pull" "git -C /data/proj pull" 0 "$ON"
    run "pipeline of whitelisted" "ls -la | grep -i err" 0 "$ON"
    run "env-assignment prefix" "FOO=1 ls" 0 "$ON"
    run "wrapper: timeout + git" "timeout 7 git -C /data/proj status" 0 "$ON"
    run "python -c one-liner" "python3 -c 'print(1+1)'" 0 "$ON"
    run "python --version" "python3 --version" 0 "$ON"
    run "curl API call" "curl -s https://api.github.com/repos/x/y" 0 "$ON"
    run "tmux control" "tmux list-sessions" 0 "$ON"
    run "scitex-hpc CLI" "scitex-hpc status" 0 "$ON"
    run "bash -lc whitelisted inner" "bash -lc 'squeue --me'" 0 "$ON"
    run "heredoc body is data, not commands" \
        $'cat <<EOF > job.sh\n#!/bin/bash\n#SBATCH --time=1:00:00\npytest tests/\nEOF\nsbatch job.sh' 0 "$ON"

    # --- block: compute-shaped work (each class) ---
    run "pytest" "pytest tests/ -x" 2 "$ON"
    run "make" "make -j8" 2 "$ON"
    run "cargo build" "cargo build --release" 2 "$ON"
    run "pip install" "pip install torch" 2 "$ON"
    run "uv sync" "uv sync" 2 "$ON"
    run "apptainer build" "apptainer build img.sif recipe.def" 2 "$ON"
    run "tar compress" "tar czf big.tgz results/" 2 "$ON"
    run "du scan" "du -sh /data/gpfs/projects/punim2354" 2 "$ON"
    run "find scan" "find / -name '*.log'" 2 "$ON"
    run "pdflatex" "pdflatex paper.tex" 2 "$ON"
    run "python script" "python3 train.py --epochs 100" 2 "$ON"
    run "python -c over size guard" \
        "python3 -c '$(printf 'x=1;%.0s' {1..150})print(x)'" 2 "$ON"
    run "git gc" "git gc --aggressive" 2 "$ON"
    run "git -C gc" "git -C /data/proj gc" 2 "$ON"
    run "shell script run" "bash run_experiments.sh" 2 "$ON"
    run "./script run" "./run_experiments.sh" 2 "$ON"
    run "bash -c non-whitelisted inner" "bash -c 'pytest tests/'" 2 "$ON"
    run "chained: second segment heavy" "ls -la && make -j4" 2 "$ON"
    run "xargs into heavy cmd" "fd -e tex | xargs pdflatex" 2 "$ON"

    # --- educational message names the right alternative ---
    run_msg "pytest -> srun --overlap" "pytest tests/" "srun --overlap" "$ON"
    run_msg "pip -> module load" "pip install numpy" "module load" "$ON"
    run_msg "python script -> scitex-hpc permanent" "python3 train.py" \
        "scitex-hpc permanent" "$ON"
    run_msg "du -> fd alternative" "du -sh ~" "fd" "$ON"
    run_msg "tar -> sbatch --wrap" "tar czf a.tgz d/" "sbatch --wrap" "$ON"
    run_msg "git gc -> day-to-day git stays allowed" "git gc" "day-to-day git" "$ON"

    # --- bypasses + extension ---
    run "env bypass" "pytest tests/" 0 "$ON" "SAC_HPC_LOGIN_ALLOW=1"
    run "inline marker bypass" "pytest tests/ # hook-bypass: hpc-login" 0 "$ON"
    run "extra-allow extends whitelist" "htop" 0 "$ON" "SAC_HPC_LOGIN_EXTRA_ALLOW=htop"

    # --- pass-through: non-Bash / bad payload / empty ---
    run "empty command" "" 0 "$ON"
    rc=0
    echo '{"tool_name":"Edit","tool_input":{}}' | env "$ON" "$0" >/dev/null 2>&1 || rc=$?
    if [[ "$rc" == "0" ]]; then
        echo "  PASS (0) non-Bash tool"
        pass=$((pass + 1))
    else
        echo "  FAIL got $rc want 0: non-Bash tool"
        fail=$((fail + 1))
    fi
    rc=0
    printf 'this is not json' | env "$ON" "$0" >/dev/null 2>&1 || rc=$?
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
[[ "${SAC_HPC_LOGIN_ALLOW:-}" == "1" ]] && exit 0

# ------------------------------------------------------------------
# Read input; string-marker bypass
# ------------------------------------------------------------------
INPUT="$(cat)"
printf '%s' "$INPUT" | grep -qF 'hook-bypass: hpc-login' && exit 0

# ------------------------------------------------------------------
# Delegate to the decision core. FAIL OPEN if the core did not deploy —
# a broken hook must never brick the agent.
# ------------------------------------------------------------------
if [[ ! -f "$CORE" ]]; then
    echo "warn(enforce_hpc_login_node_whitelist): core missing at $CORE; fail-open (allowing)." >&2
    exit 0
fi
printf '%s' "$INPUT" | LOG_PATH="$LOG_PATH" python3 "$CORE"
exit $?

# EOF
