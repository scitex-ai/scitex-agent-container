"""The boot assertion, and the measurement that decides how it is built.

Mirrors ``src/scitex_agent_container/_maintenance/_venv_dist_assertion.py``.

THE FIRST TWO TESTS ARE THE IMPORTANT ONES.
``test_distributions_sees_both_dist_infos`` and
``test_entry_points_cannot_see_a_duplicate_distribution`` are ONE measurement,
split only because one assertion per test is required — read them together.
Same fixture, same interpreter, two stdlib APIs, different answers:
``distributions()`` reports TWO while ``entry_points()`` dedupes by normalised
name and reports ONE. A gate built on ``entry_points()`` is therefore green
while the venv is broken — a gate that cannot fail, which is worse than no gate
because the config still lists it.

That is not a claim inherited from a docstring. It is re-measured here on
whatever interpreter CI runs, because ``importlib.metadata``'s duplicate
handling has changed across releases and a rule that stops holding must fail
loudly rather than quietly stop protecting anything.

Every fixture is a REAL ``.dist-info`` directory with REAL metadata read by the
REAL stdlib. No mocks and no ``monkeypatch`` (PA-306 / STX-NM002): the env and
``sys.path`` fixtures below really mutate the real process state and really put
it back, so what the test exercises is what production talks to.

The duplicate fixture reproduces the 2026-08-11 shape exactly — two dist-infos
for one distribution, each declaring a ``pytest11``-style entry point into a
module only one of them ships.
"""

from __future__ import annotations

import importlib
import os
import sys
from importlib.metadata import distributions, entry_points
from pathlib import Path

import pytest

from scitex_agent_container._maintenance._venv_dist_assertion import (
    SKIP_ENV_VAR,
    VenvDistributionError,
    assert_venv_distributions_unique,
    duplicate_distributions,
)

#: A group name nothing else in the environment can possibly declare, so
#: ``entry_points()`` (which always scans the whole ``sys.path``) is answering
#: about the fixture and only the fixture.
GROUP = "sac_overlay_venv_test_plugin"

#: The incident shape: the stale overlay copy, and the image's copy.
STALE_VERSION = "0.38.0"
IMAGE_VERSION = "0.43.1"


def _plant(site: Path, dist: str, version: str, *, entry: str = "") -> Path:
    """A real ``.dist-info`` the stdlib will read."""
    info = site / f"{dist}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {dist.replace('_', '-')}\nVersion: {version}\n"
    )
    if entry:
        (info / "entry_points.txt").write_text(f"[{GROUP}]\n{entry}\n")
    return info


def _venv(tmp_path: Path, *, versions: tuple[str, ...], entries: bool = False) -> Path:
    """A venv laid out like ``/opt/venv-sac`` — ``lib/python*/site-packages``."""
    venv = tmp_path / "venv-sac"
    site = venv / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    for i, version in enumerate(versions):
        entry = (
            f"plugin{i} = scitex_dev._core._test_execution_plugin" if entries else ""
        )
        _plant(site, "scitex_dev", version, entry=entry)
    return venv


def _site_of(venv: Path) -> Path:
    return venv / "lib" / "python3.12" / "site-packages"


@pytest.fixture
def on_sys_path():
    """Really put a directory on ``sys.path``; really take it off again.

    A factory rather than a value so the caller builds its fixture first. The
    teardown restores the real interpreter state, which is what makes this
    honest where a patch would not be: ``entry_points()`` has no path argument,
    so the only way to ask it about a fixture is to really put the fixture in
    front of it.
    """
    added: list[str] = []

    def _add(path: Path) -> None:
        sys.path.insert(0, str(path))
        added.append(str(path))
        importlib.invalidate_caches()

    try:
        yield _add
    finally:
        for entry in added:
            while entry in sys.path:
                sys.path.remove(entry)
        importlib.invalidate_caches()


@pytest.fixture
def override_set():
    """The escape hatch, really set in the real environment."""
    saved = os.environ.get(SKIP_ENV_VAR)
    os.environ[SKIP_ENV_VAR] = "1"
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(SKIP_ENV_VAR, None)
        else:
            os.environ[SKIP_ENV_VAR] = saved


