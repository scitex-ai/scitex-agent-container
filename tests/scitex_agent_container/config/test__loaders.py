"""Tests for scitex_agent_container.config._loaders.

Covers the helpers (``_resolve_venv``, ``_resolve_python_venv``,
``_parse_env_files``, ``compose_effective_name``) plus the v2/v3
dispatch and dict-shape rejection paths invoked through the public
``load_config`` API.

TQ cleanup: every test carries AAA markers (TQ002) and exactly one
assertion (TQ007). Same-shape invariants over a small set of inputs
collapse into ``pytest.parametrize``. Test names spell out the
behaviour being verified (TQ003-compatible).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scitex_agent_container.config import load_config
from scitex_agent_container.config._loaders import (
    DEFAULT_STARTUP_PROMPT,
    _parse_env_files,
    _resolve_python_venv,
    _resolve_venv,
    compose_effective_name,
)
from scitex_agent_container.config._types import HostsSpec


@pytest.fixture(autouse=True)
def _home_redirect(tmp_path: Path):
    """PA-306: explicit env save/restore — Path.home() reads $HOME."""
    import os

    home = tmp_path / "home"
    home.mkdir()
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        yield home
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


# ---------------------------------------------------------------------------
# _resolve_venv (legacy "auto" probe)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["/explicit/path", ""],
)
def test_resolve_venv_returns_input_when_not_auto(value: str) -> None:
    # Arrange
    incoming = value
    # Act
    out = _resolve_venv(incoming)
    # Assert
    assert out == incoming


def test_resolve_venv_non_string_returns_input() -> None:
    # Arrange
    incoming = None
    # Act
    out = _resolve_venv(incoming)  # type: ignore[arg-type]
    # Assert
    assert out is None


def test_resolve_venv_auto_picks_first_existing(_home_redirect: Path) -> None:
    # Arrange
    venv = _home_redirect / ".venv-3.11"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "activate").write_text("")
    # Act
    out = _resolve_venv("auto")
    # Assert
    assert out == "~/.venv-3.11"


def test_resolve_venv_auto_no_match_returns_empty(_home_redirect: Path) -> None:
    # Arrange — no venv directories created.
    _ = _home_redirect
    # Act
    out = _resolve_venv("auto")
    # Assert
    assert out == ""


def test_resolve_venv_case_insensitive(_home_redirect: Path) -> None:
    # Arrange
    venv = _home_redirect / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "activate").write_text("")
    # Act
    out = _resolve_venv("AUTO")
    # Assert
    assert out == "~/.venv"


# ---------------------------------------------------------------------------
# _resolve_python_venv (string / list / error)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [None, "", []],
    ids=["none", "empty-string", "empty-list"],
)
def test_resolve_python_venv_empty_returns_empty(value) -> None:
    # Arrange
    incoming = value
    # Act
    out = _resolve_python_venv(incoming)
    # Assert
    assert out == ""


def test_resolve_python_venv_relative_returns_verbatim() -> None:
    # Arrange
    incoming = ".venv"
    # Act
    out = _resolve_python_venv(incoming)
    # Assert
    assert out == ".venv"


def test_resolve_python_venv_absolute_existing(_home_redirect: Path) -> None:
    # Arrange
    venv = _home_redirect / "myenv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "activate").write_text("")
    # Act
    out = _resolve_python_venv(str(venv))
    # Assert
    assert out == str(venv)


def test_resolve_python_venv_absolute_missing_raises() -> None:
    # Arrange
    missing = "/nonexistent/venv"
    # Act
    ctx = pytest.raises(RuntimeError, match="bin/activate")
    # Assert
    with ctx:
        _resolve_python_venv(missing)


def test_resolve_python_venv_list_first_match_wins(_home_redirect: Path) -> None:
    # Arrange
    good = _home_redirect / "g"
    (good / "bin").mkdir(parents=True)
    (good / "bin" / "activate").write_text("")
    chain = [str(_home_redirect / "miss"), str(good)]
    # Act
    out = _resolve_python_venv(chain)
    # Assert
    assert out == str(good)


def test_resolve_python_venv_list_relative_short_circuits() -> None:
    # Arrange
    chain = ["./first", "/absolute/second"]
    # Act
    out = _resolve_python_venv(chain)
    # Assert
    assert out == "./first"


def test_resolve_python_venv_list_no_match_raises(_home_redirect: Path) -> None:
    # Arrange
    chain = [str(_home_redirect / "x"), str(_home_redirect / "y")]
    # Act
    ctx = pytest.raises(RuntimeError, match="chain")
    # Assert
    with ctx:
        _resolve_python_venv(chain)


def test_resolve_python_venv_list_with_non_string_raises() -> None:
    # Arrange
    chain = ["ok", 42]
    # Act
    ctx = pytest.raises(RuntimeError, match="strings")
    # Assert
    with ctx:
        _resolve_python_venv(chain)  # type: ignore[arg-type]


def test_resolve_python_venv_invalid_type_raises() -> None:
    # Arrange
    bad = 42
    # Act
    ctx = pytest.raises(RuntimeError, match="string or list")
    # Assert
    with ctx:
        _resolve_python_venv(bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _parse_env_files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [{}, {"env-file": ""}],
    ids=["missing-key", "empty-string"],
)
def test_parse_env_files_empty_inputs_return_empty_list(spec: dict) -> None:
    # Arrange
    incoming = spec
    # Act
    out = _parse_env_files(incoming)
    # Assert
    assert out == []


def test_parse_env_files_str() -> None:
    # Arrange
    spec = {"env-file": "/a/b.env"}
    # Act
    out = _parse_env_files(spec)
    # Assert
    assert out == ["/a/b.env"]


def test_parse_env_files_list() -> None:
    # Arrange
    spec = {"env-file": ["a.env", "b.env"]}
    # Act
    out = _parse_env_files(spec)
    # Assert
    assert out == ["a.env", "b.env"]


def test_parse_env_files_list_with_non_string_raises() -> None:
    # Arrange
    spec = {"env-file": ["a", 2]}
    # Act
    ctx = pytest.raises(RuntimeError, match="strings")
    # Assert
    with ctx:
        _parse_env_files(spec)


def test_parse_env_files_invalid_type_raises() -> None:
    # Arrange
    spec = {"env-file": {"a": "b"}}
    # Act
    ctx = pytest.raises(RuntimeError, match="string or list")
    # Assert
    with ctx:
        _parse_env_files(spec)


# ---------------------------------------------------------------------------
# compose_effective_name (v3 hosts-aware variant — the second def shadows the first)
# ---------------------------------------------------------------------------


def test_compose_effective_name_no_hosts_returns_raw() -> None:
    # Arrange
    raw, hosts, hostname = "head", None, "ywata-note-win"
    # Act
    out = compose_effective_name(raw, hosts, hostname)
    # Assert
    assert out == "head"


def test_compose_effective_name_singleton_hosts_returns_raw() -> None:
    # Arrange
    hs = HostsSpec(host="ywata-note-win", hosts="")
    # Act
    out = compose_effective_name("head", hs, "ywata-note-win")
    # Assert
    assert out == "head"


def test_compose_effective_name_multi_hosts_suffixes() -> None:
    # Arrange
    hs = HostsSpec(host="", hosts=["mba", "spartan"])
    # Act
    out = compose_effective_name("worker", hs, "mba")
    # Assert
    assert out == "worker-mba"


def test_compose_effective_name_idempotent_when_already_suffixed() -> None:
    # Arrange
    hs = HostsSpec(host="", hosts=["mba"])
    # Act
    out = compose_effective_name("worker-mba", hs, "mba")
    # Assert
    assert out == "worker-mba"


def test_compose_effective_name_raw_equals_hostname() -> None:
    # Arrange
    hs = HostsSpec(host="", hosts=["mba"])
    # Act
    out = compose_effective_name("mba", hs, "mba")
    # Assert
    assert out == "mba"


# ---------------------------------------------------------------------------
# Public load_config — v2 / non-v3 rejected
# ---------------------------------------------------------------------------


def _v2_yaml(
    tmp_path: Path, name: str = "alpha", spec_extra: dict | None = None
) -> Path:
    spec = {"runtime": "apptainer", "image": "x.sif"}
    if spec_extra:
        spec.update(spec_extra)
    body = {
        "apiVersion": "scitex-agent-container/v2",
        "kind": "Agent",
        "metadata": {"name": name, "labels": {"role": "head"}},
        "spec": spec,
    }
    p = tmp_path / name / "spec.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(body))
    return p


def test_load_config_rejects_v2(tmp_path: Path) -> None:
    # Arrange
    p = _v2_yaml(tmp_path)
    # Act
    ctx = pytest.raises(ValueError)
    # Assert
    with ctx:
        load_config(p)


def test_load_config_rejects_non_dict_top_level(tmp_path: Path) -> None:
    # Arrange
    p = tmp_path / "agent" / "spec.yaml"
    p.parent.mkdir()
    p.write_text("- one\n- two\n")  # list at top
    # Act
    ctx = pytest.raises(ValueError)
    # Assert
    with ctx:
        load_config(p)


# ---------------------------------------------------------------------------
# load_v3 path through load_config (smoke)
# ---------------------------------------------------------------------------


@pytest.fixture
def _v3_minimal_cfg(tmp_path: Path):
    """Loaded v3 minimal config — shared setup for the single-assert siblings."""
    p = tmp_path / "myname" / "myname.yaml"
    p.parent.mkdir()
    body = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {
            "runtime": "apptainer",
            "host": "local",
            "workdir": "/home/agent/work",
            "apptainer": {"image": "x.sif", "binds": []},
            "claude": {"model": "sonnet"},
            "health": {"enabled": True, "interval": 60},
            "restart": {"policy": "on-failure", "max_retries": 3},
        },
    }
    p.write_text(yaml.safe_dump(body))
    return load_config(p)


def test_load_config_v3_minimal_sets_name_from_directory(_v3_minimal_cfg) -> None:
    # Arrange
    cfg = _v3_minimal_cfg
    # Act
    name = cfg.name
    # Assert
    assert name == "myname"


def test_load_config_v3_minimal_propagates_apptainer_image(_v3_minimal_cfg) -> None:
    # Arrange
    cfg = _v3_minimal_cfg
    # Act
    image = cfg.image
    # Assert
    assert image == "x.sif"


def test_load_config_v3_minimal_injects_claude_agent_id_env(_v3_minimal_cfg) -> None:
    # Arrange
    cfg = _v3_minimal_cfg
    # Act
    agent_id = cfg.env["CLAUDE_AGENT_ID"]
    # Assert
    assert agent_id == "myname"


def test_load_config_v3_multi_host_appends_hostname(tmp_path: Path) -> None:
    # Arrange — PA-306: hand-rolled fake injection with save/restore.
    from scitex_agent_container.config import _loaders as _loaders_mod

    _saved_resolve = _loaders_mod.resolve_hostname
    _loaders_mod.resolve_hostname = lambda: "mba"
    p = tmp_path / "worker" / "worker.yaml"
    p.parent.mkdir()
    body = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {
            "runtime": "apptainer",
            "apptainer": {"image": "x.sif", "binds": []},
            "claude": {"model": "sonnet"},
            "health": {"enabled": True, "interval": 60},
            "restart": {"policy": "on-failure", "max_retries": 3},
            "hosts": ["mba", "spartan"],
        },
    }
    p.write_text(yaml.safe_dump(body))
    # Act
    try:
        cfg = load_config(p)
    finally:
        _loaders_mod.resolve_hostname = _saved_resolve
    # Assert
    assert cfg.name == "worker-mba"


# ---------------------------------------------------------------------------
# spec.claude.account soft-WARN (missing snapshot warns, never fails)
# ---------------------------------------------------------------------------


def _v3_yaml(tmp_path: Path, name: str, spec_extra: dict) -> Path:
    spec = {
        "runtime": "apptainer",
        "host": "local",
        "workdir": str(tmp_path / "wd"),
        "apptainer": {"image": "x.sif", "binds": []},
        "health": {"enabled": True, "interval": 60},
        "restart": {"policy": "on-failure", "max_retries": 3},
    }
    spec.update(spec_extra)
    # Ensure required claude.model survives an extra that overrides claude.
    spec.setdefault("claude", {}).setdefault("model", "sonnet")
    body = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "metadata": {"labels": {"role": "head"}},
        "spec": spec,
    }
    p = tmp_path / name / "spec.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(body))
    return p


def test_load_config_warns_when_pinned_account_snapshot_absent(tmp_path: Path):
    # Arrange — pin an account with no saved snapshot anywhere.
    p = _v3_yaml(tmp_path, "pinned", {"claude": {"account": "ghost"}})
    # Act
    ctx = pytest.warns(UserWarning, match="ghost")
    # Assert
    with ctx:
        load_config(p)


def test_load_config_pinned_account_still_loads_despite_missing_snapshot(
    tmp_path: Path,
):
    # Arrange — the soft-WARN must NOT escalate to a load failure. The
    # warning itself is asserted by the sibling test; here we suppress
    # it (catch_warnings, not pytest.warns) so the single assertion is
    # purely "the config loaded with the pin intact".
    import warnings

    p = _v3_yaml(tmp_path, "pinned", {"claude": {"account": "ghost"}})
    # Act
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cfg = load_config(p)
    # Assert
    assert cfg.claude.account == "ghost"


def test_load_config_warns_when_startup_prompt_is_long(tmp_path: Path):
    # Arrange — a long role/rules PROSE startup_prompt (belongs in CLAUDE.md +
    # skills, not a per-boot turn).
    p = _v3_yaml(
        tmp_path, "verbose", {"startup_prompts": ["You are X. " + "rule. " * 120]}
    )
    # Act
    ctx = pytest.warns(UserWarning, match="startup_prompts")
    # Assert
    with ctx:
        load_config(p)


def test_load_config_no_warn_for_short_startup_kick(tmp_path: Path):
    # Arrange — a short boot-KICK must NOT trip the long-prompt warning.
    import warnings

    p = _v3_yaml(
        tmp_path,
        "kick",
        {"startup_prompts": ["You restarted — check inbox + todo; report readiness."]},
    )
    # Act
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        load_config(p)
    # Assert
    assert not any("startup_prompts" in str(w.message) for w in rec)


def test_load_config_defaults_startup_prompt_when_omitted(tmp_path: Path):
    # Arrange — a spec with NO startup_prompts inherits the generic sac default.
    p = _v3_yaml(tmp_path, "nodefault", {})
    # Act
    cfg = load_config(p)
    # Assert
    assert cfg.startup_prompts == [DEFAULT_STARTUP_PROMPT]


def test_load_config_keeps_explicit_startup_prompt_over_default(tmp_path: Path):
    # Arrange — an explicit startup_prompts must NOT be replaced by the default.
    p = _v3_yaml(tmp_path, "explicit", {"startup_prompts": ["my own kick"]})
    # Act
    cfg = load_config(p)
    # Assert
    assert cfg.startup_prompts == ["my own kick"]


# ---------------------------------------------------------------------------
# #16 — CLAUDE_AGENT_ACCOUNT auto-env from spec.claude.account
#
# The injected env is consumed by:
#   * the in-container claude-code-telegrammer bridge (PR-A reads it to
#     enrich the outbound signature with the account+quota);
#   * `sac account quota` (agent self-awareness CLI);
#   * the a2a metadata enricher (peer back-pressure).
# All three converge on this single env, so the auto-injection is the
# linchpin — if it regresses the whole #16 surface goes dark.
# ---------------------------------------------------------------------------


def test_load_config_injects_claude_agent_account_when_spec_pins_account(
    tmp_path: Path,
):
    # Arrange
    import warnings

    p = _v3_yaml(tmp_path, "pinned", {"claude": {"account": "wyusuuke-gmail-com"}})
    # Act
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cfg = load_config(p)
    # Assert
    assert cfg.env["CLAUDE_AGENT_ACCOUNT"] == "wyusuuke-gmail-com"


def test_load_config_omits_claude_agent_account_when_unpinned(tmp_path: Path):
    # Arrange — no claude.account → no env (empty string would falsely
    # advertise an account; an absent key signals "host-shared OAuth").
    p = _v3_yaml(tmp_path, "unpinned", {})
    # Act
    cfg = load_config(p)
    # Assert
    assert "CLAUDE_AGENT_ACCOUNT" not in cfg.env


def test_load_config_strips_whitespace_from_account_before_injection(
    tmp_path: Path,
):
    # Arrange — defensive trim so a spec with a stray newline / space
    # does not leak into the quota-cache lookup (which uses a strict
    # ``split('-')[0] == short`` match — a leading space would break it).
    import warnings

    p = _v3_yaml(
        tmp_path, "trimmed", {"claude": {"account": "  ywata1989-gmail-com  "}}
    )
    # Act
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        cfg = load_config(p)
    # Assert
    assert cfg.env["CLAUDE_AGENT_ACCOUNT"] == "ywata1989-gmail-com"


# ---------------------------------------------------------------------------
# Phase-3 ACL (ADR-0010) — spec.comms / spec.lineage reach AgentConfig
# ---------------------------------------------------------------------------


def test_load_config_a2a_listen_false_disables_a2a_port(tmp_path: Path) -> None:
    """Gap-3 end-to-end: ``spec.comms.a2a.listen: false`` yields an
    AgentConfig whose ``a2a.port`` is None (sidecar disabled, identical
    to legacy ``spec.a2a.port: null``)."""
    # Arrange
    p = _v3_yaml(tmp_path, "cap-a", {"comms": {"a2a": {"listen": False}}})
    # Act
    cfg = load_config(p)
    # Assert
    assert cfg.a2a.port is None


def test_load_config_default_a2a_port_preserved_when_listen_absent(
    tmp_path: Path,
) -> None:
    """Default-preservation: with no ``spec.comms.a2a`` block the
    ``a2a.port`` keeps its legacy 'auto' default."""
    # Arrange
    p = _v3_yaml(tmp_path, "cap-b", {})
    # Act
    cfg = load_config(p)
    # Assert
    assert cfg.a2a.port == "auto"


def test_load_config_lineage_may_spawn_false_round_trips(tmp_path: Path) -> None:
    """Gap-5 end-to-end: ``spec.lineage.may_spawn: false`` reaches the
    loaded AgentConfig so core agent_start can persist it."""
    # Arrange
    p = _v3_yaml(tmp_path, "cap-c", {"lineage": {"may_spawn": False}})
    # Act
    cfg = load_config(p)
    # Assert
    assert cfg.lineage.may_spawn is False


# ---------------------------------------------------------------------------
# Builtin sac control plane (mcp + channel) — operator directive 2026-06-16
# ---------------------------------------------------------------------------


def test_load_config_injects_sac_channel_by_default(tmp_path: Path) -> None:
    # Arrange
    p = _v3_yaml(tmp_path, "sac-chan", {})
    # Act
    cfg = load_config(p)
    # Assert
    assert "server:sac" in cfg.claude.channels


def test_load_config_injects_sac_mcp_server_by_default(tmp_path: Path) -> None:
    # Arrange
    p = _v3_yaml(tmp_path, "sac-mcp", {})
    # Act
    cfg = load_config(p)
    # Assert
    assert "scitex-agent-container" in cfg.mcp_servers


def test_load_config_sac_builtin_optout_label_skips_channel(tmp_path: Path) -> None:
    # Arrange
    body = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "metadata": {"labels": {"sac-builtin": "off"}},
        "spec": {
            "runtime": "apptainer",
            "host": "local",
            "workdir": str(tmp_path / "wd"),
            "apptainer": {"image": "x.sif", "binds": []},
            "claude": {"model": "sonnet"},
            "health": {"enabled": True, "interval": 60},
            "restart": {"policy": "on-failure", "max_retries": 3},
        },
    }
    p = tmp_path / "sac-off" / "spec.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(body))
    # Act
    cfg = load_config(p)
    # Assert
    assert "server:sac" not in cfg.claude.channels


# ---------------------------------------------------------------------------
# spec.access — REMOVED 2026-06-23 (SSoT: explicit apptainer.binds + workdir).
# A spec carrying `access:` no longer loads; it raises loud at validation.
# ---------------------------------------------------------------------------


def test_load_config_with_access_field_raises(tmp_path: Path) -> None:
    # Arrange — a spec still carrying the removed `access:` knob.
    p = _v3_yaml(tmp_path, "acc-legacy", {"access": "full"})
    # Act
    ctx = pytest.raises(ValueError, match="spec.access has been REMOVED")
    # Assert
    with ctx:
        load_config(p)


def test_load_config_without_access_field_loads(tmp_path: Path) -> None:
    # Arrange — no `access` field (the only valid state now).
    p = _v3_yaml(tmp_path, "acc-absent", {})
    # Act
    cfg = load_config(p)
    # Assert — loads cleanly; host access is whatever apptainer.binds declares.
    assert cfg.name == "acc-absent"


# ---------------------------------------------------------------------------
# spec.provider — agent SDK family selector (openai-compat-1 foundation).
# TOP-LEVEL field, sibling of spec.runtime; distinct from the pre-existing
# spec.claude.provider (vendor backend override — see the naming-collision
# note in config._provider_types.AgentProvider).
# ---------------------------------------------------------------------------


def test_load_config_provider_defaults_to_anthropic_when_omitted(
    tmp_path: Path,
) -> None:
    # Arrange — no-op guarantee: every existing spec omits spec.provider.
    p = _v3_yaml(tmp_path, "provider-default", {})
    # Act
    cfg = load_config(p)
    # Assert
    assert cfg.provider == "anthropic"


def test_load_config_provider_threads_through_when_declared_openai(
    tmp_path: Path,
) -> None:
    # Arrange
    p = _v3_yaml(tmp_path, "provider-openai", {"provider": "openai"})
    # Act
    cfg = load_config(p)
    # Assert
    assert cfg.provider == "openai"


def test_load_config_provider_threads_through_when_declared_anthropic(
    tmp_path: Path,
) -> None:
    # Arrange — explicit "anthropic" (spelled out, not relying on default).
    p = _v3_yaml(tmp_path, "provider-explicit-anthropic", {"provider": "anthropic"})
    # Act
    cfg = load_config(p)
    # Assert
    assert cfg.provider == "anthropic"
