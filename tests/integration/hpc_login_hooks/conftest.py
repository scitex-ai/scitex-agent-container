"""Shared fixtures for the hpc_login_hooks tests.

Drives the real ``enforce_hpc_login_node_whitelist.sh`` shell hook (which
delegates to ``hpc_login_whitelist_core.py`` + ``_policy.py``) via
subprocess with real PreToolUse JSON payloads — no mocks, no patches (the
fleet rule for hook tests). The hostname gate is driven through the
hook's documented test seam ``SAC_HPC_LOGIN_TEST_HOSTNAME`` (an env
override, not a monkeypatch), mirroring how ``SAC_HOST_CLAUDE_DIR``
seams the host-merge tests.

The hook is a ``.sh`` asset with no ``tests/<pkg>/`` mirror counterpart,
so — like the sibling ``git_identity_hooks`` suite — these tests live
under ``tests/integration/`` to stay OUT of PS-204's mirror scope while
``pytest tests/`` still collects and runs them.

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
    / "hpc_login_hooks"
)
HOOK_SCRIPT = HOOK_DIR / "enforce_hpc_login_node_whitelist.sh"
CORE_SCRIPT = HOOK_DIR / "hpc_login_whitelist_core.py"
POLICY_SCRIPT = HOOK_DIR / "hpc_login_whitelist_policy.py"

LOGIN_HOSTNAME = "spartan-login1.hpc.unimelb.edu.au"
OFF_HOSTNAME = "ywata-note-win"

# Gate/knob env vars that would make the hook's decisions depend on the
# ambient environment; scrubbed from every invocation so each test
# controls its own inputs explicitly.
_SCRUB = (
    "SAC_HPC_LOGIN_ALLOW",
    "SAC_HPC_LOGIN_NODE_PATTERN",
    "SAC_HPC_LOGIN_TEST_HOSTNAME",
    "SAC_HPC_LOGIN_EXTRA_ALLOW",
    "SAC_HPC_LOGIN_PYC_MAX",
)


def _clean_env(extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    if extra:
        env.update(extra)
    return env


def run_hook(
    command: str,
    hostname: str = LOGIN_HOSTNAME,
    extra_env: dict | None = None,
) -> subprocess.CompletedProcess:
    """Invoke the hook with a Bash-tool PreToolUse payload; return the
    completed process so tests assert on returncode/stderr directly."""
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": "/tmp",
    }
    env = {"SAC_HPC_LOGIN_TEST_HOSTNAME": hostname}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=_clean_env(env),
    )


def run_hook_raw(
    stdin_text: str, hostname: str = LOGIN_HOSTNAME
) -> subprocess.CompletedProcess:
    """Invoke the hook with a RAW stdin payload (malformed-JSON cases)."""
    return subprocess.run(
        ["bash", str(HOOK_SCRIPT)],
        input=stdin_text,
        capture_output=True,
        text=True,
        env=_clean_env({"SAC_HPC_LOGIN_TEST_HOSTNAME": hostname}),
    )
