"""The every-invocation warning: it must speak on STALE and ONLY on STALE.

Driven through the real cache file and the real env vars — the same seams
production uses. ``warn_if_stale`` writes to an injected stream (the same
seam ``_drift.warn_if_spec_source_drifted`` uses), so the output is
captured without touching sys.stderr.
"""

from __future__ import annotations

import io
import time

from scitex_agent_container._freshness._cache import write_cache
from scitex_agent_container._freshness._model import (
    Finding,
    Freshness,
    FreshnessReport,
)
from scitex_agent_container._freshness._warn import EXIT_STALE, warn_if_stale


def _write(path, state, at=None):
    report = FreshnessReport(
        findings=(
            Finding(
                check="host-behind-pypi",
                state=state,
                summary="installed 0.21.14 is BEHIND PyPI 0.21.17",
                remedy="pip install -U 'scitex-agent-container==0.21.17'",
            ),
        ),
        generated_at=time.time() if at is None else at,
    )
    write_cache(report, path)
    return path


def test_stale_cache_warns_on_stream(tmp_path, env):
    """The operator's ask: typing sac tells you that you are behind."""
    # Arrange
    cache = _write(tmp_path / "f.json", Freshness.STALE)
    env("SAC_FRESHNESS_CACHE", str(cache))
    env("SAC_FRESHNESS_QUIET", None)
    env("SAC_FRESHNESS_SEVERITY", None)
    out = io.StringIO()

    # Act
    warn_if_stale(stream=out)

    # Assert
    assert "BEHIND PyPI 0.21.17" in out.getvalue()


def test_warning_names_the_fix_command(tmp_path, env):
    """An alarm that does not say what to DO is one people learn to skip."""
    # Arrange
    cache = _write(tmp_path / "f.json", Freshness.STALE)
    env("SAC_FRESHNESS_CACHE", str(cache))
    env("SAC_FRESHNESS_QUIET", None)
    env("SAC_FRESHNESS_SEVERITY", None)
    out = io.StringIO()

    # Act
    warn_if_stale(stream=out)

    # Assert
    assert "pip install -U 'scitex-agent-container==0.21.17'" in out.getvalue()


def test_fresh_cache_says_nothing(tmp_path, env):
    # Arrange
    cache = _write(tmp_path / "f.json", Freshness.FRESH)
    env("SAC_FRESHNESS_CACHE", str(cache))
    env("SAC_FRESHNESS_QUIET", None)
    env("SAC_FRESHNESS_SEVERITY", None)
    out = io.StringIO()

    # Act
    warn_if_stale(stream=out)

    # Assert
    assert out.getvalue() == ""


def test_unknown_cache_says_nothing(tmp_path, env):
    """UNKNOWN is SILENT. Not 'fine' — silent.

    A check that nags when it does not know gets muted within a day, and a
    muted check is worth less than no check, because everyone still
    believes it is watching.
    """
    # Arrange
    cache = _write(tmp_path / "f.json", Freshness.UNKNOWN)
    env("SAC_FRESHNESS_CACHE", str(cache))
    env("SAC_FRESHNESS_QUIET", None)
    env("SAC_FRESHNESS_SEVERITY", None)
    out = io.StringIO()

    # Act
    warn_if_stale(stream=out)

    # Assert
    assert out.getvalue() == ""


def test_missing_cache_says_nothing(tmp_path, env):
    """No evidence => no noise. This is the anti-cry-wolf guarantee."""
    # Arrange
    env("SAC_FRESHNESS_CACHE", str(tmp_path / "absent.json"))
    env("SAC_FRESHNESS_QUIET", None)
    env("SAC_FRESHNESS_SEVERITY", None)
    out = io.StringIO()

    # Act
    warn_if_stale(stream=out)

    # Assert
    assert out.getvalue() == ""


