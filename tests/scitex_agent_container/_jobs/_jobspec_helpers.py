"""Shared readers for the JobSpec assertions in this package.

Both helpers were defined in ``test__jobs_plugin.py`` until that file was split
into per-group modules. They live here rather than being imported from one test
module into another: a test module that reaches into a sibling's private helper
breaks the moment either file is reorganised, which is exactly what happened to
produce this file.
"""

from __future__ import annotations

import pytest

jobs_mod = pytest.importorskip(
    "scitex_dev.jobs",
    reason="installed scitex-dev predates the scitex_dev.jobs contract",
)

from scitex_agent_container._jobs._jobs_plugin import provide_jobs  # noqa: E402


def _split_command(command: str) -> tuple[str, str, str]:
    """``(bound, payload, rest)`` — the three things a job command can get wrong.

    The whole-command assertions below used to pin a literal ``sac``. They
    cannot any more: the payload is now the ABSOLUTE console script beside the
    running interpreter (:mod:`._sac_bin`), so its text differs per machine and
    a literal would pin the developer's venv into CI.

    Splitting keeps every property those assertions were protecting — the bound
    and the full argument list stay pinned character-for-character, so dropping
    a ``--to`` or an ``--apply`` is still a red test — while the one token that
    is legitimately machine-specific is checked by SHAPE instead. Asserting it
    equals ``sac_bin()`` would be no test at all: a check parameterised by the
    value it is checking cannot fail.
    """
    tokens = command.split()
    return " ".join(tokens[:2]), tokens[2], " ".join(tokens[3:])

def _job(name: str):
    (match,) = [j for j in provide_jobs() if j.name == name]
    return match


def _peer_sets(command: str) -> tuple[set[str], set[str]]:
    """``(targeted, declared_optional)`` — the two peer sets in a command.

    Read from the command rather than restated in each test on purpose. An
    assertion that hard-codes both the peer list and the expected peer list is
    parameterised by the value it is checking and cannot fail — the same trap
    :func:`_split_command` avoids by checking the payload's SHAPE. Pulling both
    sets out of the one string under test keeps the relation between them
    (targeted vs forgiven) the thing being asserted.
    """
    tokens = command.split()
    targeted = {tokens[i + 1] for i, t in enumerate(tokens) if t == "--to"}
    optional = {
        tokens[i + 1] for i, t in enumerate(tokens) if t == "--optional-peer"
    }
    return targeted, optional
