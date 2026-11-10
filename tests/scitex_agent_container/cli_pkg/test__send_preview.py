"""Send previews must not become delivery or readiness claims."""

import json
from functools import partial

import pytest
from click.testing import CliRunner

from scitex_agent_container import agent
from scitex_agent_container.cli_pkg._main import main
from scitex_agent_container.cli_pkg._send import send_to_agent


@pytest.mark.parametrize("wait", [False, True])
@pytest.mark.parametrize(
    ("input_args", "kind"),
    [({"prompt": "owned-sensitive-text"}, "prompt"), ({"key": "owned-key"}, "control")],
)
def test_public_send_preview_hides_contents_before_declared_host_dispatch(
    env_save_restore, input_args, kind, wait
):
    # Arrange
    env_save_restore.set("SAC_LISTEN_BASE_URL", "http://127.0.0.1:1")
    env_save_restore.set("SAC_LISTEN_BEARER", "owned-bearer")

    # Act
    result = agent.send("not-resolved", **input_args, dry_run=True, wait=wait)
    encoded = json.dumps(result)

    # Assert
    assert (
        result["dry_run"],
        result["scope"],
        result["input_kind"],
        result["wait"],
        "status" in result,
        any(value in encoded for value in input_args.values()),
        "owned-bearer" in encoded,
    ) == (True, "arguments_only", kind, wait, False, False, False)


def test_cli_preview_reports_options_without_echoing_prompt(env_save_restore):
    # Arrange
    env_save_restore.set("SAC_LISTEN_BASE_URL", "http://127.0.0.1:1")
    args = [
        "agents",
        "send",
        "not-resolved",
        "owned-sensitive-text",
        "--model",
        "unvalidated-model",
        "--max-turns",
        "3",
        "--dry-run",
    ]

    # Act
    result = CliRunner().invoke(main, args)
    plan = json.loads(result.stdout)

    # Assert
    assert (
        result.exit_code,
        plan["model"],
        plan["max_turns"],
        plan["wait"],
        "owned-sensitive-text" in result.output,
        result.stderr,
    ) == (0, "unvalidated-model", 3, True, False, "")


@pytest.mark.parametrize(
    ("input_args", "reason"),
    [({}, "either prompt or key"), ({"prompt": "p", "key": "k"}, "mutually exclusive")],
)
def test_preview_retains_prompt_key_admission(input_args, reason):
    # Arrange
    kwargs = {**input_args, "dry_run": True}

    # Act
    operation = partial(send_to_agent, "not-resolved", **kwargs)

    # Assert
    with pytest.raises(ValueError, match=reason):
        operation()


def test_cli_preview_still_requires_prompt():
    # Arrange
    args = ["agents", "send", "not-resolved", "--dry-run"]

    # Act
    result = CliRunner().invoke(main, args)

    # Assert
    assert (result.exit_code, "PROMPT is required" in result.stderr) == (2, True)
