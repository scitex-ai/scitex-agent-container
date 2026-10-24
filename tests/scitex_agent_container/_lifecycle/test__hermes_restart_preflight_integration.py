"""The real restart path hands one usable proof across its stop/start boundary."""

from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
import yaml

from scitex_agent_container._lifecycle import _hermes_restart_preflight as gate
from scitex_agent_container._lifecycle import lifecycle
from scitex_agent_container._lifecycle._restart_preflight import RestartPreflightAbort
from scitex_agent_container._state.registry import Registry
from scitex_agent_container.config._hermes_config import compile_hermes_config
from scitex_agent_container.runtimes import _hermes_profile as profile
from scitex_agent_container.runtimes._apptainer_provider import ProviderEnvError
from scitex_agent_container.runtimes._hermes_tui_rpc import HermesTurnActivity
from tests.scitex_agent_container._helpers.explicit_spec import explicit_doc
from tests.scitex_agent_container._helpers.spec_authority import (
    establish_test_spec_authority,
)
from tests.scitex_agent_container._lifecycle import (
    test__restart_preflight_integration as restart_test,
)
from tests.scitex_agent_container._lifecycle.test__worktree_policy import (
    FIXTURE,
    _authority,
)

MODEL = "muse-spark-1.3-contributor"
_FakeHandover = restart_test._FakeHandover
_FakeRuntime = restart_test._FakeRuntime


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path):
    previous = {}
    for name, value in (
        ("HOME", str(tmp_path)),
        ("SAC_ENGINES_FILE", str(tmp_path / "no-fleet-engines.yaml")),
    ):
        previous[name] = os.environ.get(name)
        os.environ[name] = value
    removed = {}
    for name in (
        "SAC_TEST_PRIMARY_MISSING",
        "SAC_TEST_HEALTHY_SIBLING",
        "SAC_TEST_FALLBACK",
    ):
        removed[name] = os.environ.pop(name, None)
    gate.reset_probe_cache()
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        for name, value in removed.items():
            if value is not None:
                os.environ[name] = value
        gate.reset_probe_cache()


def spec_file(tmp_path):
    directory = tmp_path / "alpha"
    directory.mkdir()
    path = directory / "spec.yaml"
    doc = explicit_doc(
        {
            "host": "${HOSTNAME}",
            "runtime": "tui",
            "harness": "hermes",
            "workdir": str(tmp_path / "work"),
            "health": {"enabled": False},
            "engine": "muse",
            "comms": {"channels": []},
            "apptainer": {"image": "/existing/base.sif", "binds": []},
            "available_engines": {
                "muse": {
                    "model": MODEL,
                    "provider": {
                        "base_url": "https://opencode.ai/zen/go/v1/responses",
                        "auth_token_env": "SAC_TEST_PRIMARY_MISSING",
                    },
                },
                "backup": {
                    "model": "meta/" + MODEL,
                    "provider": {
                        "base_url": "https://api.commandcode.ai/provider/v1",
                        "auth_token_env": "SAC_TEST_FALLBACK",
                    },
                },
            },
            "available_harnesses": {
                "hermes": {
                    "session": {"mode": "continue", "max_age_minutes": None},
                    "max_turns": 99999,
                    "goals": {"max_turns": 99999, "judge_engine": "muse"},
                    "failover": {
                        "accounts": {
                            "muse": [
                                "SAC_TEST_PRIMARY_MISSING",
                                "SAC_TEST_HEALTHY_SIBLING",
                            ],
                            "backup": ["SAC_TEST_FALLBACK"],
                        },
                        "engines": ["backup"],
                        "strategy": "fill_first",
                    },
                }
            },
        }
    )
    for legacy in ("engines", "claude", "watchdog", "container"):
        doc["spec"].pop(legacy, None)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    return establish_test_spec_authority(path)


