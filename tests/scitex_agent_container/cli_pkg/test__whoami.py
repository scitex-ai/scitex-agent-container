"""Tests for ``cli_pkg._whoami`` — the ``sac whoami`` orientation verb.

PA-306 no-mocks. Everything is real:

* ``CliRunner`` invokes the REAL registered command through the real
  top-level group (``main``), so the LazyGroup wiring is exercised too.
* The environment is controlled per-invocation via ``CliRunner.invoke``'s
  ``env=`` parameter (``None`` unsets), so the suite is hermetic even
  when it runs INSIDE a sac agent container that has these vars set.
* Spec-resolution tests write a REAL ``<dir>/<name>/spec.yaml`` into
  ``tmp_path`` and point ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` at it —
  the same search chain a live container uses.
* Secret hygiene is asserted with a real bearer value in the env.

``$SCITEX_DIR`` is pointed at an empty tmp dir in every invocation so
the primary user-scope registry never leaks host agents into the test.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from scitex_agent_container.cli_pkg._main import main
from scitex_agent_container.cli_pkg._whoami import _spec_name_candidates

# ---------------------------------------------------------------------------
# Controlled environment + spec fixture
# ---------------------------------------------------------------------------

_BEARER_SECRET = "sekrit-bearer-value-1a2b3c4d5e"

_SPEC_YAML = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    role: fixture-worker
    purpose: verify whoami spec projection
    groups: [testers]
spec:
  runtime: tui
  workdir: /home/agent/work
  claude:
    model: haiku
  extensions:
    responsibilities:
      - answer whoami queries
"""


def _blank_env(tmp_path: Path, **overrides) -> dict:
    """Env mapping that UNSETS every var whoami reads (hermetic base).

    Values of ``None`` are removed from ``os.environ`` for the duration
    of the invocation by click's ``CliRunner``; overrides layer on top.
    """
    env: dict = {
        "SAC_NAME": None,
        "SCITEX_AGENT_CONTAINER_NAME": None,
        "SAC_AGENT": None,
        "SCITEX_AGENT_CONTAINER_AGENT": None,
        "CLAUDE_AGENT_ID": None,
        "CLAUDE_AGENT_ROLE": None,
        "SAC_ROLE": None,
        "SCITEX_AGENT_CONTAINER_ROLE": None,
        "SCITEX_TODO_AGENT_ID": None,
        "SAC_MODEL": None,
        "SCITEX_AGENT_CONTAINER_MODEL": None,
        "SAC_LISTEN_BASE_URL": None,
        "SCITEX_AGENT_CONTAINER_LISTEN_BASE_URL": None,
        "SAC_LISTEN_BEARER": None,
        "SCITEX_AGENT_CONTAINER_LISTEN_BEARER": None,
        "SAC_STATE_DB": None,
        "SCITEX_AGENT_CONTAINER_STATE_DB": None,
        "APPTAINER_CONTAINER": None,
        "SINGULARITY_CONTAINER": None,
        "SCITEX_AGENT_CONTAINER_YAML_DIRS": None,
        "SAC_AGENT_SCOPE": None,
        "SAC_HOSTNAME": None,
        "SCITEX_AGENT_CONTAINER_HOSTNAME": None,
        "SCITEX_DIR": str(tmp_path / "empty-scitex-dir"),
    }
    env.update(overrides)
    return env


def _write_spec_registry(tmp_path: Path, name: str = "whoami-fixture") -> Path:
    """Write a real ``<registry>/<name>/spec.yaml`` and return the registry."""
    agent_dir = tmp_path / "agents" / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "spec.yaml").write_text(_SPEC_YAML)
    return tmp_path / "agents"


def _spec_env(tmp_path: Path, name: str = "whoami-fixture", **overrides) -> dict:
    """Controlled env where the fixture spec is resolvable by ``name``."""
    registry = _write_spec_registry(tmp_path, name)
    return _blank_env(
        tmp_path,
        SAC_NAME=name,
        SCITEX_AGENT_CONTAINER_YAML_DIRS=str(registry),
        **overrides,
    )


def _invoke(env: dict, *args: str):
    return CliRunner().invoke(main, ["whoami", *args], env=env)


def _line(output: str, key: str) -> str:
    hits = [ln for ln in output.splitlines() if ln.strip().startswith(key)]
    assert hits, f"no {key!r} line in output:\n{output}"
    return hits[0]


# ---------------------------------------------------------------------------
# Blank env — honest UNKNOWN, never a crash
# ---------------------------------------------------------------------------


def test_whoami_blank_env_exits_zero(tmp_path):
    # Arrange
    env = _blank_env(tmp_path)
    # Act
    result = _invoke(env)
    # Assert
    assert result.exit_code == 0, result.output


def test_whoami_renders_all_section_headers(tmp_path):
    # Arrange
    env = _blank_env(tmp_path)
    headers = {"IDENTITY", "PLACEMENT", "EXECUTION", "ROLE", "HOW-TO"}
    # Act
    result = _invoke(env)
    # Assert
    assert {h for h in headers if h in result.output} == headers


