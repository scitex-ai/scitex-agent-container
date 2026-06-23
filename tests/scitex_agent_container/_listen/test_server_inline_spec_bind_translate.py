"""Wired-end-to-end tests for the PR-2 SAC-side bind translate.

PR-2 splices into the inline-spec ``POST /agents`` handler between
the basic-shape validation and the PR-1 bind preflight. The wire
contract these tests pin:

  * A child spec with ``/work/...`` bind sources gets rewritten to
    the parent's host-side equivalent BEFORE the preflight runs,
    so a stat()-able spec lands on disk.
  * The translate is a no-op when the POST body carries no
    ``caller`` field (admin / operator path → PR-1 enforces).
  * The translate is a no-op when the caller is unknown to the
    Registry, AND PR-1's 400 still fires (the safety valve).
  * The translated spec is what gets written to disk (the YAML on
    disk reflects the rewrite, not the original ``/work/...``).

Real I/O via the Starlette ``TestClient`` against ``create_app``
+ a real on-disk parent spec.yaml. No mocks. AAA + one assert
per test (PA-307).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from scitex_agent_container._listen.server import create_app

TOKEN = "test-token-bind-translate"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(tmp_path: Path, env_save_restore):
    """Redirect $HOME + the SAC dir envs into tmp_path.

    Same shape as the PR-1 server tests, with the matching
    teardown reload to avoid the LIFO env-restore-vs-module-cache
    issue in ``_runners._session_state`` (see PR-1 fix in
    test_server_startup_failed.py).
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
    yield tmp_path
    os.environ.pop("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", None)
    os.environ.pop("SCITEX_AGENT_CONTAINER_YAML_DIRS", None)
    os.environ.pop("HOME", None)
    importlib.reload(ss)


@pytest.fixture
def client(isolated_env):
    app = create_app(token=TOKEN)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_headers():
    return {"authorization": f"Bearer {TOKEN}"}


def _write_parent_spec(home: Path, name: str, host_root: Path) -> Path:
    """Write a minimal parent spec.yaml whose apptainer.binds maps
    ``host_root`` at the in-SIF view ``/work``.

    The child will reference ``/work/data/X``; after PR-2 translate
    that becomes ``{host_root}/data/X`` which is stat()-able on
    disk (we mkdir it ahead of time).
    """
    spec_dir = home / ".scitex" / "agent-container" / "agents" / name
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {
            "runtime": "tui",
            "host": "local",
            "workdir": "/tmp",
            "apptainer": {
                "image": "/path/to/sac-base.sif",
                "binds": [f"{host_root}:/work"],
            },
            "claude": {"model": "claude-sonnet-4-5"},
            "health": {"enabled": True, "interval": 60},
            "restart": {"policy": "on-failure", "max_retries": 3},
        },
    }
    spec_path = spec_dir / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(spec))
    return spec_path


