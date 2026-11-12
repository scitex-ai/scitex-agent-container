"""Recovery flag forwarding without real observations or agent actions."""

import importlib
import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli import main


@pytest.fixture(
    params=[
        (
            "restart-login-expired",
            "_agents_restart_login_expired",
            "auth_heal_pass",
            "_authheal",
            {},
        ),
        (
            "resume-rate-limited",
            "_agents_resume_rate_limited",
            "resume_pass",
            "_ratelimit",
            {"switch_model": False},
        ),
    ]
)
def recorded_pass(request):
    action, module_name, engine, backend, extras = request.param
    module = importlib.import_module(f"scitex_agent_container.cli_pkg.{module_name}")
    outcome = importlib.import_module(
        f"scitex_agent_container.{backend}._pass"
    ).PassOutcome
    original = getattr(module, engine)
    calls = []

    def execute(**kwargs):
        calls.append(kwargs)
        return outcome(heartbeat_ok=True, applied=kwargs["apply"])

    setattr(module, engine, execute)
    try:
        yield action, calls, extras
    finally:
        setattr(module, engine, original)


@pytest.mark.parametrize(
    "flags, applied",
    [
        ([], False),
        (["--check"], False),
        (["--apply"], True),
        (["--yes"], True),
        (["-y"], True),
    ],
)
def test_existing_check_and_approval_aliases_forward_original_options(
    recorded_pass, flags, applied
):
    # Arrange
    action, calls, extras = recorded_pass
    args = ["agents", action, *flags, "--limit", "3", "--interval", "0.25", "--json"]
    # Act
    result = CliRunner().invoke(main, args)
    # Assert
    assert (result.exit_code, json.loads(result.stdout)["mode"], calls) == (
        0,
        "apply" if applied else "check",
        [{"apply": applied, "limit": 3, "interval": 0.25, **extras}],
    )


@pytest.mark.parametrize("approval", ["--apply", "--yes", "-y"])
@pytest.mark.parametrize("inspection", ["--check", "--dry-run"])
def test_contradictory_recovery_flags_refuse_before_pass(
    recorded_pass, approval, inspection
):
    # Arrange
    action, calls, _extras = recorded_pass
    # Act
    result = CliRunner().invoke(main, ["agents", action, approval, inspection])
    # Assert
    assert (result.exit_code, calls) == (2, [])


@pytest.mark.parametrize("prefix, suffix", [(["--json"], []), ([], ["--json"])])
def test_preview_never_observes_or_records_a_pass(recorded_pass, prefix, suffix):
    # Arrange
    action, calls, extras = recorded_pass
    args = [
        *prefix,
        "agents",
        action,
        "--dry-run",
        "--check",
        "--limit",
        "3",
        "--interval",
        "0.25",
        *suffix,
    ]
    # Act
    result = CliRunner().invoke(main, args)
    # Assert
    assert (result.exit_code, json.loads(result.stdout), calls) == (
        0,
        {
            "action": action,
            "dry_run": True,
            "scope": "arguments_only",
            "verified": False,
            "limit": 3,
            "interval": 0.25,
            **extras,
        },
        [],
    )


def test_model_switch_preview_and_approval_remain_explicit():
    # Arrange
    args = ["agents", "resume-rate-limited", "--dry-run", "--switch-model", "--json"]
    # Act
    result = CliRunner().invoke(main, args)
    # Assert
    assert (result.exit_code, json.loads(result.stdout)["switch_model"]) == (0, True)
