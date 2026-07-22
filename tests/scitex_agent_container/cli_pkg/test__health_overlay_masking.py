"""The overlay-masking observation in ``sac agents check-health``.

Same doctrine as the liveness payload it sits beside: published NEXT TO
the ``healthy`` bool, tolerant by construction (a failure to gather is an
UNKNOWN payload, never a crashed health command, never a fabricated CLEAN),
and the RED rendering must quote THE operational rule verbatim so the
operator reading a fired detector also reads what not to do next time.

No mocks: real temp overlay layouts; rendering asserted through a real
``rich.console.Console`` writing to a real ``StringIO``.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name (TQ003).
"""

from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

from scitex_agent_container._maintenance import _overlay_masking_model as M
from scitex_agent_container.cli_pkg._health_overlay_masking import (
    overlay_masking_payload,
    print_overlay_masking,
)


def _overlay_with_fossil(tmp_path: Path) -> Path:
    root = tmp_path / "overlays" / "agent-x"
    site = root / "upper" / "opt/venv-sac/lib/python3.12/site-packages"
    site.mkdir(parents=True)
    dist_info = site / "scitex_cards-0.16.1.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text("Name: scitex_cards\nVersion: 0.16.1\n")
    return root


def _config_for(root: Path) -> SimpleNamespace:
    ap = SimpleNamespace(overlay=str(root), raw_args=[], image="", overlay_size="")
    return SimpleNamespace(apptainer=ap, workdir=str(root.parent), name="agent-x")


def test_payload_reports_clean_for_untouched_overlay(tmp_path):
    # Arrange
    root = tmp_path / "overlays" / "agent-x"
    (root / "upper").mkdir(parents=True)
    payload = overlay_masking_payload("agent-x", _config_for(root))
    # Act
    verdict = payload["verdict"]
    # Assert
    assert verdict == M.VERDICT_CLEAN


def test_payload_degrades_exploding_config_to_unknown(tmp_path):
    # Arrange — a config whose attribute access raises must yield an
    # UNKNOWN payload, never crash the health command or read clean.
    class _Boom:
        @property
        def apptainer(self):
            raise RuntimeError("malformed spec")

    # Act
    payload = overlay_masking_payload("agent-x", _Boom())
    # Assert
    assert payload["verdict"] == M.VERDICT_UNKNOWN


def test_masked_rendering_quotes_the_operational_rule(tmp_path):
    # Arrange — a real masked verdict (fossil dist-info + injected base).
    from scitex_agent_container._maintenance._overlay_masking import (
        inspect_agent_overlay,
    )

    base = M.BasePackageSet(
        packages={"scitex-cards": "0.17.5"}, complete=True, source="test-live"
    )
    root = _overlay_with_fossil(tmp_path)
    payload = inspect_agent_overlay(
        "agent-x", _config_for(root), lambda: base
    ).to_dict()
    buffer = io.StringIO()
    console = Console(file=buffer, width=200, no_color=True)
    # Act
    print_overlay_masking(console, payload)
    # Assert — the RED line carries the rule, verbatim from the one string.
    assert "NEVER pip-install a base-baked package" in buffer.getvalue()


def test_clean_rendering_stays_a_single_green_line(tmp_path):
    # Arrange
    root = tmp_path / "overlays" / "agent-x"
    (root / "upper").mkdir(parents=True)
    payload = overlay_masking_payload("agent-x", _config_for(root))
    buffer = io.StringIO()
    console = Console(file=buffer, width=200, no_color=True)
    # Act
    print_overlay_masking(console, payload)
    # Assert — no rule lecture on a clean agent.
    assert "NEVER pip-install" not in buffer.getvalue()
