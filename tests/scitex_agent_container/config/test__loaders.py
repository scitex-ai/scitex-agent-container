"""Tests for scitex_agent_container.config._loaders.

Covers the helpers (``_resolve_venv``, ``_resolve_python_venv``,
``_parse_env_files``, ``compose_effective_name``) plus the v2/v3
dispatch and dict-shape rejection paths invoked through the public
``load_config`` API.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from scitex_agent_container.config import load_config
from scitex_agent_container.config._loaders import (
    _parse_env_files,
    _resolve_python_venv,
    _resolve_venv,
    compose_effective_name,
)
from scitex_agent_container.config._types import HostsSpec


@pytest.fixture(autouse=True)
def _home_redirect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


# ---------------------------------------------------------------------------
# _resolve_venv (legacy "auto" probe)
# ---------------------------------------------------------------------------


def test_resolve_venv_returns_input_when_not_auto() -> None:
    assert _resolve_venv("/explicit/path") == "/explicit/path"
    assert _resolve_venv("") == ""


def test_resolve_venv_non_string_returns_input() -> None:
    assert _resolve_venv(None) is None  # type: ignore[arg-type]


def test_resolve_venv_auto_picks_first_existing(
    _home_redirect: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    venv = _home_redirect / ".venv-3.11"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "activate").write_text("")
    out = _resolve_venv("auto")
    assert out == "~/.venv-3.11"


def test_resolve_venv_auto_no_match_returns_empty(_home_redirect: Path) -> None:
    assert _resolve_venv("auto") == ""


def test_resolve_venv_case_insensitive(_home_redirect: Path) -> None:
    venv = _home_redirect / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "activate").write_text("")
    assert _resolve_venv("AUTO") == "~/.venv"


# ---------------------------------------------------------------------------
# _resolve_python_venv (string / list / error)
# ---------------------------------------------------------------------------


def test_resolve_python_venv_empty_returns_empty() -> None:
    assert _resolve_python_venv(None) == ""
    assert _resolve_python_venv("") == ""
    assert _resolve_python_venv([]) == ""


def test_resolve_python_venv_relative_returns_verbatim() -> None:
    assert _resolve_python_venv(".venv") == ".venv"


def test_resolve_python_venv_absolute_existing(_home_redirect: Path) -> None:
    venv = _home_redirect / "myenv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "activate").write_text("")
    assert _resolve_python_venv(str(venv)) == str(venv)


def test_resolve_python_venv_absolute_missing_raises() -> None:
    with pytest.raises(RuntimeError, match="bin/activate"):
        _resolve_python_venv("/nonexistent/venv")


def test_resolve_python_venv_list_first_match_wins(_home_redirect: Path) -> None:
    good = _home_redirect / "g"
    (good / "bin").mkdir(parents=True)
    (good / "bin" / "activate").write_text("")
    out = _resolve_python_venv([str(_home_redirect / "miss"), str(good)])
    assert out == str(good)


def test_resolve_python_venv_list_relative_short_circuits() -> None:
    out = _resolve_python_venv(["./first", "/absolute/second"])
    assert out == "./first"


def test_resolve_python_venv_list_no_match_raises(_home_redirect: Path) -> None:
    with pytest.raises(RuntimeError, match="chain"):
        _resolve_python_venv([str(_home_redirect / "x"), str(_home_redirect / "y")])


def test_resolve_python_venv_list_with_non_string_raises() -> None:
    with pytest.raises(RuntimeError, match="strings"):
        _resolve_python_venv(["ok", 42])  # type: ignore[list-item]


def test_resolve_python_venv_invalid_type_raises() -> None:
    with pytest.raises(RuntimeError, match="string or list"):
        _resolve_python_venv(42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _parse_env_files
# ---------------------------------------------------------------------------


def test_parse_env_files_empty() -> None:
    assert _parse_env_files({}) == []
    assert _parse_env_files({"env-file": ""}) == []


def test_parse_env_files_str() -> None:
    assert _parse_env_files({"env-file": "/a/b.env"}) == ["/a/b.env"]


def test_parse_env_files_list() -> None:
    assert _parse_env_files({"env-file": ["a.env", "b.env"]}) == ["a.env", "b.env"]


def test_parse_env_files_list_with_non_string_raises() -> None:
    with pytest.raises(RuntimeError, match="strings"):
        _parse_env_files({"env-file": ["a", 2]})


def test_parse_env_files_invalid_type_raises() -> None:
    with pytest.raises(RuntimeError, match="string or list"):
        _parse_env_files({"env-file": {"a": "b"}})


# ---------------------------------------------------------------------------
# compose_effective_name (v3 hosts-aware variant — the second def shadows the first)
# ---------------------------------------------------------------------------


def test_compose_effective_name_no_hosts_returns_raw() -> None:
    assert compose_effective_name("head", None, "ywata-note-win") == "head"


def test_compose_effective_name_singleton_hosts_returns_raw() -> None:
    hs = HostsSpec(host="ywata-note-win", hosts="")
    assert compose_effective_name("head", hs, "ywata-note-win") == "head"


def test_compose_effective_name_multi_hosts_suffixes() -> None:
    hs = HostsSpec(host="", hosts=["mba", "spartan"])
    assert compose_effective_name("worker", hs, "mba") == "worker-mba"


def test_compose_effective_name_idempotent_when_already_suffixed() -> None:
    hs = HostsSpec(host="", hosts=["mba"])
    assert compose_effective_name("worker-mba", hs, "mba") == "worker-mba"


def test_compose_effective_name_raw_equals_hostname() -> None:
    hs = HostsSpec(host="", hosts=["mba"])
    assert compose_effective_name("mba", hs, "mba") == "mba"


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
    p = _v2_yaml(tmp_path)
    with pytest.raises(ValueError):
        load_config(p)


def test_load_config_rejects_non_dict_top_level(tmp_path: Path) -> None:
    p = tmp_path / "agent" / "spec.yaml"
    p.parent.mkdir()
    p.write_text("- one\n- two\n")  # list at top
    with pytest.raises(ValueError):
        load_config(p)


# ---------------------------------------------------------------------------
# load_v3 path through load_config (smoke)
# ---------------------------------------------------------------------------


def test_load_config_v3_minimal(tmp_path: Path) -> None:
    p = tmp_path / "myname" / "myname.yaml"
    p.parent.mkdir()
    body = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {
            "runtime": "apptainer",
            "apptainer": {"image": "x.sif"},
        },
    }
    p.write_text(yaml.safe_dump(body))
    cfg = load_config(p)
    assert cfg.name == "myname"
    assert cfg.image == "x.sif"
    assert cfg.env["CLAUDE_AGENT_ID"] == "myname"


def test_load_config_v3_multi_host_appends_hostname(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "scitex_agent_container.config._loaders.resolve_hostname",
        lambda: "mba",
    )
    p = tmp_path / "worker" / "worker.yaml"
    p.parent.mkdir()
    body = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {
            "runtime": "apptainer",
            "apptainer": {"image": "x.sif"},
            "hosts": ["mba", "spartan"],
        },
    }
    p.write_text(yaml.safe_dump(body))
    cfg = load_config(p)
    assert cfg.name == "worker-mba"
