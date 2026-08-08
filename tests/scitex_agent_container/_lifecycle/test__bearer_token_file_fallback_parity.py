"""Every listen client must find an on-disk bearer, not just an env one.

THE DEFECT. ``_resolve_bearer`` was copied into five modules. Three of them
(spawn, restart, card-event delivery) resolve a THIRD source after the env var —
the host token file at ``~/.scitex/agent-container/tokens/listen-<host>.token``
— because the apptainer runtime injects ``SAC_LISTEN_BEARER`` ONLY for agents
whose spec registers the ``server:sac`` channel. Two of them did not:

    _lifecycle/_in_sif_http_client.py     stopped at the env var
    _state/_acl_broker_client.py          stopped at the env var

So for an agent without ``server:sac``, the spawn route authenticated and these
two sent an UNAUTHENTICATED request that the listen answers 401 — same
container, same token readable on disk, different copy of the resolver. The 401
then reads as a credentials/ACL problem, which sends the investigation away from
the actual cause (an unsent header).

These tests pin the PARITY rather than the implementation: each client must
resolve the same three sources in the same order. They are written against
``_resolve_bearer`` directly because that is the unit that differed; no mocks
(STX-NM002) — a real token file is written under a redirected HOME.

NOT COVERED ON PURPOSE: ``_resolve_base_url`` is deliberately NOT shared. Each
module raises its own fail-loud type (``HostListenTransportError`` /
``AclBrokerError``) and callers catch those, so unifying it would change an
exception contract — a different change, with a different blast radius.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._lifecycle._in_sif_http_client import (
    _resolve_bearer as in_sif_resolve_bearer,
)
from scitex_agent_container._state._acl_broker_client import (
    _resolve_bearer as acl_broker_resolve_bearer,
)

_BEARER_KEYS = (
    "SAC_LISTEN_BEARER",
    "SCITEX_AGENT_CONTAINER_LISTEN_BEARER",
)


@pytest.fixture
def isolated_bearer_env(tmp_path: Path) -> Iterator[Path]:
    """Clear both env spellings and redirect HOME to a clean tmp dir.

    Starting from a cleared slate matters: a stray ``SAC_LISTEN_BEARER`` in the
    operator's shell would make a must-fall-back-to-file test pass for the wrong
    reason. HOME is redirected so the file fallback reads an isolated tokens dir
    and never the operator's real token.
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
    """Write a real listen token file under ``home`` for the local host."""
    from scitex_agent_container._listen.tokens import default_token_path

    path = default_token_path(home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")


# ---------------------------------------------------------------------------
# The regression: env unset + token file present -> the token is found
# ---------------------------------------------------------------------------


def test_in_sif_client_falls_back_to_the_host_token_file(isolated_bearer_env) -> None:
    # Arrange — no bearer in the env; a real token file on disk.
    _write_host_token_file(isolated_bearer_env, "file-tok-in-sif")
    # Act
    resolved = in_sif_resolve_bearer(None)
    # Assert
    assert resolved == "file-tok-in-sif"


def test_acl_broker_falls_back_to_the_host_token_file(isolated_bearer_env) -> None:
    # Arrange — no bearer in the env; a real token file on disk.
    _write_host_token_file(isolated_bearer_env, "file-tok-acl")
    # Act
    resolved = acl_broker_resolve_bearer(None)
    # Assert
    assert resolved == "file-tok-acl"


# ---------------------------------------------------------------------------
# Precedence and opt-out are UNCHANGED by the fallback
# ---------------------------------------------------------------------------


def test_in_sif_client_prefers_the_env_bearer_over_the_file(
    isolated_bearer_env,
) -> None:
    # Arrange — both sources present; the env must win.
    _write_host_token_file(isolated_bearer_env, "file-tok")
    os.environ["SAC_LISTEN_BEARER"] = "env-tok"
    # Act
    resolved = in_sif_resolve_bearer(None)
    # Assert
    assert resolved == "env-tok"


def test_acl_broker_prefers_the_env_bearer_over_the_file(isolated_bearer_env) -> None:
    # Arrange — both sources present; the env must win.
    _write_host_token_file(isolated_bearer_env, "file-tok")
    os.environ["SAC_LISTEN_BEARER"] = "env-tok"
    # Act
    resolved = acl_broker_resolve_bearer(None)
    # Assert
    assert resolved == "env-tok"


def test_in_sif_client_treats_an_empty_explicit_bearer_as_unauthenticated(
    isolated_bearer_env,
) -> None:
    """``""`` is the deliberate opt-out — it must NOT reach for the file."""
    # Arrange
    _write_host_token_file(isolated_bearer_env, "file-tok")
    # Act
    resolved = in_sif_resolve_bearer("")
    # Assert
    assert resolved is None


def test_acl_broker_treats_an_empty_explicit_bearer_as_unauthenticated(
    isolated_bearer_env,
) -> None:
    """``""`` is the deliberate opt-out — it must NOT reach for the file."""
    # Arrange
    _write_host_token_file(isolated_bearer_env, "file-tok")
    # Act
    resolved = acl_broker_resolve_bearer("")
    # Assert
    assert resolved is None


def test_in_sif_client_returns_none_when_neither_source_exists(
    isolated_bearer_env,
) -> None:
    """An absent bearer stays non-fatal — a dev listen may run without auth."""
    # Arrange — cleared env, and no token file written.
    # Act
    resolved = in_sif_resolve_bearer(None)
    # Assert
    assert resolved is None