def test_whoami_blank_env_agent_is_unknown(tmp_path):
    # Arrange
    env = _blank_env(tmp_path)
    # Act
    result = _invoke(env)
    # Assert
    assert "UNKNOWN" in _line(result.output, "agent:")


def test_whoami_blank_env_role_points_to_claude_md(tmp_path):
    # Arrange
    env = _blank_env(tmp_path)
    # Act
    result = _invoke(env)
    # Assert
    assert ".claude/CLAUDE.md" in _line(result.output, "role:")


def test_whoami_mounts_line_always_present(tmp_path):
    # Arrange
    env = _blank_env(tmp_path)
    # Act
    result = _invoke(env)
    # Assert
    assert "mounts:" in result.output


# ---------------------------------------------------------------------------
# Env-derived facts
# ---------------------------------------------------------------------------


def test_whoami_reports_agent_name_from_env(tmp_path):
    # Arrange
    env = _blank_env(tmp_path, SAC_NAME="whoami-env-name")
    # Act
    result = _invoke(env)
    # Assert
    assert "whoami-env-name" in _line(result.output, "agent:")


def test_whoami_reports_board_id_from_env(tmp_path):
    # Arrange
    env = _blank_env(tmp_path, SCITEX_TODO_AGENT_ID="board-id-77")
    # Act
    result = _invoke(env)
    # Assert
    assert "board-id-77" in _line(result.output, "board-id:")


def test_whoami_reports_listen_url_from_env(tmp_path):
    # Arrange
    env = _blank_env(tmp_path, SAC_LISTEN_BASE_URL="http://127.0.0.1:7878")
    # Act
    result = _invoke(env)
    # Assert
    assert "http://127.0.0.1:7878" in _line(result.output, "listen:")


def test_whoami_reports_image_from_apptainer_env(tmp_path):
    # Arrange
    env = _blank_env(tmp_path, APPTAINER_CONTAINER="/containers/sac-base.sif")
    # Act
    result = _invoke(env)
    # Assert
    assert "/containers/sac-base.sif" in _line(result.output, "image:")


def test_whoami_workdir_reports_cwd(tmp_path):
    # Arrange
    env = _blank_env(tmp_path)
    # Act
    result = _invoke(env)
    # Assert
    assert str(Path.cwd()) in _line(result.output, "workdir:")


def test_whoami_env_role_used_when_spec_unresolvable(tmp_path):
    # Arrange
    env = _blank_env(tmp_path, CLAUDE_AGENT_ROLE="env-only-role")
    # Act
    result = _invoke(env)
    # Assert
    assert "env-only-role" in _line(result.output, "role:")


# ---------------------------------------------------------------------------
# Secret hygiene — the bearer VALUE must never appear
# ---------------------------------------------------------------------------


def test_whoami_never_prints_bearer_value(tmp_path):
    # Arrange
    env = _blank_env(tmp_path, SAC_LISTEN_BEARER=_BEARER_SECRET)
    # Act
    result = _invoke(env)
    # Assert
    assert _BEARER_SECRET not in result.output


def test_whoami_reports_bearer_presence_as_set(tmp_path):
    # Arrange
    env = _blank_env(tmp_path, SAC_LISTEN_BEARER=_BEARER_SECRET)
    # Act
    result = _invoke(env)
    # Assert
    assert "set" in _line(result.output, "bearer:")


def test_whoami_json_never_contains_bearer_value(tmp_path):
    # Arrange
    env = _blank_env(tmp_path, SAC_LISTEN_BEARER=_BEARER_SECRET)
    # Act
    result = _invoke(env, "--json")
    # Assert
    assert _BEARER_SECRET not in result.output


# ---------------------------------------------------------------------------
# --json shape
# ---------------------------------------------------------------------------


def test_whoami_json_top_level_shape(tmp_path):
    # Arrange
    env = _blank_env(tmp_path)
    # Act
    facts = json.loads(_invoke(env, "--json").output)
    # Assert
    assert set(facts) == {"identity", "placement", "execution", "role", "howto"}


def test_whoami_json_unknown_agent_is_null(tmp_path):
    # Arrange
    env = _blank_env(tmp_path)
    # Act
    facts = json.loads(_invoke(env, "--json").output)
    # Assert
    assert facts["identity"]["agent"] is None


def test_whoami_json_bearer_is_presence_marker(tmp_path):
    # Arrange
    env = _blank_env(tmp_path, SAC_LISTEN_BEARER=_BEARER_SECRET)
    # Act
    facts = json.loads(_invoke(env, "--json").output)
    # Assert
    assert facts["execution"]["listen_bearer"] == "set"


