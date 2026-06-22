"""Shared fixtures for the ``_notify`` tests.

Real-environment isolation only — no ``monkeypatch``. The ``email_env``
fixture snapshots the live ``os.environ`` email keys, clears them, points
the config override at a temp (by default absent) ``config.yaml``, and
restores everything on teardown.
"""

from __future__ import annotations

import os

import pytest

_PREFIXES = ("SAC_", "SCITEX_AGENT_CONTAINER_")
_SUFFIXES = ("FROM", "PASSWORD", "SMTP_HOST", "SMTP_PORT", "TO", "ENABLED")
_EMAIL_KEYS = tuple(f"{p}EMAIL_{s}" for p in _PREFIXES for s in _SUFFIXES) + (
    "SCITEX_AGENT_CONTAINER_CONFIG",
)


@pytest.fixture
def email_env(tmp_path):
    """Isolate every EMAIL_* env key (both prefixes) + the config override.

    Yields the temp ``config.yaml`` path so a test can write a real
    ``email:`` section. Pure env/file control — no monkeypatch, no
    internals patching.
    """
    saved = {k: os.environ.get(k) for k in _EMAIL_KEYS}
    for k in _EMAIL_KEYS:
        os.environ.pop(k, None)
    cfg_path = tmp_path / "config.yaml"
    os.environ["SCITEX_AGENT_CONTAINER_CONFIG"] = str(cfg_path)
    try:
        yield cfg_path
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
