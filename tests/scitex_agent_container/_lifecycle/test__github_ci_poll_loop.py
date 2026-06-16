"""Tests for the GitHub-CI poll loop (sac #404).

Exercises the asyncio task the listen lifespan launches — without a real
``gh`` / network / state.db — by injecting the ``ready_check``,
``repos_source``, ``list_prs``, ``conclusion_for`` and ``deliver`` seams.
Mirrors test__periodic_drive_loop.py's create-task → sleep → cancel
pattern.

STX-TQ002 AAA-markers + STX-TQ007 one-assert. No mocks (DI seams).
"""

from __future__ import annotations

import asyncio

import pytest

from scitex_agent_container._lifecycle._github_ci_poll_loop import (
    github_ci_poll_loop,
)


@pytest.mark.asyncio
async def test_loop_disabled_when_gh_not_ready_delivers_nothing():
    # Arrange — fail-loud preflight: gh not authenticated → loop returns.
    calls: list = []
    # Act — loop returns immediately (no infinite loop), so await directly.
    await github_ci_poll_loop(
        ready_check=lambda: False,
        repos_source=lambda: ["o/r"],
        list_prs=lambda repo: [{"number": 1, "head_sha": "s", "body": ""}],
        conclusion_for=lambda repo, pr: "success",
        deliver=lambda *a, **k: calls.append(a),
    )
    # Assert
    assert calls == []


@pytest.mark.asyncio
async def test_loop_disabled_via_env_var_delivers_nothing():
    # Arrange — explicit env save/restore (no monkeypatch, PA-306).
    import os as _os

    calls: list = []
    key = "SAC_GITHUB_CI_POLLER_DISABLED"
    saved = _os.environ.get(key)
    _os.environ[key] = "1"
    # Act
    try:
        await github_ci_poll_loop(
            ready_check=lambda: True,
            repos_source=lambda: ["o/r"],
            list_prs=lambda repo: [{"number": 1, "head_sha": "s", "body": ""}],
            conclusion_for=lambda repo, pr: "success",
            deliver=lambda *a, **k: calls.append(a),
        )
    finally:
        if saved is None:
            _os.environ.pop(key, None)
        else:
            _os.environ[key] = saved
    # Assert
    assert calls == []


@pytest.mark.asyncio
async def test_loop_delivers_open_pr_verdict_in_one_tick():
    # Arrange
    calls: list = []
    task = asyncio.create_task(
        github_ci_poll_loop(
            poll_interval_s=0.05,
            ready_check=lambda: True,
            repos_source=lambda: ["o/r"],
            list_prs=lambda repo: [{"number": 7, "head_sha": "abc", "body": ""}],
            conclusion_for=lambda repo, pr: "success",
            deliver=lambda repo, pr, head_sha, conclusion, **k: calls.append(
                (repo, pr, head_sha, conclusion)
            ),
        )
    )
    # Act — let one tick run, then cancel.
    await asyncio.sleep(0.12)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # Assert
    assert ("o/r", 7, "abc", "success") in calls


@pytest.mark.asyncio
async def test_loop_survives_a_tick_exception_then_cancels_cleanly():
    # Arrange — a tick that raises must NOT kill the loop (logged + retried).
    def boom(repo):
        raise RuntimeError("transient gh blip")

    task = asyncio.create_task(
        github_ci_poll_loop(
            poll_interval_s=0.05,
            ready_check=lambda: True,
            repos_source=lambda: ["o/r"],
            list_prs=boom,
            conclusion_for=lambda repo, pr: "success",
            deliver=lambda *a, **k: None,
        )
    )
    # Act — let the failing tick run, then cancel.
    await asyncio.sleep(0.12)
    task.cancel()
    # Assert — the loop swallowed the tick error and stayed alive, so
    # cancellation (not the RuntimeError) is what surfaces.
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_loop_honours_cancellation_cleanly():
    # Arrange
    task = asyncio.create_task(
        github_ci_poll_loop(
            poll_interval_s=0.05,
            ready_check=lambda: True,
            repos_source=lambda: [],
            list_prs=lambda repo: [],
            conclusion_for=lambda repo, pr: "none",
            deliver=lambda *a, **k: None,
        )
    )
    # Act
    await asyncio.sleep(0.06)
    task.cancel()
    # Assert — the finally must re-raise CancelledError, not swallow it.
    with pytest.raises(asyncio.CancelledError):
        await task
