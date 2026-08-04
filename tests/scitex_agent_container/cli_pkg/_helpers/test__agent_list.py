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
import os
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

import scitex_agent_container.cli_pkg._helpers._agent_list as _al
from scitex_agent_container.cli_pkg._helpers._agent_list import (
    _extract_damaged_fields,
    _is_self_peer_marker,
    _probe_local,
    get_agent_list_data,
    print_agent_list,
    print_agent_list_json,
)
from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

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
    dir_: Path,
    *,
    capabilities: str | None = None,
    machine: str | None = None,
    groups: list[str] | None = None,
) -> Path:
    """Write a minimal real v3 spec.yaml; optionally with labels.

    ``groups`` is authored as a YAML FLOW LIST (``groups: [a, b]``) —
    the real fleet convention, and deliberately NOT the CSV string the
    abolished ``tags`` label used. A filter that assumed a string would
    pass a test that faked one, so the fixture writes the real shape.
    """
    dir_.mkdir(parents=True, exist_ok=True)
    spec = dir_ / "spec.yaml"
    lines = ["apiVersion: scitex-agent-container/v3", "kind: Agent"]
    label_lines: list[str] = []
    if capabilities is not None:
        label_lines.append(f'    capabilities: "{capabilities}"')
    if machine is not None:
        label_lines.append(f'    machine: "{machine}"')
    if groups is not None:
        label_lines.append(f"    groups: [{', '.join(groups)}]")
    if label_lines:
        # ``cfg.labels`` is sourced from ``metadata.labels`` by the v3 loader.
        lines.append("metadata:")
        lines.append("  labels:")
        lines.extend(label_lines)
    else:
        lines.append("metadata: {}")
    lines.extend(
        [
            "spec:",
            "  runtime: apptainer",
            "  host: ${HOSTNAME}",
            "  workdir: /home/agent/work",
            "  apptainer:",
            "    image: /x.sif",
            "    binds: []",
            "  claude:",
            "    model: sonnet",
            "  health:",
            "    enabled: true",
            "    interval: 60",
            "  restart:",
            "    policy: on-failure",
            "    max_retries: 3",
        ]
    )
    spec.write_text(explicitize_yaml("\n".join(lines) + "\n"))
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
        explicitize_yaml(
            "apiVersion: scitex-agent-container/v3\nkind: Agent\nmetadata: {}\n"
            "spec:\n  runtime: docker\n"
        )
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
# Regression (fix liveness-live-agents-read-stopped): _probe_local must
# select the agent's DECLARED runtime (via _get_runtime), NOT a hardcoded
# ClaudeSessionRuntime. The default runtime is ``tui``; probing a live
# TUI agent through ClaudeSessionRuntime → ApptainerContainerRuntime read
# a nonexistent apptainer_pid and reported "stopped" for a running agent.
#
# THE TWO TESTS BELOW DID NOT GUARD THAT, AND WERE NAMED AS IF THEY DID.
# Neither called ``_probe_local``: one asserted ``_get_runtime``'s mapping,
# the other ``TuiSessionRuntime.is_running``'s rule — both true statements
# about components the probe HAPPENS to use, neither an assertion that the
# probe uses them. Measured 2026-08-04: restoring the exact historical bug
# (``_probe_local`` hardcoding ``ClaudeSessionRuntime``) left this whole
# file at 68 passed. A defect that has now recurred TWICE had no test that
# could go red for it.
#
# They are renamed to say what they actually check, and the real guard —
# ``test_probe_local_reports_a_live_tui_session_as_running`` below — drives
# ``_probe_local`` itself against a live tmux session.
# ---------------------------------------------------------------------------


def test_get_runtime_maps_a_tui_config_to_the_tui_runtime():
    # Arrange — default AgentConfig resolves runtime="tui".
    from scitex_agent_container._lifecycle._runtime_select import _get_runtime
    from scitex_agent_container.config._types import AgentConfig
    from scitex_agent_container.runtimes.tui_session import TuiSessionRuntime

    cfg = AgentConfig(name="tui-probe-agent")
    # Act
    runtime = _get_runtime(cfg)
    # Assert — the SELECTOR's mapping only. Says nothing about its callers.
    assert isinstance(runtime, TuiSessionRuntime)


