"""CLI-seam tests for the ``sac agents start`` credential preflight gate.

These drive the REAL click entry point (``cli_pkg.lifecycle._start.start``)
rather than the resolver alone, because the seam is where the last defect
of this class hid: PR #949 shipped a click default the resolver read as
"be lenient", silently disabling a gate on every CLI start, and every
unit test passed because none crossed click → gate → resolver.

The gate itself is unit-tested in
``tests/scitex_agent_container/_state/test__preflight_creds_spec.py``.
What is proved HERE is only that the CLI reaches it, with the target's
own spec, and honours its verdict in both directions.

Style: one assert per test, AAA markers, no monkeypatch fixture params.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

import pytest
import yaml
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.lifecycle._start import start
from tests.scitex_agent_container._helpers.explicit_spec import explicit_doc

_ONE_HOUR_S = 3600


def _write_creds(path: Path, expires_at_ms: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-oat-fake",
                    "refreshToken": "sk-ant-ort-fake",
                    "expiresAt": expires_at_ms,
                    "scopes": ["user:inference"],
                    "subscriptionType": "max",
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _fresh(path: Path) -> Path:
    import time

    return _write_creds(path, int((time.time() + _ONE_HOUR_S) * 1000))


def _stale(path: Path) -> Path:
    import time

    return _write_creds(path, int((time.time() - _ONE_HOUR_S) * 1000))


@pytest.fixture
def isolated_home(tmp_path: Path) -> Iterator[Path]:
    """Pin ``$HOME`` to a tmp dir and strip the API-key opt-out env vars."""
    # Arrange
    saved = {
        "HOME": os.environ.get("HOME"),
        "ANTHROPIC_API_KEY": os.environ.pop("ANTHROPIC_API_KEY", None),
        "SAC_ANTHROPIC_API_KEY": os.environ.pop("SAC_ANTHROPIC_API_KEY", None),
    }
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _write_spec(home: Path, name: str, pool: list[Path]) -> Path:
    """Materialise a loadable v3 spec declaring ``pool`` as its account pool.

    ``host`` names a peer that is deliberately NOT registered, so the run
    stops at the cross-host dispatch guard immediately after the preflight
    — the gate's verdict is observed without launching anything.
    """
    doc = explicit_doc(
        {
            "host": "sac-test-unregistered-peer",
            "workdir": str(home),
            "claude": {"credentials_files": [str(p) for p in pool]},
        }
    )
    spec_dir = home / "agents" / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_path = spec_dir / f"{name}.yaml"
    spec_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return spec_path


class TestStartHonoursTheSpecsOwnCredentials:
    def test_start_is_not_refused_when_a_declared_pool_entry_is_fresh(
        self, isolated_home: Path
    ) -> None:
        # Arrange — the 2026-08-10 outage shape: lead token dead, pool fresh.
        _stale(isolated_home / ".claude" / ".credentials.json")
        good = _fresh(isolated_home / "accounts" / "alpha" / ".credentials.json")
        spec = _write_spec(isolated_home, "poolagent", [good])
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(spec), "-y"])
        # Assert — the lead file is never even named; it is not this agent's.
        assert ".claude/.credentials.json expired" not in result.output

    def test_start_is_refused_when_every_declared_entry_is_expired(
        self, isolated_home: Path
    ) -> None:
        # Arrange — inverse control: lead token FRESH (the old gate would
        # have waved this through), every declared credential dead.
        _fresh(isolated_home / ".claude" / ".credentials.json")
        dead_a = _stale(isolated_home / "accounts" / "alpha" / ".credentials.json")
        dead_b = _stale(isolated_home / "accounts" / "beta" / ".credentials.json")
        spec = _write_spec(isolated_home, "deadpool", [dead_a, dead_b])
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(spec), "-y"])
        # Assert
        assert "every credential its spec declares is unusable" in result.output

    def test_refusal_exits_nonzero(self, isolated_home: Path) -> None:
        # Arrange — the gate is an ERROR, never a warning: a refusal must
        # cost the caller a non-zero exit, not just a line of output.
        _fresh(isolated_home / ".claude" / ".credentials.json")
        dead = _stale(isolated_home / "accounts" / "alpha" / ".credentials.json")
        spec = _write_spec(isolated_home, "deadpool", [dead])
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(spec), "-y"])
        # Assert
        assert result.exit_code == 1

    def test_refusal_happens_before_any_dispatch_attempt(
        self, isolated_home: Path
    ) -> None:
        # Arrange — the spec pins an unregistered peer, so reaching the
        # dispatch stage is loudly visible. The gate must fire first.
        _fresh(isolated_home / ".claude" / ".credentials.json")
        dead = _stale(isolated_home / "accounts" / "alpha" / ".credentials.json")
        spec = _write_spec(isolated_home, "deadpool", [dead])
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(spec), "-y"])
        # Assert
        assert "registered peer" not in result.output

    def test_refusal_names_every_declared_candidate(self, isolated_home: Path) -> None:
        # Arrange
        _fresh(isolated_home / ".claude" / ".credentials.json")
        dead_a = _stale(isolated_home / "accounts" / "alpha" / ".credentials.json")
        dead_b = _stale(isolated_home / "accounts" / "beta" / ".credentials.json")
        spec = _write_spec(isolated_home, "deadpool", [dead_a, dead_b])
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(spec), "-y"])
        # Assert
        assert str(dead_a) in result.output and str(dead_b) in result.output

    def test_api_key_env_skips_the_gate_at_the_cli_seam(
        self, isolated_home: Path
    ) -> None:
        # Arrange — every credential dead, but the operator opted into the
        # API-key auth path, so the OAuth gate must not fire at all.
        _stale(isolated_home / ".claude" / ".credentials.json")
        dead = _stale(isolated_home / "accounts" / "alpha" / ".credentials.json")
        spec = _write_spec(isolated_home, "keyagent", [dead])
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-fake"
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(spec), "-y"])
        # Assert
        assert "every credential its spec declares is unusable" not in result.output


class TestUndeclaredSpecKeepsTheLeadFileGate:
    def test_unloadable_spec_still_gates_on_the_expired_lead_file(
        self, isolated_home: Path
    ) -> None:
        # Arrange — a spec that will not parse keeps the defensive default:
        # gate on the lead file rather than skip the check.
        _stale(isolated_home / ".claude" / ".credentials.json")
        agents_dir = isolated_home / "agents"
        (agents_dir / "broken").mkdir(parents=True)
        (agents_dir / "broken" / "broken.yaml").write_text("{{{ not yaml")
        runner = CliRunner()
        # Act
        result = runner.invoke(start, [str(agents_dir), "-y"])
        # Assert
        assert "expired" in result.output
