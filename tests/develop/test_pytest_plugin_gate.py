#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A run that lost pytest-asyncio must ABORT, not report async tests as failures.

2026-08-11 incident. A wide run in an agent container produced 93 failures that
were all ``async def`` tests, every one of them "async def functions are not
natively supported". Nothing was wrong with the code: pytest-asyncio was not
installed in that interpreter, so each coroutine test was collected, never
awaited, and counted as a failure. Two agents attributed it to their own
changes before the header gave it away::

    plugins: anyio-4.14.2, scitex-dev-0.47.0        # broken
    plugins: anyio-4.14.2, asyncio-1.4.0, ...       # same tests, passing

The environmental cause recurs by construction — the base SIF installs the
``[openai]`` extra, not ``[dev]``, so a fresh container has neither pytest nor
pytest-asyncio, and CI's install chain in ``.github/ci/run-in-sif.sh`` has a
final ``-e .`` leg that likewise carries no test plugins. So the harness itself
has to refuse to run rather than produce numbers nobody can trust.

``required_plugins`` in ``[tool.pytest.ini_options]`` is that refusal. These
tests hold it in place: one asserts the declaration is still there, the rest
drive REAL pytest against THIS repo's REAL config file and prove the abort
still happens — a gate that cannot fail is not a gate, so it is exercised, not
described.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

# The plugins whose absence must abort a run. pytest-asyncio is the one that
# fails SILENTLY (async tests turn red instead of erroring), which is what made
# the incident so expensive to diagnose.
_MUST_REQUIRE = ("pytest-asyncio", "pytest-xdist")

# The message a run WITHOUT the gate produces — 93 of these was the incident.
_UNGATED_SYMPTOM = "async def functions are not natively supported"


def _declared_required_plugins() -> list[str]:
    with _PYPROJECT.open("rb") as handle:
        doc = tomllib.load(handle)
    ini = doc["tool"]["pytest"]["ini_options"]
    declared = ini.get("required_plugins", [])
    # pytest accepts either a list or a whitespace-separated string.
    if isinstance(declared, str):
        return declared.split()
    return list(declared)


@pytest.fixture
def blocked_run(tmp_path: Path) -> str:
    """Run REAL pytest against THIS repo's REAL config, with asyncio blocked.

    ``-p no:asyncio`` blocks the plugin exactly as an uninstalled distribution
    would: ``required_plugins`` is validated against the plugins that actually
    loaded, so both routes reach the same check. ``-c <pyproject>`` points the
    subprocess at the repo's real configuration rather than a fixture copy, so
    these tests fail if the declaration is weakened in any way.

    Returns the combined output with the exit code appended on its own line, so
    each behaviour below can assert exactly one thing about it. Function-scoped
    on purpose: each run aborts before collection, so re-running it per test
    costs a fraction of a second and keeps the fixture free of shared state.
    """
    probe = tmp_path / "test_probe.py"
    probe.write_text(
        "import pytest\n\n\n"
        "@pytest.mark.asyncio\n"
        "async def test_async_probe():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(_PYPROJECT),
            "-p",
            "no:asyncio",
            "-p",
            "no:cacheprovider",
            str(probe),
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=120,
    )
    return f"{completed.stdout}{completed.stderr}\nexit_code={completed.returncode}"


@pytest.mark.parametrize("plugin", _MUST_REQUIRE)
def test_pyproject_requires_the_plugin(plugin: str) -> None:
    """The declaration exists, so a missing plugin is a startup error."""
    # Arrange
    expected = plugin
    # Act
    declared = _declared_required_plugins()
    # Assert
    assert expected in declared, (
        f"{_PYPROJECT} no longer requires {expected!r} "
        f"(required_plugins={declared!r}). Without it, a run in an "
        f"environment that lacks {expected} does not abort — it reports every "
        f"async test as a failure, which reads as a code regression. "
        f"See this module's docstring for the 2026-08-11 incident."
    )


def test_missing_plugin_aborts_with_the_usage_error_exit_code(
    blocked_run: str,
) -> None:
    """Exit 4 is pytest's USAGE_ERROR — the session never starts."""
    # Arrange
    expected = "exit_code=4"
    # Act
    output = blocked_run
    # Assert
    assert expected in output, (
        "a run without pytest-asyncio must abort with pytest's usage-error "
        f"exit code 4.\n{output}"
    )


def test_missing_plugin_abort_says_a_required_plugin_is_missing(
    blocked_run: str,
) -> None:
    """The one line a reader needs, instead of a wall of async failures."""
    # Arrange
    expected = "Missing required plugins"
    # Act
    output = blocked_run
    # Assert
    assert expected in output, f"the abort must state what went wrong.\n{output}"


def test_missing_plugin_abort_names_pytest_asyncio(blocked_run: str) -> None:
    """Naming the plugin is what makes the error actionable."""
    # Arrange
    expected = "pytest-asyncio"
    # Act
    output = blocked_run
    # Assert
    assert expected in output, (
        f"the abort must name the missing plugin.\n{output}"
    )


def test_missing_plugin_never_reaches_collection(blocked_run: str) -> None:
    """The incident's symptom must be unreachable once the gate is in place."""
    # Arrange
    forbidden = _UNGATED_SYMPTOM
    # Act
    output = blocked_run
    # Assert
    assert forbidden not in output, (
        "the run reached collection instead of aborting — required_plugins is "
        f"not taking effect.\n{output}"
    )


def test_the_required_plugins_are_installed_in_this_environment() -> None:
    """This very session loaded them — otherwise the gate above is untested."""
    # Arrange
    from importlib.metadata import distributions

    # Act
    installed = {
        dist.metadata["Name"] for dist in distributions() if dist.metadata["Name"]
    }
    # Assert
    assert not [name for name in _MUST_REQUIRE if name not in installed], (
        f"missing from this interpreter ({sys.executable}): "
        f"{[name for name in _MUST_REQUIRE if name not in installed]}. "
        "Install the declared test extra: pip install -e '.[dev]'"
    )
