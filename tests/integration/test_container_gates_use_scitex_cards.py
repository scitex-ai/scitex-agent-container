"""Guard: the SIF build/artifact gates must assert scitex_cards, not the shim.

WHAT THIS POLICES — AND WHAT IT DELIBERATELY DOES NOT
-----------------------------------------------------
Only the four CONTAINER assets that carry a Python symbol gate:

  * ``containers/apptainer-base.def``     (%post build-time gate)
  * ``containers/apptainer-scitex.def``   (%post build-time gate)
  * ``containers/sif_symbol_probe.py``    (artifact probe run against the SIF)
  * ``containers/spartan-sif-bake.sh``    (embeds a copy of that probe)

These gates are about the PACKAGE THE IMAGE INSTALLS. The image installs
``scitex-cards[mcp]`` because AGENTS talk to the cards MCP server — which stays
true regardless of what sac itself imports. So this guard is orthogonal to, and
survives, the sac/cards decoupling work: it does not look at
``src/scitex_agent_container/**`` at all, and it makes no claim about which
Python packages sac imports.

WHY THE SHIM ASSERTION HAD TO GO
--------------------------------
The board package was renamed scitex-todo -> scitex-cards (2026-07-16). Its
wheel still ships a ``scitex_todo`` import shim as a transition alias. Each gate
above used to ``import scitex_todo`` and assert ``scitex_todo is scitex_cards``.
That made the SIF bake depend on an alias whose removal is scitex-cards' call,
not ours: the day they drop it, every bake dies at the gate — the build broken
by something the image does not need.

Removing it is not weakening the gate. Each gate still imports
``scitex_cards._throughput.WIP_STATUSES``, which is the load-bearing assertion:
the import raises ImportError on a stale bake (unhandled -> non-zero exit ->
dead build), and the membership check that follows catches a regressed WIP gate
that would otherwise count DEFERRED + CANCELLED cards as open. That pair is what
ended the 2026-07-12 fleet-drift incident; both halves are asserted below so a
future edit cannot quietly reduce the gate to an import that proves nothing.

STX-TQ002 AAA + STX-TQ007 one-assert per test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONTAINERS = _REPO_ROOT / "src" / "scitex_agent_container" / "containers"

#: The gate-bearing assets, by filename.
GATE_ASSETS: tuple[str, ...] = (
    "apptainer-base.def",
    "apptainer-scitex.def",
    "sif_symbol_probe.py",
    "spartan-sif-bake.sh",
)

#: The pre-rename module alias no gate may depend on.
LEGACY_IMPORT = "import scitex_todo"

#: The symbol whose presence IS the freshness proof.
WIP_IMPORT = "from scitex_cards._throughput import WIP_STATUSES"


def _asset_text(name: str) -> str:
    return (_CONTAINERS / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("asset", GATE_ASSETS)
def test_gate_asset_exists(asset: str) -> None:
    # Arrange — a guard over a file that moved silently protects nothing.
    target = _CONTAINERS / asset
    # Act
    exists = target.is_file()
    # Assert
    assert exists, f"{asset} is missing from {_CONTAINERS}"


@pytest.mark.parametrize("asset", GATE_ASSETS)
def test_gate_does_not_import_the_legacy_shim(asset: str) -> None:
    # Arrange — gating the bake on the transition alias breaks every build the
    # day scitex-cards drops it, over a module the image does not need. RED if
    # the shim import is reintroduced.
    text = _asset_text(asset)
    # Act
    imports_shim = LEGACY_IMPORT in text
    # Assert
    assert not imports_shim, (
        f"{asset} imports the transitional scitex_todo shim. The gate must "
        "assert scitex_cards directly — the alias is scitex-cards' to remove, "
        "and depending on it makes their cleanup our broken build."
    )


@pytest.mark.parametrize("asset", GATE_ASSETS)
def test_gate_still_imports_the_wip_statuses_symbol(asset: str) -> None:
    # Arrange — this is the half that must NOT disappear with the shim check.
    # A stale bake dies on this import; that is the whole freshness proof.
    text = _asset_text(asset)
    # Act
    imports_symbol = WIP_IMPORT in text
    # Assert
    assert imports_symbol, (
        f"{asset} no longer imports WIP_STATUSES from scitex_cards._throughput. "
        "Dropping the shim assertion must not reduce this gate to a no-op — "
        "that import is what fails a stale bake."
    )


@pytest.mark.parametrize("asset", GATE_ASSETS)
def test_gate_still_checks_in_progress_membership(asset: str) -> None:
    # Arrange — importing the symbol proves it EXISTS; only the membership
    # check proves it is CORRECT. A regressed WIP gate imports fine.
    text = _asset_text(asset)
    # Act
    checks_membership = '"in_progress" not in WIP_STATUSES' in text
    # Assert
    assert checks_membership, (
        f"{asset} imports WIP_STATUSES but never checks 'in_progress' is in it. "
        "That is a gate that can only pass — the 2026-07-12 fleet-drift "
        "incident shipped exactly that way."
    )


def test_bake_script_probe_matches_the_probe_asset_verbatim() -> None:
    # Arrange — spartan-sif-bake.sh embeds a copy of sif_symbol_probe.py and
    # says so. Editing one and not the other means the artifact probe run on
    # Spartan differs from the one the master verifies with.
    probe = _asset_text("sif_symbol_probe.py").strip()
    bake = _asset_text("spartan-sif-bake.sh")
    # Act
    embedded = probe in bake
    # Assert
    assert embedded, (
        "spartan-sif-bake.sh no longer contains sif_symbol_probe.py verbatim. "
        "Edit both together — the bake and the master-side verify must run the "
        "same gate."
    )
