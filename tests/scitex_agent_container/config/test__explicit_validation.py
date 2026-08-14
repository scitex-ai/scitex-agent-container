"""Tests for config._explicit_validation (red-start ruling 2026-07-21).

EVERY spec field must be written explicitly; an omitted field is a load
ERROR listing all missing fields at once with a paste-ready hint that
actually clears the condition (round-trip proven — the fleet has an
incident memory about hints that do not clear their own gate).

The GREEN fixture below is HAND-WRITTEN field by field (not generated
from the production map) so the map and the fixture cannot agree by
construction; the mutation-proof parametrization then shows each
sampled field's absence turns the load RED naming that exact path.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from scitex_agent_container.config import load_config
from scitex_agent_container.config._explicit_validation import (
    PASTE_BEGIN,
    PASTE_END,
    ExplicitSpecError,
    validate,
)

# Hand-written, fully-explicit kind: Agent spec — every required field
# spelled out (86 fields + placement).
_EXPLICIT_YAML = """
apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: tui
  provider: anthropic
  host: ${HOSTNAME}
  workdir: /home/agent/work
  python-venv: ""
  startup_commands: []
  startup_prompts: []
  listen: []
  extensions: {}
  mcp_servers: {}
  user: ""
  to_home: ./to_home
  container:
    image: scitex-agent-container:latest
    volumes: []
    network: host
    mount_host_claude: false
  claude:
    model: sonnet
    channels: []
    flags: []
    raw_options: {}
    session: fresh
    continue_max_age_minutes: null
    resume_id: ""
    auto_accept: true
    account: ""
    credentials_file: ""
    credentials_files: []
    provider: null
  health:
    enabled: true
    interval: 60
    timeout: 5
    method: sdk-alive
  watchdog:
    enabled: false
    interval: 1.5
    responses:
      y_n: "1"
      y_y_n: "2"
      waiting: /speak-and-call
  restart:
    policy: never
    max_retries: 3
    prune_on_stop: false
    backoff:
      initial: 30
      max: 300
      multiplier: 2
  autonomous:
    enabled: false
    drive_until: DONE
    max_turns: 50
    idle_kick_after_s: 120
    kick_text: Continue. Print DONE when finished.
  apptainer:
    image: /opt/sac/scitex.sif
    binds: []
    env: {}
    raw_args: []
    post: ""
    environment: {}
    def_file: ""
    nv: false
    rocm: false
    overlay: ""
    overlay_size: ""
    overlay_create_if_missing: true
    tmpfs_size: 2G
    relaxed: false
    fakeroot: false
    jail: false
    nested_build: false
  hooks:
    pre_start: []
    post_start: []
    pre_stop: []
    post_stop: []
    on_compact: []
    on_restart: []
    on_diff: []
  context_management:
    trigger_at_percent: 70.0
    strategy: noop
    warn_before_n_checks: 0
    check_interval_seconds: 300
    state_file: ~/.scitex/agent-container/state/<agent>.json
  a2a:
    host: 127.0.0.1
    port: auto
  comms:
    outbound:
      siblings: allow
      parent: allow
    inbound:
      siblings: allow
      parent: allow
    a2a:
      listen: true
  lineage:
    group: ""
    may_spawn: true
"""


def _explicit_doc() -> dict:
    return yaml.safe_load(_EXPLICIT_YAML)


def _write_doc(tmp_path: Path, doc: dict, name: str = "explicit-agent") -> Path:
    agent_dir = tmp_path / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "spec.yaml"
    path.write_text(yaml.safe_dump(doc))
    return path


def _remove(doc: dict, dotted: str) -> dict:
    out = copy.deepcopy(doc)
    cur = out["spec"]
    parts = dotted.split(".")
    for part in parts[:-1]:
        cur = cur[part]
    del cur[parts[-1]]
    return out


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _red_error(path: Path) -> ValueError:
    """Load a spec expected to be RED; return the raised error."""
    with pytest.raises(ValueError) as excinfo:
        load_config(path)
    return excinfo.value


def _raised_by(action) -> ValueError:
    """Run ``action`` expected to raise; return the raised error."""
    with pytest.raises(ValueError) as excinfo:
        action()
    return excinfo.value


def _healed_doc(doc: dict, error: ValueError) -> dict:
    """Merge the error's paste-ready block back into ``doc``."""
    block = str(error).split(PASTE_BEGIN)[1].split(PASTE_END)[0]
    return _deep_merge(doc, yaml.safe_load(block))


# ---------------------------------------------------------------------------
# GREEN: the hand-written fully-explicit spec loads.
# ---------------------------------------------------------------------------


def test_fully_explicit_spec_loads_green(tmp_path: Path) -> None:
    # Arrange
    path = _write_doc(tmp_path, _explicit_doc())
    # Act
    cfg = load_config(path)
    # Assert
    assert cfg.name == "explicit-agent"