@contextmanager
def configured_probe(*, tokens):
    calls = []
    real_prepare = gate.prepare_hermes_successor

    def resolver(config):
        slot = config.claude.provider.auth_token_env
        if slot not in tokens:
            raise ProviderEnvError("Synthetic unresolved slot " + slot)
        return tokens[slot]

    def native(spec, token):
        calls.append((spec["provider"], token))
        return (
            {"last_status": "dead", "last_status_at": 1000, "last_error_code": 401}
            if token == gate._CONTROL_TOKEN
            else {"last_status": "ok", "last_status_at": 1000}
        )

    def prepare(config, **kwargs):
        return real_prepare(
            config,
            probe=native,
            resolver=resolver,
            pool_reader=lambda: SimpleNamespace(env={}),
            **kwargs,
        )

    previous = gate.prepare_hermes_successor
    gate.prepare_hermes_successor = prepare
    try:
        yield calls
    finally:
        gate.prepare_hermes_successor = previous


def test_real_restart_all_missing_refuses_without_declared_credential(tmp_path):
    # Arrange
    path = spec_file(tmp_path)
    registry = Registry(registry_dir=tmp_path / "registry")
    registry.add("alpha", str(path), "tui-alpha")
    runtime = _FakeRuntime(running=True)
    # Act
    # Assert
    with configured_probe(tokens={}):
        with pytest.raises(RestartPreflightAbort, match="No declared Hermes credential"):
            lifecycle.agent_restart(
                "alpha", registry=registry, runtime_factory=lambda _c: runtime
            )


def test_real_restart_all_missing_keeps_live_process_and_session(tmp_path):
    # Arrange
    path = spec_file(tmp_path)
    registry = Registry(registry_dir=tmp_path / "registry")
    registry.add("alpha", str(path), "tui-alpha")
    runtime = _FakeRuntime(running=True)
    # Act
    with configured_probe(tokens={}) as calls:
        try:
            lifecycle.agent_restart(
                "alpha", registry=registry, runtime_factory=lambda _c: runtime
            )
        except RestartPreflightAbort:
            refused = True
        else:
            refused = False
    # Assert
    assert (refused, runtime.running, runtime.stop_calls, runtime.start_calls, calls) == (
        True,
        True,
        [],
        [],
        [],
    )


def test_real_restart_resumes_existing_image_and_carries_exact_sibling_proof(pg_schema, tmp_path):
    # Arrange
    path = spec_file(tmp_path)
    # Act
    registry = Registry(registry_dir=tmp_path / "registry")
    registry.add("alpha", str(path), "tui-alpha")
    observed = []
    class Successor(_FakeRuntime):
        def start(self, config, **kwargs):
            plan, selection, _, _ = profile._verified_route(
                config, tmp_path / "unused-home", launch_mode="tui"
            )
            rendered = compile_hermes_config(
                plan,
                workdir=config.workdir,
                max_turns=config.hermes_max_turns,
                goals=config.hermes_goals,
            )
            profile._apply_verified_route(rendered, selection)
            observed.append(
                (
                    config.claude.session,
                    config.apptainer.image,
                    rendered["auxiliary"]["goal_judge"],
                    rendered["agent"]["max_turns"],
                    rendered["providers"]["sac-muse"]["key_env"],
                )
            )
            return super().start(config, **kwargs)
    runtime = Successor(running=True)
    with configured_probe(
        tokens={"SAC_TEST_HEALTHY_SIBLING": "synthetic-working"}
    ) as calls:
        result = lifecycle.agent_restart(
            "alpha",
            registry=registry,
            runtime_factory=lambda _c: runtime,
            sleep_fn=lambda _seconds: None,
            handover_mod=_FakeHandover(),
            managed_turn_probe=lambda _c: HermesTurnActivity(
                "idle", "idle", "existing-session"
            ),
        )
    # Assert
    assert (
        result,
        len(runtime.stop_calls),
        len(runtime.start_calls),
        len(calls),
        observed,
    ) == (
        True,
        1,
        1,
        2,
        [
            (
                "continue",
                "/existing/base.sif",
                {"provider": "main", "model": MODEL},
                99999,
                "",
            )
        ],
    )



