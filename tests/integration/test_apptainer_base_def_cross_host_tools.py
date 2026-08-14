"""The base image must carry every tool the CALLER needs for a cross-host start.

INCIDENT 2026-08-05: the first real cross-host agent start died with

    FileNotFoundError: [Errno 2] No such file or directory: 'rsync'

raised from ``_dispatch_remote_start``, which shells ``rsync`` to sync the
workspace before starting an agent on another host. It runs in the CALLER's
environment — and every agent runs inside this base image, which did not have
rsync. So no agent could ever start an agent on another host: the multi-host
feature was unreachable from exactly the processes meant to use it, while the
TARGET host had rsync installed the whole time.

The failure was loud, which is the only lucky part. What makes it worth a test
is that the dependency is invisible from the code — nothing in the def says
"cross-host start needs this", and a future trim of the apt list would be
reviewed with no reason to keep it.

These pin the CAPABILITY (the tool is installed), not the spelling of the apt
line, so reordering or resplitting the install is free.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ._base_stack import base_stack_paths

# The apt install lives in :system-deps since the four-layer split, but the
# property under test is about the :base IMAGE — so scan every recipe that
# composes it rather than pinning this test to whichever layer currently
# holds the apt block.
_DEFS = base_stack_paths()


def _installed_tokens() -> set[str]:
    """Package names apt is actually asked to install. COMMENTS EXCLUDED.

    The exclusion is the whole point, and it was learned the hard way: the
    first version of this helper scanned every token in the file. The def
    carries a long comment explaining WHY rsync is required — so the test
    passed against a def where rsync appeared only in prose and was never
    installed. A test that its own documentation satisfies cannot fail.

    So: parse the ``apt-get install`` continuation block, drop anything after
    a ``#``, and ignore flags. What is asserted is that apt installs the
    package, not that someone wrote its name down.
    """
    tokens: set[str] = set()
    for path in _DEFS:
        in_block = False
        # Reset per recipe: a continuation must never bleed across a file
        # boundary, or a def ending mid-block would swallow the next def's
        # opening lines as package names.
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0]  # comments carry no packages
            stripped = line.strip()
            if "apt-get install" in stripped:
                in_block = True
                stripped = stripped.split("apt-get install", 1)[1]
            elif not in_block:
                continue
            continues = stripped.endswith("\\")
            for tok in stripped.rstrip("\\").split():
                if not tok.startswith("-"):  # skip -y, --no-install-recommends
                    tokens.add(tok)
            if not continues:
                in_block = False
    return tokens


def test_the_base_def_exists_at_the_expected_path() -> None:
    """Control: if this fails, every other assertion here is vacuous."""
    # Arrange — every recipe composing :base, not just one file
    path = _DEFS[0]
    # Act
    found = all(p.is_file() for p in _DEFS)
    # Assert
    assert found is True


def test_rsync_is_installed_for_cross_host_start() -> None:
    """`_dispatch_remote_start` shells rsync IN THE CALLER's environment."""
    # Arrange
    tokens = _installed_tokens()
    # Act
    present = "rsync" in tokens
    # Assert
    assert present is True


def test_openssh_client_is_installed_for_ssh_dispatch() -> None:
    """The other half of cross-host dispatch — ssh carries the command."""
    # Arrange
    tokens = _installed_tokens()
    # Act
    present = "openssh-client" in tokens
    # Assert
    assert present is True


@pytest.mark.parametrize("tool", ["git", "curl", "ca-certificates"])
def test_the_hard_dependencies_named_in_the_def_are_installed(tool: str) -> None:
    """The def's own comment says a trim must not silently drop these."""
    # Arrange
    tokens = _installed_tokens()
    # Act
    present = tool in tokens
    # Assert
    assert present is True