# ---------------------------------------------------------------------------
# MUTATION-PROOF: removing any sampled field turns the load RED and the
# error names that exact YAML path. Sample spans top-level, claude,
# apptainer, health, watchdog, a2a (+ restart/comms/context_management).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dotted",
    [
        "runtime",
        "workdir",
        "to_home",
        "claude.model",
        "claude.session",
        "apptainer.binds",
        "apptainer.tmpfs_size",
        "health.method",
        "watchdog.responses.y_n",
        "a2a.port",
        "restart.backoff.initial",
        "comms.a2a.listen",
        "context_management.strategy",
    ],
)
def test_removing_field_turns_load_red_naming_exact_path(
    tmp_path: Path, dotted: str
) -> None:
    # Arrange
    path = _write_doc(tmp_path, _remove(_explicit_doc(), dotted))
    # Act
    error = _red_error(path)
    # Assert
    assert f"spec.{dotted}" in str(error)


# ---------------------------------------------------------------------------
# Completeness: several missing fields are ALL listed in ONE error.
# ---------------------------------------------------------------------------


@pytest.fixture
def _three_missing_error(tmp_path: Path) -> str:
    """Error message for a spec missing three fields across sections."""
    doc = _explicit_doc()
    for dotted in ("runtime", "claude.session", "a2a.port"):
        doc = _remove(doc, dotted)
    return str(_red_error(_write_doc(tmp_path, doc, "three-missing")))


def test_error_counts_all_missing_fields(_three_missing_error: str) -> None:
    # Arrange
    message = _three_missing_error
    # Act
    counted = "3 required field(s)" in message
    # Assert
    assert counted


@pytest.mark.parametrize("dotted", ["runtime", "claude.session", "a2a.port"])
def test_error_lists_each_missing_field(_three_missing_error: str, dotted: str) -> None:
    # Arrange
    message = _three_missing_error
    # Act
    named = f"spec.{dotted}" in message
    # Assert
    assert named


def test_error_names_the_loaded_file(tmp_path: Path) -> None:
    # Arrange
    path = _write_doc(tmp_path, _remove(_explicit_doc(), "runtime"), "named")
    # Act
    error = _red_error(path)
    # Assert
    assert f"While loading: {path}" in str(error)


# ---------------------------------------------------------------------------
# The hint CLEARS the condition: merging the paste-ready block into the
# failing spec makes it load green (round-trip).
# ---------------------------------------------------------------------------


@pytest.fixture
def _round_trip_healed(tmp_path: Path) -> dict:
    """Red doc (five fields struck across five sections) healed by the hint."""
    doc = _explicit_doc()
    for dotted in (
        "runtime",
        "claude.session",
        "apptainer.tmpfs_size",
        "watchdog.responses.waiting",
        "a2a.port",
    ):
        doc = _remove(doc, dotted)
    error = _red_error(_write_doc(tmp_path, doc, "red-agent"))
    return _healed_doc(doc, error)


def test_paste_ready_hint_round_trips_to_green(
    tmp_path: Path, _round_trip_healed: dict
) -> None:
    # Arrange
    healed_path = _write_doc(tmp_path, _round_trip_healed, "healed-agent")
    # Act
    cfg = load_config(healed_path)
    # Assert
    assert cfg.name == "healed-agent"


def test_paste_ready_hint_preserves_omission_behaviour(
    tmp_path: Path, _round_trip_healed: dict
) -> None:
    # Arrange — pasted claude.session must reproduce what omission meant:
    # role-less agent -> 'fresh' (null keeps the role-derived default).
    healed_path = _write_doc(tmp_path, _round_trip_healed, "healed-session")
    # Act
    cfg = load_config(healed_path)
    # Assert
    assert cfg.claude.session == "fresh"


# ---------------------------------------------------------------------------
# Contract shape: error type + no-bypass signature + load_v3 wiring.
# ---------------------------------------------------------------------------


def test_explicit_spec_error_is_a_value_error() -> None:
    # Arrange
    error_type = ExplicitSpecError
    # Act
    is_value_error = issubclass(error_type, ValueError)
    # Assert
    assert is_value_error


def test_validate_signature_has_no_bypass_parameter() -> None:
    # Arrange
    import inspect

    # Act
    parameters = list(inspect.signature(validate).parameters)
    # Assert — doc + path only: no strict=, no env flag, no escape hatch.
    assert parameters == ["doc", "path"]


def test_load_v3_direct_call_raises_explicit_spec_error(tmp_path: Path) -> None:
    # Arrange — load_v3 guards independently of load_config's validate_raw.
    from scitex_agent_container.config._loaders import load_v3

    doc = _remove(_explicit_doc(), "health.method")
    path = _write_doc(tmp_path, doc, "direct-v3")
    # Act
    error = _raised_by(lambda: load_v3(doc, path))
    # Assert
    assert isinstance(error, ExplicitSpecError)


# ---------------------------------------------------------------------------
# kind: AgentProxy — proxy fields required too.
# ---------------------------------------------------------------------------


def test_agent_proxy_missing_proxy_upstream_is_red(tmp_path: Path) -> None:
    # Arrange
    from tests.scitex_agent_container._helpers.explicit_spec import (
        explicit_doc,
    )

    doc = explicit_doc(kind="AgentProxy")
    del doc["spec"]["proxy"]["upstream"]
    path = _write_doc(tmp_path, doc, "proxy-red")
    # Act
    error = _red_error(path)
    # Assert
    assert "spec.proxy.upstream" in str(error)