def test_real_spec_proof_crosses_engine_gate_and_reload_without_install_or_reprobe(tmp_path):
    # Arrange
    # Act
    from scitex_agent_container._lifecycle._engine_select import (
        check_engine_before_stop,
    )
    from scitex_agent_container._lifecycle._restart_preflight import (
        preflight_from_config_path,
    )
    from scitex_agent_container.config import load_config
    path = spec_file(tmp_path)
    with configured_probe(
        tokens={"SAC_TEST_HEALTHY_SIBLING": "synthetic-working"}
    ) as calls:
        proof = preflight_from_config_path(str(path))
        check_engine_before_stop(str(path))
        successor = load_config(path)
        gate.attach_prepared_route(successor, proof)
        plan, selection, _, _ = profile._verified_route(
            successor, tmp_path / "unused-home", launch_mode="tui"
        )
    rendered = compile_hermes_config(
        plan,
        workdir=successor.workdir,
        max_turns=successor.hermes_max_turns,
        goals=successor.hermes_goals,
    )
    profile._apply_verified_route(rendered, selection)
    # Assert
    assert (
        len(calls),
        successor.claude.session,
        successor.apptainer.image,
        rendered["auxiliary"]["goal_judge"],
        rendered["agent"]["max_turns"],
    ) == (
        2,
        "continue",
        "/existing/base.sif",
        {
            "provider": "custom:sac-muse",
            "model": "muse-spark-1.3-contributor",
            "reasoning_effort": "xhigh",
            "timeout": 60,
            "max_tokens": 4096,
        },
        99999,
    )



def _run_refusal(tmp_path, registry, expected):
    """Act the restart; assert the refusal happens before any stop."""
    from scitex_agent_container._lifecycle import _restart_preflight, _stop
    from scitex_agent_container._lifecycle._worktree_policy import WorktreePolicyError

    previous_preflight = _restart_preflight.preflight_workspace_from_config_path
    real_workspace_preflight = previous_preflight
    _restart_preflight.preflight_workspace_from_config_path = (
        lambda config_path, **kwargs: real_workspace_preflight(
            config_path, cli_path=FIXTURE, **kwargs
        )
    )
    previous_stop = _stop.agent_stop
    stops = []
    _stop.agent_stop = lambda *args, **kwargs: stops.append(args)
    try:
        with configured_probe(
            tokens={"SAC_TEST_HEALTHY_SIBLING": "synthetic-working"}
        ):
            with pytest.raises(WorktreePolicyError, match=expected):
                lifecycle.agent_restart("alpha", registry=registry)
    finally:
        _restart_preflight.preflight_workspace_from_config_path = previous_preflight
        _stop.agent_stop = previous_stop
    return stops


def test_production_restart_dirty_authority_refusal_happens_before_stop(tmp_path):
    # Arrange
    root = _authority(tmp_path / "work")
    path = spec_file(tmp_path)
    (root / "tracked.txt").write_text("preserve this edit\n")
    registry = Registry(registry_dir=tmp_path / "registry")
    registry.add("alpha", str(path), "tui-alpha")
    # Act
    stops = _run_refusal(tmp_path, registry, "authority checkout .* is dirty")
    # Assert
    assert stops == []


def test_production_restart_unowned_worktree_refusal_happens_before_stop(tmp_path):
    # Arrange
    from scitex_agent_container._lifecycle._worktree_policy import (
        plan_task_worktree,
    )
    from scitex_agent_container.config import load_config

    root = _authority(tmp_path / "work")
    path = spec_file(tmp_path)
    (root / ".gitignore").write_text(".worktrees/\n")
    subprocess.run(["git", "-C", str(root), "add", ".gitignore"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=tests@scitex.invalid",
            "commit",
            "-qm",
            "ignore linked worktrees",
        ],
        check=True,
    )
    FIXTURE.chmod(FIXTURE.stat().st_mode | 0o111)
    plan = plan_task_worktree(load_config(path), provision=False, cli_path=FIXTURE)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "worktree",
            "add",
            "-q",
            "-b",
            plan.branch,
            plan.resolved_workdir,
        ],
        check=True,
    )
    registry = Registry(registry_dir=tmp_path / "registry")
    registry.add("alpha", str(path), "tui-alpha")
    # Act
    stops = _run_refusal(tmp_path, registry, "exists without SAC ownership")
    # Assert
    assert stops == []