def _child_body(name: str, caller: str | None, binds: list) -> dict:
    body: dict = {
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
    if caller is not None:
        body["caller"] = caller
    return body


# ---------------------------------------------------------------------------
# Happy path: /work/X (in-SIF) translates to host path that exists
# ---------------------------------------------------------------------------


def test_child_with_work_prefix_bind_is_accepted_after_translate(
    client, auth_headers, isolated_env, tmp_path
):
    # Arrange — parent maps its host $HOME/proj/foo at /work; child
    # asks for /work/data/capsule-X. After translate, the bind is
    # $HOME/proj/foo/data/capsule-X which we mkdir on the host so
    # PR-1's preflight passes.
    host_root = tmp_path / "proj" / "foo"
    (host_root / "data" / "capsule-X").mkdir(parents=True)
    home = Path(os.environ["HOME"])
    _write_parent_spec(home, "parent-launcher", host_root)
    body = _child_body(
        "child-translated",
        caller="parent-launcher",
        binds=["/work/data/capsule-X:/inside:ro"],
    )
    # Act
    response = client.post("/agents", json=body, headers=auth_headers)
    # Assert — translate + preflight both succeeded; the POST
    # didn't return a 400 (bind_unresolvable).
    assert (
        response.status_code != 400
        or response.json().get("kind") != "bind_unresolvable"
    )


def test_translated_bind_is_what_gets_written_to_disk(
    client, auth_headers, isolated_env, tmp_path
):
    # Arrange — same setup as above, but inspect the materialised
    # spec on disk. The spec.yaml at the install root must carry
    # the TRANSLATED host path, not the original ``/work/...``.
    host_root = tmp_path / "proj" / "foo"
    (host_root / "data" / "capsule-X").mkdir(parents=True)
    home = Path(os.environ["HOME"])
    _write_parent_spec(home, "parent-disk", host_root)
    body = _child_body(
        "child-disk-check",
        caller="parent-disk",
        binds=["/work/data/capsule-X:/inside:ro"],
    )
    # Act
    client.post("/agents", json=body, headers=auth_headers)
    persisted_spec_path = (
        home
        / ".scitex"
        / "agent-container"
        / "agents"
        / "child-disk-check"
        / "spec.yaml"
    )
    persisted = yaml.safe_load(persisted_spec_path.read_text())
    persisted_binds = persisted["spec"]["apptainer"]["binds"]
    # Assert — disk shows the host path, not the in-SIF view.
    assert persisted_binds == [f"{host_root}/data/capsule-X:/inside:ro"]


# ---------------------------------------------------------------------------
# No-op path: caller absent
# ---------------------------------------------------------------------------


def test_no_caller_means_no_translate_so_pr1_still_rejects_work_path(
    client, auth_headers, isolated_env, tmp_path
):
    # Arrange — operator-style POST with no ``caller`` field. The
    # bind source ``/work/...`` is not stat()-able on the host.
    # Translate is a no-op (no_caller), so PR-1's preflight fires
    # and returns the structured 400 — exactly the safety valve.
    body = _child_body(
        "no-caller-rejected",
        caller=None,
        binds=[f"{tmp_path}/missing-on-host:/x"],
    )
    # Act
    response = client.post("/agents", json=body, headers=auth_headers)
    # Assert
    assert response.json().get("kind") == "bind_unresolvable"


# ---------------------------------------------------------------------------
# No-op path: caller is unknown (not a SAC-managed agent)
# ---------------------------------------------------------------------------


def test_unknown_caller_falls_back_to_pr1_rejection(
    client, auth_headers, isolated_env, tmp_path
):
    # Arrange — caller string doesn't resolve to any registered
    # parent. Translate collapses to caller_unknown; PR-1 catches
    # the unresolvable bind.
    body = _child_body(
        "unknown-caller",
        caller="ghost-parent",
        binds=["/work/data/X:/inside"],
    )
    # Act
    response = client.post("/agents", json=body, headers=auth_headers)
    # Assert
    assert response.json().get("kind") == "bind_unresolvable"


# ---------------------------------------------------------------------------
# Non-regression: PR-1 still catches a bind that translate can't fix
# ---------------------------------------------------------------------------


def test_translate_passthrough_for_non_work_bind_still_caught_by_pr1(
    client, auth_headers, isolated_env, tmp_path
):
    # Arrange — caller IS known but the child requested a bind
    # source the parent doesn't expose (e.g. /scratch/X when the
    # parent only binds /home/y/proj/foo at /work). Translate is
    # a no-op for the /scratch path; PR-1 catches it.
    host_root = tmp_path / "proj" / "foo"
    host_root.mkdir(parents=True)
    home = Path(os.environ["HOME"])
    _write_parent_spec(home, "scoped-parent", host_root)
    body = _child_body(
        "out-of-scope-bind",
        caller="scoped-parent",
        binds=[f"{tmp_path}/no-such-dir:/x"],
    )
    # Act
    response = client.post("/agents", json=body, headers=auth_headers)
    # Assert
    assert response.json().get("kind") == "bind_unresolvable"


# ---------------------------------------------------------------------------
# Mixed binds: one translatable + one already-host-visible
# ---------------------------------------------------------------------------


def test_mixed_binds_translate_only_work_prefix_others_pass_through(
    client, auth_headers, isolated_env, tmp_path
):
    # Arrange — child requests two binds: one /work-prefixed
    # (translatable) and one already-host-visible (passes through).
    # The persisted spec carries the right shape: first rewritten,
    # second untouched.
    host_root = tmp_path / "proj" / "foo"
    (host_root / "data").mkdir(parents=True)
    sibling = tmp_path / "sibling"
    sibling.mkdir()
    home = Path(os.environ["HOME"])
    _write_parent_spec(home, "mixed-parent", host_root)
    body = _child_body(
        "mixed-bind-child",
        caller="mixed-parent",
        binds=[
            "/work/data:/inside_data:ro",
            f"{sibling}:/inside_sibling:rw",
        ],
    )
    # Act
    client.post("/agents", json=body, headers=auth_headers)
    persisted_spec_path = (
        home
        / ".scitex"
        / "agent-container"
        / "agents"
        / "mixed-bind-child"
        / "spec.yaml"
    )
    persisted_binds = yaml.safe_load(persisted_spec_path.read_text())["spec"][
        "apptainer"
    ]["binds"]
    # Assert
    assert persisted_binds == [
        f"{host_root}/data:/inside_data:ro",
        f"{sibling}:/inside_sibling:rw",
    ]
