"""Catalog tests must not inherit an operator's live scheduler selection."""

import pytest


@pytest.fixture(autouse=True)
def isolated_job_selection(tmp_path, monkeypatch):
    monkeypatch.setenv("SCITEX_DIR", str(tmp_path / "ecosystem-root"))
    monkeypatch.delenv("SAC_JOBS_ENABLED", raising=False)
