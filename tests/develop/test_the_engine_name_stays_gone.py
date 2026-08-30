#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The engine is gone; this keeps its NAME from coming back.

WHY A NAME SCAN WHEN THE FOOTPRINT RATCHET ALREADY EXISTS
=========================================================
:mod:`test_retired_engine_footprint_frozen` asserts that nothing in this
repository USES the retired engine — nothing imports its driver, defines an
undeclared table, or opens it through a vendor library. That is the gate on
BEHAVIOUR, and it is the one that matters most.

It is not a gate on VOCABULARY, and vocabulary is how the last reversal
happened. Measured in the fleet campaign: a previous removal was undone in
effect rather than in commit — the code kept working while the DOCUMENTATION
went on naming the retired engine as the default, and a survey then counted 66
of 68 live tables sitting on it. Whoever put them there was following the prose
correctly. A rule that only bans the import leaves the sentence that does the
damage.

So this file bans the sentence. The two gates are deliberately independent: a
module can name the engine without importing it (prose), and — as the vendor
scan next door exists to prove — it can open the engine without naming it.
Neither scan implies the other.

THE RULE, AND WHOSE IT IS
=========================
Operator ruling 2026-08-29, applied fleet-wide with no per-package exceptions:
the name may appear only in ``docs/adr/``. An ADR records a decision that was
actually taken, and rewriting one destroys the record rather than the
dependency. Everything else — source, tests, documentation, and the CHANGELOG
— reaches zero.

The reference implementation is scitex-dev's
``tests/develop/test__sqlite_is_not_named_anywhere.py``. This is sac's copy of
that rule, not a competing one.

WHY TWO FILES ARE EXEMPT AND NOT ONE
====================================
A detector may name what it forbids, but only in a detector. sac has TWO,
because it guards two different properties: this file guards the NAME, and
:mod:`test_retired_engine_footprint_frozen` guards the USE. The footprint
ratchet cannot state its own rule without writing the driver's import line, the
vendor constructions it matches, and the operator instruction it exists to
carry — 82 lines of it, all load-bearing.

BOTH EXEMPTIONS ARE UNDER A STALENESS GATE. An exemption that stops matching
must be deleted, or it decays into a blessed filename that whatever drifts into
its place inherits. ``test_every_exempt_detector_really_names_the_engine``
below is what makes that impossible to forget.

THE SCAN IS OVER TRACKED FILES, via ``git ls-files``: a scratch file, a stray
download or an untracked note is not what ships.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

#: Repo root — this file is ``tests/develop/<name>.py``.
REPO = Path(__file__).resolve().parents[2]

#: The only place the name may still appear because of what the file IS,
#: rather than because of what it does: an ADR is a record of a decision, not
#: an instruction to a reader.
ALLOWED_PREFIXES = ("docs/adr/",)

#: The detectors. Each names the engine in order to refuse it, and each is
#: asserted below to still do so.
EXEMPT_DETECTORS = (
    "tests/develop/test_the_engine_name_stays_gone.py",
    "tests/develop/test_retired_engine_footprint_frozen.py",
)

#: Generated trees, rebuilt from ``src/``. A hit here is a stale artefact
#: rather than a source of truth, and failing on one sends the reader to
#: delete a build directory instead of fixing anything. NEITHER IS TRACKED IN
#: THIS REPO TODAY — kept because the scan reads what ``git ls-files`` returns,
#: and a packaging change is exactly the sort of thing that starts tracking
#: one without anybody deciding to.
IGNORED_PREFIXES = ("build/", "src/scitex_agent_container.egg-info/")

#: Ignore-rule files where a PATTERN line is a refusal, not a mention: writing
#: ``*.sqlite3`` into ``.gitignore`` is the repository declining to carry the
#: thing. Only pattern lines are exempt — a COMMENT inside such a file is
#: prose, and prose is what this gate is for. Adopted from scitex-dev's fix for
#: the same false positive.
IGNORE_RULE_BASENAMES = frozenset(
    {
        ".gitignore",
        ".dockerignore",
        ".npmignore",
        ".prettierignore",
        ".eslintignore",
        ".rgignore",
        ".ignore",
    }
)

