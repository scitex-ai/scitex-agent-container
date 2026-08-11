"""The hook audit-log env contract — the wrapper writes it, the core reads it.

Every enforcement hook under ``_baseline_assets`` ships as a WRAPPER (``.sh``)
plus a decision CORE, and the wrapper hands the core its audit-log destination
through ONE environment variable. Rename one half only and the hook still
gates correctly — same exit code, same block message — while logging NOWHERE.
Every other hook test asserts exit codes and stderr text, so not one of them
can see that: the audit trail just disappears, silently.

That failure mode is why this file exists. On 2026-08-12 the variable was
renamed from the unnamespaced ``LOG_PATH`` to
``SCITEX_AGENT_CONTAINER_HOOK_LOG_PATH``, closing a scitex-dev §6a audit-cli
ERROR ("env var 'LOG_PATH' has no recognized prefix"). Three wrapper/core
pairs had to move together. These tests pin BOTH halves to the SAME name so a
later edit cannot drift them apart without going red.

The hooks are COPIED into ``tmp_path`` and run from there. That is how they
are really deployed (every file of a pair lands in one directory), and it
makes each wrapper resolve ``$THIS_DIR`` — and therefore its own log file —
inside the temp dir. So the log assertion needs no repo write and no
monkeypatching: the real ``.sh`` drives the real core over a real subprocess,
which is the fleet rule for hook tests.

``LOG_PATH`` remains perfectly fine as a shell-LOCAL variable inside a
wrapper — it never leaves the process, and §6a governs the env-var surface,
not shell locals. What it may not be again is the name crossing into the
child's environment.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

LOG_ENV = "SCITEX_AGENT_CONTAINER_HOOK_LOG_PATH"

_ASSETS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "scitex_agent_container"
    / "_baseline_assets"
)

# Env vars that would let the ambient environment decide a hook's verdict.
# Scrubbed from every invocation so each case controls its own inputs.
_SCRUB = (
    "SAC_HPC_LOGIN_ALLOW",
    "SAC_HPC_LOGIN_NODE_PATTERN",
    "SAC_HPC_LOGIN_TEST_HOSTNAME",
    "SAC_HPC_LOGIN_EXTRA_ALLOW",
    "SAC_HPC_LOGIN_PYC_MAX",
    "SAC_HEAVY_JOB_ALLOW",
    "SAC_HEAVY_JOB_GUARD_DISABLE",
    "SAC_HEAVY_JOB_JOBS_MAX",
    "SAC_HEAVY_JOB_EXTRA_DENY",
    LOG_ENV,
)

# (hook dir, files to deploy, wrapper, blocking command, extra env)
_PAIRS = (
    (
        "hpc_login_hooks",
        (
            "enforce_hpc_login_node_whitelist.sh",
            "hpc_login_whitelist_core.py",
            "hpc_login_whitelist_policy.py",
        ),
        "enforce_hpc_login_node_whitelist.sh",
        "pytest tests/ -x",
        {"SAC_HPC_LOGIN_TEST_HOSTNAME": "spartan-login1.hpc.unimelb.edu.au"},
    ),
    (
        "heavy_job_hooks",
        (
            "enforce_heavy_job_demotion.sh",
            "heavy_job_demotion_core.py",
            "heavy_job_demotion_policy.py",
        ),
        "enforce_heavy_job_demotion.sh",
        "mksquashfs squashfs-root out.squashfs",
        {},
    ),
)

# Every wrapper that hands a log destination to a python child.
_WRAPPERS = (
    ("hpc_login_hooks", "enforce_hpc_login_node_whitelist.sh"),
    ("heavy_job_hooks", "enforce_heavy_job_demotion.sh"),
    ("git_identity_hooks", "enforce_commit_author_allowlist.sh"),
)

_GIT_IDENTITY_HOOK = (
    _ASSETS / "git_identity_hooks" / "enforce_commit_author_allowlist.sh"
)


def _clean_env(extra: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    env.update(extra)
    return env


@pytest.fixture(params=_PAIRS, ids=["hpc-login", "heavy-job"])
def blocked_run(request, tmp_path):
    """Deploy one hook pair into `tmp_path` and drive it to a BLOCK verdict.

    Returns the completed process plus the log path the WRAPPER chose, so
    each test below can assert exactly one thing about the outcome.
    """
    hook_dir, files, wrapper, command, extra = request.param

    dest = tmp_path / hook_dir
    dest.mkdir()
    for name in files:
        shutil.copy2(_ASSETS / hook_dir / name, dest / name)

    log = dest / f".{wrapper}.log"
    if log.exists():  # pragma: no cover  -- fresh tmp_path; guard, not a case
        raise AssertionError(f"log unexpectedly pre-existed at {log}")

    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(tmp_path),
    }
    res = subprocess.run(
        ["bash", str(dest / wrapper)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=_clean_env(extra),
    )
    return SimpleNamespace(res=res, log=log, wrapper=wrapper)


def test_heavy_command_is_blocked(blocked_run):
    """Precondition for the log assertions: the hook really did block.

    Without this the next test could pass vacuously in the other direction —
    no log because nothing was ever blocked.
    """
    # Arrange — the fixture deployed the pair and fed it a heavy command.
    run = blocked_run
    # Act — the verdict is the process exit code.
    verdict = run.res.returncode
    # Assert
    assert verdict == 2, (
        f"expected a block (exit 2) from {blocked_run.wrapper}; got "
        f"{blocked_run.res.returncode}. stderr:\n{blocked_run.res.stderr}"
    )


def test_block_is_recorded_in_the_wrapper_chosen_log_file(blocked_run):
    """The core writes where the wrapper told it to — the whole contract.

    Falsifiable, and precisely so: rename the variable in the wrapper but not
    the core (or the reverse) and the hook still exits 2 — the block still
    works — but this file is never created and this test goes red. That is
    exactly the half-rename this module guards against.
    """
    # Arrange — the fixture blocked a command through the real wrapper.
    run = blocked_run
    # Act — ask whether the core wrote where the wrapper pointed it.
    wrote_the_log = run.log.is_file()
    # Assert
    assert wrote_the_log, (
        f"{blocked_run.wrapper} blocked the command but wrote no audit log "
        f"at {blocked_run.log}. Wrapper and core disagree about {LOG_ENV}: "
        f"the hook still gates, but its audit trail is silently gone."
    )


def test_audit_log_names_the_decision(blocked_run):
    # Arrange
    log = blocked_run.log
    # Act
    written = log.read_text(encoding="utf-8")
    # Assert
    assert "BLOCK" in written


@pytest.mark.parametrize(("hook_dir", "wrapper"), _WRAPPERS)
def test_wrapper_exports_the_namespaced_log_var(hook_dir, wrapper):
    # Arrange
    text = (_ASSETS / hook_dir / wrapper).read_text(encoding="utf-8")
    # Act — the env-prefix assignment on the pipe into python.
    exported = re.search(rf'\|\s*{LOG_ENV}="\$LOG_PATH"\s+python3', text)
    # Assert
    assert exported, (
        f"{wrapper} does not pass {LOG_ENV} to its python child; the child "
        f"then reads an unset variable and logs nowhere."
    )


@pytest.mark.parametrize(("hook_dir", "wrapper"), _WRAPPERS)
def test_wrapper_does_not_export_bare_log_path(hook_dir, wrapper):
    # Arrange
    text = (_ASSETS / hook_dir / wrapper).read_text(encoding="utf-8")
    # Act
    bare = re.search(r'\|\s*LOG_PATH="\$LOG_PATH"\s+python3', text)
    # Assert
    assert bare is None, (
        f"{wrapper} passes the unnamespaced LOG_PATH into the child "
        f"environment — that re-opens the scitex-dev §6a audit-cli ERROR."
    )


def test_git_identity_inline_python_reads_the_namespaced_var():
    """This hook's writer and reader both live in ONE file — pin them together.

    `enforce_commit_author_allowlist.sh` carries its decision body as inline
    python passed to `python3 -c`, so there is no separate core module to
    drive. The contract is still two-sided, and a one-sided edit here is just
    as silent, so the reader half is asserted directly.
    """
    # Arrange
    text = _GIT_IDENTITY_HOOK.read_text(encoding="utf-8")
    # Act
    reads = re.search(rf'os\.environ\.get\("{LOG_ENV}", ""\)', text)
    # Assert
    assert reads, (
        "the inline-python half no longer reads the log var the surrounding "
        "shell exports"
    )


def test_git_identity_inline_python_does_not_read_bare_log_path():
    # Arrange
    text = _GIT_IDENTITY_HOOK.read_text(encoding="utf-8")
    # Act
    reads_bare = 'os.environ.get("LOG_PATH"' in text
    # Assert
    assert not reads_bare, (
        "the inline python still reads the unnamespaced LOG_PATH — the two "
        "halves of the contract have drifted apart."
    )
