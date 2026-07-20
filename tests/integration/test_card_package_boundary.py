"""sac's runtime code must not import the third-party task-card package.

sac and that application are separate lineages. sac emits its own operational
events to its own log (:mod:`scitex_agent_container._events`) and reads nothing
of anyone else's; whether anything ever consumes those events is not sac's
concern and must not appear in sac's imports.

WHY A TEST AND NOT A CONVENTION
    The boundary was stated in prose before and eroded anyway: five modules
    grew a direct dependency on that application's public writer, and one on a
    PRIVATE submodule of it, while every docstring involved went on describing
    the coupling as deliberate. A rule with no detector is a preference.

    The detector is deliberately file-only — it parses source with ``ast`` and
    imports nothing — so it cannot flake on an install profile, a missing
    optional dependency, or whatever happens to be on ``sys.path``. It sees the
    same bytes a reviewer would.

WHY AST AND NOT A GREP
    Every one of these modules discusses the card application at length in its
    docstrings, and several legitimately name its ENV VARS and CLI. A text
    search cannot tell a sentence about a package from a dependency on it. Only
    ``import`` / ``from … import`` statements count here.

EXACT MATCH, IN BOTH DIRECTIONS
    :func:`test_only_known_modules_import_card_package` asserts the offender
    set EQUALS :data:`KNOWN_IMPORTERS` rather than merely being contained in
    it. A new import fails the test — and so does quietly fixing one of the
    known two without removing it from the list, because a stale allowance is
    how an empty list eventually becomes a long one again.

WHY IT LIVES IN tests/integration/
    It asserts a CROSS-PACKAGE property of the whole source tree rather than
    the behaviour of any one module, so it has no ``src/`` counterpart to
    mirror and does not belong under ``tests/scitex_agent_container/``. It
    sits beside ``test_cross_package_imports.py`` — the positive gate listing
    the cross-package imports sac DOES have — as that gate's negative
    counterpart.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

#: Import names that mean "the third-party task-card application". Both are
#: listed because the older name is still installed as an alias shim for the
#: newer one, so importing either is the same dependency.
CARD_PACKAGES = frozenset({"scitex_todo", "scitex_cards"})

#: The ONLY files under ``src/`` still permitted to import it, each with the
#: reason it is not yet gone. Both are out of scope for the change that
#: introduced this test; neither is a runtime alarm path.
KNOWN_IMPORTERS: dict[str, str] = {
    "scitex_agent_container/_lifecycle/_rename_cards.py": (
        "sac agents rename migrates that application's records to the new "
        "agent name by calling its store directly. Removing this needs a "
        "decision about what a rename means across the boundary, not a "
        "mechanical edit — analysed and proposed separately."
    ),
    "scitex_agent_container/containers/sif_symbol_probe.py": (
        "A BUILD-TIME probe that asserts the package was installed into the "
        "image for OTHER agents to use. sac distributing a package is not sac "
        "depending on one — this file is never imported by sac at runtime."
    ),
}

_SRC = Path(__file__).resolve().parents[2] / "src"


def _imports_card_package(source: str) -> bool:
    """True when ``source`` contains a real import of the card application.

    Handles both statement forms at any nesting depth, so a lazy import inside
    a function — which is how every one of the removed dependencies was
    written — counts exactly like a module-level one.
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[0] in CARD_PACKAGES for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in CARD_PACKAGES:
                return True
    return False


def _offenders() -> set[str]:
    """Every file under ``src/`` importing the card application, repo-relative."""
    found = set()
    for path in sorted(_SRC.rglob("*.py")):
        if _imports_card_package(path.read_text(encoding="utf-8")):
            found.add(path.relative_to(_SRC).as_posix())
    return found


def test_only_known_modules_import_card_package() -> None:
    # Arrange
    allowed = set(KNOWN_IMPORTERS)

    # Act
    offenders = _offenders()

    # Assert
    assert offenders == allowed, (
        "sac's import boundary moved. Newly importing the card application: "
        f"{sorted(offenders - allowed)}. No longer importing it, so it must be "
        f"dropped from KNOWN_IMPORTERS: {sorted(allowed - offenders)}."
    )


@pytest.mark.parametrize(
    "module",
    [
        "scitex_agent_container/_reconcile/_alarm.py",
        "scitex_agent_container/_authheal/_alarm.py",
        "scitex_agent_container/_hostsync/_alarm.py",
        "scitex_agent_container/_maintenance/_worktree_gc_alarm.py",
        "scitex_agent_container/_account/refresh_alarm.py",
    ],
)
def test_alarm_rails_do_not_import_card_package(module: str) -> None:
    """Named individually so a regression says WHICH rail regressed.

    These five wrote to that application's store on every scheduled pass. They
    are the reason the boundary is enforced rather than described.
    """
    # Arrange
    source = (_SRC / module).read_text(encoding="utf-8")

    # Act
    imports_it = _imports_card_package(source)

    # Assert
    assert not imports_it, f"{module} imports the card application again"


def test_detector_sees_a_planted_import() -> None:
    """MUTATION PROOF: the detector must go RED on a reintroduced import.

    A boundary test that cannot fail is decoration. This plants the exact
    statement form the alarm rails used — a lazy ``from … import`` inside a
    function body — and asserts the detector catches it. Without this, a
    detector broken into always returning ``False`` would keep the suite green
    while the boundary quietly rotted.
    """
    # Arrange
    planted = (
        "def _upsert():\n    from scitex_todo import add_task\n    return add_task\n"
    )

    # Act
    detected = _imports_card_package(planted)

    # Assert
    assert detected, "the detector failed to see a lazy card-package import"


def test_detector_ignores_a_mere_mention() -> None:
    """The counterpart: prose about the package is not a dependency on it.

    Several modules legitimately name that application's env vars and CLI in
    docstrings and string constants. A detector that flagged those would be
    unusable, and would be silenced — which is the real failure mode.
    """
    # Arrange
    prose = (
        '"""Sets SCITEX_TODO_AGENT_ID; see scitex_todo._store."""\n'
        'CMD = "scitex-cards may-stop"\n'
    )

    # Act
    detected = _imports_card_package(prose)

    # Assert
    assert not detected, "the detector flagged a docstring mention as an import"
