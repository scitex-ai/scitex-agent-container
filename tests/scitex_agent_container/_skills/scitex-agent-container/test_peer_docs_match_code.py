"""Peer docs must not state config-only sourcing.

Measured 2026-10-15: bundled skill docs told readers peers come only
from ``config.yaml`` while the code resolves the scitex-dev hosts
registry too (``peers_with_registry``). Skills that drift from code
teach every reader the wrong mechanism, so this pins it: wherever a
shipped skill doc names the config peers source, the registry SSOT
must be named alongside it (same paragraph).

The claim spellings and the scanner live in the ``peer_docs_match_code``
src leaf (same directory); this module only drives them. The leaf is
loaded from its file path because the ``scitex-agent-container`` skill
directory is not an importable package (hyphenated name).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]

SKILL_DIR = (
    REPO_ROOT / "src" / "scitex_agent_container" / "_skills" / "scitex-agent-container"
)

_SRC_LEAF = SKILL_DIR / "peer_docs_match_code.py"


def _load_src_leaf():
    """Load the ``peer_docs_match_code`` src leaf from its file path."""
    spec = importlib.util.spec_from_file_location("peer_docs_match_code", _SRC_LEAF)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"cannot load src leaf at {_SRC_LEAF}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PEER_DOCS = _load_src_leaf()


def test_skill_docs_exist_for_the_registry_pin() -> None:
    # Arrange
    skill_dir = SKILL_DIR
    # Act
    files = PEER_DOCS.iter_skill_docs(skill_dir)
    # Assert
    assert files, "no skill docs found — the pin covers nothing"


def test_config_peer_claims_name_the_registry() -> None:
    # Arrange
    skill_dir = SKILL_DIR
    # Act
    offenders = PEER_DOCS.find_config_only_offenders(skill_dir)
    # Assert
    assert not offenders, f"skill docs state config-only peers: {offenders}"
