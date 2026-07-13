"""Regression tests for ``enforce_hpc_login_node_whitelist.sh``.

Operator directive (2026-07-10): agents hosted ON spartan-login* must not
run heavy compute there — only whitelisted control-plane commands pass;
everything else is blocked with an EDUCATIONAL message naming the right
route (sbatch / srun --overlap / module load / scitex-hpc permanent).
These tests drive the real shell hook + python core via subprocess with
real PreToolUse JSON payloads — no mocks — and assert the allow/block
decision, the gate (hostname pattern, fail-open), the educational message
content, and the bypasses.
"""

from __future__ import annotations

import subprocess

import pytest

from .conftest import (
    CORE_SCRIPT,
    HOOK_SCRIPT,
    OFF_HOSTNAME,
    POLICY_SCRIPT,
    run_hook,
    run_hook_raw,
)

# --- result fixtures (run the hook once; each test asserts one thing) --


@pytest.fixture(scope="module")
def self_test_result():
    return subprocess.run(
        ["bash", str(HOOK_SCRIPT), "--self-test"], capture_output=True, text=True
    )


@pytest.fixture
def pytest_block_result():
    return run_hook("pytest tests/ -x")


@pytest.fixture
def pip_block_result():
    return run_hook("pip install torch")


@pytest.fixture
def python_script_block_result():
    return run_hook("python3 train.py --epochs 100")


@pytest.fixture
def git_gc_block_result():
    return run_hook("git gc --aggressive")


# --- asset presence ---------------------------------------------------


def test_hook_script_file_exists():
    # Arrange
    script = HOOK_SCRIPT
    # Act
    present = script.is_file()
    # Assert
    assert present, f"missing hook: {script}"


def test_hook_script_is_executable():
    # Arrange
    script = HOOK_SCRIPT
    # Act
    mode = script.stat().st_mode
    # Assert
    assert mode & 0o111, "hook is not executable"


def test_core_script_file_exists():
    # Arrange
    script = CORE_SCRIPT
    # Act
    present = script.is_file()
    # Assert
    assert present, f"missing core: {script}"


def test_policy_script_file_exists():
    # Arrange
    script = POLICY_SCRIPT
    # Act
    present = script.is_file()
    # Assert
    assert present, f"missing policy: {script}"


# --- the script's own self-test (broadest built-in coverage) ----------


def test_self_test_exits_zero(self_test_result):
    # Arrange
    res = self_test_result
    # Act
    rc = res.returncode
    # Assert
    assert rc == 0, res.stdout + res.stderr


def test_self_test_reports_no_failures(self_test_result):
    # Arrange
    res = self_test_result
    # Act
    out = res.stdout
    # Assert
    assert "fail=0" in out


# --- gate: hostname pattern + fail-open --------------------------------


def test_off_login_node_heavy_command_is_allowed():
    # Arrange
    cmd = "pytest tests/ -x"
    # Act
    res = run_hook(cmd, hostname=OFF_HOSTNAME)
    # Assert
    assert res.returncode == 0, res.stderr


def test_empty_pattern_disables_the_hook():
    # Arrange
    extra = {"SAC_HPC_LOGIN_NODE_PATTERN": ""}
    # Act
    res = run_hook("pytest tests/", extra_env=extra)
    # Assert
    assert res.returncode == 0, res.stderr


def test_custom_pattern_gates_another_cluster():
    # Arrange
    extra = {"SAC_HPC_LOGIN_NODE_PATTERN": "mylogin"}
    # Act
    res = run_hook("pytest tests/", hostname="mylogin01.example.edu", extra_env=extra)
    # Assert
    assert res.returncode == 2


def test_hostname_introspection_failure_fails_open():
    # Arrange
    cmd = "pytest tests/"
    # Act
    res = run_hook(cmd, hostname="__fail__")
    # Assert
    assert res.returncode == 0, res.stderr


def test_hostname_introspection_failure_warns_on_stderr():
    # Arrange
    cmd = "pytest tests/"
    # Act
    res = run_hook(cmd, hostname="__fail__")
    # Assert
    assert "fail-open" in res.stderr


def test_invalid_gate_regex_fails_open():
    # Arrange
    extra = {"SAC_HPC_LOGIN_NODE_PATTERN": "("}
    # Act
    res = run_hook("pytest tests/", extra_env=extra)
    # Assert
    assert res.returncode == 0, res.stderr


# --- allow: the control-plane whitelist --------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "squeue --me",
        "sbatch job.sh",
        "srun --overlap --jobid 12345 pytest tests/",
        "salloc -n1 --time=1:00:00",
        "module load GCC/12.3.0",
        "ssh other-host 'hostname'",
        "rsync -av results/ dest:/data/",
        "git status",
        "git -C /data/proj pull",
        "ls -la | grep -i err",
        "FOO=1 ls",
        "timeout 7 git -C /data/proj status",
        "python3 -c 'print(1+1)'",
        "python3 --version",
        "curl -s https://api.github.com/repos/x/y",
        "tmux list-sessions",
        "scitex-hpc status",
        "bash -lc 'squeue --me'",
    ],
)
def test_whitelisted_control_plane_command_is_allowed(cmd):
    # Arrange
    command = cmd
    # Act
    res = run_hook(command)
    # Assert
    assert res.returncode == 0, f"{cmd!r} blocked: {res.stderr}"


