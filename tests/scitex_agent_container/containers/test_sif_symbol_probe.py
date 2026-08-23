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

import pytest

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


def test_probe_avoids_the_deleted_shim() -> None:
    # Arrange — INVERTED 2026-08-16. This asserted the probe imported scitex_todo,
    # which was right while that name was a shim onto scitex_cards. scitex-cards
    # DELETES the name outright (operator ruling: not deprecated, not aliased), so
    # the import it used to require now fails the BUILD — this probe runs inside
    # the container definitions, where an ImportError is a bake failure, not a
    # test failure. Asserting its ABSENCE keeps a gate that can still fail: if
    # anyone reinstates the import, this goes red before a bake does.
    source = _probe_source()
    # Act
    imported = _imported_names(source)
    # Assert
    assert "scitex_todo" not in imported


def test_probe_checks_the_wip_statuses_symbol() -> None:
    # Arrange — WIP_STATUSES membership is the concrete symbol the gate reads to
    # prove scitex_cards._throughput is present and whole in the baked image.
    source = _probe_source()
    # Act
    from_imports = _from_imports(source)
    # Assert
    assert ("scitex_cards._throughput", "WIP_STATUSES") in from_imports


def test_probe_checks_the_comment_merge_symbol() -> None:
    # Arrange — scitex-cards 0.49.0's comment-preserving mirror write. The bare
    # `import scitex_cards` CANNOT catch its absence: 0.48.0 imports perfectly
    # and then silently drops peer comment rows on every card write.
    source = _probe_source()
    # Act
    from_imports = _from_imports(source)
    # Assert
    assert ("scitex_cards._mirror_rows", "_merge_unseen_comment_rows") in from_imports


def test_probe_fails_loud_on_a_bad_sif() -> None:
    # Arrange — a gate that can only PASS is a hope; the probe must sys.exit()
    # non-zero so a mis-baked SIF is REJECTED, not silently published.
    source = _probe_source()
    # Act
    exits_nonzero = _has_nonzero_sys_exit(source)
    # Assert
    assert exits_nonzero


# ---------------------------------------------------------------------------
# UNDEFINED-NAME CHECKS (added 2026-08-16 after sac #1072)
#
# #1072 deleted the ``scitex_todo`` shim and removed every ``import
# scitex_todo`` line. It did NOT remove the line that USES the name::
#
#     import scitex_cards
#     if scitex_todo is not scitex_cards:      # <- NameError at runtime
#         sys.exit(1)
#
# The probe sys.exit(1)s the bake, so it aborted with ``NameError: name
# 'scitex_todo' is not defined`` — which reads like "the shim is missing from
# the image" rather than "the probe is broken". scitex-cards found it by
# EXECUTING the lines; the suite stayed green because nothing executes the
# probe.
#
# WHY THE ORIGINAL VERIFICATION MISSED IT: it counted anchored
# ``^\s*(import|from)\s+scitex_todo`` lines and correctly got zero. That
# answers "is the import gone", never "is the NAME gone" — and the surviving
# reference was not an import, so the anchor could not see it.
# ---------------------------------------------------------------------------

import builtins  # noqa: E402

#: The bake recipes embed the same probe as a heredoc, so they cannot be
#: parsed as Python and get a token check instead.
EMBEDS = (
    PROBE.parent / "apptainer-base.def",
    PROBE.parent / "apptainer-scitex.def",
    PROBE.parent / "spartan-sif-bake.sh",
)


def _module_level_bound_names(tree: ast.Module) -> set[str]:
    """Names bound at module level: imports, assignments, defs, args, excepts."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                bound.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                bound.add(a.asname or a.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
    return bound


def _loaded_names(tree: ast.Module) -> set[str]:
    return {
        n.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }


def _undefined(source: str) -> set[str]:
    tree = ast.parse(source)
    return _loaded_names(tree) - _module_level_bound_names(tree) - set(dir(builtins))


def test_probe_loads_no_name_it_never_binds() -> None:
    """The exact #1072 defect: a name used but never imported or assigned."""
    # Arrange
    source = _probe_source()
    # Act
    undefined = _undefined(source)
    # Assert
    assert not undefined, (
        f"{PROBE.name} references name(s) it never binds: {sorted(undefined)} — "
        "a runtime NameError inside a gate that sys.exit(1)s, so the bake "
        "aborts with a message about the wrong thing."
    )


def test_the_undefined_name_check_still_catches_the_1072_shape() -> None:
    """A gate I cannot show failing is not a gate.

    Feeds the checker the exact code #1072 shipped and requires it to object,
    so a checker that silently stopped working cannot look like a clean repo.
    """
    # Arrange
    broken = (
        "import sys\n"
        "import scitex_cards\n"
        "if scitex_todo is not scitex_cards:\n"
        "    sys.exit(1)\n"
    )
    # Act
    undefined = _undefined(broken)
    # Assert
    assert undefined == {"scitex_todo"}, (
        f"the undefined-name check no longer detects the #1072 defect; got {undefined}"
    )


@pytest.mark.parametrize("path", EMBEDS, ids=lambda p: p.name)
def test_bake_recipes_do_not_use_the_deleted_name(path) -> None:
    """The embedded probe copies must not mention the deleted module either.

    Comments may still explain the history — that is documentation, not a
    reference the interpreter will try to resolve.
    """
    # Arrange
    lines = path.read_text(encoding="utf-8").splitlines()
    # Act
    offenders = [
        f"{path.name}:{n}: {ln.strip()}"
        for n, ln in enumerate(lines, 1)
        if "scitex_todo" in ln.split("#", 1)[0]
    ]
    # Assert
    assert not offenders, (
        "deleted module referenced in executable position:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize("path", EMBEDS, ids=lambda p: p.name)
def test_every_probe_copy_carries_the_comment_merge_symbol(path) -> None:
    """A floor bump that reaches ONE probe copy is not a fix.

    The probe exists four times — the shipped asset plus these three embedded
    heredocs — and each gates a different path (local bake, layered bake,
    Spartan bake, master-side verify of a pulled SIF). On 2026-08-23 the
    scitex-cards floor was raised to 0.49.0 because below it every card write
    silently destroys peer comment rows; a copy left un-updated keeps baking
    images that carry the defect while the other copies report a clean gate.
    Token check, not AST: these are shell heredocs and do not parse as Python.
    """
    # Arrange
    source = path.read_text(encoding="utf-8")
    # Act
    present = "_merge_unseen_comment_rows" in source
    # Assert
    assert present, (
        f"{path.name} embeds the symbol probe but not the 0.49.0 "
        "comment-merge check — this copy still passes on a 0.48.0 image"
    )
