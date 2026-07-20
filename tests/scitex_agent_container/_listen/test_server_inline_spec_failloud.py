"""Wired-end-to-end tests for the PR-1 fail-loud bind preflight.

The historical inline-spec POST handler wrote the spec to disk + handed
off to ``sac agents start <name>`` without checking that the spec's
``apptainer.binds[*]`` host sources actually existed on the host
filesystem. The clew-cohort-a-capsule-0201225 case escaped through
that gap and FATAL'd silently 50 minutes later.

The preflight added in PR-1 catches it AT THE POST. Verifying through
the real Starlette ``TestClient`` so the wire shape clew launcher reads
is what we ship.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._listen.server import create_app

TOKEN = "test-token-failloud"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(tmp_path: Path, env_save_restore):
    """Real env redirect; same shape as the sibling startup_failed tests.

    Reload-on-teardown is critical (see same-named fixture in
    ``test_server_startup_failed.py``): ``_runners._session_state``
    reads the runtime-dir env at import time, so we must reload BOTH
    before AND after each test or the next test in the worker inherits
    our tmp_path as the default state root and unrelated assertions
    like ``test_state_dir_for_default_root_is_under_user_home`` break.
    """
    home = tmp_path / "home"
    home.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    yaml_dir = home / ".scitex" / "agent-container" / "agents"
    yaml_dir.mkdir(parents=True, exist_ok=True)
    env_save_restore.set("HOME", str(home))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(runtime))
    env_save_restore.set("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(yaml_dir))
    import importlib

    import scitex_agent_container._runners._session_state as ss

    importlib.reload(ss)
    # See test_server_startup_failed.py's same-named fixture: picking up "the
    # OPERATOR's HOME" was the BUG, not the goal. Reload on the far side of the
    # env restore so the constant lands back on the conftest sandbox floor.
    env_save_restore.reload_after_restore(ss)
    yield tmp_path


@pytest.fixture
def client(isolated_env):
    app = create_app(token=TOKEN)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_headers():
    return {"authorization": f"Bearer {TOKEN}"}


def _post_body_with_binds(name: str, binds: list) -> dict:
    return {
        "name": name,
        "spec": {
            "apiVersion": "scitex-agent-container/v3",
            "kind": "Agent",
            "spec": {
                "workdir": "/tmp",
                "apptainer": {
                    "image": "/path/to/sac-base.sif",
                    "binds": binds,
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# Fail-loud rejection — happy + failure paths
# ---------------------------------------------------------------------------


def test_post_400_when_bind_source_missing(
    client, auth_headers, isolated_env, tmp_path
):
    # Arrange — bind source path that doesn't exist on the host.
    body = _post_body_with_binds(
        "missing-bind",
        [f"{tmp_path}/nope/missing:/inside:ro"],
    )
    # Act
    response = client.post("/agents", json=body, headers=auth_headers)
    # Assert
    assert response.status_code == 400


def test_post_400_body_has_kind_bind_unresolvable(
    client, auth_headers, isolated_env, tmp_path
):
    # Arrange
    body = _post_body_with_binds(
        "missing-bind-kind",
        [f"{tmp_path}/missing:/x"],
    )
    # Act
    response = client.post("/agents", json=body, headers=auth_headers)
    # Assert
    assert response.json()["kind"] == "bind_unresolvable"


def test_post_400_body_lists_offending_bind(
    client, auth_headers, isolated_env, tmp_path
):
    # Arrange — failure body now uses ``details.binds[]`` (was
    # ``details.unresolvable[]``) per clew review for the array-form
    # contract. Entries key the raw spec line under ``source``
    # (was ``bind``).
    offending = f"{tmp_path}/missing:/x"
    body = _post_body_with_binds("missing-bind-list", [offending])
    # Act
    response = client.post("/agents", json=body, headers=auth_headers)
    listed = [e["source"] for e in response.json()["details"]["binds"]]
    # Assert
    assert offending in listed


def test_post_400_body_includes_translation_hint(
    client, auth_headers, isolated_env, tmp_path
):
    # Arrange — hint renamed from ``remediation_hint`` to
    # ``translation_hint`` per clew review (names what the caller
    # actually needs to DO — translate the path).
    body = _post_body_with_binds(
        "missing-bind-hint",
        [f"{tmp_path}/missing:/x"],
    )
    # Act
    response = client.post("/agents", json=body, headers=auth_headers)
    # Assert
    assert "translation_hint" in response.json()["details"]


def test_post_rejection_does_not_materialise_spec(
    client, auth_headers, isolated_env, tmp_path
):
    # Arrange — rejected spec MUST leave zero artifacts on disk so a
    # subsequent re-submit with the corrected paths gets a clean slate.
    body = _post_body_with_binds(
        "no-side-effect",
        [f"{tmp_path}/missing:/x"],
    )
    # Act
    client.post("/agents", json=body, headers=auth_headers)
    spec_path = (
        tmp_path
        / "home"
        / ".scitex"
        / "agent-container"
        / "agents"
        / "no-side-effect"
        / "spec.yaml"
    )
    # Assert
    assert not spec_path.exists()


def test_post_passes_preflight_when_bind_source_exists(
    client, auth_headers, isolated_env, tmp_path
):
    # Arrange — real dir on disk; preflight should be silent (no 400).
    real = tmp_path / "real-bind-src"
    real.mkdir()
    body = _post_body_with_binds(
        "ok-bind",
        [f"{real}:/inside"],
    )
    # Act
    response = client.post("/agents", json=body, headers=auth_headers)
    # Assert — preflight is silent. The subsequent `sac agents start`
    # may still fail (no real SIF in CI) — we just verify the 400 is
    # NOT a bind_unresolvable.
    assert (
        response.status_code != 400
        or response.json().get("kind") != "bind_unresolvable"
    )


def test_post_400_when_one_of_many_binds_missing(
    client, auth_headers, isolated_env, tmp_path
):
    # Arrange — one OK + one missing.
    ok = tmp_path / "ok-source"
    ok.mkdir()
    body = _post_body_with_binds(
        "mixed",
        [f"{ok}:/o", f"{tmp_path}/missing:/m"],
    )
    # Act
    response = client.post("/agents", json=body, headers=auth_headers)
    # Assert
    assert response.status_code == 400


def test_post_400_only_lists_missing_bind_not_existing_one(
    client, auth_headers, isolated_env, tmp_path
):
    # Arrange — array form (``details.binds``) must contain ONLY the
    # missing entries; resolved binds get filtered out so 49-capsule
    # callers can act on the failure list directly.
    ok = tmp_path / "ok"
    ok.mkdir()
    body = _post_body_with_binds(
        "mixed-filter",
        [f"{ok}:/o", f"{tmp_path}/missing:/m"],
    )
    # Act
    response = client.post("/agents", json=body, headers=auth_headers)
    listed = [e["source"] for e in response.json()["details"]["binds"]]
    # Assert
    assert f"{ok}:/o" not in listed


# ---------------------------------------------------------------------------
# Existing 400 kinds keep working
# ---------------------------------------------------------------------------


def test_post_400_for_missing_apiversion(client, auth_headers, isolated_env):
    # Arrange — pre-existing validation that should keep ``kind=spec_invalid``.
    body = {
        "name": "no-api-version",
        "spec": {"kind": "Agent", "spec": {}},
    }
    # Act
    response = client.post("/agents", json=body, headers=auth_headers)
    # Assert
    assert response.status_code == 400


def test_post_400_spec_invalid_kind_tag_for_bad_shape(
    client, auth_headers, isolated_env
):
    # Arrange
    body = {"name": "wrong-kind", "spec": {"apiVersion": "x", "kind": "Y"}}
    # Act
    response = client.post("/agents", json=body, headers=auth_headers)
    # Assert
    assert response.json().get("kind") == "spec_invalid"