def test_tui_runtime_reports_a_live_session_as_running():
    # Arrange — a tui runtime over an in-memory multiplexer reporting a live
    # session. Pins the RUNTIME's liveness rule; again says nothing about
    # which runtime _probe_local reaches.
    import time

    from scitex_agent_container.config._types import AgentConfig
    from scitex_agent_container.runtimes.tui_session import (
        TuiSessionRuntime,
        session_name_for,
    )

    class _LiveMux:
        """Real MultiplexerProtocol stand-in: session exists + fresh
        activity (no mock — a hand-rolled in-memory multiplexer)."""

        @staticmethod
        def exists(name: str) -> bool:
            return True

        @staticmethod
        def session_activity(name: str) -> int:
            return int(time.time())

    cfg = AgentConfig(name="tui-live-agent")
    runtime = TuiSessionRuntime(multiplexer=_LiveMux)
    del session_name_for  # imported only to document the tui-<name> convention
    # Act
    running = runtime.is_running(cfg)
    # Assert
    assert running is True


@pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="this guard drives the real probe against a real tmux session",
)
def test_probe_local_reports_a_live_tui_session_as_running():
    # Arrange — a REAL tmux session under the ``tui-<name>`` convention
    # TuiSessionRuntime keys on, with a live pane process. The original
    # defect in one line: a running TUI agent the list must not call
    # "stopped". Hardcode ClaudeSessionRuntime back into _probe_local and
    # this goes RED (no apptainer pidfile → False) — which is exactly what
    # the two tests above could not do.
    from scitex_agent_container.config._types import AgentConfig

    session = f"tui-probe-guard-{os.getpid()}"
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "sleep 120"], check=True
    )
    try:
        # Act — the production probe, not its parts.
        running = _probe_local(AgentConfig(name=session[len("tui-") :]))
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session], check=False)
    # Assert
    assert running is True


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


# ---------------------------------------------------------------------------
# group filter — `metadata.labels.groups`, the ONLY classification field
# (operator decision 2026-07-19). Replaces the abolished `tags` label, whose
# every value duplicated a group the same spec already carried. Read through
# the SSOT multi-value reader `config._group_resolver.all_named_groups`, so a
# YAML LIST is matched natively — the abolished `tags` matcher read a CSV
# STRING and would not have worked here.
# ---------------------------------------------------------------------------


def test_get_data_with_group_filter_includes_matching_agent(tmp_path):
    # Arrange — real spec with labels.groups: [active, researcher].
    spec = _write_valid_spec(tmp_path / "x", groups=["active", "researcher"])
    entries = [
        {"name": "x", "screen": "s", "started_at": "ts", "config": str(spec)},
    ]
    registry = _FakeRegistry(entries)
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        out = get_agent_list_data(registry, group="active")
    # Assert
    assert [r["name"] for r in out] == ["x"]


def test_get_data_with_group_filter_excludes_non_matching_agent(tmp_path):
    # Arrange
    spec = _write_valid_spec(tmp_path / "x", groups=["researcher"])
    entries = [{"name": "x", "config": str(spec)}]
    registry = _FakeRegistry(entries)
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        out = get_agent_list_data(registry, group="active")
    # Assert
    assert out == []


def test_get_data_with_group_filter_matches_any_of_multiple_wanted_values(tmp_path):
    # Arrange — caller passes two comma-separated groups; agent is in one.
    spec = _write_valid_spec(tmp_path / "x", groups=["researcher"])
    entries = [{"name": "x", "config": str(spec)}]
    registry = _FakeRegistry(entries)
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        out = get_agent_list_data(registry, group="active,researcher")
    # Assert — OR-match: any overlap between wanted and carried groups hits.
    assert [r["name"] for r in out] == ["x"]


