#!/usr/bin/env python3
"""Per-edge INTEGRATION + DEGRADATION tests for scitex-agent-container's
optional peer dependencies.

Mirrors the canonical SciTeX edge-test template
(``scitex-io: tests/integration/test_figrecipe_edge.py``).

Scope (STEP 0 self-filter result)
----------------------------------
Two peers were declared as optional in ``pyproject.toml``: ``scitex-git`` and
``scitex-hpc``. Only ONE of them is a genuine optional *code* path:

* ``scitex-git``  — REAL optional edge. ``sac dev`` commands load it lazily via
  ``dev_group._load_scitex_git()`` →
  ``scitex_dev.try_import_optional("scitex_git", extra="dev", …)``, which
  returns ``None`` when the package is absent. ``_require_scitex_git()`` then
  raises a clean ``click.ClickException`` ("[dev] extra") instead of letting a
  raw ``ImportError`` escape. **Tested here.**

* ``scitex-hpc``  — NOT a real edge. It is declared in the ``[slurm]`` / ``[dev]``
  extras with an aspirational "SlurmRuntime uses Reservation.from_jobid" note,
  but there is *no* ``import scitex_hpc`` / ``from scitex_hpc`` anywhere under
  ``src/`` (or ``tests/``). There is nothing to degrade, so there is nothing to
  test. **Skipped — no meaningful edge.**

The edge under test (scitex-git)
--------------------------------
``sac dev upload-apikey-from-credentials-to-github`` needs ``scitex_git`` for
its gh-secret / repo-variable wrappers and the sha256 sidecar. When the [dev]
extra is installed the command drives the real backend; when it is absent the
command must degrade through the documented contract (clean ClickException),
while the non-scitex_git ``sac dev`` commands (e.g. ``extract-apikey-from-
credentials``) keep working untouched.

The two test kinds every optional edge should have
--------------------------------------------------
1. INTEGRATION (collaborator PRESENT): exercise the real ``scitex_git`` module
   through ``dev_group._load_scitex_git()`` and assert on its concrete public
   surface. Guarded with ``pytest.importorskip("scitex_git")`` so minimal
   installs skip instead of erroring.

2. DEGRADATION (collaborator ABSENT): simulate ``scitex_git`` being missing in
   a hermetic, reversible way (snapshot ``sys.modules``, evict + shadow
   ``scitex_git`` with ``None`` so a fresh import raises ImportError, reload the
   ``scitex_dev`` import machinery and ``dev_group`` so the optional-import
   guard re-runs), then assert the loader returns ``None``, the require-helper
   raises a *clean* ``ClickException`` (not a raw ``ImportError``), and an
   unrelated ``sac dev`` command is unaffected.

Conventions honoured (matching repo + template):
  - One assertion per test (TQ007): shared/expensive setup is lifted into
    fixtures; each behaviour gets its own single-assert test.
  - Explicit Arrange / Act / Assert markers in every test (TQ002).
  - No ``monkeypatch`` / ``mocker`` (banned): the absent-peer fixture
    hand-swaps ``sys.modules`` and restores it on teardown; the loader seam is
    swapped via plain attribute save/restore.
"""

from __future__ import annotations

import importlib
import sys

import pytest
from click.testing import CliRunner

# ===========================================================================
# 1. INTEGRATION  —  scitex_git PRESENT
# ===========================================================================
scitex_git = pytest.importorskip("scitex_git")


# Public surface dev_group.upload-* commands actually call on the backend.
_REQUIRED_BACKEND_ATTRS = (
    "format_age",
    "get_variable",
    "list_secrets",
    "set_secret_with_sha_sidecar",
    "sha256_hex",
)


@pytest.fixture
def loaded_scitex_git_backend():
    """Load the scitex_git backend exactly as production does, once."""
    from scitex_agent_container.cli_pkg import dev_group as dg

    return dg._load_scitex_git()


def test_loader_returns_a_backend_when_present(loaded_scitex_git_backend):
    """``_load_scitex_git()`` returns a real module (not the None sentinel)."""
    # Arrange
    backend = loaded_scitex_git_backend
    # Act
    is_present = backend is not None
    # Assert
    assert is_present


def test_loaded_backend_is_the_scitex_git_module(loaded_scitex_git_backend):
    """The loaded backend is genuinely the ``scitex_git`` package."""
    # Arrange
    backend = loaded_scitex_git_backend
    # Act
    name = getattr(backend, "__name__", None)
    # Assert
    assert name == "scitex_git"


def test_loaded_backend_exposes_required_secret_api(loaded_scitex_git_backend):
    """The backend exposes the full gh-secret/sha256 surface dev_group calls."""
    # Arrange
    backend = loaded_scitex_git_backend
    # Act
    missing = [a for a in _REQUIRED_BACKEND_ATTRS if not hasattr(backend, a)]
    # Assert
    assert missing == []


def test_require_helper_returns_backend_when_present(loaded_scitex_git_backend):
    """``_require_scitex_git()`` returns the backend without raising."""
    # Arrange
    from scitex_agent_container.cli_pkg import dev_group as dg

    _ = loaded_scitex_git_backend
    # Act
    backend = dg._require_scitex_git()
    # Assert
    assert backend is not None


