"""``cleanup_stale`` must not delete the record of a LIVING agent.

MEASURED 2026-08-02 on the live host — the defect these tests pin:

    tmux has-session -t scitex-dev      -> not found
    tmux has-session -t tui-scitex-dev  -> ALIVE

The registry entry's ``screen`` field holds the BARE agent name while the
runtime creates ``tui-<name>``, so the old probe returned False for EVERY live
agent and the sweep unlinked the whole fleet's records. Downstream, a2a_peers /
``sac agents list`` / agent_health then reported those agents as nonexistent
rather than unknown — which cost scitex-cards ~6 messages to an agent that was
never unreachable.

WHY THESE ASSERT PRESENCE, NOT ABSENCE (dotfiles' catch, and it is the point):
"cleanup_stale removed 0 entries" passes when the fix works, when the sweep
never ran, when it errored before the removal step, and when it found nothing
to consider. Four causes, one observation, no discrimination — the same defect
as a fixture that cannot exercise the failure. So each test demands a specific
POSITIVE outcome the buggy code cannot produce: the entry SURVIVES a sweep in
which the probe was actually called with the tui-prefixed name.

PA-307 / STX-TQ002 / STX-TQ007 — one assert per test, full AAA markers.
"""

from __future__ import annotations

import json

from scitex_agent_container._state.registry import Registry


def _write_entry(reg_dir, name: str, screen: str) -> None:
    """Write a registry entry the way older sac versions did: BARE screen."""
    (reg_dir / f"{name}.json").write_text(
        json.dumps({"name": name, "config": "", "pid": 4242, "screen": screen})
    )


def test_live_agent_under_prefixed_session_is_probed_for_that_name(tmp_path):
    # Arrange — the real fleet shape: entry says "demo", session is "tui-demo".
    reg = Registry(registry_dir=tmp_path)
    _write_entry(tmp_path, "demo", "demo")
    asked: list[str] = []

    def probe(session: str):
        asked.append(session)
        return session == "tui-demo"

    # Act
    reg.cleanup_stale(probe=probe)
    # Assert — the buggy code asks ONLY "demo" and never the prefixed name.
    assert "tui-demo" in asked


def test_live_agent_under_prefixed_session_survives_the_sweep(tmp_path):
    # Arrange
    reg = Registry(registry_dir=tmp_path)
    _write_entry(tmp_path, "demo", "demo")

    def probe(session: str):
        return session == "tui-demo"

    # Act
    reg.cleanup_stale(probe=probe)
    # Assert — POSITIVE: the record is still there to be read.
    assert reg.exists("demo")


def test_an_unknowable_probe_never_deletes(tmp_path):
    # Arrange — no multiplexer binary available: every probe returns None.
    # The old code treated that as death (FileNotFoundError is an OSError and
    # the caller unlinked on OSError), so an absent tmux wiped the registry.
    reg = Registry(registry_dir=tmp_path)
    _write_entry(tmp_path, "demo", "demo")

    def probe(session: str):
        return None

    # Act
    reg.cleanup_stale(probe=probe)
    # Assert
    assert reg.exists("demo")


def test_a_positively_absent_session_is_still_removed(tmp_path):
    # Arrange — the sweep must keep WORKING; "never delete" would be a gate
    # that cannot fire, which is the opposite defect.
    reg = Registry(registry_dir=tmp_path)
    _write_entry(tmp_path, "demo", "demo")

    def probe(session: str):
        return False

    # Act
    reg.cleanup_stale(probe=probe)
    # Assert
    assert not reg.exists("demo")


def test_unreadable_entry_is_kept_rather_than_unlinked(tmp_path):
    # Arrange — malformed JSON is UNKNOWN, not dead. The old code deleted it.
    reg = Registry(registry_dir=tmp_path)
    (tmp_path / "broken.json").write_text("{not json")

    # Act
    reg.cleanup_stale(probe=lambda s: False)
    # Assert
    assert (tmp_path / "broken.json").exists()
