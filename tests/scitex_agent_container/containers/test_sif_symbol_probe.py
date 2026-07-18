"""Tests for the SIF artifact symbol-probe asset (`containers/sif_symbol_probe.py`).

The probe is package DATA, not an importable module: it runs INSIDE a freshly
baked SIF (`python sif_symbol_probe.py`) and `sys.exit(1)`s the bake when the
container's symbols are wrong. Importing it HERE would execute that gate against
this test interpreter, so every check validates the asset statically (read +
compile + AST) — never by import. The point is WATCH-IT-STILL-GATE: if a future
edit drops the scitex_cards symbol check or the fail-loud exit, these go red.
"""

from __future__ import annotations

import ast
from pathlib import Path

import scitex_agent_container

PROBE = (
    Path(scitex_agent_container.__file__).resolve().parent
    / "containers"
    / "sif_symbol_probe.py"
)


def _probe_source() -> str:
    return PROBE.read_text(encoding="utf-8")


def _imported_names(source: str) -> set[str]:
    tree = ast.parse(source, str(PROBE))
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }


def _from_imports(source: str) -> set[tuple[str | None, str]]:
    tree = ast.parse(source, str(PROBE))
    return {
        (node.module, alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }


def _has_nonzero_sys_exit(source: str) -> bool:
    tree = ast.parse(source, str(PROBE))
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "exit"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "sys"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value not in (0, None)
        for node in ast.walk(tree)
    )


def test_probe_asset_ships_next_to_the_container_recipes() -> None:
    # Arrange — _remote_bake_core.SYMBOL_PROBE resolves to this exact path and
    # the master-side verify runs it inside the pulled SIF; a missing file makes
    # the gate un-runnable (which the pipeline treats as FAILED, never passed).
    probe = PROBE
    # Act
    exists = probe.is_file()
    # Assert
    assert exists


def test_probe_is_valid_python() -> None:
    # Arrange — the probe is executed verbatim inside the SIF; a syntax error
    # would surface only at bake time on Spartan, far from this repo. compile()
    # raises SyntaxError on malformed source, so reaching the assert means valid.
    source = _probe_source()
    # Act
    compiled = compile(source, str(PROBE), "exec")
    # Assert
    assert compiled is not None


def test_probe_imports_scitex_cards() -> None:
    # Arrange — the probe asserts BY SYMBOL (never version strings) that the SIF
    # shipped a whole scitex_cards; dropping this import blinds the gate.
    source = _probe_source()
    # Act
    imported = _imported_names(source)
    # Assert
    assert "scitex_cards" in imported


def test_probe_imports_the_scitex_todo_shim() -> None:
    # Arrange — scitex_todo must resolve to the scitex_cards shim inside the SIF;
    # the probe imports it so it can prove the two are the same module tree.
    source = _probe_source()
    # Act
    imported = _imported_names(source)
    # Assert
    assert "scitex_todo" in imported


def test_probe_checks_the_wip_statuses_symbol() -> None:
    # Arrange — WIP_STATUSES membership is the concrete symbol the gate reads to
    # prove scitex_cards._throughput is present and whole in the baked image.
    source = _probe_source()
    # Act
    from_imports = _from_imports(source)
    # Assert
    assert ("scitex_cards._throughput", "WIP_STATUSES") in from_imports


def test_probe_fails_loud_on_a_bad_sif() -> None:
    # Arrange — a gate that can only PASS is a hope; the probe must sys.exit()
    # non-zero so a mis-baked SIF is REJECTED, not silently published.
    source = _probe_source()
    # Act
    exits_nonzero = _has_nonzero_sys_exit(source)
    # Assert
    assert exits_nonzero