def test_heredoc_body_is_data_not_commands():
    # Arrange
    cmd = (
        "cat <<EOF > job.sh\n#!/bin/bash\n#SBATCH --time=1:00:00\n"
        "pytest tests/\nEOF\nsbatch job.sh"
    )
    # Act
    res = run_hook(cmd)
    # Assert
    assert res.returncode == 0, res.stderr


# --- block: compute-shaped work ----------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "pytest tests/ -x",
        "make -j8",
        "cargo build --release",
        "pip install torch",
        "uv sync",
        "apptainer build img.sif recipe.def",
        "tar czf big.tgz results/",
        "du -sh /data/gpfs/projects/punim2354",
        "find / -name '*.log'",
        "pdflatex paper.tex",
        "python3 train.py --epochs 100",
        "git gc --aggressive",
        "git -C /data/proj gc",
        "bash run_experiments.sh",
        "./run_experiments.sh",
        "bash -c 'pytest tests/'",
        "ls -la && make -j4",
        "fd -e tex | xargs pdflatex",
    ],
)
def test_non_whitelisted_command_is_blocked(cmd):
    # Arrange
    command = cmd
    # Act
    res = run_hook(command)
    # Assert
    assert res.returncode == 2, f"{cmd!r} not blocked (rc={res.returncode})"


def test_python_dash_c_over_size_guard_is_blocked():
    # Arrange
    cmd = "python3 -c '%sprint(x)'" % ("x=1;" * 150)
    # Act
    res = run_hook(cmd)
    # Assert
    assert res.returncode == 2


# --- educational message names the right alternative -------------------


def test_block_message_names_the_offending_command(pytest_block_result):
    # Arrange
    res = pytest_block_result
    # Act
    stderr = res.stderr
    # Assert
    assert "'pytest'" in stderr


def test_build_block_message_teaches_srun_overlap(pytest_block_result):
    # Arrange
    res = pytest_block_result
    # Act
    stderr = res.stderr
    # Assert
    assert "srun --overlap" in stderr


def test_build_block_message_teaches_sbatch(pytest_block_result):
    # Arrange
    res = pytest_block_result
    # Act
    stderr = res.stderr
    # Assert
    assert "sbatch" in stderr


def test_pkg_block_message_teaches_module_load(pip_block_result):
    # Arrange
    res = pip_block_result
    # Act
    stderr = res.stderr
    # Assert
    assert "module load" in stderr


def test_interpreter_block_message_teaches_scitex_hpc_permanent(
    python_script_block_result,
):
    # Arrange
    res = python_script_block_result
    # Act
    stderr = res.stderr
    # Assert
    assert "scitex-hpc permanent" in stderr


def test_git_heavy_block_message_keeps_daily_git_whitelisted(git_gc_block_result):
    # Arrange
    res = git_gc_block_result
    # Act
    stderr = res.stderr
    # Assert
    assert "day-to-day git" in stderr


def test_block_message_documents_the_bypass(pytest_block_result):
    # Arrange
    res = pytest_block_result
    # Act
    stderr = res.stderr
    # Assert
    assert "SAC_HPC_LOGIN_ALLOW=1" in stderr


# --- bypasses + per-host extension -------------------------------------


def test_env_var_bypasses_the_guard():
    # Arrange
    extra = {"SAC_HPC_LOGIN_ALLOW": "1"}
    # Act
    res = run_hook("pytest tests/", extra_env=extra)
    # Assert
    assert res.returncode == 0


def test_inline_marker_bypasses_the_guard():
    # Arrange
    cmd = "pytest tests/ # hook-bypass: hpc-login"
    # Act
    res = run_hook(cmd)
    # Assert
    assert res.returncode == 0


def test_extra_allow_env_extends_the_whitelist():
    # Arrange
    extra = {"SAC_HPC_LOGIN_EXTRA_ALLOW": "htop"}
    # Act
    res = run_hook("htop", extra_env=extra)
    # Assert
    assert res.returncode == 0, res.stderr


# --- pass-through: non-Bash / bad payload / empty ----------------------


def test_non_bash_tool_invocation_passes_through():
    # Arrange
    payload = '{"tool_name":"Edit","tool_input":{}}'
    # Act
    res = run_hook_raw(payload)
    # Assert
    assert res.returncode == 0


def test_invalid_json_payload_fails_open():
    # Arrange
    payload = "this is not json"
    # Act
    res = run_hook_raw(payload)
    # Assert
    assert res.returncode == 0


def test_empty_command_passes_through():
    # Arrange
    cmd = ""
    # Act
    res = run_hook(cmd)
    # Assert
    assert res.returncode == 0
