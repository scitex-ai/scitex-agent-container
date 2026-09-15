"""An explicit engine request is never swallowed by an already-running no-op."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

from scitex_agent_container._lifecycle import _start_engine_noop as engine_noop
from scitex_agent_container._lifecycle import _start_prelaunch as start_prelaunch
from scitex_agent_container._lifecycle import lifecycle as lc
from scitex_agent_container._lifecycle._start_engine_noop import ExplicitEngineNoopError
from scitex_agent_container._lifecycle._start_outcome import (
    KIND_ALREADY_RUNNING,
    outcome_kind,
)
from scitex_agent_container._state.registry import Registry
from scitex_agent_container.cli_pkg._health_engine import EngineScan
from tests.scitex_agent_container._helpers.explicit_spec import explicit_spec

_TOKEN_ENV = "SAC_TEST_START_ENGINE_TOKEN"


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch) -> Iterator[None]:
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    os.environ[_TOKEN_ENV] = "test-token"
    monkeypatch.setattr(start_prelaunch, "persist_acl_policy", lambda _config: None)
    monkeypatch.setattr(start_prelaunch, "resolve_a2a_port", lambda _config: None)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved
        os.environ.pop(_TOKEN_ENV, None)


class _Runtime:
    def __init__(self) -> None:
        self.start_calls = []
        self.stop_calls = []

    def is_running(self, _config: Any) -> bool:
        return True

    def start(self, config: Any, **kwargs: Any) -> bool:
        self.start_calls.append((config, kwargs))
        return True

    def stop(self, config: Any) -> bool:
        self.stop_calls.append(config)
        return True

    def logs(self, _config: Any, lines: int = 50) -> str:
        return ""


class _Handover:
    def ensure_instance_uuid(self, _config: Any) -> None:
        return None

    def hydrate_from_hub(self, _config: Any) -> None:
        return None

    def start_failback_poller(self, _config: Any) -> None:
        return None


def _write_spec(tmp_path: Path) -> Path:
    spec = explicit_spec(
        {
            "host": "${HOSTNAME}",
            "runtime": "apptainer",
            "workdir": str(tmp_path / "work"),
            "apptainer": {"image": "/x.sif", "binds": []},
            "health": {"enabled": False, "interval": 60},
            "engines": {
                "claude": {
                    "harness": "anthropic",
                    "model": "fable[1m]",
                    "default": True,
                },
                "codex": {
                    "harness": "hermes",
                    "model": "gpt-5.6-sol",
                    "provider": {
                        "base_url": "http://127.0.0.1:18765/v1/responses",
                        "auth_token_env": _TOKEN_ENV,
                    },
                },
            },
        }
    )
    agent_dir = tmp_path / "hub"
    agent_dir.mkdir()
    path = agent_dir / "spec.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "scitex-agent-container/v3",
                "kind": "Agent",
                "spec": spec,
            },
            sort_keys=False,
        )
    )
    return path


def _start_live(spec: Path, registry: Registry, runtime: _Runtime):
    registry.add("hub", str(spec), "tui-hub")
    return lc.agent_start(
        str(spec),
        registry=registry,
        runtime_factory=lambda _config: runtime,
        handover_mod=_Handover(),
        session_override="continue",
        engine_override="codex",
        liveness_verifier=lambda _config, _runtime: True,
    )


def test_continue_with_explicit_engine_refuses_noop_on_different_live_engine(
    monkeypatch, tmp_path: Path
) -> None:
    registry = Registry(registry_dir=tmp_path / "registry")
    runtime = _Runtime()
    monkeypatch.setattr(
        engine_noop,
        "_read_running_engine",
        lambda _name: ("qwen38-27b", EngineScan(pids_matched=1), None),
    )

    with pytest.raises(ExplicitEngineNoopError, match="--force --continue"):
        _start_live(_write_spec(tmp_path), registry, runtime)

    assert runtime.start_calls == []
    assert runtime.stop_calls == []


def test_continue_with_explicit_engine_allows_noop_when_live_engine_matches(
    monkeypatch, tmp_path: Path
) -> None:
    registry = Registry(registry_dir=tmp_path / "registry")
    runtime = _Runtime()
    monkeypatch.setattr(
        engine_noop,
        "_read_running_engine",
        lambda _name: ("codex", EngineScan(pids_matched=1), None),
    )

    result = _start_live(_write_spec(tmp_path), registry, runtime)

    assert outcome_kind(result) == KIND_ALREADY_RUNNING
    assert runtime.start_calls == []
