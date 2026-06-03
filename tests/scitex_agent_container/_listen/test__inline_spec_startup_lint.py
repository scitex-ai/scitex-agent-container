"""Tests for :mod:`scitex_agent_container._listen._inline_spec_startup_lint`.

Real :func:`shutil.which` against the test process ``$PATH``; no
mocks. AAA + one assert per test (PA-307). Pinning the wire-shape
contract here so clew + future SAC-from-SAC clients can branch on
it without grepping prose. Per the PR-1 review pattern, the
failure-body keys are:

  * ``kind`` (top-level branch tag) = ``"spec_invalid"`` (re-uses the
    existing enum already in use by apiVersion/kind validation in
    :mod:`_inline_spec`; the per-entry ``reason`` carries the sub-shade)
  * ``details.startup_commands[]`` (array form so multi-entry callers
    see EVERY miss in one round-trip)
  * ``details.startup_commands[].index`` (stable position pointer)
  * ``details.startup_commands[].command`` (verbatim spec entry)
  * ``details.startup_commands[].first_token`` (the bareword we
    evaluated, post env-assignment stripping)
  * ``details.startup_commands[].reason`` (stable enum:
    ``shell_syntax_malformed`` | ``first_token_looks_like_prompt_text``
    | ``first_token_not_on_path``)
  * ``details.startup_commands[].suggestion`` (human prose, present
    for every recognised reason)
"""

from __future__ import annotations

from scitex_agent_container._listen._inline_spec_startup_lint import (
    StartupCommandIssue,
    StartupLintResult,
    preflight_failure_response_body,
    preflight_startup_commands,
)

# ---------------------------------------------------------------------------
# Spec builders
# ---------------------------------------------------------------------------


def _spec_with_startup_commands(cmds: list) -> dict:
    return {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"startup_commands": cmds},
    }


# ---------------------------------------------------------------------------
# preflight_startup_commands — happy path (pass-through)
# ---------------------------------------------------------------------------


def test_preflight_ok_when_no_startup_commands() -> None:
    # Arrange — spec carries no startup_commands at all.
    spec = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {},
    }
    # Act
    result = preflight_startup_commands(spec)
    # Assert
    assert result.ok is True


def test_preflight_ok_when_startup_commands_empty_list() -> None:
    # Arrange
    spec = _spec_with_startup_commands([])
    # Act
    result = preflight_startup_commands(spec)
    # Assert
    assert result.ok is True


def test_preflight_ok_for_real_executable_on_path() -> None:
    # Arrange — ``echo`` is a shell builtin AND is in /usr/bin/echo
    # on every POSIX system; ``ls`` is also a stable PATH executable.
    spec = _spec_with_startup_commands([{"command": "ls -la /tmp"}])
    # Act
    result = preflight_startup_commands(spec)
    # Assert
    assert result.ok is True


def test_preflight_ok_for_shell_builtin_echo() -> None:
    # Arrange — ``echo`` is in the builtins allowlist.
    spec = _spec_with_startup_commands([{"command": "echo hello"}])
    # Act
    result = preflight_startup_commands(spec)
    # Assert
    assert result.ok is True


def test_preflight_ok_for_absolute_path_command() -> None:
    # Arrange — absolute path commands are pass-through (we can't
    # introspect SIF-internal paths from the host).
    spec = _spec_with_startup_commands(
        [{"command": "/opt/scitex/bin/this-does-not-exist-on-host arg"}]
    )
    # Act
    result = preflight_startup_commands(spec)
    # Assert
    assert result.ok is True


def test_preflight_ok_for_relative_path_command() -> None:
    # Arrange
    spec = _spec_with_startup_commands([{"command": "./does-not-exist-relative-x"}])
    # Act
    result = preflight_startup_commands(spec)
    # Assert
    assert result.ok is True


def test_preflight_ok_for_variable_prefixed_command() -> None:
    # Arrange — ``$HOME/bin/x`` is pass-through; we can't expand
    # ``$HOME`` against the container's env from the host.
    spec = _spec_with_startup_commands(
        [{"command": "$HOME/bin/never-exists-on-host-tool"}]
    )
    # Act
    result = preflight_startup_commands(spec)
    # Assert
    assert result.ok is True


def test_preflight_ok_for_leading_env_var_assignments() -> None:
    # Arrange — env-var assignments are dropped; ``echo`` remains.
    spec = _spec_with_startup_commands([{"command": "FOO=bar BAZ=qux echo done"}])
    # Act
    result = preflight_startup_commands(spec)
    # Assert
    assert result.ok is True


