"""The artifact-gate probe must not reference a name it never binds.

WHY THIS EXISTS (regression, 2026-08-16, sac #1072):

#1072 deleted the ``scitex_todo`` shim and removed every ``import scitex_todo``
line. It did NOT remove the line that USES the name::

    import scitex_cards
    if scitex_todo is not scitex_cards:      # <- NameError at runtime
        sys.exit(1)

The probe is an ARTIFACT GATE that ``sys.exit(1)``s, so the bake aborted — and
it aborted with ``NameError: name 'scitex_todo' is not defined``, which reads
like "the shim is missing from the image" rather than "the probe is broken".
That misdirection was the expensive part; scitex-cards found it by execution.

WHY THE ORIGINAL VERIFICATION MISSED IT: it counted anchored
``^\\s*(import|from)\\s+scitex_todo`` lines and correctly got zero. That answers
"is the import gone", never "is the NAME gone" — and the surviving reference was
not an import, so the anchor could not see it. A search answers "is this string
present", never "does this behaviour exist".

So this test asks the second question directly, two ways:

1. AST: every module-level name the probe LOADS must be bound (import,
   assignment, def/class) or be a builtin. This is what actually catches a
   dangling reference, and it needs no package installed.
2. Token: the embedded copies of the probe inside the .def/.sh bake recipes are
   not importable Python files, so they get the blunt check — the token must not
   appear on any non-comment line.

Both must hold. Deleting either one restores the blind spot.
"""

import ast
import builtins
import pathlib

import pytest

CONTAINERS = (
    pathlib.Path(__file__).resolve().parents[3]
    / "src"
    / "scitex_agent_container"
    / "containers"
)

PROBE = CONTAINERS / "sif_symbol_probe.py"

# The bake recipes embed the same probe as a heredoc, so they cannot be parsed
# as Python — they get the token check instead.
EMBEDS = (
    CONTAINERS / "apptainer-base.def",
    CONTAINERS / "apptainer-scitex.def",
    CONTAINERS / "spartan-sif-bake.sh",
)


def _module_level_bound_names(tree: ast.Module) -> set[str]:
    """Names bound at module level: imports, assignments, defs, classes, with/for."""
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
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }


def test_probe_loads_no_name_it_never_binds():
    """The exact defect: a name used but never imported or assigned."""
    # Arrange: the real probe as it will be baked into the image
    source = PROBE.read_text()

    # Act: bind-vs-load analysis, which needs no package installed
    tree = ast.parse(source, filename=str(PROBE))
    undefined = _loaded_names(tree) - _module_level_bound_names(tree) - set(dir(builtins))

    # Assert
    assert not undefined, (
        f"{PROBE.name} references name(s) it never binds: {sorted(undefined)}. "
        "This is a runtime NameError inside an artifact gate that sys.exit(1)s, "
        "so the bake aborts with a message about the wrong thing."
    )


def test_the_detector_itself_catches_the_1072_shape():
    """A gate I cannot show failing is not a gate.

    Feed the checker the exact code #1072 shipped and require it to object.
    Without this, a checker that silently stopped working would look identical
    to a clean repo.
    """
    # Arrange: verbatim shape of what #1072 shipped
    broken = (
        "import sys\n"
        "import scitex_cards\n"
        "if scitex_todo is not scitex_cards:\n"
        "    sys.exit(1)\n"
    )

    # Act
    tree = ast.parse(broken)
    undefined = _loaded_names(tree) - _module_level_bound_names(tree) - set(dir(builtins))

    # Assert
    assert undefined == {"scitex_todo"}, (
        f"the undefined-name check no longer detects the #1072 defect; got {undefined}"
    )


@pytest.mark.parametrize("path", EMBEDS, ids=lambda p: p.name)
def test_bake_recipes_do_not_use_the_deleted_name(path: pathlib.Path):
    """The embedded probe copies must not mention the deleted module either.

    Comments may still explain the history — that is documentation, not a
    reference the interpreter will try to resolve.
    """
    # Arrange
    lines = path.read_text().splitlines()

    # Act: keep only mentions that survive stripping the comment
    offenders = []
    for lineno, line in enumerate(lines, 1):
        if "scitex_todo" not in line:
            continue
        code = line.split("#", 1)[0]
        if "scitex_todo" in code:
            offenders.append(f"{path.name}:{lineno}: {line.strip()}")

    # Assert
    assert not offenders, "deleted module referenced in executable position:\n" + "\n".join(
        offenders
    )
