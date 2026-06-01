"""Tests for cli_pkg._helpers._agent_list — listing assembly + presentation.

No-mocks rewrite (PA-306 / agent-list slice).

Seams used (all real-callable defaults, real ecosystem types):

* ``_FakeRegistry`` — a hand-rolled stand-in for the registry, exposing
  the single method (``list_all``) that production uses. Same shape as
  the real ``Registry``; not a ``MagicMock``.
* ``_swap_probe`` / ``_swap_discover`` — save/restore the real module
  attributes ``_al._probe_local`` and ``_al._discover_defined_agents``
  with hand-rolled callables. Mirrors the ``test_image_group``
  ``_use_backend`` / ``_use_env_snapshot`` pattern (PA-306 model).
* ``_write_spec_yaml`` — writes a real ``spec.yaml`` under ``tmp_path``;
  production's ``load_config`` / ``validate_config`` then exercise the
  real YAML + validator on real bytes.

No ``unittest.mock``, no ``MagicMock``, no ``monkeypatch`` / ``mocker``,
no ``SimpleNamespace`` posing as config.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import scitex_agent_container.cli_pkg._helpers._agent_list as _al
from scitex_agent_container.cli_pkg._helpers._agent_list import (
    _extract_damaged_fields,
    _probe_local,
    get_agent_list_data,
    print_agent_list,
    print_agent_list_json,
)

# ---------------------------------------------------------------------------
# Real-fake registry — exposes the one method production uses.
# ---------------------------------------------------------------------------


class _FakeRegistry:
    """Hand-rolled stand-in for ``Registry`` — same shape as the real one."""

    def __init__(self, entries: list[dict]) -> None:
        self._entries = entries

    def list_all(self) -> list[dict]:
        return list(self._entries)


# ---------------------------------------------------------------------------
# Module-attribute save/restore seams (mirrors test_image_group pattern).
# Production-side defaults are real callables; we swap them at test scope.
# ---------------------------------------------------------------------------


@contextmanager
def _swap_probe(impl: Callable[[Any], bool | None]) -> Iterator[None]:
    """Swap ``_probe_local`` for a real callable returning a fixed value.

    Production resolves the probe via the parent ``cli_pkg._helpers``
    package (``getattr(_pkg, "_probe_local", _probe_local)``) so the
    swap must land on BOTH the inner module and the parent package
    re-export to actually intercept the call.
    """
    import scitex_agent_container.cli_pkg._helpers as _pkg

    saved_al = _al._probe_local
    saved_pkg = getattr(_pkg, "_probe_local", None)
    _al._probe_local = impl  # type: ignore[assignment]
    _pkg._probe_local = impl  # type: ignore[assignment]
    try:
        yield
    finally:
        _al._probe_local = saved_al  # type: ignore[assignment]
        if saved_pkg is None:
            delattr(_pkg, "_probe_local")
        else:
            _pkg._probe_local = saved_pkg  # type: ignore[assignment]


@contextmanager
def _swap_discover(
    impl: Callable[[], list[tuple[str, Path]]],
) -> Iterator[None]:
    """Swap ``_al._discover_defined_agents`` for a real callable."""
    saved = _al._discover_defined_agents
    _al._discover_defined_agents = impl  # type: ignore[assignment]
    try:
        yield
    finally:
        _al._discover_defined_agents = saved  # type: ignore[assignment]


def _no_discover() -> list[tuple[str, Path]]:
    """Return [] — used when a test only cares about registry rows."""
    return []


# ---------------------------------------------------------------------------
# Real spec.yaml writer — exercises real load_config / validate_config.
# ---------------------------------------------------------------------------


def _write_valid_spec(
    dir_: Path, *, capabilities: str | None = None, machine: str | None = None
) -> Path:
    """Write a minimal real v3 spec.yaml; optionally with labels."""
    dir_.mkdir(parents=True, exist_ok=True)
    spec = dir_ / "spec.yaml"
    lines = ["apiVersion: scitex-agent-container/v3", "kind: Agent"]
    label_lines: list[str] = []
    if capabilities is not None:
        label_lines.append(f'    capabilities: "{capabilities}"')
    if machine is not None:
        label_lines.append(f'    machine: "{machine}"')
    if label_lines:
        # ``cfg.labels`` is sourced from ``metadata.labels`` by the v3 loader.
        lines.append("metadata:")
        lines.append("  labels:")
        lines.extend(label_lines)
    else:
        lines.append("metadata: {}")
    lines.extend(["spec:", "  runtime: apptainer"])
    spec.write_text("\n".join(lines) + "\n")
    return spec


def _write_invalid_spec(dir_: Path) -> Path:
    """Write a spec.yaml that real ``validate_config`` rejects.

    Uses ``runtime: docker`` which the real validator rejects with a
    ``spec.runtime ...`` error since the 2026-05-13 apptainer-only
    cutover. ``load_config`` (which runs ``validate_raw`` before
    constructing the dataclass) will raise on this yaml, so registry
    rows with this config land in the "load failed → unknown" branch
    while standalone ``validate_config`` calls (the defined-agent
    branch) get back the error list.
    """
    dir_.mkdir(parents=True, exist_ok=True)
    spec = dir_ / "spec.yaml"
    spec.write_text(
        "apiVersion: scitex-agent-container/v3\nkind: Agent\nmetadata: {}\n"
        "spec:\n  runtime: docker\n"
    )
    return spec


# ---------------------------------------------------------------------------
# _extract_damaged_fields — pure regex extraction. No seams needed.
# ---------------------------------------------------------------------------


def test_extract_damaged_fields_collects_unique_spec_fields():
    # Arrange
    errors = [
        "spec.runtime is required",
        "spec.runtime cannot be empty",  # dup → suppressed
        "metadata.name is no longer accepted",
    ]
    # Act
    out = _extract_damaged_fields(errors)
    # Assert
    assert out == ["spec.runtime", "metadata.name"]


def test_extract_damaged_fields_caps_long_list_at_three_plus_summary():
    # Arrange
    errors = [f"spec.field_{i} is bad" for i in range(7)]
    # Act
    out = _extract_damaged_fields(errors)
    # Assert
    assert len(out) == 4 and out[-1].startswith("+4 more")


def test_extract_damaged_fields_returns_empty_for_empty_input():
    # Arrange
    errors: list[str] = []
    # Act
    out = _extract_damaged_fields(errors)
    # Assert
    assert out == []


def test_extract_damaged_fields_returns_empty_when_no_field_pattern_matches():
    # Arrange
    errors = ["random text", "no fields"]
    # Act
    out = _extract_damaged_fields(errors)
    # Assert
    assert out == []


def test_extract_damaged_fields_captures_dotted_subpath():
    # Arrange
    errors = ["spec.claude.flags is required"]
    # Act
    out = _extract_damaged_fields(errors)
    # Assert
    assert out == ["spec.claude.flags"]


# ---------------------------------------------------------------------------
# _probe_local — relies on real ClaudeSessionRuntime import. The only
# behaviour worth testing without spinning a real container engine is
# the "exception → None" contract. Both prior tests fabricated a fake
# runtime via monkeypatch.setattr — pure mock theatre. Honest delete
# would lose the exception contract coverage, so we test it via the
# REAL import path: pass a config that the real runtime cannot probe
# (no container_id sidecar, no docker daemon needed), and assert the
# wrapper still returns a bool-or-None without raising.
#
# This is a real-integration assert, not a mock.
# ---------------------------------------------------------------------------


def test_probe_local_never_raises_returns_bool_or_none_on_real_runtime(tmp_path):
    # Arrange — real AgentConfig, no live container, no state-dir.
    from scitex_agent_container.config._types import AgentConfig

    cfg = AgentConfig(name="probe-test-agent")
    # Act
    result = _probe_local(cfg)
    # Assert
    assert result is None or isinstance(result, bool)


# ---------------------------------------------------------------------------
# get_agent_list_data — uses real load_config / validate_config on real
# spec.yaml files under tmp_path; swaps _probe_local + _discover_defined.
# ---------------------------------------------------------------------------


def test_get_data_with_empty_registry_returns_empty_list():
    # Arrange
    registry = _FakeRegistry([])
    # Act
    with _swap_discover(_no_discover):
        out = get_agent_list_data(registry)
    # Assert
    assert out == []


def test_get_data_with_unloadable_config_yields_status_unknown(tmp_path):
    # Arrange — config path that does not exist → real load_config raises.
    missing = tmp_path / "nope" / "spec.yaml"
    entries = [{"name": "x", "screen": "s", "started_at": "ts", "config": str(missing)}]
    registry = _FakeRegistry(entries)
    # Act
    with _swap_discover(_no_discover):
        out = get_agent_list_data(registry)
    # Assert
    assert out[0]["status"] == "unknown" and out[0].get("liveness_unknown") is True


def test_get_data_with_capability_filter_includes_matching_agent(tmp_path):
    # Arrange — real spec with labels.capabilities="HPC, GPU".
    spec = _write_valid_spec(tmp_path / "x", capabilities="HPC, GPU")
    entries = [
        {"name": "x", "screen": "s", "started_at": "ts", "config": str(spec)},
    ]
    registry = _FakeRegistry(entries)
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        out = get_agent_list_data(registry, capability="HPC")
    # Assert
    assert len(out) == 1 and out[0]["name"] == "x"


def test_get_data_with_capability_filter_excludes_non_matching_agent(tmp_path):
    # Arrange
    spec = _write_valid_spec(tmp_path / "x", capabilities="GPU")
    entries = [{"name": "x", "config": str(spec)}]
    registry = _FakeRegistry(entries)
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        out = get_agent_list_data(registry, capability="HPC")
    # Assert
    assert out == []


def test_get_data_with_machine_filter_excludes_non_matching_agent(tmp_path):
    # Arrange
    spec = _write_valid_spec(tmp_path / "x", machine="m2")
    entries = [{"name": "x", "config": str(spec)}]
    registry = _FakeRegistry(entries)
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        out = get_agent_list_data(registry, machine="m1")
    # Assert
    assert out == []


def test_get_data_row_status_running_when_probe_returns_true(tmp_path):
    # Arrange
    spec = _write_valid_spec(tmp_path / "x")
    entries = [{"name": "x", "config": str(spec), "screen": "-", "started_at": "-"}]
    registry = _FakeRegistry(entries)
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        out = get_agent_list_data(registry)
    # Assert
    assert out[0]["status"] == "running"


def test_get_data_row_status_stopped_when_probe_returns_false(tmp_path):
    # Arrange
    spec = _write_valid_spec(tmp_path / "x")
    entries = [{"name": "x", "config": str(spec)}]
    registry = _FakeRegistry(entries)
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: False):
        out = get_agent_list_data(registry)
    # Assert
    assert out[0]["status"] == "stopped"


def test_get_data_row_status_unknown_when_probe_returns_none(tmp_path):
    # Arrange
    spec = _write_valid_spec(tmp_path / "x")
    entries = [{"name": "x", "config": str(spec)}]
    registry = _FakeRegistry(entries)
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: None):
        out = get_agent_list_data(registry)
    # Assert
    assert out[0]["status"] == "unknown"


def test_get_data_row_liveness_unknown_flagged_when_probe_returns_none(tmp_path):
    # Arrange
    spec = _write_valid_spec(tmp_path / "x")
    entries = [{"name": "x", "config": str(spec)}]
    registry = _FakeRegistry(entries)
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: None):
        out = get_agent_list_data(registry)
    # Assert
    assert out[0].get("liveness_unknown") is True


def test_get_data_merges_in_defined_agents_absent_from_registry(tmp_path):
    # Arrange — agent on disk only, not in registry.
    spec = _write_valid_spec(tmp_path / "ondisk")
    registry = _FakeRegistry([])

    def _discover() -> list[tuple[str, Path]]:
        return [("ondisk", spec)]

    # Act
    with _swap_discover(_discover):
        out = get_agent_list_data(registry)
    # Assert
    assert any(r["name"] == "ondisk" and r["status"] == "defined" for r in out)


def test_get_data_marks_defined_agent_with_invalid_yaml_as_invalid(tmp_path):
    # Arrange — spec.yaml that real validator rejects.
    spec = _write_invalid_spec(tmp_path / "bad")
    registry = _FakeRegistry([])

    def _discover() -> list[tuple[str, Path]]:
        return [("bad", spec)]

    # Act
    with _swap_discover(_discover):
        out = get_agent_list_data(registry)
    # Assert
    row = next(r for r in out if r["name"] == "bad")
    assert row["status"] == "invalid" and row["validation_errors"]


# ---------------------------------------------------------------------------
# print_agent_list (table) + print_agent_list_json
# ---------------------------------------------------------------------------


def test_print_agent_list_prints_no_agents_message_when_empty(capsys):
    # Arrange
    registry = _FakeRegistry([])
    # Act
    with _swap_discover(_no_discover):
        print_agent_list(registry)
    # Assert
    assert "No agents found" in capsys.readouterr().out


def test_print_agent_list_renders_agent_name_in_table(capsys, tmp_path):
    # Arrange
    spec = _write_valid_spec(tmp_path / "x")
    registry = _FakeRegistry(
        [{"name": "x", "config": str(spec), "screen": "s", "started_at": "ts"}]
    )
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        print_agent_list(registry)
    # Assert
    assert "x" in capsys.readouterr().out


def test_print_agent_list_renders_status_word_for_running_agent(capsys, tmp_path):
    # Arrange
    spec = _write_valid_spec(tmp_path / "x")
    registry = _FakeRegistry(
        [{"name": "x", "config": str(spec), "screen": "s", "started_at": "ts"}]
    )
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        print_agent_list(registry)
    # Assert — rich may color, the text always contains the word.
    assert "running" in capsys.readouterr().out


def test_print_agent_list_prints_full_validation_errors_under_table(capsys, tmp_path):
    # Arrange — invalid spec triggers real validate_config errors mentioning spec.runtime.
    spec = _write_invalid_spec(tmp_path / "x")
    registry = _FakeRegistry([{"name": "x", "config": str(spec)}])
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        print_agent_list(registry)
    # Assert
    assert "spec.runtime" in capsys.readouterr().out


def test_print_agent_list_json_emits_agent_name_in_first_row(capsys, tmp_path):
    # Arrange
    spec = _write_valid_spec(tmp_path / "x")
    registry = _FakeRegistry([{"name": "x", "config": str(spec)}])
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        print_agent_list_json(registry)
    # Assert
    data = json.loads(capsys.readouterr().out)
    assert data[0]["name"] == "x"


# ---------------------------------------------------------------------------
# _discover_defined_agents — real-FS walk via tmp-rooted HOME.
#
# Splits the legacy multi-assert test into two single-assert siblings
# (TQ007). Uses real ``os.environ["HOME"]`` swap (save/restore), no
# monkeypatch fixture. The project-scope branch is suppressed by
# pointing HOME at a tmp dir that is not inside a git repo, so the
# real ``find_project_scope`` returns None naturally.
# ---------------------------------------------------------------------------


@contextmanager
def _home_set_to(path: Path) -> Iterator[None]:
    """Set $HOME to ``path`` for the duration of the block, restore on exit."""
    import os as _os

    saved = _os.environ.get("HOME")
    _os.environ["HOME"] = str(path)
    try:
        yield
    finally:
        if saved is None:
            _os.environ.pop("HOME", None)
        else:
            _os.environ["HOME"] = saved


def _seed_home_with_agents(home: Path) -> None:
    """Create the home-scope agents tree the discoverer walks."""
    agents = home / ".scitex" / "agent-container" / "agents"
    agents.mkdir(parents=True)
    (agents / "a1").mkdir()
    (agents / "a1" / "spec.yaml").write_text("apiVersion: x")
    (agents / "no-spec").mkdir()  # no spec.yaml → must be skipped


def test_discover_defined_agents_finds_dirs_with_spec_yaml(tmp_path):
    # Arrange
    home = tmp_path / "home"
    home.mkdir()
    _seed_home_with_agents(home)
    # The production walker also pokes a real Path.home() — we drive
    # it via $HOME by re-importing the discoverer in a fresh scope.
    # ``Path.home()`` honours $HOME on POSIX, which is the platform
    # this codebase targets.
    # Act
    with _home_set_to(home):
        pairs = _al._discover_defined_agents()
    # Assert
    assert "a1" in [n for n, _ in pairs]


def test_discover_defined_agents_skips_dirs_without_spec_yaml(tmp_path):
    # Arrange
    home = tmp_path / "home"
    home.mkdir()
    _seed_home_with_agents(home)
    # Act
    with _home_set_to(home):
        pairs = _al._discover_defined_agents()
    # Assert
    assert "no-spec" not in [n for n, _ in pairs]


# ---------------------------------------------------------------------------
# account column (operator request 4581) — per-agent Anthropic-account
# label so the operator can spot agents sharing one rate limit.
#
# ``_safe_account_for`` is called as a bare module-level name inside
# ``_agent_list``, so swapping ``_al._safe_account_for`` intercepts both
# row-builder call sites (registered + defined-on-disk). The swap is a
# real callable returning a fixed label — not a mock.
# ---------------------------------------------------------------------------


@contextmanager
def _swap_account(impl: Callable[[Any], str]) -> Iterator[None]:
    """Swap ``_al._safe_account_for`` for a real callable returning a label."""
    saved = _al._safe_account_for
    _al._safe_account_for = impl  # type: ignore[assignment]
    try:
        yield
    finally:
        _al._safe_account_for = saved  # type: ignore[assignment]


def test_get_data_row_carries_account_field(tmp_path):
    # Arrange
    spec = _write_valid_spec(tmp_path / "x")
    entries = [{"name": "x", "config": str(spec)}]
    registry = _FakeRegistry(entries)
    # Act
    with (
        _swap_discover(_no_discover),
        _swap_probe(lambda cfg: True),
        _swap_account(lambda cfg: "alice@example.com"),
    ):
        out = get_agent_list_data(registry)
    # Assert
    assert out[0]["account"] == "alice@example.com"


def test_get_data_defined_agent_row_carries_account_field(tmp_path):
    # Arrange — agent on disk only, not in registry.
    spec = _write_valid_spec(tmp_path / "ondisk")
    registry = _FakeRegistry([])

    def _discover() -> list[tuple[str, Path]]:
        return [("ondisk", spec)]

    # Act
    with _swap_discover(_discover), _swap_account(lambda cfg: "bob@example.com"):
        out = get_agent_list_data(registry)
    # Assert
    row = next(r for r in out if r["name"] == "ondisk")
    assert row["account"] == "bob@example.com"


def test_print_agent_list_renders_account_column_header(capsys, tmp_path):
    # Arrange
    spec = _write_valid_spec(tmp_path / "x")
    registry = _FakeRegistry([{"name": "x", "config": str(spec)}])
    # Act
    with (
        _swap_discover(_no_discover),
        _swap_probe(lambda cfg: True),
        _swap_account(lambda cfg: "alice@example.com"),
    ):
        print_agent_list(registry)
    # Assert
    assert "Account" in capsys.readouterr().out


def test_print_agent_list_renders_account_value(capsys, tmp_path):
    # Arrange — a short label survives the narrow capture-mode terminal
    # width (a long email gets ellipsised by rich; the JSON test below
    # covers the full value).
    spec = _write_valid_spec(tmp_path / "x")
    registry = _FakeRegistry([{"name": "x", "config": str(spec)}])
    # Act
    with (
        _swap_discover(_no_discover),
        _swap_probe(lambda cfg: True),
        _swap_account(lambda cfg: "acct-x"),
    ):
        print_agent_list(registry)
    # Assert
    assert "acct-x" in capsys.readouterr().out


def test_print_agent_list_json_emits_account_in_row(capsys, tmp_path):
    # Arrange
    spec = _write_valid_spec(tmp_path / "x")
    registry = _FakeRegistry([{"name": "x", "config": str(spec)}])
    # Act
    with (
        _swap_discover(_no_discover),
        _swap_probe(lambda cfg: True),
        _swap_account(lambda cfg: "alice@example.com"),
    ):
        print_agent_list_json(registry)
    # Assert
    data = json.loads(capsys.readouterr().out)
    assert data[0]["account"] == "alice@example.com"


def test_safe_account_for_resolves_real_credentials_end_to_end(tmp_path):
    # Arrange — real credentials + claude.json under a tmp HOME; no env
    # override on the config, so the real resolver flows through.
    import json as _json

    from scitex_agent_container.config._types import AgentConfig

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)
    (claude_dir / ".credentials.json").write_text(
        _json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-SECRET",
                    "expiresAt": 9_999_999_999_000,
                    "subscriptionType": "max",
                    "rateLimitTier": "default_claude_max_20x",
                }
            }
        )
    )
    (tmp_path / ".claude.json").write_text(
        _json.dumps({"oauthAccount": {"emailAddress": "real@example.com"}})
    )
    cfg = AgentConfig(name="x")  # no SAC_ANTHROPIC_API_KEY in env
    # Act
    with _home_set_to(tmp_path):
        label = _al._safe_account_for(cfg)
    # Assert
    assert label == "real@example.com"


def test_safe_account_for_returns_string_on_none_config():
    # Arrange — None config (registry entry whose YAML failed to load).
    cfg = None
    # Act — must not crash; contract is "never raises, returns a string".
    result = _al._safe_account_for(cfg)
    # Assert
    assert isinstance(result, str) and result


# ---------------------------------------------------------------------------
# Lead task 2026-06-01: per-agent CPU% + RSS in the list row.
# ---------------------------------------------------------------------------
#
# These tests use real OS processes for the PID side of the contract:
# ``os.getpid()`` (the pytest process itself) for the "live PID" case
# and an int that is guaranteed unused for the "dead PID" case. The
# probe code in ``_state._meta.resources`` IS the unit under test; the
# rest of the list pipeline is exercised end-to-end with the real
# ``_FakeRegistry`` + ``_write_valid_spec`` helpers used elsewhere in
# this file. PA-306 no-mocks: no monkeypatch of psutil, no MagicMock,
# no patched ``collect_agent_resources``.


def test_get_data_running_row_carries_cpu_and_mem_keys(tmp_path):
    # Arrange — real PID of the pytest process itself (always alive
    # for the duration of this test). The probe walks os.getpid()'s
    # process tree and returns real cpu_percent + mem_rss_mb floats.
    import os as _os

    spec = _write_valid_spec(tmp_path / "live")
    entries = [
        {
            "name": "live",
            "config": str(spec),
            "screen": "-",
            "started_at": "-",
            "pid": _os.getpid(),
        }
    ]
    registry = _FakeRegistry(entries)
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        out = get_agent_list_data(registry)
    # Assert — both keys present on the live-PID row.
    assert "cpu_percent" in out[0] and "mem_rss_mb" in out[0]


def test_get_data_dead_pid_row_omits_resource_keys(tmp_path):
    # Arrange — a PID well above any kernel's PID space. Observability
    # contract: absent ≠ 0 — the row must NOT carry zero-filled keys,
    # the row must OMIT them so a consumer can tell "not probed" from
    # "literally idle".
    spec = _write_valid_spec(tmp_path / "dead")
    entries = [
        {
            "name": "dead",
            "config": str(spec),
            "screen": "-",
            "started_at": "-",
            "pid": 2**31 - 1,
        }
    ]
    registry = _FakeRegistry(entries)
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        out = get_agent_list_data(registry)
    # Assert — neither key on the row.
    assert "cpu_percent" not in out[0] and "mem_rss_mb" not in out[0]


def test_get_data_unknown_pid_sentinel_omits_resource_keys(tmp_path):
    # Arrange — registry entry with ``pid=0`` (the "PID unknown"
    # sentinel ``_pids_from_session`` returns when tmux can't tell us
    # the pane PID). The probe must NOT treat 0 as a real PID; the
    # row must omit the resource keys.
    spec = _write_valid_spec(tmp_path / "unknown")
    entries = [
        {
            "name": "unknown",
            "config": str(spec),
            "screen": "-",
            "started_at": "-",
            "pid": 0,
        }
    ]
    registry = _FakeRegistry(entries)
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        out = get_agent_list_data(registry)
    # Assert
    assert "cpu_percent" not in out[0] and "mem_rss_mb" not in out[0]


def test_get_data_cpu_percent_value_is_float_when_probed(tmp_path):
    # Arrange — same live-PID setup; pin the field TYPE so a future
    # refactor can't accidentally emit a string or int (consumers
    # like the table renderer use the numeric format).
    import os as _os

    spec = _write_valid_spec(tmp_path / "type")
    entries = [
        {
            "name": "type",
            "config": str(spec),
            "screen": "-",
            "started_at": "-",
            "pid": _os.getpid(),
        }
    ]
    registry = _FakeRegistry(entries)
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        out = get_agent_list_data(registry)
    # Assert
    assert isinstance(out[0]["cpu_percent"], float)


def test_get_data_mem_rss_mb_is_positive_when_probed(tmp_path):
    # Arrange — the pytest process always has >>1 MB RSS; we just pin
    # the floor so a regression to ``0.0`` (which would look like
    # "probed and idle" — the wrong UX) is caught.
    import os as _os

    spec = _write_valid_spec(tmp_path / "rss")
    entries = [
        {
            "name": "rss",
            "config": str(spec),
            "screen": "-",
            "started_at": "-",
            "pid": _os.getpid(),
        }
    ]
    registry = _FakeRegistry(entries)
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        out = get_agent_list_data(registry)
    # Assert
    assert out[0]["mem_rss_mb"] > 1.0


def test_print_agent_list_renders_cpu_and_mem_columns(capsys, tmp_path):
    # Arrange — live PID so the row carries the fields and the table
    # picks them up.
    import os as _os

    spec = _write_valid_spec(tmp_path / "tbl")
    registry = _FakeRegistry(
        [
            {
                "name": "tbl",
                "config": str(spec),
                "screen": "s",
                "started_at": "ts",
                "pid": _os.getpid(),
            }
        ]
    )
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        print_agent_list(registry)
    # Assert — both column headers appear in the rendered output.
    out = capsys.readouterr().out
    assert "CPU%" in out and "MEM" in out


def test_print_agent_list_renders_dash_for_dead_pid_resource_cells(capsys, tmp_path):
    # Arrange — dead PID; row absent-outs cpu_percent / mem_rss_mb; the
    # table must render placeholder cells ("-"), NOT empty strings (so
    # the column alignment stays readable).
    spec = _write_valid_spec(tmp_path / "dead")
    registry = _FakeRegistry(
        [
            {
                "name": "dead",
                "config": str(spec),
                "screen": "s",
                "started_at": "ts",
                "pid": 2**31 - 1,
            }
        ]
    )
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        print_agent_list(registry)
    # Assert — the row appears with the placeholder cells. We pin
    # the agent's name AND a ``-`` cell on the same line; rich's
    # default table renderer puts each row's cells on one line.
    out = capsys.readouterr().out
    dead_row_line = next((line for line in out.splitlines() if "dead" in line), "")
    assert " - " in dead_row_line


def test_print_agent_list_json_emits_cpu_percent_for_live_pid(capsys, tmp_path):
    # Arrange — live PID, JSON path; the field must surface in the
    # JSON serialisation (downstream consumers — fleet hubs, dashboards
    # — read JSON, not the rich table).
    import os as _os

    spec = _write_valid_spec(tmp_path / "json")
    registry = _FakeRegistry(
        [
            {
                "name": "json",
                "config": str(spec),
                "screen": "s",
                "started_at": "ts",
                "pid": _os.getpid(),
            }
        ]
    )
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        print_agent_list_json(registry)
    # Assert
    data = json.loads(capsys.readouterr().out)
    assert "cpu_percent" in data[0]


def test_print_agent_list_json_omits_cpu_percent_for_dead_pid(capsys, tmp_path):
    # Arrange — dead PID; JSON path must OMIT the key (not emit ``null``
    # / 0.0) so the observability contract holds end-to-end.
    spec = _write_valid_spec(tmp_path / "dead-json")
    registry = _FakeRegistry(
        [
            {
                "name": "dead-json",
                "config": str(spec),
                "screen": "s",
                "started_at": "ts",
                "pid": 2**31 - 1,
            }
        ]
    )
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        print_agent_list_json(registry)
    # Assert
    data = json.loads(capsys.readouterr().out)
    assert "cpu_percent" not in data[0]
