"""The SHIPPED bake script must satisfy the stdin guard the caller enforces.

MEASURED (2026-07-19). PR #771 put ``--input=none`` on every ``srun`` in
``containers/spartan-sif-bake.sh``. The next real bake failed identically —
build complete, ``.partial`` left behind, no ``SAC_BAKE_RESULT`` line, exit 1
with nothing readable after ``bake-remote FAILED:``. The script that ran was
not the fixed one: ``sac image bake-remote`` pipes the script off the
INSTALLED wheel, and the installed wheel still held pre-#771 bytes. Its
version said ``0.22.0``, and so did the checkout — a wheel cache keyed on
``(name, version)`` had served the stale build straight back through a
``--force-reinstall``. The version string could not tell the two apart.

So this file checks the two halves that could each drift on their own:

* the script this repo SHIPS passes the preflight the caller runs, and
* the preflight still FAILS a script with the guards taken back off —
  a checker that cannot go red proves nothing about the one that is green.

The stdin mechanism itself (an srun draining the piped script, and the
controls that stop a "fix" from simply deleting the guarded work) is covered
behaviourally in ``test_spartan_sif_bake_stdin.py``.
"""

from __future__ import annotations

from pathlib import Path

import scitex_agent_container
from scitex_agent_container.cli_pkg import _remote_bake_core as core

BAKE_SCRIPT = (
    Path(scitex_agent_container.__file__).resolve().parent
    / "containers"
    / "spartan-sif-bake.sh"
)


def _script_source() -> str:
    return BAKE_SCRIPT.read_text(encoding="utf-8")


def test_the_shipped_bake_script_passes_the_stdin_preflight() -> None:
    # Arrange — the caller refuses to bake when this is non-empty, so a
    # regression here takes the whole periodic bake offline loudly rather
    # than silently producing another orphan .partial.
    source = _script_source()
    # Act
    offenders = core.unguarded_srun_invocations(source)
    # Assert
    assert offenders == []


def test_control_the_preflight_catches_the_script_with_its_guards_removed() -> None:
    # Arrange — MUTATION PROOF. Strip the guard back off the real shipped
    # script; if the preflight still reports it clean, the green above is
    # measuring nothing and this file is decoration.
    mutated = _script_source().replace("--input=none", "")
    # Act
    offenders = core.unguarded_srun_invocations(mutated)
    # Assert
    assert len(offenders) == 3


def test_control_the_preflight_names_a_line_number_for_every_offender() -> None:
    # Arrange — the refusal message quotes these numbers; a zero or a None
    # in here would print an error that names nothing.
    mutated = _script_source().replace("--input=none", "")
    # Act
    numbered = [lineno for lineno, _ in core.unguarded_srun_invocations(mutated)]
    # Assert
    assert all(lineno > 0 for lineno in numbered)
