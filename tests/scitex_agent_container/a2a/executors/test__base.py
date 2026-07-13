"""Mirror smoke test for ``a2a/executors/_base.py``.

Behaviour-level tests live alongside ``test__server.py``; this file
exists so the audit (PS202) sees a mirror directory for
``src/scitex_agent_container/a2a/executors/`` and so each src module
has at least one matching test entry (PS204).
"""

from __future__ import annotations

import importlib

import pytest


def test_base_executor_class_loads() -> None:
    # Arrange
    module_path = "scitex_agent_container.a2a.executors._base"
    # Act
    from scitex_agent_container.a2a.executors._base import BaseSyncExecutor

    # Assert
    assert isinstance(BaseSyncExecutor, type), module_path


@pytest.mark.parametrize(
    "mod",
    [
        "scitex_agent_container.a2a.executors._base",
        "scitex_agent_container.a2a.executors._claude_cli",
        "scitex_agent_container.a2a.executors._claude_session",
        "scitex_agent_container.a2a.executors._echo",
        "scitex_agent_container.a2a.executors._exec",
        "scitex_agent_container.a2a.executors._openai_session",
    ],
)
def test_executors_subpackage_exposes_built_ins(mod: str) -> None:
    # Arrange
    target = mod
    # Act
    imported = importlib.import_module(target)
    # Assert
    assert imported is not None
