"""Daemon-mode runners spawned by sac runtimes.

Each module under this package is the entry point for a long-lived
agent process. The matching ``runtimes/<name>.py`` adapter is
responsible for spawning + supervising the runner; the runner itself
only knows how to do its single job (heartbeat, stream messages,
handle signals).

Phase 1 (2026-05-03): only ``claude_session`` is wired here.
"""
