"""Static contract: ``apptainer-base.def`` must install the headless-Chromium
runtime libs.

Operator directive (scitex-hub, 2026-07-09): ``sac-base.sif`` must carry the
system shared objects a headless Chromium needs to LAUNCH, so ANY agent can
take screenshots / run browser e2e (Playwright) regardless of which scitex
packages its overlay installs. Without these, chrome dies at launch with
``libnspr4.so: cannot open shared object file``.

The set is the ``npx playwright install-deps chromium`` equivalent. It lands in
the plain apt block of the :base layer (NOT via ``scitex_dev.system_deps`` —
that SSoT only runs at the :scitex layer, where scitex-dev is installed; the
base image predates it). Two consumers already baked into :base —
``@mermaid-js/mermaid-cli`` and puppeteer's ``chrome-headless-shell`` — need the
same libs, so this guard protects them too.

This test pins the requirement as code: drop any of the critical libs from the
.def and CI yells before a SIF rebuild ships a base image whose Chromium
cannot launch.

The sibling ``test_apptainer_base_def_scitex_todo.py`` sets the same static
contract pattern for the scitex-todo[mcp] install at this layer.

STX-TQ002 AAA + STX-TQ007 one-assert per test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BASE_DEF = (
    _REPO_ROOT / "src" / "scitex_agent_container" / "containers" / "apptainer-base.def"
)

# The launch-critical subset of `npx playwright install-deps chromium`. Every
# one of these was reported "not found" by `ldd` on the current SIF's chromium.
_REQUIRED_LIBS = (
    "libnss3",
    "libnspr4",
    "libatk1.0-0",
    "libatk-bridge2.0-0",
    "libatspi2.0-0",
    "libcups2",
    "libdrm2",
    "libgbm1",
    "libxshmfence1",
    "libxkbcommon0",
    "libasound2",
    "libcairo2",
    "libpango-1.0-0",
    "libxcomposite1",
    "libxdamage1",
    "libxfixes3",
    "libxrandr2",
    "libxext6",
    "libxrender1",
    "fonts-liberation",
)


@pytest.fixture(scope="module")
def base_def_text() -> str:
    # Arrange
    return _BASE_DEF.read_text()


def _apt_install_block(text: str) -> str:
    """Return the first ``apt-get install ...`` continuation chunk joined.

    Section 1's apt block is the base OS package set; the playwright libs
    are appended to its tail. Joining the ``\\``-continued lines lets a
    single membership check see the whole package list.
    """
    lines = text.splitlines()
    out: list[str] = []
    in_block = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("apt-get install"):
            in_block = True
        if in_block:
            out.append(stripped.rstrip("\\").strip())
            if not stripped.endswith("\\"):
                break
    return " ".join(out)


@pytest.mark.parametrize("lib", _REQUIRED_LIBS)
def test_base_apt_block_installs_headless_chromium_lib(
    base_def_text: str, lib: str
) -> None:
    # Arrange
    block = _apt_install_block(base_def_text)
    # Act
    present = lib in block.split()
    # Assert
    assert present, (
        f"{lib!r} missing from the section-1 apt install block in "
        f"apptainer-base.def — headless Chromium (Playwright / mermaid-cli / "
        f"chrome-headless-shell) will fail to launch. Block:\n{block}"
    )
