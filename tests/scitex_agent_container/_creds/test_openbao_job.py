"""Job identity fences that must hold even when the issuer is unavailable."""

from uuid import uuid4

import pytest

from scitex_agent_container._creds.openbao_job import JobDatabaseBroker


def test_stop_before_start_survives_reconstruction(tmp_path):
    origin = "http://127.0.0.1:1"
    run = uuid4()
    JobDatabaseBroker(origin, "test", "reader", tmp_path).stop(run)
    broker = JobDatabaseBroker(origin, "test", "reader", tmp_path)
    broker.stop(run)
    with pytest.raises(RuntimeError, match="already registered"):
        broker.issue(run)


def test_http_remote_origin_rejected(tmp_path):
    with pytest.raises(ValueError, match="HTTPS"):
        JobDatabaseBroker("http://example.invalid", "test", "reader", tmp_path)


def test_uncertain_issuance_prevents_reissue(tmp_path):
    broker = JobDatabaseBroker("http://127.0.0.1:1", "test", "reader", tmp_path)
    run = uuid4()
    with pytest.raises(ConnectionError):
        broker.issue(run)
    with pytest.raises(RuntimeError, match="already registered"):
        broker.issue(run)
    with pytest.raises(RuntimeError, match="unresolved"):
        broker.stop(run)
