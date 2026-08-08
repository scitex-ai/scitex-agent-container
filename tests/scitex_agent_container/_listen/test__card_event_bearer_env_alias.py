"""Card-event delivery must honour BOTH env spellings of the listen bearer.

THE DEFECT. Every sac client resolves its env via :func:`_env.getenv`, which
accepts ``SAC_<NAME>`` *and* ``SCITEX_AGENT_CONTAINER_<NAME>``. This module did
not: it read ``os.environ.get("SAC_LISTEN_BEARER")`` directly, so a deployment
that set only the long form authenticated on every other route and 401'd here
alone. Its own docstring asserted the opposite ("what every other sac client
reads"), which is why the divergence read as deliberate.

Fixed by delegating to the canonical resolver. These tests pin the behaviour
that was missing (long-form prefix) plus the behaviour that must NOT regress
(short form still works, token-file fallback still works, absent stays None).

No mocks (STX-NM002): real env vars set and restored, a real token file written
under a redirected HOME.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._listen._card_event_delivery import _resolve_bearer

_BEARER_KEYS = (
    "SAC_LISTEN_BEARER",
    "SCITEX_AGENT_CONTAINER_LISTEN_BEARER",
)


@pytest.fixture
def isolated_bearer_env(tmp_path: Path) -> Iterator[Path]:
    """Clear BOTH env spellings and redirect HOME to a clean tmp dir.

    Clearing both matters: a stray value in the operator's shell would make a
    must-read-the-long-form test pass for the wrong reason. HOME is redirected
    so the token-file fallback reads an isolated dir, never the real token.
    """
    saved = {k: os.environ.get(k) for k in _BEARER_KEYS}
    saved_home = os.environ.get("HOME")
    for k in _BEARER_KEYS:
        os.environ.pop(k, None)
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home


def _write_host_token_file(home: Path, token: str) -> None:
    from scitex_agent_container._listen.tokens import default_token_path

    path = default_token_path(home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")


# ---------------------------------------------------------------------------
# The regression: the LONG-FORM prefix must be seen
# ---------------------------------------------------------------------------


def test_the_long_form_env_prefix_is_honoured(isolated_bearer_env) -> None:
    """The bug: this spelling was invisible here and worked everywhere else."""
    # Arrange
    os.environ["SCITEX_AGENT_CONTAINER_LISTEN_BEARER"] = "long-form-tok"
    # Act
    resolved = _resolve_bearer()
    # Assert
    assert resolved == "long-form-tok"


# ---------------------------------------------------------------------------
# Behaviour that must NOT regress
# ---------------------------------------------------------------------------


def test_the_short_form_env_prefix_still_works(isolated_bearer_env) -> None:
    # Arrange
    os.environ["SAC_LISTEN_BEARER"] = "short-form-tok"
    # Act
    resolved = _resolve_bearer()
    # Assert
    assert resolved == "short-form-tok"


def test_the_host_token_file_fallback_still_works(isolated_bearer_env) -> None:
    # Arrange — no env at all; a real token file on disk.
    _write_host_token_file(isolated_bearer_env, "file-tok-card-event")
    # Act
    resolved = _resolve_bearer()
    # Assert
    assert resolved == "file-tok-card-event"


def test_an_env_bearer_still_wins_over_the_token_file(isolated_bearer_env) -> None:
    # Arrange — both present; the env must win.
    _write_host_token_file(isolated_bearer_env, "file-tok")
    os.environ["SAC_LISTEN_BEARER"] = "env-tok"
    # Act
    resolved = _resolve_bearer()
    # Assert
    assert resolved == "env-tok"


def test_no_env_and_no_file_still_resolves_to_none(isolated_bearer_env) -> None:
    """Absent stays non-fatal — the POST goes out unauthenticated and 401s
    loudly in the per-target log line, which is the documented behaviour."""
    # Arrange — cleared env, no token file written.
    # Act
    resolved = _resolve_bearer()
    # Assert
    assert resolved is None