def test_whoami_global_json_flag_propagates(tmp_path):
    # Arrange: top-level `sac --json whoami` (no local flag).
    env = _blank_env(tmp_path)
    # Act
    result = CliRunner().invoke(main, ["--json", "whoami"], env=env)
    # Assert
    assert set(json.loads(result.output)) == {
        "identity",
        "placement",
        "execution",
        "role",
        "howto",
    }


# ---------------------------------------------------------------------------
# Spec-derived facts (resolution via the injected YAML_DIRS search path)
# ---------------------------------------------------------------------------


def test_whoami_spec_role_resolved(tmp_path):
    # Arrange
    env = _spec_env(tmp_path)
    # Act
    result = _invoke(env)
    # Assert
    assert "fixture-worker" in _line(result.output, "role:")


def test_whoami_spec_purpose_resolved(tmp_path):
    # Arrange
    env = _spec_env(tmp_path)
    # Act
    result = _invoke(env)
    # Assert
    assert "verify whoami spec projection" in _line(result.output, "purpose:")


def test_whoami_spec_runtime_resolved(tmp_path):
    # Arrange
    env = _spec_env(tmp_path)
    # Act
    result = _invoke(env)
    # Assert
    assert "tui" in _line(result.output, "runtime:")


def test_whoami_spec_path_reported(tmp_path):
    # Arrange
    env = _spec_env(tmp_path)
    expected = tmp_path / "agents" / "whoami-fixture" / "spec.yaml"
    # Act
    result = _invoke(env)
    # Assert
    assert str(expected) in _line(result.output, "spec:")


def test_whoami_spec_a2a_port_defaults_to_auto(tmp_path):
    # Arrange
    env = _spec_env(tmp_path)
    # Act
    result = _invoke(env)
    # Assert
    assert "auto" in _line(result.output, "a2a-port:")


def test_whoami_model_env_wins_over_spec(tmp_path):
    # Arrange
    env = _spec_env(tmp_path, SCITEX_AGENT_CONTAINER_MODEL="EnvDisplayModel")
    # Act
    result = _invoke(env)
    # Assert
    assert "EnvDisplayModel" in _line(result.output, "model:")


def test_whoami_model_falls_back_to_spec(tmp_path):
    # Arrange
    env = _spec_env(tmp_path)
    # Act
    result = _invoke(env)
    # Assert
    assert "haiku" in _line(result.output, "model:")


def test_whoami_host_suffixed_name_resolves_bare_spec_dir(tmp_path):
    # Arrange: effective name carries the canonical-host suffix while the
    # spec dir keeps the bare name (multi-host composition).
    registry = _write_spec_registry(tmp_path, "whoami-fixture")
    env = _blank_env(
        tmp_path,
        SCITEX_AGENT_CONTAINER_HOSTNAME="canonhost",
        SAC_NAME="whoami-fixture-canonhost",
        SCITEX_AGENT_CONTAINER_YAML_DIRS=str(registry),
    )
    # Act
    result = _invoke(env)
    # Assert
    assert "fixture-worker" in _line(result.output, "role:")


def test_whoami_json_role_source_is_spec(tmp_path):
    # Arrange
    env = _spec_env(tmp_path)
    # Act
    facts = json.loads(_invoke(env, "--json").output)
    # Assert
    assert facts["role"]["source"] == "spec"


def test_whoami_json_responsibilities_from_extensions(tmp_path):
    # Arrange
    env = _spec_env(tmp_path)
    # Act
    facts = json.loads(_invoke(env, "--json").output)
    # Assert
    assert facts["role"]["responsibilities"] == ["answer whoami queries"]


# ---------------------------------------------------------------------------
# HOW-TO content
# ---------------------------------------------------------------------------


def test_whoami_howto_names_the_emergency_endpoint(tmp_path):
    # Arrange
    env = _blank_env(tmp_path)
    # Act
    result = _invoke(env)
    # Assert
    assert "/v1/host_exec" in result.output


def test_whoami_howto_points_at_bundled_skills(tmp_path):
    # Arrange
    env = _blank_env(tmp_path)
    # Act
    result = _invoke(env)
    # Assert
    assert ".claude/skills/scitex/scitex-agent-container/" in result.output


def test_whoami_howto_scope_uses_board_id(tmp_path):
    # Arrange
    env = _blank_env(tmp_path, SCITEX_TODO_AGENT_ID="board-id-42")
    # Act
    result = _invoke(env)
    # Assert
    assert 'scope="agent:board-id-42"' in result.output


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_spec_name_candidates_strips_host_suffix():
    # Arrange
    name, host = "worker-x-canonhost", "canonhost"
    # Act
    candidates = _spec_name_candidates(name, host)
    # Assert
    assert candidates == ["worker-x-canonhost", "worker-x"]


def test_spec_name_candidates_without_suffix_is_identity():
    # Arrange
    name, host = "worker-x", "canonhost"
    # Act
    candidates = _spec_name_candidates(name, host)
    # Assert
    assert candidates == ["worker-x"]
