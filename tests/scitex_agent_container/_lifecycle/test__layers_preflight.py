"""Tests for the ``to_home_layers`` launch gate and its de-duplication.

Two facts are pinned here, and they are separate:

1. **The gate.** An undeclared spec refuses to start once enforcement is on,
   and the NAMED override (``--allow-undeclared-layers`` /
   ``SAC_ALLOW_UNDECLARED_LAYERS``) starts it anyway while saying so at ERROR.
2. **The duplication.** The finding used to be logged inside
   ``settings_layer_dirs`` — a pure resolver that ONE start calls TWICE
   (workspace home, then the apptainer overlay upper), which is why the
   operator's paste showed the same paragraph twice for one agent. The
   resolver must now log nothing, and ``agent_start`` must report it once.

PA-306 no-mocks: real spec objects, a real spec.yaml on disk, a real
hand-rolled runtime/handover surface. STX-TQ002/TQ007: AAA markers, one fact
per test.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Iterator

import pytest

from scitex_agent_container._lifecycle._layers_preflight import (
    ALLOW_ENV,
    ENFORCE_BY_DEFAULT,
    ENFORCE_ENV,
    check_to_home_layers_at_launch,
)
from scitex_agent_container._lifecycle._start import agent_start
from scitex_agent_container._state.registry import Registry
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes._to_home_errors import UndeclaredToHomeLayers
from scitex_agent_container.runtimes._to_home_resolve import settings_layer_dirs
from tests.scitex_agent_container._helpers.explicit_spec import explicitize_yaml

_RESOLVER_LOGGER = "scitex_agent_container.runtimes._to_home_resolve"
_GATE_LOGGER = "scitex_agent_container._lifecycle._layers_preflight"


class _Spec:
    """The four attributes the gate and the resolver read off a config.

    A real object with real attributes, not a stand-in for a collaborator:
    both functions take a config and read exactly these.
    """

    def __init__(self, to_home_layers, *, name="agent-under-test", to_home=""):
        self.name = name
        self.to_home_layers = to_home_layers
        self.to_home = to_home
        self.config_path = "/nonexistent/agent-under-test/spec.yaml"


def _refusal_message(spec) -> str:
    """The text of the refusal the gate raises for ``spec``.

    Captured with try/except rather than ``pytest.raises`` so each caller keeps
    exactly ONE assertion (STX-TQ007 counts a ``raises`` block as an assert).
    Returns ``""`` when nothing was raised, which fails the content assertions
    honestly instead of erroring somewhere unrelated.
    """
    try:
        check_to_home_layers_at_launch(spec, enforce=True)
    except UndeclaredToHomeLayers as exc:
        return str(exc)
    return ""


# ---------------------------------------------------------------------------
# the gate itself
# ---------------------------------------------------------------------------


class TestDeclaredSpecPasses:
    """A spec that states its layers is exactly what the gate wants."""

    def test_declared_spec_returns_true(self):
        # Arrange
        spec = _Spec(["user-shared"])
        # Act
        allowed = check_to_home_layers_at_launch(spec, enforce=True)
        # Assert
        assert allowed is True

    def test_declared_spec_logs_nothing(self, caplog):
        # Arrange
        spec = _Spec(["user-shared"])
        # Act
        with caplog.at_level(logging.WARNING, logger=_GATE_LOGGER):
            check_to_home_layers_at_launch(spec, enforce=True)
        # Assert
        assert caplog.records == []

    def test_empty_declaration_is_a_declaration(self):
        # Arrange — an explicit [] pins the agent to its own spec; not absent.
        spec = _Spec([])
        # Act
        allowed = check_to_home_layers_at_launch(spec, enforce=True)
        # Assert
        assert allowed is True


class TestUndeclaredSpecRefuses:
    """Undeclared + enforcing = a start-time REFUSAL, not a warning.

    The operator's reason, verbatim in intent: nobody fixes warnings. This one
    had been printing for months.
    """

    def test_undeclared_spec_raises_when_enforcing(self):
        # Arrange
        spec = _Spec(None)
        # Act
        # Assert
        with pytest.raises(UndeclaredToHomeLayers):
            check_to_home_layers_at_launch(spec, enforce=True)

    def test_refusal_names_the_agent(self):
        # Arrange
        message = _refusal_message(_Spec(None, name="grant"))
        # Act
        # Assert
        assert "grant" in message

    def test_refusal_names_the_fix_command(self):
        # Arrange
        message = _refusal_message(_Spec(None))
        # Act
        # Assert
        assert "sac agents migrate-layers --apply" in message

    def test_refusal_names_the_override(self):
        # Arrange — a refusal with no stated way past it is a dead end.
        message = _refusal_message(_Spec(None))
        # Act
        # Assert
        assert "--allow-undeclared-layers" in message


class TestNamedOverride:
    """``--allow-undeclared-layers`` starts the agent — loudly."""

    def test_override_allows_the_start(self):
        # Arrange
        spec = _Spec(None)
        # Act
        allowed = check_to_home_layers_at_launch(
            spec, enforce=True, allow_undeclared=True
        )
        # Assert
        assert allowed is True

    def test_override_is_logged_at_error(self, caplog):
        # Arrange — a silent override is a slower version of the ignored warning.
        spec = _Spec(None)
        # Act
        with caplog.at_level(logging.ERROR, logger=_GATE_LOGGER):
            check_to_home_layers_at_launch(spec, enforce=True, allow_undeclared=True)
        # Assert
        assert any("BYPASSED" in rec.getMessage() for rec in caplog.records)

    def test_override_log_names_the_agent(self, caplog):
        # Arrange
        spec = _Spec(None, name="grant")
        # Act
        with caplog.at_level(logging.ERROR, logger=_GATE_LOGGER):
            check_to_home_layers_at_launch(spec, enforce=True, allow_undeclared=True)
        # Assert
        assert any("grant" in rec.getMessage() for rec in caplog.records)

    def test_env_override_is_honoured(self, env_save_restore):
        # Arrange
        env_save_restore.set(ALLOW_ENV, "1")
        spec = _Spec(None)
        # Act
        allowed = check_to_home_layers_at_launch(spec, enforce=True)
        # Assert
        assert allowed is True

    def test_explicit_arg_beats_the_env(self, env_save_restore):
        # Arrange — an explicit False must not be overridden by a stale export.
        env_save_restore.set(ALLOW_ENV, "1")
        spec = _Spec(None)
        # Act
        # Assert
        with pytest.raises(UndeclaredToHomeLayers):
            check_to_home_layers_at_launch(spec, enforce=True, allow_undeclared=False)


class TestEnforcementSequencing:
    """The refusal is REAL but not yet the default: 101 of the fleet's 102
    specs are undeclared, so flipping it before the dotfiles migration lands
    would stop every agent booting. This pins the sequencing, not a preference.
    """

    def test_enforcement_is_still_opt_in(self):
        # Arrange
        # Act
        # Assert — flip this (and the constant) only after the specs migrate.
        assert ENFORCE_BY_DEFAULT is False

    def test_undeclared_spec_starts_while_not_enforcing(self):
        # Arrange
        spec = _Spec(None)
        # Act
        allowed = check_to_home_layers_at_launch(spec, enforce=False)
        # Assert
        assert allowed is True

    def test_not_enforcing_still_warns(self, caplog):
        # Arrange — visible as a migration to-do, just not a refusal yet.
        spec = _Spec(None)
        # Act
        with caplog.at_level(logging.WARNING, logger=_GATE_LOGGER):
            check_to_home_layers_at_launch(spec, enforce=False)
        # Assert
        assert any("to_home_layers" in rec.getMessage() for rec in caplog.records)

    def test_env_switch_turns_enforcement_on(self, env_save_restore):
        # Arrange
        env_save_restore.set(ENFORCE_ENV, "1")
        spec = _Spec(None)
        # Act
        # Assert
        with pytest.raises(UndeclaredToHomeLayers):
            check_to_home_layers_at_launch(spec)


# ---------------------------------------------------------------------------
# the duplication: the resolver must be silent, the start must report once
# ---------------------------------------------------------------------------


class TestResolverIsSilent:
    """``settings_layer_dirs`` is a PURE resolver. It logged the finding, and a
    single start calls it twice — once for the workspace home and once for the
    apptainer overlay upper — which is why one agent produced two identical
    paragraphs. It must now say nothing at all."""

    def test_resolver_logs_nothing_when_undeclared(self, caplog):
        # Arrange
        spec = _Spec(None)
        # Act
        with caplog.at_level(logging.DEBUG, logger=_RESOLVER_LOGGER):
            settings_layer_dirs(spec)
        # Assert
        assert [r for r in caplog.records if "to_home_layers" in r.getMessage()] == []

    def test_two_resolutions_stay_silent(self, caplog):
        # Arrange — the exact shape of the duplication: resolve, then resolve.
        spec = _Spec(None)
        # Act
        with caplog.at_level(logging.DEBUG, logger=_RESOLVER_LOGGER):
            settings_layer_dirs(spec)
            settings_layer_dirs(spec)
        # Assert
        assert [r for r in caplog.records if "to_home_layers" in r.getMessage()] == []

    def test_resolver_still_returns_the_implicit_cascade(self):
        # Arrange — silence must not change WHAT an undeclared spec inherits.
        spec = _Spec(None)
        # Act
        names = [name for name, _ in settings_layer_dirs(spec)]
        # Assert
        assert names == ["user-shared", "project-shared", "per-agent"]


class _FakeRuntime:
    """Real runtime surface; records whether start() was reached."""

    def __init__(self) -> None:
        self.started: list[AgentConfig] = []

    def is_running(self, config: AgentConfig) -> bool:
        return False

    def start(self, config: AgentConfig, **kwargs: Any) -> bool:
        self.started.append(config)
        return True

    def stop(self, config: AgentConfig) -> None:  # pragma: no cover - unused
        pass


class _FakeHandover:
    """Real handover surface; no-op for the module callables."""

    def ensure_instance_uuid(self, config: AgentConfig) -> str:
        return "uuid"

    def hydrate_from_hub(self, config: AgentConfig) -> bool:
        return True

    def start_failback_poller(self, config: AgentConfig) -> None:
        pass


@pytest.fixture
def isolated_home(tmp_path: Path) -> Iterator[Path]:
    """HOME + SCITEX_DIR inside tmp_path so nothing touches the real fleet."""
    home = tmp_path / "home"
    home.mkdir()
    previous = {k: os.environ.get(k) for k in ("HOME", "SCITEX_DIR")}
    os.environ["HOME"] = str(home)
    os.environ["SCITEX_DIR"] = str(home / ".scitex")
    try:
        yield home
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _write_undeclared_spec(tmp_path: Path) -> Path:
    """A real, loadable spec.yaml that declares no ``to_home_layers``.

    Deliberately NOT inside a git repo: the sibling drift gate then reports
    NOT_A_REPO (drift unknown, never a refusal), so this test measures the
    layers gate alone.
    """
    agent_dir = tmp_path / "agents" / "alpha"
    agent_dir.mkdir(parents=True)
    spec = agent_dir / "spec.yaml"
    spec.write_text(
        explicitize_yaml(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n"
            "  runtime: apptainer\n"
            "  host: ${HOSTNAME}\n"
            f"  workdir: {tmp_path / 'work'}\n"
            "  apptainer:\n    image: /x.sif\n    binds: []\n"
            "  restart:\n    policy: on-failure\n    max_retries: 3\n"
            "  claude:\n"
            "    model: sonnet\n"
            "  health:\n"
            "    enabled: false\n"
            "    interval: 60\n"
        )
    )
    return spec


class TestReportedOncePerStart:
    """The whole point of moving the check out of the resolver: ONE start, ONE
    report. Before the move this same start produced two."""

    def test_start_reports_undeclared_layers_exactly_once(
        self, pg_schema, tmp_path, isolated_home, caplog
    ):
        # Arrange
        spec = _write_undeclared_spec(tmp_path)
        runtime = _FakeRuntime()
        # Act
        with caplog.at_level(logging.WARNING, logger=_GATE_LOGGER):
            agent_start(
                str(spec),
                registry=Registry(registry_dir=tmp_path / "reg"),
                runtime_factory=lambda _c: runtime,
                handover_mod=_FakeHandover(),
                sleep_fn=lambda _s: None,
            )
        # Assert
        hits = [r for r in caplog.records if "to_home_layers" in r.getMessage()]
        assert len(hits) == 1

    def test_start_still_reaches_the_runtime_while_not_enforcing(
        self, pg_schema, tmp_path, isolated_home
    ):
        # Arrange
        spec = _write_undeclared_spec(tmp_path)
        runtime = _FakeRuntime()
        # Act
        agent_start(
            str(spec),
            registry=Registry(registry_dir=tmp_path / "reg"),
            runtime_factory=lambda _c: runtime,
            handover_mod=_FakeHandover(),
            sleep_fn=lambda _s: None,
        )
        # Assert
        assert len(runtime.started) == 1

    def test_start_refuses_an_undeclared_spec_when_enforcing(
        self, tmp_path, isolated_home, env_save_restore
    ):
        # Arrange
        env_save_restore.set(ENFORCE_ENV, "1")
        spec = _write_undeclared_spec(tmp_path)
        runtime = _FakeRuntime()
        # Act
        try:
            agent_start(
                str(spec),
                registry=Registry(registry_dir=tmp_path / "reg"),
                runtime_factory=lambda _c: runtime,
                handover_mod=_FakeHandover(),
                sleep_fn=lambda _s: None,
            )
        except UndeclaredToHomeLayers:
            pass
        # Assert — refused BEFORE the runtime was touched.
        assert runtime.started == []