def test_get_data_with_group_filter_matches_a_non_first_group(tmp_path):
    # Arrange — `active` is NOT the first element. The ACL resolver reduces a
    # spec to its FIRST group; selection must use the MULTI-value read instead,
    # or every real fleet spec (groups: [developer, active]) would be missed.
    spec = _write_valid_spec(tmp_path / "x", groups=["developer", "active"])
    entries = [{"name": "x", "config": str(spec)}]
    registry = _FakeRegistry(entries)
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        out = get_agent_list_data(registry, group="active")
    # Assert
    assert [r["name"] for r in out] == ["x"]


def test_get_data_with_group_filter_ungrouped_agent_is_excluded(tmp_path):
    # Arrange — agent has no groups label at all.
    spec = _write_valid_spec(tmp_path / "x")
    entries = [{"name": "x", "config": str(spec)}]
    registry = _FakeRegistry(entries)
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        out = get_agent_list_data(registry, group="active")
    # Assert
    assert out == []


def test_get_data_without_group_filter_includes_ungrouped_agent(tmp_path):
    # Arrange — no --group passed at all: the filter must be a pure no-op.
    spec = _write_valid_spec(tmp_path / "x")
    entries = [{"name": "x", "config": str(spec)}]
    registry = _FakeRegistry(entries)
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        out = get_agent_list_data(registry)
    # Assert
    assert [r["name"] for r in out] == ["x"]


def test_get_data_with_group_filter_includes_matching_defined_agent(tmp_path):
    # Arrange — defined-on-disk (not registered) agent; the second filter
    # site (the disk-merge loop) must apply the SAME group matching.
    spec = _write_valid_spec(tmp_path / "ondisk", groups=["developer", "active"])
    registry = _FakeRegistry([])

    def _discover() -> list[tuple[str, Path]]:
        return [("ondisk", spec)]

    # Act
    with _swap_discover(_discover):
        out = get_agent_list_data(registry, group="active")
    # Assert
    assert any(r["name"] == "ondisk" for r in out)


def test_get_data_group_filter_selects_what_the_tags_filter_used_to_select(tmp_path):
    """MIGRATION EQUIVALENCE — `--group active` == the old `--tags active-development`.

    The abolition rests on the claim that `tags` carried no information
    `groups` did not: all 16 fleet specs carrying `tags: "active-development"`
    ALSO carried `active` in `groups:`. This pins the consequence — the
    replacement filter returns the SAME agents the removed one did — so the
    migration cannot silently narrow or widen the fleet view.
    """
    # Arrange — two agents in the real fleet shape (the tagged one carried
    # groups: [developer, active]), and one deliberately outside the cohort.
    tagged = _write_valid_spec(tmp_path / "figrecipe", groups=["developer", "active"])
    other = _write_valid_spec(tmp_path / "dormant", groups=["developer"])
    registry = _FakeRegistry(
        [
            {"name": "figrecipe", "config": str(tagged)},
            {"name": "dormant", "config": str(other)},
        ]
    )
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        out = get_agent_list_data(registry, group="active")
    # Assert — exactly the formerly-tagged agent, not the whole developer set.
    assert [r["name"] for r in out] == ["figrecipe"]


def test_get_data_with_group_filter_excludes_non_matching_defined_agent(tmp_path):
    # Arrange — same disk-merge loop, non-matching group this time.
    spec = _write_valid_spec(tmp_path / "ondisk", groups=["researcher"])
    registry = _FakeRegistry([])

    def _discover() -> list[tuple[str, Path]]:
        return [("ondisk", spec)]

    # Act
    with _swap_discover(_discover):
        out = get_agent_list_data(registry, group="active")
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
    # Arrange — invalid spec triggers real validate_config errors mentioning
    # spec.runtime. The per-agent error blocks now live in the FULL (`-v`)
    # view; the default view hides them (operator TG 1490-1495).
    spec = _write_invalid_spec(tmp_path / "x")
    registry = _FakeRegistry([{"name": "x", "config": str(spec)}])
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        print_agent_list(registry, verbose=True)
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
# _is_self_peer_marker / self-peer exclusion from the defined-agent walk.
#
# ``agents/self/spec.yaml`` (see ``_listen/_self_peers.py``) deliberately
# omits apiVersion/kind/spec — its own header says "DO NOT add" them —
# because their ABSENCE is what makes it recognizable as a self-peer
# registration marker rather than a launchable Agent. Running the generic
# Agent validator against it always reported "invalid", even though it
# was working exactly as designed. These tests pin the fix: such markers
# are excluded from ``_discover_defined_agents`` at the source, so they
# never reach validation as a spurious agent in the first place.
# ---------------------------------------------------------------------------


