"""Catalog tests must not inherit an operator's live scheduler selection."""

import os

import pytest


@pytest.fixture(autouse=True)
def isolated_job_selection(tmp_path):
    previous_dir = os.environ.get("SCITEX_DIR")
    previous_enabled = os.environ.get("SAC_JOBS_ENABLED")
    os.environ["SCITEX_DIR"] = str(tmp_path / "ecosystem-root")
    os.environ.pop("SAC_JOBS_ENABLED", None)
    try:
        yield
    finally:
        if previous_dir is None:
            os.environ.pop("SCITEX_DIR", None)
        else:
            os.environ["SCITEX_DIR"] = previous_dir
        if previous_enabled is None:
            os.environ.pop("SAC_JOBS_ENABLED", None)
        else:
            os.environ["SAC_JOBS_ENABLED"] = previous_enabled
