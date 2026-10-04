"""Native admission and genuine hosted test/verdict boundaries, without services."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/pytest-matrix-on-ubuntu-py3-11-3-12-3-13.yml"
HOSTED = ROOT / ".github/ci/run-on-hosted.sh"


@pytest.fixture
def workflow_doc() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


@pytest.mark.parametrize("hook_id", ["no-hosted-runners", "no-hardcoded-runner-pool"])
def test_workflow_hooks_use_fixed_existing_environment_routes(hook_id):
    # Arrange
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    hooks = [hook for repo in config["repos"] for hook in repo["hooks"]]
    # Act
    hook = next(hook for hook in hooks if hook["id"] == hook_id)
    # Assert
    assert (hook["language"], hook["entry"], hook.get("additional_dependencies")) == (
        "system",
        "bash .githooks/run-workflow-guard.sh "
        + ("hosted" if hook_id == "no-hosted-runners" else "pool"),
        None,
    )


@pytest.fixture
def workflow_hook_process(tmp_path):
    """Observe the real shell's dispatch without installing or creating an env."""
    log = tmp_path / "hook.json"
    uv = tmp_path / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        + "import json, os, sys\nfrom pathlib import Path\n"
        + "keys=['VIRTUAL_ENV','CONDA_PREFIX','PYTHONHOME','PYTHONPATH',"
        + "'PYTHONUSERBASE','UV_PROJECT_ENVIRONMENT']\n"
        + "Path(os.environ['HOOK_LOG']).write_text(json.dumps("
        + "{'argv':sys.argv[1:],'overrides':[k for k in keys if k in os.environ]}))\n"
        + "raise SystemExit(int(os.environ.get('HOOK_EXIT','0')))\n"
    )
    uv.chmod(0o755)
    env = {
        "PATH": "/usr/bin:/bin",
        "SAC_HOOK_UV": str(uv),
        "SAC_HOOK_PYTHON": sys.executable,
        "HOOK_LOG": str(log),
        "VIRTUAL_ENV": "synthetic-active-env",
        "CONDA_PREFIX": "synthetic-active-env",
        "PYTHONHOME": "synthetic-python-home",
        "PYTHONPATH": "synthetic-import-path",
        "PYTHONUSERBASE": "synthetic-user-site",
        "UV_PROJECT_ENVIRONMENT": "synthetic-project-env",
    }

    def run(*args, **updates):
        result = subprocess.run(
            ["/bin/bash", str(ROOT / ".githooks/run-workflow-guard.sh"), *args],
            cwd=tmp_path,
            env=env | updates,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result, json.loads(log.read_text()) if log.exists() else None

    return run


@pytest.mark.parametrize("guard", ["hosted", "pool"])
def test_hook_dispatch_is_offline_and_bound_to_the_explicit_interpreter(
    workflow_hook_process, guard
):
    # Arrange
    run = workflow_hook_process
    filename = (
        "_hosted_runner_guard.py" if guard == "hosted" else "_runner_pool_guard.py"
    )
    # Act
    result, observation = run(guard)
    # Assert
    assert (result.returncode, observation) == (
        0,
        {
            "argv": [
                "run",
                "--no-project",
                "--no-python-downloads",
                "--no-config",
                "--offline",
                "--no-index",
                "--python",
                sys.executable,
                str(ROOT / "src/scitex_agent_container" / filename),
                str(ROOT),
            ],
            "overrides": [],
        },
    )


@pytest.mark.parametrize("args", [(), ("unknown",), ("hosted", "extra")])
def test_unknown_hook_dispatch_refuses_before_executing_uv(workflow_hook_process, args):
    # Arrange
    run = workflow_hook_process
    # Act
    result, observation = run(*args)
    # Assert
    assert (result.returncode, observation) == (2, None)


@pytest.mark.parametrize(
    "updates",
    [
        {"SAC_HOOK_PYTHON": ""},
        {"SAC_HOOK_PYTHON": "python3"},
        {"SAC_HOOK_UV": "uv"},
        {"SAC_HOOK_PYTHON": "/does-not-exist/selected-python"},
    ],
)
def test_unqualified_hook_executables_refuse_before_uv(workflow_hook_process, updates):
    # Arrange
    run = workflow_hook_process
    # Act
    result, observation = run("hosted", **updates)
    # Assert
    assert (result.returncode, observation) == (2, None)


def test_hook_guard_failure_is_preserved_without_fallback(workflow_hook_process):
    # Arrange
    run = workflow_hook_process
    # Act
    result, _ = run("hosted", HOOK_EXIT="17")
    # Assert
    assert result.returncode == 17


def test_leaf_ci_is_only_a_central_reusable_workflow_call(workflow_doc):
    """Execution policy belongs to the organization workflow, not this leaf."""
    job = workflow_doc["jobs"]["tests"]
    assert job == {
        "uses": "scitex-ai/.github/.github/workflows/ci-sif-matrix.yml@main",
        "with": {"suite": "matrix"},
    } and set(workflow_doc["jobs"]) == {"tests"}


@pytest.fixture
def hosted_process(tmp_path):
    """Real shell/child execution with synthetic UV and Python, no installation."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.jsonl"
    python_body = """import json, os, sys
from pathlib import Path
with Path(os.environ['CALL_LOG']).open('a') as out:
 row={'kind':'python','args':sys.argv[1:]}
 row['store']='SCITEX_STORE_DSN' in os.environ
 out.write(json.dumps(row)+'\\n')
if '--collect-only' in sys.argv:
 print('plugins: '+os.environ.get('PLUGINS','asyncio, xdist, cov, timeout'))
 raise SystemExit(0)
if '-c' in sys.argv: raise SystemExit(1)
if '-V' in sys.argv: print('Python synthetic'); raise SystemExit(0)
raise SystemExit(int(os.environ.get('SUITE_EXIT','0')))
"""
    uv = bin_dir / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        + """import json, os, sys
from pathlib import Path
with Path(os.environ['CALL_LOG']).open('a') as out:
 out.write(json.dumps({'kind':'uv','args':sys.argv[1:]})+'\\n')
if sys.argv[1]=='venv':
 dest=Path(sys.argv[-1])/'bin';dest.mkdir(parents=True)
 p=dest/'python';p.write_text(os.environ['SYNTHETIC_PYTHON']);p.chmod(0o755)
install=sys.argv[1:3]==['pip','install'] and '.[all,dev]' in sys.argv
if install and os.environ.get('INSTALL_FAIL')=='1':
 raise SystemExit(42)
"""
    )
    uv.chmod(0o755)
    nproc = bin_dir / "nproc"
    nproc.write_text("#!/bin/sh\nprintf '2\\n'\n")
    nproc.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "RUNNER_TEMP": str(tmp_path),
        "CALL_LOG": str(log),
        "SYNTHETIC_PYTHON": f"#!{sys.executable}\n" + python_body,
        "SCITEX_STORE_DSN": "synthetic-untrusted-input",
        "PGUSER": "synthetic-untrusted-role",
    }

    def run(*args, **updates):
        result = subprocess.run(
            ["/bin/bash", str(HOSTED), *args],
            cwd=tmp_path,
            env=env | updates,
            capture_output=True,
            text=True,
            timeout=10,
        )
        calls = (
            [json.loads(s) for s in log.read_text().splitlines()]
            if log.exists()
            else []
        )
        return result, calls

    return run


def test_failed_full_install_never_runs_a_reduced_install_or_pytest(hosted_process):
    # Arrange
    run = hosted_process
    # Act
    result, calls = run("3.11", "matrix", INSTALL_FAIL="1")
    installs = [
        c for c in calls if c["kind"] == "uv" and c["args"][:2] == ["pip", "install"]
    ]
    # Assert
    assert (
        result.returncode,
        len(installs),
        [c for c in calls if c["kind"] == "python"],
    ) == (42, 1, [])


@pytest.mark.parametrize("suite,coverage", [("matrix", True), ("nightly", False)])
def test_full_hosted_suite_retains_coverage_policy_and_owned_target(
    hosted_process, suite, coverage
):
    # Arrange
    run = hosted_process
    # Act
    result, calls = run("3.13", suite)
    actual = next(
        c
        for c in calls
        if c["kind"] == "python" and c["args"][:3] == ["-m", "pytest", "tests/"]
    )
    # Assert
    assert (
        result.returncode,
        actual["store"],
        "--cov=src/scitex_agent_container" in actual["args"],
    ) == (0, False, coverage)


@pytest.mark.parametrize("suite", ["matrix", "nightly"])
def test_hosted_suite_uses_full_collection_and_owned_target(hosted_process, suite):
    # Arrange
    run = hosted_process
    # Act
    _, calls = run("3.13", suite)
    actual = next(
        c
        for c in calls
        if c["kind"] == "python" and c["args"][:3] == ["-m", "pytest", "tests/"]
    )
    venv = next(
        c["args"][-1] for c in calls if c["kind"] == "uv" and c["args"][0] == "venv"
    )
    # Assert
    assert (
        "--dist" in actual["args"],
        "-rfEs" in actual["args"],
        Path(venv).parent.name.startswith("sac-hosted-3.13."),
    ) == (True, True, True)


def test_missing_plugin_refuses_before_test_execution(hosted_process):
    # Arrange
    run = hosted_process
    # Act
    result, calls = run("3.11", "matrix", PLUGINS="asyncio, xdist, cov")
    actual = [
        c
        for c in calls
        if c["kind"] == "python" and c["args"][:3] == ["-m", "pytest", "tests/"]
    ]
    # Assert
    assert (result.returncode, actual) == (1, [])


def test_test_failure_propagates_to_the_hosted_leg(hosted_process):
    # Arrange
    run = hosted_process
    # Act
    result, _ = run("3.11", "matrix", SUITE_EXIT="7")
    # Assert
    assert result.returncode == 7


@pytest.mark.parametrize("args", [("3.10", "matrix"), ("3.11", "unknown")])
def test_unknown_suite_or_python_refuses_before_uv(hosted_process, args):
    # Arrange
    run = hosted_process
    # Act
    result, calls = run(*args)
    # Assert
    assert (result.returncode, calls) == (2, [])