def _write_self_peer_marker(dir_: Path) -> Path:
    """Write a real self-peer marker spec (the ``agents/self/`` shape)."""
    dir_.mkdir(parents=True, exist_ok=True)
    spec = dir_ / "spec.yaml"
    spec.write_text(
        'listen_url: "http://127.0.0.1:7878"\n'
        'description: "self-registered listen session"\n'
    )
    return spec


def test_is_self_peer_marker_true_for_real_self_peer_spec(tmp_path):
    # Arrange
    spec = _write_self_peer_marker(tmp_path / "self")
    # Act
    result = _is_self_peer_marker(spec)
    # Assert
    assert result is True


def test_is_self_peer_marker_false_for_real_agent_spec(tmp_path):
    # Arrange
    spec = _write_valid_spec(tmp_path / "an-agent")
    # Act
    result = _is_self_peer_marker(spec)
    # Assert
    assert result is False


def test_is_self_peer_marker_false_for_malformed_yaml(tmp_path):
    # Arrange — tolerant: a read/parse failure is NOT a self-peer marker.
    dir_ = tmp_path / "broken"
    dir_.mkdir()
    spec = dir_ / "spec.yaml"
    spec.write_text("{not: valid: yaml: [")
    # Act
    result = _is_self_peer_marker(spec)
    # Assert
    assert result is False


def test_is_self_peer_marker_false_for_missing_file(tmp_path):
    # Arrange — tolerant: an absent file is NOT a self-peer marker.
    spec = tmp_path / "gone" / "spec.yaml"
    # Act
    result = _is_self_peer_marker(spec)
    # Assert
    assert result is False


def test_discover_defined_agents_excludes_self_peer_marker(tmp_path):
    # Arrange — the literal ``agents/self/`` shape sac ships in production.
    home = tmp_path / "home"
    home.mkdir()
    _seed_home_with_agents(home)
    agents = home / ".scitex" / "agent-container" / "agents"
    _write_self_peer_marker(agents / "self")
    # Act
    with _home_set_to(home):
        pairs = _al._discover_defined_agents()
    # Assert
    assert "self" not in [n for n, _ in pairs]


def test_discover_defined_agents_still_finds_sibling_agent_next_to_self(tmp_path):
    # Arrange — the self-marker exclusion must not swallow real siblings.
    home = tmp_path / "home"
    home.mkdir()
    _seed_home_with_agents(home)
    agents = home / ".scitex" / "agent-container" / "agents"
    _write_self_peer_marker(agents / "self")
    # Act
    with _home_set_to(home):
        pairs = _al._discover_defined_agents()
    # Assert
    assert "a1" in [n for n, _ in pairs]


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
# _is_ghost_row — dead-registry-entry filter (operator 2026-06-17:
# `sac agents list` should show only active agents by default).
# ---------------------------------------------------------------------------


def test_local_row_with_file_not_found_is_a_ghost():
    # Arrange — a stale local registry entry whose spec was deleted (e.g. a
    # pytest-temp dir cleaned up after the run).
    row = {
        "host": "local",
        "validation_errors": [
            "File not found: /tmp/pytest-.../agents/archive-target/spec.yaml"
        ],
    }
    # Act
    result = _al._is_ghost_row(row)
    # Assert
    assert result is True