def test_preflight_ok_when_command_is_all_env_assignments() -> None:
    # Arrange — degenerate but legal. After stripping there is no
    # bareword to check; we pass-through (style not our problem).
    spec = _spec_with_startup_commands([{"command": "FOO=bar BAZ=qux"}])
    # Act
    result = preflight_startup_commands(spec)
    # Assert
    assert result.ok is True


def test_preflight_ok_for_empty_command_string() -> None:
    # Arrange — empty command is dropped by the parser anyway; not
    # our job to flag.
    spec = _spec_with_startup_commands([{"command": ""}])
    # Act
    result = preflight_startup_commands(spec)
    # Assert
    assert result.ok is True


# ---------------------------------------------------------------------------
# preflight_startup_commands — sad path
# ---------------------------------------------------------------------------


def test_preflight_rejects_prompt_text_with_colon() -> None:
    # Arrange — the clew launcher #70 canonical bug: ``You: ...``
    # prompt content was placed in startup_commands instead of
    # startup_prompts. Colon in first bareword is the smoking gun.
    spec = _spec_with_startup_commands(
        [{"command": "You: please run the experiment with seed=42"}]
    )
    # Act
    result = preflight_startup_commands(spec)
    # Assert
    assert result.issues[0].reason == "first_token_looks_like_prompt_text"


def test_preflight_rejects_unknown_bareword() -> None:
    # Arrange — clearly not a real executable, not a builtin, not
    # absolute/relative, no colon. Pure typo / nonsense.
    spec = _spec_with_startup_commands(
        [{"command": "this-binary-definitely-does-not-exist-on-any-system"}]
    )
    # Act
    result = preflight_startup_commands(spec)
    # Assert
    assert result.issues[0].reason == "first_token_not_on_path"


def test_preflight_rejects_shlex_unbalanced_quote() -> None:
    # Arrange — unbalanced single quote makes shlex.split raise.
    spec = _spec_with_startup_commands([{"command": "echo 'unterminated quote here"}])
    # Act
    result = preflight_startup_commands(spec)
    # Assert
    assert result.issues[0].reason == "shell_syntax_malformed"


def test_preflight_records_index_of_failing_entry() -> None:
    # Arrange — first entry OK, second entry has prompt-text leak,
    # third entry OK. Index should pin the bad one at 1.
    spec = _spec_with_startup_commands(
        [
            {"command": "echo first"},
            {"command": "You: leaked prompt"},
            {"command": "ls /tmp"},
        ]
    )
    # Act
    result = preflight_startup_commands(spec)
    # Assert
    assert result.issues[0].index == 1


def test_preflight_records_verbatim_command_string() -> None:
    # Arrange
    spec = _spec_with_startup_commands([{"command": "You: do thing X"}])
    # Act
    result = preflight_startup_commands(spec)
    # Assert
    assert result.issues[0].command == "You: do thing X"


def test_preflight_records_first_token_after_env_strip() -> None:
    # Arrange — env vars come off the front; the recorded first_token
    # is the bareword that actually failed, not the K=V prefix.
    spec = _spec_with_startup_commands([{"command": "FOO=bar nonexistent-cmd-xyz arg"}])
    # Act
    result = preflight_startup_commands(spec)
    # Assert
    assert result.issues[0].first_token == "nonexistent-cmd-xyz"


def test_preflight_emits_every_miss_in_input_order() -> None:
    # Arrange — two bad entries; both must surface in one round-trip.
    spec = _spec_with_startup_commands(
        [
            {"command": "You: prompt-text-A"},
            {"command": "alsofake-binary-zyx"},
        ]
    )
    # Act
    result = preflight_startup_commands(spec)
    # Assert
    assert len(result.issues) == 2


# ---------------------------------------------------------------------------
# Defensive — bad spec shape collapses to ok=True
# ---------------------------------------------------------------------------


def test_preflight_ok_when_spec_is_not_a_dict() -> None:
    # Arrange — caller passed something weird; collapse to ok=True
    # (downstream validators will reject the shape).
    spec = ["not", "a", "dict"]
    # Act
    result = preflight_startup_commands(spec)  # type: ignore[arg-type]
    # Assert
    assert result.ok is True


def test_preflight_ok_when_spec_body_is_not_a_dict() -> None:
    # Arrange
    spec = {"apiVersion": "scitex-agent-container/v3", "kind": "Agent", "spec": "oops"}
    # Act
    result = preflight_startup_commands(spec)
    # Assert
    assert result.ok is True


def test_preflight_ok_when_startup_commands_is_not_a_list() -> None:
    # Arrange — wrong shape (dict instead of list); skip entirely.
    spec = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "spec": {"startup_commands": {"command": "echo hi"}},
    }
    # Act
    result = preflight_startup_commands(spec)
    # Assert
    assert result.ok is True


