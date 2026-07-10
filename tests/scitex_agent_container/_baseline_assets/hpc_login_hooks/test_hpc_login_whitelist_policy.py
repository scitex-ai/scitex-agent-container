"""Mirror tests for ``hpc_login_whitelist_policy.py`` (policy invariants).

The behavioral suite (subprocess-driven, real PreToolUse payloads) lives
in ``tests/integration/hpc_login_hooks/`` — like the sibling
``git_identity_hooks``, the hook is exercised end-to-end there. THIS
mirror file guards the POLICY DATA invariants the engine relies on: the
whitelist and the blocked-class sets must be disjoint (a command in both
would make the verdict depend on evaluation order), and every blocked
class the engine can emit must carry an educational text (the operator's
key ask — a block without a taught alternative is a regression).

The module is loaded by file path (the ``_baseline_assets`` asset tree
is not an importable package — hook scripts deploy to
``$HOME/.claude/hooks/pre-tool-use/``).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_POLICY_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "scitex_agent_container"
    / "_baseline_assets"
    / "hpc_login_hooks"
    / "hpc_login_whitelist_policy.py"
)
_spec = importlib.util.spec_from_file_location(
    "hpc_login_whitelist_policy", _POLICY_PATH
)
policy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(policy)


def test_policy_module_file_exists():
    # Arrange
    path = _POLICY_PATH
    # Act
    present = path.is_file()
    # Assert
    assert present, f"missing policy module: {path}"


def test_whitelist_is_disjoint_from_every_blocked_class():
    # Arrange
    blocked = set().union(*policy.CLASS_SETS.values())
    # Act
    overlap = policy.ALLOW & blocked
    # Assert
    assert overlap == set(), f"commands both allowed and blocked: {overlap}"


def test_every_blocked_class_has_an_educational_text():
    # Arrange
    emitted_classes = set(policy.CLASS_SETS) | {
        "pyc_too_long",
        "git_heavy",
        "script",
        "default",
    }
    # Act
    missing = emitted_classes - set(policy.EDU)
    # Assert
    assert missing == set(), f"classes without educational text: {missing}"


def test_slurm_dispatch_verbs_are_whitelisted():
    # Arrange
    dispatch_verbs = {"srun", "sbatch", "salloc"}
    # Act
    covered = dispatch_verbs & policy.ALLOW
    # Assert
    assert covered == dispatch_verbs


def test_incident_class_scanners_are_not_whitelisted():
    # Arrange (2026-06-09 incident: du/find on spartan-login)
    scanners = {"du", "find", "ncdu"}
    # Act
    leaked = scanners & policy.ALLOW
    # Assert
    assert leaked == set(), f"incident-class scanners whitelisted: {leaked}"


def test_block_message_names_word_host_and_bypass():
    # Arrange
    msg = policy.block_message("pytest", "build_test", "spartan-login1", "spartan-login")
    # Act
    has_all = (
        "'pytest'" in msg
        and "spartan-login1" in msg
        and "SAC_HPC_LOGIN_ALLOW=1" in msg
        and "srun --overlap" in msg
    )
    # Assert
    assert has_all, msg


def test_pyc_max_defaults_to_500():
    # Arrange
    import os

    prior = os.environ.pop("SAC_HPC_LOGIN_PYC_MAX", None)
    # Act
    try:
        value = policy.pyc_max()
    finally:
        if prior is not None:
            os.environ["SAC_HPC_LOGIN_PYC_MAX"] = prior
    # Assert
    assert value == 500
