"""Static contract: ``apptainer-base.def`` must install scitex-todo[mcp].

P3a-1.5 (operator directive ``feedback_scitex_todo_single_shared_store``,
lead a2a ``5c0c1fe32a9a43888e01151d6fc0fb9e``): every sac-launched
agent must be able to attach the scitex-todo MCP and write to the
shared store. The MCP wiring (P3a) lands a ``.mcp.json`` entry that
invokes ``scitex-todo mcp start`` — but that only works when
``scitex-todo`` is on PATH inside the SIF. The ``[mcp]`` extra pulls
in fastmcp>=2.0 (the FastMCP server backing
``scitex_todo._mcp_server:mcp``) without which ``mcp start`` exits
with a missing-extra hint.

This test pins both requirements as code: drop scitex-todo or its
``[mcp]`` extra from the .def and CI yells before a SIF rebuild
ships an agent that can't reach its shared todo store.

The sibling ``test_apptainer_scitex_def_libxcb.py`` sets the same
contract pattern at the :scitex layer; this file enforces the
:base layer.

STX-TQ002 AAA + STX-TQ007 one-assert per test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BASE_DEF = (
    _REPO_ROOT / "src" / "scitex_agent_container" / "containers" / "apptainer-base.def"
)


@pytest.fixture(scope="module")
def base_def_text() -> str:
    # Arrange
    return _BASE_DEF.read_text()


def _uv_pip_install_block(text: str) -> str:
    """Return the first ``uv pip install ...`` continuation chunk joined."""
    lines = text.splitlines()
    out: list[str] = []
    in_block = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("uv pip install"):
            in_block = True
        if in_block:
            out.append(stripped.rstrip("\\").strip())
            if not stripped.endswith("\\"):
                break
    return " ".join(out)


# ---------------------------------------------------------------------------
# scitex-todo install line is present + carries the [mcp] extra
# ---------------------------------------------------------------------------


def test_uv_pip_install_block_mentions_scitex_todo(base_def_text: str) -> None:
    # Arrange
    block = _uv_pip_install_block(base_def_text)
    # Act
    present = "scitex-todo" in block
    # Assert
    assert present, (
        f"scitex-todo missing from uv pip install in apptainer-base.def:\n{block}"
    )


def test_uv_pip_install_block_carries_mcp_extra(base_def_text: str) -> None:
    # Arrange
    block = _uv_pip_install_block(base_def_text)
    # Act
    present = "scitex-todo[mcp]" in block
    # Assert
    assert present, (
        "scitex-todo install must carry the [mcp] extra (pulls fastmcp>=2.0)"
        f" in apptainer-base.def:\n{block}"
    )


def test_uv_pip_install_block_pins_scitex_todo_minimum_version(
    base_def_text: str,
) -> None:
    # Arrange — a versioned constraint ensures the SIF gets at least
    # the version with FastMCP 2.x compatibility + the 16-tool surface.
    block = _uv_pip_install_block(base_def_text)
    # Act
    has_version_pin = ">=0.3.0" in block or "==0.3.0" in block or ">=0.3" in block
    # Assert
    assert has_version_pin, (
        "scitex-todo install must carry a version pin (>=0.3 or stricter)"
        f" in apptainer-base.def:\n{block}"
    )