def test_corrupt_cache_never_breaks_the_cli(tmp_path, env):
    """Rule 2: this may NEVER break sac. Garbage on disk => silence, exit 0."""
    # Arrange
    cache = tmp_path / "f.json"
    cache.write_text("}{ not json at all")
    env("SAC_FRESHNESS_CACHE", str(cache))
    env("SAC_FRESHNESS_QUIET", None)
    env("SAC_FRESHNESS_SEVERITY", None)
    out = io.StringIO()

    # Act
    code = warn_if_stale(stream=out)

    # Assert
    assert code == 0


def test_quiet_env_silences_the_warning(tmp_path, env):
    """The escape hatch. Precedent: SCITEX_DEV_LINTER_QUIET."""
    # Arrange
    cache = _write(tmp_path / "f.json", Freshness.STALE)
    env("SAC_FRESHNESS_CACHE", str(cache))
    env("SAC_FRESHNESS_QUIET", "1")
    out = io.StringIO()

    # Act
    warn_if_stale(stream=out)

    # Assert
    assert out.getvalue() == ""


def test_warn_severity_exits_zero(tmp_path, env):
    """Default severity WARNS — it must not break anybody's pipeline."""
    # Arrange
    cache = _write(tmp_path / "f.json", Freshness.STALE)
    env("SAC_FRESHNESS_CACHE", str(cache))
    env("SAC_FRESHNESS_QUIET", None)
    env("SAC_FRESHNESS_SEVERITY", None)
    out = io.StringIO()

    # Act
    code = warn_if_stale(stream=out)

    # Assert
    assert code == 0


def test_error_severity_returns_stale_exit_code(tmp_path, env):
    """The single knob that escalates warning -> error, when it has earned it."""
    # Arrange
    cache = _write(tmp_path / "f.json", Freshness.STALE)
    env("SAC_FRESHNESS_CACHE", str(cache))
    env("SAC_FRESHNESS_QUIET", None)
    env("SAC_FRESHNESS_SEVERITY", "error")
    out = io.StringIO()

    # Act
    code = warn_if_stale(stream=out)

    # Assert
    assert code == EXIT_STALE


def test_error_severity_still_silent_when_unknown(tmp_path, env):
    """Even escalated, UNKNOWN must not fail a command. Only STALE may.

    Otherwise every offline laptop turns into a hard error and the knob
    gets reverted, taking the whole feature with it.
    """
    # Arrange
    env("SAC_FRESHNESS_CACHE", str(tmp_path / "absent.json"))
    env("SAC_FRESHNESS_QUIET", None)
    env("SAC_FRESHNESS_SEVERITY", "error")
    out = io.StringIO()

    # Act
    code = warn_if_stale(stream=out)

    # Assert
    assert code == 0


def test_bad_severity_value_falls_back_to_warn(tmp_path, env):
    """A typo in an env var must never be able to break sac."""
    # Arrange
    cache = _write(tmp_path / "f.json", Freshness.STALE)
    env("SAC_FRESHNESS_CACHE", str(cache))
    env("SAC_FRESHNESS_QUIET", None)
    env("SAC_FRESHNESS_SEVERITY", "erorr")
    out = io.StringIO()

    # Act
    code = warn_if_stale(stream=out)

    # Assert
    assert code == 0


def test_silent_severity_suppresses_stale(tmp_path, env):
    # Arrange
    cache = _write(tmp_path / "f.json", Freshness.STALE)
    env("SAC_FRESHNESS_CACHE", str(cache))
    env("SAC_FRESHNESS_QUIET", None)
    env("SAC_FRESHNESS_SEVERITY", "silent")
    out = io.StringIO()

    # Act
    warn_if_stale(stream=out)

    # Assert
    assert out.getvalue() == ""


def test_expired_cache_says_nothing(tmp_path, env):
    """A dead refresher's last answer is a fossil, not evidence."""
    # Arrange — 30 days old, far past any TTL.
    cache = _write(tmp_path / "f.json", Freshness.STALE, at=time.time() - 30 * 86_400)
    env("SAC_FRESHNESS_CACHE", str(cache))
    env("SAC_FRESHNESS_QUIET", None)
    env("SAC_FRESHNESS_SEVERITY", None)
    out = io.StringIO()

    # Act
    warn_if_stale(stream=out)

    # Assert
    assert out.getvalue() == ""


# EOF
