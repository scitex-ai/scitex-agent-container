"""Tests for GitHub-token forwarding into agent containers.

The defect these guard (measured on scitex-compute-04, 2026-08-09): the token
lives in dotfiles secrets that only a LOGIN shell sources, sac starts containers
without one, and nothing forwarded it — so ``gh`` inside reported "not logged
into any GitHub hosts" and no agent could open a pull request.
"""

from __future__ import annotations

import logging

import pytest

from scitex_agent_container.runtimes._apptainer_secret_env import is_secret_env_key
from scitex_agent_container.runtimes._github_token import (
    GITHUB_TOKEN_VARS,
    github_token_env_flags,
    resolve_github_token,
)


class TestResolveGithubToken:
    def test_returns_none_when_pool_is_empty(self):
        # Arrange
        pool: dict[str, str] = {}
        # Act
        got = resolve_github_token(pool)
        # Assert
        assert got is None

    def test_prefers_github_token_over_gh_token(self):
        # Arrange
        pool = {"GITHUB_TOKEN": "aaa", "GH_TOKEN": "bbb"}
        # Act
        got = resolve_github_token(pool)
        # Assert
        assert got == "aaa"

    def test_falls_back_to_gh_token(self):
        # Arrange
        pool = {"GH_TOKEN": "bbb"}
        # Act
        got = resolve_github_token(pool)
        # Assert
        assert got == "bbb"

    @pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
    def test_blank_token_is_absent_not_present(self, blank):
        """A defined-but-EMPTY token is the exact state the containers were in.

        Forwarding it would reproduce the bug while looking like a fix, so it
        must be indistinguishable from absent.
        """
        # Arrange
        pool = {"GITHUB_TOKEN": blank}
        # Act
        got = resolve_github_token(pool)
        # Assert
        assert got is None

    def test_strips_surrounding_whitespace(self):
        # Arrange
        pool = {"GITHUB_TOKEN": "  ghp_x  "}
        # Act
        got = resolve_github_token(pool)
        # Assert
        assert got == "ghp_x"


class TestGithubTokenEnvFlags:
    def test_absent_token_yields_no_flags(self):
        # Arrange
        pool: dict[str, str] = {}
        # Act
        flags = github_token_env_flags(agent_name="a", pool_env=pool)
        # Assert
        assert flags == []

    def test_absent_token_warning_names_the_agent(self, caplog):
        # Arrange
        caplog.set_level(logging.WARNING)
        # Act
        github_token_env_flags(agent_name="scitex-dev", pool_env={})
        # Assert
        assert "scitex-dev" in caplog.text

    def test_absent_token_warning_names_the_consequence(self, caplog):
        """Silence here is what let three agents each lose work time.

        The warning must name what will FAIL, not just the missing variable.
        """
        # Arrange
        caplog.set_level(logging.WARNING)
        # Act
        github_token_env_flags(agent_name="a", pool_env={})
        # Assert
        assert "gh pr create" in caplog.text

    def test_absent_token_warning_names_the_remedy(self, caplog):
        # Arrange
        caplog.set_level(logging.WARNING)
        # Act
        github_token_env_flags(agent_name="a", pool_env={})
        # Assert
        assert "SAC_SECRETS_ENVRC" in caplog.text

    def test_present_token_sets_both_spellings(self):
        # Arrange
        pool = {"GITHUB_TOKEN": "ghp_secret"}
        # Act
        flags = github_token_env_flags(agent_name="a", pool_env=pool)
        # Assert
        assert flags == [
            "--env",
            "GITHUB_TOKEN=ghp_secret",
            "--env",
            "GH_TOKEN=ghp_secret",
        ]

    def test_token_value_is_never_logged(self, caplog):
        """The log may carry the LENGTH; it must never carry the value."""
        # Arrange
        caplog.set_level(logging.DEBUG)
        # Act
        github_token_env_flags(agent_name="a", pool_env={"GITHUB_TOKEN": "ghp_sekrit"})
        # Assert
        assert "ghp_sekrit" not in caplog.text


class TestRedactionCoupling:
    def test_every_forwarded_var_is_recognised_as_secret_shaped(self):
        """THE SECURITY INVARIANT — do not delete this test.

        The token is emitted as ``--env KEY=VALUE``, which lands in a
        world-readable argv (a tmux ``bash -c`` pane cmd; /proc/<pid>/cmdline
        leaks it to any local process). That is acceptable ONLY because
        ``redact_secret_env_to_file`` lifts secret-shaped pairs into a 0600
        env-file before exec. If its predicate ever narrows so these names stop
        matching, the token silently starts leaking with no other symptom.
        """
        # Arrange
        forwarded = list(GITHUB_TOKEN_VARS)
        # Act
        unrecognised = [var for var in forwarded if not is_secret_env_key(var)]
        # Assert
        assert unrecognised == []
