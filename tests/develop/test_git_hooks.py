"""The repo's git hooks must actually run, and must not claim what they can't do.

Until 2026-07-15 this repo had THREE hook mechanisms and none of them executed:

  * `.pre-commit-config.yaml` existed; the framework shim was never installed
    into `.git/hooks` (3 of 136 repos on this box had it).
  * `.githooks/pre-push` (the ruff gate) was SHADOWED — `core.hooksPath` pointed
    at the absolute `.git/hooks`, which overrides the version-controlled dir.
  * What actually ran was an April-7 git-TEMPLATE hook.

So the conventions were ADVERTISED as hook-enforced and enforced by nothing. A
false claim of coverage is worse than no coverage, and it is why lint.yml's own
comment says the CI ruff job exists because pushes bypass the local hook.

These tests pin the two things that keep that from silently returning: the hook
that runs the framework must exist, and the config must not declare a gate it
cannot pass. The checker tests feed the BAD INPUT — every case below is one the
OLD regex checker got wrong.
"""

from __future__ import annotations

import importlib.util
import os
import stat
from pathlib import Path
from types import ModuleType

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def checker() -> ModuleType:
    """Load scripts/check_stx_allow.py (not an importable package)."""
    path = _REPO / "scripts" / "check_stx_allow.py"
    spec = importlib.util.spec_from_file_location("check_stx_allow", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def precommit_config() -> dict:
    raw = (_REPO / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(raw)


def _hooks(config: dict) -> list[dict]:
    return [h for repo in config.get("repos", []) for h in repo.get("hooks", [])]


# ---------------------------------------------------------------------------
# The hook that makes the framework run at all.
# ---------------------------------------------------------------------------


def test_the_pre_commit_hook_exists() -> None:
    # Arrange — .githooks/ is version-controlled so it travels with the clone;
    # .git/hooks is untracked and every fresh clone silently loses it.
    hook = _REPO / ".githooks" / "pre-commit"
    # Act
    found = hook.is_file()
    # Assert
    assert found


def test_the_pre_commit_hook_is_executable() -> None:
    # Arrange — git will not run a non-executable hook, and will not tell you.
    hook = _REPO / ".githooks" / "pre-commit"
    # Act
    mode = hook.stat().st_mode
    # Assert
    assert mode & stat.S_IXUSR


def test_the_installer_exists() -> None:
    # Arrange — core.hooksPath is LOCAL config; a PR cannot ship it, so the one
    # line that cannot travel with the clone lives in a script, not a README.
    installer = _REPO / "scripts" / "install-git-hooks.sh"
    # Act
    found = installer.is_file() and os.access(installer, os.X_OK)
    # Assert
    assert found


# ---------------------------------------------------------------------------
# The config must not claim coverage it cannot deliver.
# ---------------------------------------------------------------------------


def test_no_hook_runs_the_test_suite(precommit_config: dict) -> None:
    # Arrange — scitex-dev's policy, adopted verbatim: "Pre-commit runs fast,
    # bounded, deterministic checks. It does NOT run the test suite." CI is the
    # gate; pre-commit only saves you a wasted CI cycle.
    entries = [str(h.get("entry", "")) for h in _hooks(precommit_config)]
    # Act
    test_runners = [e for e in entries if "pytest" in e or "unittest" in e]
    # Assert
    assert test_runners == []


def test_no_hook_invokes_an_ambient_python_tool(precommit_config: dict) -> None:
    # Arrange — PS-HOOK-001 (severity E): a bare command name under
    # `language: system` is a $PATH lookup, so it resolves to whichever
    # virtualenv is active at commit time — a different interpreter on every
    # machine. Pinned locally so a regression fails here, not only in CI.
    system_hooks = [
        h for h in _hooks(precommit_config) if h.get("language") == "system"
    ]
    # Act
    offenders = [
        h
        for h in system_hooks
        if str(h.get("entry", "")).split()[:1] in (["python"], ["python3"])
    ]
    # Assert
    assert offenders == []


# ---------------------------------------------------------------------------
# check_stx_allow — every case here is one the OLD regex checker got WRONG.
# ---------------------------------------------------------------------------


def test_flags_a_genuine_silent_swallow(checker: ModuleType, tmp_path: Path) -> None:
    # Arrange — the real violation: swallowed, unexplained.
    src = tmp_path / "m.py"
    src.write_text("try:\n    pass\nexcept OSError:\n    pass\n")
    # Act
    violations = checker.check_file(src)
    # Assert
    assert len(violations) == 1


def test_does_not_flag_a_re_raise(checker: ModuleType, tmp_path: Path) -> None:
    # Arrange — a re-raise is the OPPOSITE of a silent fallback. The old regex
    # never read the handler body and reported this as a violation; 187 of the
    # 552 it flagged in src/ (34%) were this shape.
    src = tmp_path / "m.py"
    src.write_text(
        "try:\n    pass\nexcept ValueError as exc:\n"
        "    raise RuntimeError('converted') from exc\n"
    )
    # Act
    violations = checker.check_file(src)
    # Assert
    assert violations == []


def test_does_not_flag_a_handler_that_logs(checker: ModuleType, tmp_path: Path) -> None:
    # Arrange — reporting is not silence. Also a false positive of the old one.
    src = tmp_path / "m.py"
    src.write_text(
        "import logging\n"
        "try:\n    pass\nexcept OSError as exc:\n"
        "    logging.getLogger(__name__).warning('failed: %s', exc)\n"
    )
    # Act
    violations = checker.check_file(src)
    # Assert
    assert violations == []


def test_sees_a_multiline_silent_swallow(checker: ModuleType, tmp_path: Path) -> None:
    # Arrange — THE BLIND SPOT. The old pattern was `^\s*except\b.*:`; there is
    # no colon on the `except (` line, so it never matched. 619 handlers in src/
    # were invisible to it — it under-fired as badly as it over-fired.
    src = tmp_path / "m.py"
    src.write_text(
        "try:\n    pass\nexcept (\n    OSError,\n    KeyError,\n):\n    pass\n"
    )
    # Act
    violations = checker.check_file(src)
    # Assert
    assert len(violations) == 1


def test_honours_stx_allow_on_a_multiline_closing_line(
    checker: ModuleType, tmp_path: Path
) -> None:
    # Arrange — a multi-line clause carries its comment on the CLOSING line.
    # The old checker could not read it there either, so annotating such a
    # handler correctly still did not silence it.
    src = tmp_path / "m.py"
    src.write_text(
        "try:\n    pass\nexcept (\n    OSError,\n"
        "):  # stx-allow: fallback (reason: absent file is the normal case)\n"
        "    pass\n"
    )
    # Act
    violations = checker.check_file(src)
    # Assert
    assert violations == []
