"""Job identity fences that must hold even when the issuer is unavailable."""

from uuid import uuid4

import pytest

from scitex_agent_container._creds.openbao_job import JobDatabaseBroker

UNREACHABLE = "http://127.0.0.1:1"


def _broker_after_uncertain_issuance(tmp_path):
    """Return a broker and an incarnation whose issuance never got a reply."""
    broker = JobDatabaseBroker(UNREACHABLE, "test", "reader", tmp_path)
    run = uuid4()
    with pytest.raises(ConnectionError):
        broker.issue(run)
    return broker, run


def test_stop_before_start_survives_reconstruction(tmp_path):
    # Arrange
    run = uuid4()
    JobDatabaseBroker(UNREACHABLE, "test", "reader", tmp_path).stop(run)
    broker = JobDatabaseBroker(UNREACHABLE, "test", "reader", tmp_path)
    # Act
    broker.stop(run)
    # Assert
    with pytest.raises(RuntimeError, match="already registered"):
        broker.issue(run)


def test_http_remote_origin_rejected(tmp_path):
    # Arrange
    remote_plain_http = "http://example.invalid"
    # Act
    # Assert
    with pytest.raises(ValueError, match="HTTPS"):
        JobDatabaseBroker(remote_plain_http, "test", "reader", tmp_path)


def test_uncertain_issuance_raises_connection_error(tmp_path):
    # Arrange
    broker = JobDatabaseBroker(UNREACHABLE, "test", "reader", tmp_path)
    run = uuid4()
    # Act
    # Assert
    with pytest.raises(ConnectionError):
        broker.issue(run)


def test_uncertain_issuance_prevents_reissue(tmp_path):
    # Arrange
    broker, run = _broker_after_uncertain_issuance(tmp_path)
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="already registered"):
        broker.issue(run)


def test_uncertain_issuance_blocks_stop_until_reconciled(tmp_path):
    # Arrange
    broker, run = _broker_after_uncertain_issuance(tmp_path)
    # Act
    # Assert
    with pytest.raises(RuntimeError, match="unresolved"):
        broker.stop(run)
