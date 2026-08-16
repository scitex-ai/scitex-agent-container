# -*- coding: utf-8 -*-
# File: tests/integration/image_build_hooks/conftest.py
"""Fixtures for the ``deny_raw_apptainer_build.sh`` suite.

The hook is a ``.sh`` asset with no ``tests/<pkg>/`` mirror counterpart, so
— like the sibling ``heavy_job_hooks`` / ``hpc_login_hooks`` suites — these
tests live under ``tests/integration/`` to stay OUT of PS-204's mirror
scope while ``pytest tests/`` still collects and runs them.

The hook is resolved by repo-relative path so a refactor of the asset
location is caught at collection time, not at agent-boot time.

No mocks: every case drives the real script in a subprocess with the real
PreToolUse JSON envelope Claude Code sends, and asserts on the real exit
code / stderr.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

HOOK_DIR = (
    REPO_ROOT
    / "src"
    / "scitex_agent_container"
    / "_baseline_assets"
    / "image_build_hooks"
)

HOOK = HOOK_DIR / "deny_raw_apptainer_build.sh"

#: The real shipped recipes, used to prove the guard works on the actual
#: artifact rather than only on a synthetic stand-in.
RECIPES_DIR = REPO_ROOT / "src" / "scitex_agent_container" / "containers"

#: Exit code Claude Code reads as "deny this tool call".
DENY = 2
ALLOW = 0


def _payload(command: str) -> str:
    return json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": command}}
    )


@pytest.fixture
def run_hook():
    """Drive the hook with ``command``; return the CompletedProcess."""

    def _run(command: str, env: dict[str, str] | None = None):
        return subprocess.run(
            ["bash", str(HOOK)],
            input=_payload(command),
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    return _run


@pytest.fixture
def sac_like_recipe(tmp_path: Path) -> Path:
    """A recipe carrying ONLY the sac label key, under a neutral filename.

    The layer value is deliberately one that does not exist yet: the guard
    must key on the label KEY, so that a stage added by the in-flight .def
    restructuring is caught the day it lands rather than silently skipped.
    """
    recipe = tmp_path / "some-renamed-stage.def"
    recipe.write_text(
        "Bootstrap: docker\n"
        "From: ubuntu@sha256:dead\n"
        "%labels\n"
        "    org.scitex.layer a-stage-that-does-not-exist-yet\n",
        encoding="utf-8",
    )
    return recipe


@pytest.fixture
def unrelated_recipe(tmp_path: Path) -> Path:
    """Somebody else's recipe — must keep building."""
    recipe = tmp_path / "unrelated.def"
    recipe.write_text(
        "Bootstrap: docker\n"
        "From: alpine:3.20\n"
        "%labels\n"
        "    org.example.layer whatever\n",
        encoding="utf-8",
    )
    return recipe

# EOF
