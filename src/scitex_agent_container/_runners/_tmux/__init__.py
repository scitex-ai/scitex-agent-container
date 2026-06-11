#!/usr/bin/env python3
"""Tmux-driver runtime subpackage.

Salvaged on 2026-06-12 from ba6755e^ (commit before the SDK-only
purge in #111). This package drives an interactive ``claude`` TUI
through tmux send-keys / capture-pane, instead of via the SDK or
``claude -p``. It exists to keep the SAC fleet on subscription-flat
economics after Anthropic's 2026-06-15 SDK split.

Modules:
- ``tmux``: thin wrapper around the tmux CLI.
- ``multiplexer``: multiplexer-agnostic session abstraction.
- ``pane_capture``: capture-pane helpers.
- ``prompts``: 10 auto-accept marker detectors for the claude TUI.
- ``claude_code``: orchestrator that drives interactive ``claude``.
- ``auto.accept`` / ``auto.daemon``: auto-accept loop.

This is a pure relocation from ``runtimes/{tmux,multiplexer,pane_capture,
prompts,claude_code}.py`` + ``auto/{accept,daemon}.py``. No behavior
changes vs ba6755e^; any drift fix is recorded per-marker in
``prompts.py``.
"""