def test_sha256_hex_is_a_real_irreversible_fingerprint(loaded_scitex_git_backend):
    """The real ``sha256_hex`` produces the canonical 64-hex-char digest."""
    # Arrange
    backend = loaded_scitex_git_backend
    # Act
    digest = backend.sha256_hex("sk-ant-oat-example")
    # Assert
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)


# ===========================================================================
# 2. DEGRADATION  —  scitex_git ABSENT
# ===========================================================================
@pytest.fixture
def scitex_git_absent():
    """Make ``import scitex_git`` fail for the duration of the test.

    Hermetic and reversible:
      1. snapshot the whole ``sys.modules`` so teardown restores it exactly;
      2. evict ``scitex_git`` and the ``dev_group`` / ``scitex_dev`` modules
         that wrap it, then shadow ``scitex_git`` with ``None`` so a *fresh*
         ``import scitex_git`` raises ImportError;
      3. reload ``scitex_dev`` (whose ``try_import_optional`` performs the real
         ``importlib.import_module`` that must now fail) and ``dev_group`` so
         its lazy-import seam re-runs under the missing dependency.

    Yields the freshly reloaded ``dev_group`` module.
    """
    import scitex_dev  # noqa: F401

    import scitex_agent_container.cli_pkg.dev_group  # noqa: F401  ensure importable

    # 1. Full snapshot for an exact restore.
    snapshot = dict(sys.modules)

    # 2. Evict the scitex_git stack + the wrappers that import it, then block.
    def _to_evict(name: str) -> bool:
        return (
            name == "scitex_git"
            or name.startswith("scitex_git.")
            or name == "scitex_dev"
            or name.startswith("scitex_dev.")
            or name == "scitex_agent_container.cli_pkg.dev_group"
        )

    for name in [n for n in list(sys.modules) if _to_evict(n)]:
        del sys.modules[name]
    sys.modules["scitex_git"] = None  # type: ignore[assignment]

    importlib.import_module("scitex_dev")
    reloaded = importlib.import_module("scitex_agent_container.cli_pkg.dev_group")

    try:
        yield reloaded
    finally:
        # Restore the exact pre-test module table.
        for name in list(sys.modules):
            if name not in snapshot:
                del sys.modules[name]
        sys.modules.update(snapshot)


def test_scitex_git_absent_fixture_blocks_the_import(scitex_git_absent):
    """Sanity: under the fixture, ``import scitex_git`` really does fail."""
    # Arrange
    _ = scitex_git_absent
    # Act
    module_name = "scitex_git"
    # Assert
    with pytest.raises(ImportError):
        importlib.import_module(module_name)


def test_loader_returns_none_when_absent(scitex_git_absent):
    """``_load_scitex_git()`` degrades to ``None`` (swallows ImportError)."""
    # Arrange
    dg = scitex_git_absent
    # Act
    backend = dg._load_scitex_git()
    # Assert
    assert backend is None


def test_require_helper_raises_clean_clickexception_when_absent(scitex_git_absent):
    """``_require_scitex_git()`` raises a documented ClickException, not ImportError."""
    # Arrange
    import click

    dg = scitex_git_absent
    require = dg._require_scitex_git
    # Act
    raised = pytest.raises(click.ClickException)
    # Assert
    with raised:
        require()


def test_require_helper_message_points_at_dev_extra_when_absent(scitex_git_absent):
    """The degraded error names the [dev] extra so the operator can self-serve."""
    # Arrange
    import click

    dg = scitex_git_absent
    # Act
    try:
        dg._require_scitex_git()
        message = ""
    except click.ClickException as exc:
        message = exc.message
    # Assert
    assert "[dev] extra" in message


def test_upload_command_degrades_to_clean_error_when_absent(scitex_git_absent):
    """The CLI command surfaces the clean [dev]-extra message, not a traceback."""
    # Arrange
    dg = scitex_git_absent
    runner = CliRunner()
    # Act
    result = runner.invoke(
        dg.dev_group,
        ["upload-apikey-from-credentials-to-github", "--dry-run"],
    )
    # Assert
    assert result.exit_code != 0 and "[dev] extra" in result.output


def test_upload_command_does_not_leak_importerror_when_absent(scitex_git_absent):
    """No raw ImportError escapes to the caller through the CLI runner."""
    # Arrange
    dg = scitex_git_absent
    runner = CliRunner()
    # Act
    result = runner.invoke(
        dg.dev_group,
        ["upload-apikey-from-credentials-to-github", "--dry-run"],
    )
    # Assert
    assert not isinstance(result.exception, ImportError)


def test_non_scitex_git_command_unaffected_when_absent(scitex_git_absent, tmp_path):
    """A ``sac dev`` command that does NOT need scitex_git keeps working."""
    # Arrange — extract-apikey-from-credentials reads a creds file only.
    import json

    dg = scitex_git_absent
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat-ok"}}))
    runner = CliRunner()
    # Act
    result = runner.invoke(
        dg.dev_group,
        ["extract-apikey-from-credentials", "--path", str(creds)],
    )
    # Assert
    assert result.exit_code == 0 and "sk-ant-oat-ok" in result.output
