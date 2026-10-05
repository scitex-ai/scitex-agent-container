"""Static contract: ``apptainer-base.def`` must install the Hermes local
browser (agent-browser + pinned Chromium) with a fail-loud launch gate.

hermes browser tools (browser_exec / browser-harness) resolve Chromium
through ``hermes_cli.browser_runtime.chromium_executable``: the
``AGENT_BROWSER_EXECUTABLE_PATH`` override, else
``pm.installed_package("chromium")``. A fresh image has neither:
agent-browser is an OPTIONAL default PM package (``uv sync`` does not
install it) and the "chromium" PM package is not a default at all — so
every browser tool failed at launch, silently from the agent's view.

This test pins the requirement as code: the def must run
``hermes pm install agent-browser`` (which pulls the pinned CLI plus
its declared ``chromium`` dep) and must assert a resolvable, executable
binary plus a headless launch smoke. Drop any of the three and CI yells
before a SIF rebuild ships an image whose browser tools are dead.

STX-TQ002 AAA + STX-TQ007 one-assert per test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BASE_DEF = (
    _REPO_ROOT / "src" / "scitex_agent_container" / "containers" / "apptainer-base.def"
)

# Each marker must appear in the def: install command, resolution gate,
# executable gate, headless launch smoke.
_REQUIRED_MARKERS = (
    "pm install agent-browser",
    "HERMES_RUNTIME_DIR=/opt/hermes-tools",
    "export HERMES_RUNTIME_DIR=/opt/hermes-tools",
    "hermes_cli.browser_runtime import chromium_executable",
    "BROWSER: FAIL - no Chromium resolves",
    "BROWSER: FAIL - not executable",
    "--headless",
    "--dump-dom about:blank",
)


@pytest.fixture(scope="module")
def base_def_text() -> str:
    # Arrange
    return _BASE_DEF.read_text()


@pytest.mark.parametrize("marker", _REQUIRED_MARKERS)
def test_base_def_installs_hermes_local_browser(base_def_text: str, marker: str) -> None:
    # Arrange + Act
    present = marker in base_def_text
    # Assert
    assert present, (
        f"{marker!r} missing from apptainer-base.def — the Hermes local "
        f"browser (agent-browser + pinned Chromium) will not ship and every "
        f"browser tool will fail at launch."
    )
