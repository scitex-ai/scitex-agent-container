"""Peer docs must not state config-only sourcing.

Measured 2026-10-15: bundled skill docs told readers peers come only
from ``config.yaml`` while the code resolves the scitex-dev hosts
registry too (``peers_with_registry``). Skills that drift from code
teach every reader the wrong mechanism, so this pins it: wherever a
shipped skill doc names the config peers source, the registry SSOT
must be named alongside it (same paragraph).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]

SKILL_DIR = (
    REPO_ROOT / "src" / "scitex_agent_container" / "_skills" / "scitex-agent-container"
)

# Claim spellings observed in the wild, each paired with the window in
# which the registry correction must appear.
_CLAIMS = (
    "defined in `config.yaml`'s `peers:` block",
    "peers are\n`config.yaml::peers:`",
)

_WINDOW = 400


def test_config_peer_claims_name_the_registry() -> None:
    # Arrange
    files = sorted(SKILL_DIR.glob("*.md"))
    # Act
    offenders = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for claim in _CLAIMS:
            start = text.find(claim)
            while start != -1:
                window = text[start : start + len(claim) + _WINDOW]
                if "registry" not in window:
                    offenders.append(f"{path.name}: {claim[:40]}...")
                    break
                start = text.find(claim, start + 1)
    # Assert
    assert files, "no skill docs found — the pin covers nothing"
    assert not offenders, f"skill docs state config-only peers: {offenders}"