@pytest.fixture
def override_absent():
    """The default state — the negative control's precondition, guaranteed."""
    saved = os.environ.pop(SKIP_ENV_VAR, None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ[SKIP_ENV_VAR] = saved


# ---------------------------------------------------------------------------
# THE METHOD NOTE, re-measured on this interpreter
# ---------------------------------------------------------------------------
def test_distributions_sees_both_dist_infos(tmp_path) -> None:
    """``distributions()`` does not dedupe — this is what pluggy itself uses."""
    # Arrange
    venv = _venv(tmp_path, versions=(STALE_VERSION, IMAGE_VERSION), entries=True)
    # Act
    found = list(distributions(path=[str(_site_of(venv))]))
    # Assert
    assert len(found) == 2


def test_entry_points_cannot_see_a_duplicate_distribution(
    tmp_path, on_sys_path
) -> None:
    """``entry_points()`` dedupes by normalised name, so it reports ONE.

    This is exactly why the assertion is NOT built on it: such a gate passes
    while the venv is broken.
    """
    # Arrange
    venv = _venv(tmp_path, versions=(STALE_VERSION, IMAGE_VERSION), entries=True)
    on_sys_path(_site_of(venv))
    # Act
    eps = list(entry_points(group=GROUP))
    # Assert
    assert len(eps) == 1


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------
def test_a_duplicated_distribution_fails(tmp_path) -> None:
    # Arrange
    venv = _venv(tmp_path, versions=(STALE_VERSION, IMAGE_VERSION))
    # Act
    check = duplicate_distributions(venv)
    # Assert
    assert check.ok is False


def test_a_single_distribution_passes(tmp_path) -> None:
    # Arrange
    venv = _venv(tmp_path, versions=(IMAGE_VERSION,))
    # Act
    check = duplicate_distributions(venv)
    # Assert
    assert check.ok is True


def test_the_failure_names_the_package(tmp_path) -> None:
    # Arrange
    venv = _venv(tmp_path, versions=(STALE_VERSION, IMAGE_VERSION))
    # Act
    check = duplicate_distributions(venv)
    # Assert
    assert "scitex-dev" in check.detail


def test_the_failure_names_every_version_found(tmp_path) -> None:
    """Naming only one would send the operator hunting for the other."""
    # Arrange
    venv = _venv(tmp_path, versions=(STALE_VERSION, IMAGE_VERSION))
    # Act
    check = duplicate_distributions(venv)
    # Assert
    assert STALE_VERSION in check.detail and IMAGE_VERSION in check.detail


def test_the_failure_names_the_paths(tmp_path) -> None:
    """Evidence, not just a complaint — the operator must be able to look."""
    # Arrange
    venv = _venv(tmp_path, versions=(STALE_VERSION, IMAGE_VERSION))
    expected = str(_site_of(venv) / f"scitex_dev-{STALE_VERSION}.dist-info")
    # Act
    check = duplicate_distributions(venv)
    # Assert
    assert expected in check.detail


def test_a_venv_without_site_packages_is_unknown(tmp_path) -> None:
    """Nothing was measured, so nothing may be concluded."""
    # Arrange
    venv = tmp_path / "not-a-venv"
    venv.mkdir()
    # Act
    check = duplicate_distributions(venv)
    # Assert
    assert check.ok is None


def test_an_empty_site_packages_is_unknown_not_clean(tmp_path) -> None:
    """An empty venv is not a healthy one — the exact fold this rail closes."""
    # Arrange
    venv = _venv(tmp_path, versions=())
    # Act
    check = duplicate_distributions(venv)
    # Assert
    assert check.ok is None


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------
def test_a_duplicated_venv_refuses_the_boot(tmp_path, override_absent) -> None:
    # Arrange
    venv = _venv(tmp_path, versions=(STALE_VERSION, IMAGE_VERSION))
    # Act
    act = lambda: assert_venv_distributions_unique("agent-x", venv=venv)  # noqa: E731
    # Assert
    with pytest.raises(VenvDistributionError):
        act()


def test_the_refusal_names_the_agent(tmp_path, override_absent) -> None:
    # Arrange
    venv = _venv(tmp_path, versions=(STALE_VERSION, IMAGE_VERSION))
    # Act
    act = lambda: assert_venv_distributions_unique("agent-x", venv=venv)  # noqa: E731
    # Assert
    with pytest.raises(VenvDistributionError, match="agent-x"):
        act()


def test_the_refusal_carries_the_repair(tmp_path, override_absent) -> None:
    """The complaint alone would read as a broken repo, which is the whole bug."""
    # Arrange
    venv = _venv(tmp_path, versions=(STALE_VERSION, IMAGE_VERSION))
    # Act
    act = lambda: assert_venv_distributions_unique("agent-x", venv=venv)  # noqa: E731
    # Assert
    with pytest.raises(VenvDistributionError, match="REPAIR"):
        act()


def test_the_refusal_warns_against_deleting_from_inside(
    tmp_path, override_absent
) -> None:
    """The obvious in-container fix is the one that permanently breaks the tree."""
    # Arrange
    venv = _venv(tmp_path, versions=(STALE_VERSION, IMAGE_VERSION))
    # Act
    act = lambda: assert_venv_distributions_unique("agent-x", venv=venv)  # noqa: E731
    # Assert
    with pytest.raises(VenvDistributionError, match="whiteout"):
        act()


def test_a_clean_venv_boots(tmp_path, override_absent) -> None:
    # Arrange
    venv = _venv(tmp_path, versions=(IMAGE_VERSION,))
    # Act
    check = assert_venv_distributions_unique("agent-x", venv=venv)
    # Assert
    assert check.ok is True


def test_an_absent_venv_is_out_of_scope_not_a_pass(tmp_path, override_absent) -> None:
    """Host-side unit runs and source checkouts must not be refused."""
    # Arrange
    venv = tmp_path / "nowhere"
    # Act
    check = assert_venv_distributions_unique("agent-x", venv=venv)
    # Assert
    assert check is None


def test_an_unmeasurable_venv_refuses_as_firmly_as_a_duplicate(
    tmp_path, override_absent
) -> None:
    """UNKNOWN is not a shade of pass, even at the gate."""
    # Arrange
    venv = _venv(tmp_path, versions=())
    # Act
    act = lambda: assert_venv_distributions_unique("agent-x", venv=venv)  # noqa: E731
    # Assert
    with pytest.raises(VenvDistributionError):
        act()


def test_the_override_skips_the_gate(tmp_path, override_set) -> None:
    """An escape hatch exists, and the hazard lives in it rather than the default."""
    # Arrange
    venv = _venv(tmp_path, versions=(STALE_VERSION, IMAGE_VERSION))
    # Act
    check = assert_venv_distributions_unique("agent-x", venv=venv)
    # Assert
    assert check is None


def test_the_gate_is_armed_without_the_override(tmp_path, override_absent) -> None:
    """Negative control for the override: absent env must NOT skip."""
    # Arrange
    venv = _venv(tmp_path, versions=(STALE_VERSION, IMAGE_VERSION))
    # Act
    act = lambda: assert_venv_distributions_unique("agent-x", venv=venv)  # noqa: E731
    # Assert
    with pytest.raises(VenvDistributionError):
        act()
