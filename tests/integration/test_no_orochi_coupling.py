"""No-coupling guard: sac's shippable source must not know 'orochi'.

sac is a standalone container wrapper. A separate fleet product (orochi)
is a downstream CONSUMER of sac, never a dependency — the coupling must
only ever point one way. This guard asserts that coupling has not
regressed back into the shippable package: the string ``orochi`` in any
case, and the concrete wire identifiers that once tied sac to that
product, are ABSENT from ``src/`` (excluding the generated Sphinx HTML
mirror, which is build output regenerated from source by the docs CI).

This is the ONE file in the repository permitted to contain the string,
by operator sanction: a guard that forbids a token must name it to check.
Keep the literal confined to this file.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
# Generated Sphinx HTML mirror — build output, regenerated from source by
# the docs workflow. Not authored source, so out of scope for this guard.
_GENERATED = _SRC / "scitex_agent_container" / "_sphinx_html"

_NEEDLE = re.compile("orochi", re.IGNORECASE)

# Concrete wire identifiers that once coupled sac to the external product.
_WIRE_IDENTIFIERS = (
    "SCITEX_OROCHI_HUB_URL",
    "SCITEX_OROCHI_TOKEN",
    "SCITEX_OROCHI_AGENT",
    "SCITEX_OROCHI_MACHINE",
    "_orochi_dm",
    "mcp__scitex-orochi__",
)

_SKIP_SUFFIXES = {
    ".pyc",
    ".so",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".svg",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".pdf",
}


def _shippable_files():
    for path in _SRC.rglob("*"):
        if not path.is_file():
            continue
        if _GENERATED == path or _GENERATED in path.parents:
            continue
        if "__pycache__" in path.parts:
            continue
        if path.suffix.lower() in _SKIP_SUFFIXES:
            continue
        yield path


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


def test_shippable_source_has_no_coupling_string():
    # Arrange
    hits: list[str] = []
    # Act
    for path in _shippable_files():
        for lineno, line in enumerate(_read(path).splitlines(), 1):
            if _NEEDLE.search(line):
                rel = path.relative_to(_REPO_ROOT)
                hits.append(f"{rel}:{lineno}: {line.strip()}")
    # Assert
    assert hits == [], "coupling string found in shippable source:\n" + "\n".join(hits)


def test_no_wire_identifiers_in_shippable_source():
    # Arrange
    found: dict[str, list[str]] = {}
    # Act
    for path in _shippable_files():
        text = _read(path)
        for ident in _WIRE_IDENTIFIERS:
            if ident in text:
                found.setdefault(ident, []).append(str(path.relative_to(_REPO_ROOT)))
    # Assert
    assert found == {}, f"wire identifiers reintroduced into shippable source: {found}"