def test_local_row_with_valid_spec_is_not_a_ghost():
    # Arrange — a real defined agent (no validation errors).
    row = {"host": "local", "validation_errors": []}
    # Act
    result = _al._is_ghost_row(row)
    # Assert
    assert result is False


def test_invalid_schema_row_is_not_a_ghost():
    # Arrange — a real on-disk spec that fails schema validation must STAY
    # visible (operator needs to see + fix it); only a MISSING file is a ghost.
    row = {
        "host": "local",
        "validation_errors": ["kind must be one of ['Agent', 'AgentProxy'], got None"],
    }
    # Act
    result = _al._is_ghost_row(row)
    # Assert
    assert result is False


def test_remote_row_is_never_a_ghost_even_if_spec_missing_locally():
    # Arrange — a remote agent's spec lives on its own host; a local
    # "File not found" must NOT hide it (it would erase the fleet view).
    row = {
        "host": "spartan-bm159",
        "validation_errors": ["File not found: /home/.../spec.yaml"],
    }
    # Act
    result = _al._is_ghost_row(row)
    # Assert
    assert result is False


# ---------------------------------------------------------------------------
# print_agent_list — active-only / verbose / --all rendering (operator 2026-06-17)
# ---------------------------------------------------------------------------


def test_print_agent_list_hides_ghost_and_shows_hidden_footer(capsys, tmp_path):
    # Arrange — a real agent + a ghost (registry entry whose spec file is gone).
    good = _write_valid_spec(tmp_path / "good")
    registry = _FakeRegistry(
        [
            {"name": "good", "config": str(good), "screen": "s", "started_at": "ts"},
            {"name": "deadrow", "config": str(tmp_path / "gone" / "spec.yaml")},
        ]
    )
    # Act — default view.
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        print_agent_list(registry)
    # Assert — ghost row hidden + footer shown ("deadrow" avoids collision
    # with the footer's own "stale/ghost" wording).
    out = capsys.readouterr().out
    assert "deadrow" not in out and "hidden" in out


def test_print_agent_list_show_all_includes_ghost(capsys, tmp_path):
    # Arrange
    registry = _FakeRegistry(
        [{"name": "ghost", "config": str(tmp_path / "gone" / "spec.yaml")}]
    )
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        print_agent_list(registry, show_all=True)
    # Assert
    assert "ghost" in capsys.readouterr().out


def test_print_agent_list_verbose_adds_path_column(capsys, tmp_path):
    # Arrange
    good = _write_valid_spec(tmp_path / "good")
    registry = _FakeRegistry(
        [{"name": "good", "config": str(good), "screen": "s", "started_at": "ts"}]
    )
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        print_agent_list(registry, verbose=True)
    # Assert — the Path header appears only in verbose mode.
    assert "Path" in capsys.readouterr().out


def test_print_agent_list_default_omits_path_column(capsys, tmp_path):
    # Arrange
    good = _write_valid_spec(tmp_path / "good")
    registry = _FakeRegistry(
        [{"name": "good", "config": str(good), "screen": "s", "started_at": "ts"}]
    )
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: True):
        print_agent_list(registry)
    # Assert
    assert "Path" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Default view = RUNNING-ONLY (operator TG 1490-1495). The full
# stopped/invalid/definition roster + the per-agent validation-error blocks
# are an unusable wall by default; they move behind -v/--all. The default
# view shows only running agents with their Account, plus a hidden-count
# footer.
# ---------------------------------------------------------------------------


@contextmanager
def _swap_runtime_account(impl: Callable[[str], str | None]) -> Iterator[None]:
    """Swap ``_al._runtime_account_for`` for a real callable (not a mock)."""
    saved = _al._runtime_account_for
    _al._runtime_account_for = impl  # type: ignore[assignment]
    try:
        yield
    finally:
        _al._runtime_account_for = saved  # type: ignore[assignment]