def test_preflight_skips_entry_that_is_not_a_dict() -> None:
    # Arrange — one bare-string entry (wrong shape, parser drops it)
    # and one valid dict entry that should still be evaluated.
    spec = _spec_with_startup_commands(["just a string entry", {"command": "echo ok"}])
    # Act
    result = preflight_startup_commands(spec)
    # Assert
    assert result.ok is True


def test_preflight_skips_entry_with_non_string_command() -> None:
    # Arrange
    spec = _spec_with_startup_commands([{"command": 42}])
    # Act
    result = preflight_startup_commands(spec)
    # Assert
    assert result.ok is True


# ---------------------------------------------------------------------------
# Failure response body — wire-shape contract
# ---------------------------------------------------------------------------


def test_response_body_kind_is_spec_invalid() -> None:
    # Arrange
    result = StartupLintResult(
        ok=False,
        issues=(
            StartupCommandIssue(
                index=0,
                command="You: x",
                first_token="You:",
                reason="first_token_looks_like_prompt_text",
            ),
        ),
    )
    # Act
    body = preflight_failure_response_body(result)
    # Assert
    assert body["kind"] == "spec_invalid"


def test_response_body_top_level_error_is_present() -> None:
    # Arrange
    result = StartupLintResult(
        ok=False,
        issues=(
            StartupCommandIssue(
                index=0,
                command="You: x",
                first_token="You:",
                reason="first_token_looks_like_prompt_text",
            ),
        ),
    )
    # Act
    body = preflight_failure_response_body(result)
    # Assert
    assert "startup_commands" in body["error"]


def test_response_body_details_carries_array_form() -> None:
    # Arrange — two failing entries; body must list both.
    result = StartupLintResult(
        ok=False,
        issues=(
            StartupCommandIssue(
                index=0,
                command="You: a",
                first_token="You:",
                reason="first_token_looks_like_prompt_text",
            ),
            StartupCommandIssue(
                index=2,
                command="fake-xyz",
                first_token="fake-xyz",
                reason="first_token_not_on_path",
            ),
        ),
    )
    # Act
    body = preflight_failure_response_body(result)
    # Assert
    assert len(body["details"]["startup_commands"]) == 2


def test_response_body_entry_carries_index() -> None:
    # Arrange
    result = StartupLintResult(
        ok=False,
        issues=(
            StartupCommandIssue(
                index=7,
                command="bad",
                first_token="bad",
                reason="first_token_not_on_path",
            ),
        ),
    )
    # Act
    body = preflight_failure_response_body(result)
    # Assert
    assert body["details"]["startup_commands"][0]["index"] == 7


def test_response_body_entry_carries_reason_enum() -> None:
    # Arrange
    result = StartupLintResult(
        ok=False,
        issues=(
            StartupCommandIssue(
                index=0,
                command="You: x",
                first_token="You:",
                reason="first_token_looks_like_prompt_text",
            ),
        ),
    )
    # Act
    body = preflight_failure_response_body(result)
    # Assert
    assert (
        body["details"]["startup_commands"][0]["reason"]
        == "first_token_looks_like_prompt_text"
    )


def test_response_body_entry_carries_suggestion_for_prompt_text_reason() -> None:
    # Arrange
    result = StartupLintResult(
        ok=False,
        issues=(
            StartupCommandIssue(
                index=0,
                command="You: x",
                first_token="You:",
                reason="first_token_looks_like_prompt_text",
            ),
        ),
    )
    # Act
    body = preflight_failure_response_body(result)
    # Assert
    assert "startup_prompts" in body["details"]["startup_commands"][0]["suggestion"]


def test_response_body_entry_carries_suggestion_for_not_on_path_reason() -> None:
    # Arrange
    result = StartupLintResult(
        ok=False,
        issues=(
            StartupCommandIssue(
                index=0,
                command="myfaketool",
                first_token="myfaketool",
                reason="first_token_not_on_path",
            ),
        ),
    )
    # Act
    body = preflight_failure_response_body(result)
    # Assert
    assert "myfaketool" in body["details"]["startup_commands"][0]["suggestion"]


def test_response_body_entry_carries_suggestion_for_shell_syntax_reason() -> None:
    # Arrange
    result = StartupLintResult(
        ok=False,
        issues=(
            StartupCommandIssue(
                index=0,
                command="echo 'oops",
                first_token="",
                reason="shell_syntax_malformed",
            ),
        ),
    )
    # Act
    body = preflight_failure_response_body(result)
    # Assert
    assert "shlex" in body["details"]["startup_commands"][0]["suggestion"]
