"""Test package mirror for ``src/scitex_agent_container/_runners/_tmux/auto/``.

Required by PS-202 / PS-207 (src↔tests mirror discipline + no
empty-test-dir) so the auditor finds real ``test_*.py`` files for
each src module. The auto-accept loop + daemon submodule was
salvaged with the Day-1 tmux modules (PR-cherry-pick of #353); the
substantive integration is deferred behind the TUI hedge flag.
"""