@contextmanager
def _state_root_set_to(path: Path) -> Iterator[None]:
    """Rebind the runner's DEFAULT_STATE_ROOT to a real tmp Path.

    ``resolve_state_dir`` → ``state_dir_for`` reads this module constant at
    call time, so pointing it at a seeded tmp tree lets us exercise the REAL
    ``_runtime_account_for`` resolution on real files (no mock).
    """
    import scitex_agent_container._runners._session_state as _ss

    saved = _ss.DEFAULT_STATE_ROOT
    _ss.DEFAULT_STATE_ROOT = path  # type: ignore[assignment]
    try:
        yield
    finally:
        _ss.DEFAULT_STATE_ROOT = saved  # type: ignore[assignment]


def _running_plus_defined_and_invalid(tmp_path):
    """One RUNNING registry agent + one defined + one invalid on-disk agent."""
    good = _write_valid_spec(tmp_path / "runner")
    registry = _FakeRegistry(
        [{"name": "runner", "config": str(good), "screen": "s", "started_at": "ts"}]
    )
    def_spec = _write_valid_spec(tmp_path / "def1")
    bad_spec = _write_invalid_spec(tmp_path / "bad1")

    def _discover() -> list[tuple[str, Path]]:
        return [("def1", def_spec), ("bad1", bad_spec)]

    return registry, _discover


def test_print_agent_list_default_shows_running_agent(capsys, tmp_path):
    # Arrange
    registry, discover = _running_plus_defined_and_invalid(tmp_path)
    # Act — default view.
    with _swap_discover(discover), _swap_probe(lambda cfg: True):
        print_agent_list(registry)
    # Assert
    assert "runner" in capsys.readouterr().out


def test_print_agent_list_default_hides_definition_and_invalid(capsys, tmp_path):
    # Arrange
    registry, discover = _running_plus_defined_and_invalid(tmp_path)
    # Act
    with _swap_discover(discover), _swap_probe(lambda cfg: True):
        print_agent_list(registry)
    # Assert — the definition ("def1") + invalid ("bad1") rows are hidden.
    out = capsys.readouterr().out
    assert "def1" not in out and "bad1" not in out


def test_print_agent_list_default_footer_counts_hidden(capsys, tmp_path):
    # Arrange
    registry, discover = _running_plus_defined_and_invalid(tmp_path)
    # Act
    with _swap_discover(discover), _swap_probe(lambda cfg: True):
        print_agent_list(registry)
    # Assert — footer names both hidden categories.
    out = capsys.readouterr().out
    assert "definitions" in out and "invalid" in out and "hidden" in out


def test_print_agent_list_default_omits_validation_blocks(capsys, tmp_path):
    # Arrange — the invalid agent's real spec.runtime error block must NOT
    # print in the default view (the wall the operator asked us to remove).
    registry, discover = _running_plus_defined_and_invalid(tmp_path)
    # Act
    with _swap_discover(discover), _swap_probe(lambda cfg: True):
        print_agent_list(registry)
    # Assert
    assert "spec.runtime" not in capsys.readouterr().out


def test_print_agent_list_default_hides_stopped_agent(capsys, tmp_path):
    # Arrange — a single registered-but-stopped agent (probe False).
    spec = _write_valid_spec(tmp_path / "stopper")
    registry = _FakeRegistry([{"name": "stopper", "config": str(spec)}])
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: False):
        print_agent_list(registry)
    # Assert — no table (name absent); footer reports the hidden stopped one.
    out = capsys.readouterr().out
    assert "stopper" not in out and "stopped" in out and "No running agents" in out


def test_print_agent_list_verbose_includes_stopped_agent(capsys, tmp_path):
    # Arrange — same stopped agent; -v restores the full roster.
    spec = _write_valid_spec(tmp_path / "stopper")
    registry = _FakeRegistry([{"name": "stopper", "config": str(spec)}])
    # Act
    with _swap_discover(_no_discover), _swap_probe(lambda cfg: False):
        print_agent_list(registry, verbose=True)
    # Assert
    assert "stopper" in capsys.readouterr().out