_NAME = re.compile(r"sqlite", re.IGNORECASE)


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def _is_ignore_pattern(rel: str, line: str) -> bool:
    """Whether ``line`` is an exclusion RULE rather than prose about one."""
    if Path(rel).name not in IGNORE_RULE_BASENAMES:
        return False
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _names_the_engine(rel: str) -> list[str]:
    """Every line of ``rel`` naming the engine, as ``path:lineno: text``."""
    try:
        text = (REPO / rel).read_text(errors="ignore")
    except (OSError, UnicodeDecodeError):  # pragma: no cover - binary/unreadable
        return []
    return [
        f"{rel}:{number}: {line.strip()[:110]}"
        for number, line in enumerate(text.splitlines(), start=1)
        if _NAME.search(line) and not _is_ignore_pattern(rel, line)
    ]


def _offenders() -> list[str]:
    hits: list[str] = []
    for rel in _tracked_files():
        if rel.startswith(ALLOWED_PREFIXES) or rel.startswith(IGNORED_PREFIXES):
            continue
        if rel in EXEMPT_DETECTORS:
            continue
        hits.extend(_names_the_engine(rel))
    return hits


# ----------------------------------------------------------------------
# Controls. A scan that cannot fail is not a scan.
# ----------------------------------------------------------------------


def test_the_scan_can_actually_find_the_name() -> None:
    """POSITIVE CONTROL — the pattern matches the name it exists to catch.

    Without this, an over-eager edit to the regex would return zero offenders
    and read exactly like success.
    """
    # Arrange
    probe = "a line mentioning SQLite in passing"
    # Act
    found = _NAME.search(probe)
    # Assert
    assert found is not None


def test_the_tracked_file_list_is_not_empty() -> None:
    """SECOND CONTROL — the scan must have had something to look at.

    A broken ``git ls-files`` (wrong cwd, no repo, a worktree the subprocess
    cannot see) returns nothing, and an empty population produces an empty
    offender list that is indistinguishable from a clean tree.
    """
    # Arrange
    # Act
    tracked = _tracked_files()
    # Assert
    assert len(tracked) > 100


def test_an_ignore_rule_pattern_is_not_an_offence() -> None:
    """A ``.gitignore`` line refusing the engine is a refusal, not a mention."""
    # Arrange
    rel = "some/dir/.gitignore"
    # Act
    exempt = _is_ignore_pattern(rel, "*.sqlite3\n")
    # Assert
    assert exempt is True


def test_a_comment_inside_an_ignore_rule_file_is_still_prose() -> None:
    """NEGATIVE CONTROL — the ignore-file exemption covers PATTERNS only.

    Without this the exemption would launder a whole file: anyone could park a
    paragraph about the retired engine behind a ``#`` in ``.gitignore``.
    """
    # Arrange
    rel = ".gitignore"
    # Act
    exempt = _is_ignore_pattern(rel, "# we used to keep sqlite files here\n")
    # Assert
    assert exempt is False


def test_the_ignore_exemption_does_not_reach_ordinary_files() -> None:
    """NEGATIVE CONTROL — matching is by BASENAME, not by content shape."""
    # Arrange
    rel = "src/scitex_agent_container/_state/state_db.py"
    # Act
    exempt = _is_ignore_pattern(rel, "*.sqlite3\n")
    # Assert
    assert exempt is False


def test_every_exempt_detector_really_names_the_engine() -> None:
    """STALENESS GATE — an exemption that stops matching must be deleted.

    Two files are excused here, which is one more than the reference rule
    allows itself, so the excuse has to keep earning itself. A detector that no
    longer names the engine is no longer a detector; leaving it listed turns a
    reasoned exemption into a blessed coordinate that the next file to take
    that path inherits for free.
    """
    # Arrange
    silent = [rel for rel in EXEMPT_DETECTORS if not _names_the_engine(rel)]
    # Act
    stale = sorted(silent)
    # Assert
    assert not stale, (
        f"these exempt detectors no longer name the engine: {stale}. Either the "
        "file stopped being a detector — delete the entry — or it was renamed "
        "and the entry needs to follow it."
    )


# ----------------------------------------------------------------------
# The gate.
# ----------------------------------------------------------------------


def test_no_tracked_file_outside_an_adr_names_the_retired_engine() -> None:
    # Arrange — source, tests and documentation must reach zero.
    # Act
    offenders = _offenders()
    # Assert
    assert offenders == [], (
        f"{len(offenders)} line(s) in tracked files still name the retired "
        "engine. Only docs/adr/ may, because an ADR records a decision that "
        "was taken. Everything else — including the CHANGELOG, which is prose "
        "and is NOT exempt — describes the change without the name:\n"
        + "\n".join(offenders[:40])
    )

# EOF
