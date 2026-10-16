"""Programmatic surface for the skill peer-docs registry pin.

The bundled skill docs once told readers peers come only from
``config.yaml`` while the code resolves the scitex-dev hosts registry
too (``peers_with_registry``). Skills that drift from code teach every
reader the wrong mechanism, so the pin is: wherever a shipped skill
doc names the config peers source, the registry SSOT must be named
alongside it (same paragraph).

This module gives test code (and any tooling that wants to verify the
docs still agree with the code) a single import-able handle: the
claim spellings observed in the wild, the window in which the
registry correction must appear, and the scanner itself.

Reference: ``peers_with_registry`` in ``_state/_peer_resolve.py``
(the code side of the agreement).
"""

from __future__ import annotations

from pathlib import Path

# Claim spellings observed in the wild, each paired with the window in
# which the registry correction must appear.
CLAIMS: tuple[str, ...] = (
    "defined in `config.yaml`'s `peers:` block",
    "peers are\n`config.yaml::peers:`",
)

WINDOW: int = 400


def iter_skill_docs(skill_dir: Path) -> list[Path]:
    """Return the shipped skill markdown files under ``skill_dir``."""
    return sorted(skill_dir.glob("*.md"))


def find_config_only_offenders(skill_dir: Path) -> list[str]:
    """Return skill docs that state config-only peers.

    Every doc that names a config-peers claim must name the registry
    SSOT within ``WINDOW`` characters of it; entries that do not are
    returned as ``"<leaf>: <claim>..."`` strings.
    """
    offenders: list[str] = []
    for path in iter_skill_docs(skill_dir):
        text = path.read_text(encoding="utf-8")
        for claim in CLAIMS:
            start = text.find(claim)
            while start != -1:
                window = text[start : start + len(claim) + WINDOW]
                if "registry" not in window:
                    offenders.append(f"{path.name}: {claim[:40]}...")
                    break
                start = text.find(claim, start + 1)
    return offenders
