"""Shared fixtures for the heavy_job_hooks tests.

Drives the real ``enforce_heavy_job_demotion.sh`` shell hook (which
delegates to ``heavy_job_demotion_core.py`` + ``_policy.py``) via
subprocess with real PreToolUse JSON payloads — no mocks, no patches
(the fleet rule for hook tests). Knobs are driven through the hook's
documented env vars, never a monkeypatch.

The hook is a ``.sh`` asset with no ``tests/<pkg>/`` mirror
counterpart, so — like the sibling ``hpc_login_hooks`` suite — these
tests live under ``tests/integration/`` to stay OUT of PS-204's mirror
scope while ``pytest tests/`` still collects and runs them.

The hook is resolved by repo-relative path so a refactor of the asset
location is caught at collection time, not at agent-boot time.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

HOOK_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "scitex_agent_container"
    / "_baseline_assets"
    / "heavy_job_hooks"
)
HOOK_SCRIPT = HOOK_DIR / "enforce_heavy_job_demotion.sh"
CORE_SCRIPT = HOOK_DIR / "heavy_job_demotion_core.py"
POLICY_SCRIPT = HOOK_DIR / "heavy_job_demotion_policy.py"

# Knob env vars that would make the hook's decisions depend on the
# ambient environment; scrubbed from every invocation so each test
# controls its own inputs explicitly.
_SCRUB = (
    "SAC_HEAVY_JOB_ALLOW",
    "SAC_HEAVY_JOB_GUARD_DISABLE",
    "SAC_HEAVY_JOB_JOBS_MAX",
    "SAC_HEAVY_JOB_EXTRA_DENY",
)


def _clean_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    if extra:
        env.update(extra)
    return env


def run_hook(
    command: str,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Invoke the hook with a Bash-tool PreToolUse payload; return the
    completed process so tests assert on returncode/stderr directly."""
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": "/tmp",
    }
    return subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=_clean_env(extra_env),
    )


def run_hook_raw(stdin_text: str) -> subprocess.CompletedProcess:
    """Invoke the hook with a RAW stdin payload (malformed-JSON cases)."""
    return subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        input=stdin_text,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