def test_print_agent_list_verbose_includes_definition_and_validation(capsys, tmp_path):
    # Arrange
    registry, discover = _running_plus_defined_and_invalid(tmp_path)
    # Act — -v shows every status AND the per-agent validation-error detail.
    with _swap_discover(discover), _swap_probe(lambda cfg: True):
        print_agent_list(registry, verbose=True)
    # Assert
    out = capsys.readouterr().out
    assert "def1" in out and "spec.runtime" in out


# ---------------------------------------------------------------------------
# Account column = ACTUAL runtime account for running agents (operator TG
# 1490-1495). Pool-based agents (``credentials_files`` with no ``account``
# pin) all resolve to the same host-OAuth spec label; the runtime picker
# binds a different pool account per agent, and its identity is host-readable
# from ``<runtime>/home/.claude.json``. A running row prefers that; a
# non-running row (no live auth) keeps the spec label.
# ---------------------------------------------------------------------------


def test_get_data_running_row_prefers_runtime_account(tmp_path):
    # Arrange
    spec = _write_valid_spec(tmp_path / "x")
    registry = _FakeRegistry([{"name": "x", "config": str(spec)}])
    # Act — running (probe True): runtime account wins over the spec label.
    with (
        _swap_discover(_no_discover),
        _swap_probe(lambda cfg: True),
        _swap_account(lambda cfg: "spec-label (host@example.com)"),
        _swap_runtime_account(lambda name: "runtime-pick@example.com"),
    ):
        out = get_agent_list_data(registry)
    # Assert
    assert out[0]["account"] == "runtime-pick@example.com"


def test_get_data_stopped_row_uses_spec_account_not_runtime(tmp_path):
    # Arrange
    spec = _write_valid_spec(tmp_path / "x")
    registry = _FakeRegistry([{"name": "x", "config": str(spec)}])
    # Act — stopped (probe False): the runtime probe is NOT consulted.
    with (
        _swap_discover(_no_discover),
        _swap_probe(lambda cfg: False),
        _swap_account(lambda cfg: "spec-label"),
        _swap_runtime_account(lambda name: "should-not-be-used@example.com"),
    ):
        out = get_agent_list_data(registry)
    # Assert
    assert out[0]["account"] == "spec-label"


def test_get_data_running_row_falls_back_to_spec_when_runtime_unresolved(tmp_path):
    # Arrange — runtime resolver returns None (agent auth not written yet).
    spec = _write_valid_spec(tmp_path / "x")
    registry = _FakeRegistry([{"name": "x", "config": str(spec)}])
    # Act
    with (
        _swap_discover(_no_discover),
        _swap_probe(lambda cfg: True),
        _swap_account(lambda cfg: "spec-fallback"),
        _swap_runtime_account(lambda name: None),
    ):
        out = get_agent_list_data(registry)
    # Assert
    assert out[0]["account"] == "spec-fallback"


def test_runtime_account_for_reads_per_agent_oauth_email(tmp_path):
    # Arrange — a REAL per-agent runtime home with the picked account's
    # identity written into <runtime>/home/.claude.json (no mock).
    import json as _json

    root = tmp_path / "runtime"
    home = root / "myagent" / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text(
        _json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-x",
                    "expiresAt": 9_999_999_999_000,
                }
            }
        )
    )
    (home / ".claude.json").write_text(
        _json.dumps({"oauthAccount": {"emailAddress": "runtime-pick@example.com"}})
    )
    # Act — HOME→tmp so the saved-account match reads an empty store (the
    # email maps to no saved account) → the bare runtime email is returned.
    with _state_root_set_to(root), _home_set_to(tmp_path):
        label = _al._runtime_account_for("myagent")
    # Assert
    assert label == "runtime-pick@example.com"


def test_runtime_account_for_returns_none_without_runtime_dir(tmp_path):
    # Arrange — no runtime dir seeded → resolver must return None so the
    # caller falls back to the spec label.
    # Act
    with _state_root_set_to(tmp_path / "empty"):
        result = _al._runtime_account_for("no-such-runtime-agent")
    # Assert
    assert result is None
