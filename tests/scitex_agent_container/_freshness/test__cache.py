"""Cache round-trip, TTL, and the anti-fork guard on path resolution.

``test_cache_path_matches_ecosystem_resolver`` is the important one: the
CLI hot path cannot afford to import ``scitex_config._ecosystem`` (1.59 s,
measured), so ``_cache`` mirrors its one-line ``$SCITEX_DIR`` contract in
stdlib. That mirror is only safe while it is PINNED — this test is what
turns "a fork waiting to drift" into "SSOT held by a test".
"""

from __future__ import annotations

import json
import time

from scitex_agent_container._freshness._cache import (
    DEFAULT_TTL_S,
    cache_path,
    read_cache,
    scitex_dir,
    write_cache,
)
from scitex_agent_container._freshness._model import (
    Finding,
    Freshness,
    FreshnessReport,
)


def _report(state=Freshness.STALE, at=None):
    return FreshnessReport(
        findings=(
            Finding(
                check="host-behind-pypi",
                state=state,
                summary="installed 0.21.14 is BEHIND PyPI 0.21.17",
                remedy="pip install -U scitex-agent-container==0.21.17",
            ),
        ),
        generated_at=time.time() if at is None else at,
    )


def test_cache_round_trips_the_verdict(tmp_path):
    # Arrange
    path = tmp_path / "freshness.json"

    # Act
    write_cache(_report(), path)
    loaded = read_cache(path)

    # Assert
    assert loaded.state is Freshness.STALE


def test_cache_round_trips_the_remedy(tmp_path):
    """The fix command must survive the trip — it is the actionable half."""
    # Arrange
    path = tmp_path / "freshness.json"

    # Act
    write_cache(_report(), path)
    loaded = read_cache(path)

    # Assert
    assert loaded.findings[0].remedy == "pip install -U scitex-agent-container==0.21.17"


def test_missing_cache_reads_as_unknown(tmp_path):
    """No file => None => UNKNOWN => the CLI stays silent."""
    # Arrange
    path = tmp_path / "nope.json"

    # Act
    loaded = read_cache(path)

    # Assert
    assert loaded is None


def test_corrupt_cache_reads_as_unknown(tmp_path):
    """A half-written or garbled file must never be parsed into a verdict."""
    # Arrange
    path = tmp_path / "freshness.json"
    path.write_text("{ this is not json")

    # Act
    loaded = read_cache(path)

    # Assert
    assert loaded is None


def test_expired_cache_reads_as_unknown(tmp_path):
    """An old cache means the refresher died. Its last answer is a fossil.

    Serving it would be exactly the bug this subsystem exists to kill: a
    confident claim backed by nothing current.
    """
    # Arrange — written just over the TTL ago.
    path = tmp_path / "freshness.json"
    write_cache(_report(at=time.time() - DEFAULT_TTL_S - 60), path)

    # Act
    loaded = read_cache(path)

    # Assert
    assert loaded is None


def test_fresh_cache_within_ttl_is_served(tmp_path):
    # Arrange
    path = tmp_path / "freshness.json"
    write_cache(_report(at=time.time() - 60), path)

    # Act
    loaded = read_cache(path)

    # Assert
    assert loaded is not None


def test_undated_cache_reads_as_unknown(tmp_path):
    """No timestamp means we cannot age it, so we cannot trust it."""
    # Arrange
    path = tmp_path / "freshness.json"
    path.write_text(json.dumps({"state": "stale", "findings": []}))

    # Act
    loaded = read_cache(path)

    # Assert
    assert loaded is None


def test_cache_write_is_atomic(tmp_path):
    """No .tmp left behind — a reader must never catch a torn file."""
    # Arrange
    path = tmp_path / "freshness.json"

    # Act
    write_cache(_report(), path)

    # Assert
    assert list(tmp_path.glob("*.tmp")) == []


def test_env_override_redirects_the_cache(tmp_path, env):
    """$SAC_FRESHNESS_CACHE is the seam containers and tests use."""
    # Arrange
    target = tmp_path / "elsewhere.json"
    env("SAC_FRESHNESS_CACHE", str(target))

    # Act
    resolved = cache_path()

    # Assert
    assert resolved == target


def test_cache_path_follows_scitex_dir(tmp_path, env):
    """$SCITEX_DIR moves the whole tree — resolved per call, not at import.

    An import-time Path.home() constant could not be redirected by a test
    that sets the env afterwards; that exact bug had tests reading and
    writing the REAL fleet registry.
    """
    # Arrange
    env("SAC_FRESHNESS_CACHE", None)
    env("SCITEX_DIR", str(tmp_path))

    # Act
    resolved = cache_path()

    # Assert
    assert resolved.is_relative_to(tmp_path)


def test_cache_path_matches_ecosystem_resolver(tmp_path, env):
    """Our stdlib mirror must agree with the ecosystem's canonical resolver.

    We cannot IMPORT ``local_state`` on the CLI hot path (1.59 s), so we
    mirror its contract — and pin the mirror here. If the ecosystem ever
    changes where ``$SCITEX_DIR`` points, this fails loudly instead of the
    writer and the reader silently disagreeing about where the file is.
    """
    # Arrange
    from scitex_config._ecosystem import local_state

    env("SCITEX_DIR", str(tmp_path))

    # Act
    ours = scitex_dir()

    # Assert
    assert ours == local_state.user_root()


# EOF
