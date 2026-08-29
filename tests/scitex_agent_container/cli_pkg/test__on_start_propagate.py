"""Tests for ``--on <peer> agents start`` lead-side registry propagation.

Issue #192: a ``sac --on spartan-bm001 agents start clew`` recorded the
new instance ONLY in the remote peer's local registry; the dispatching
(lead) host's cross-host ``instances`` table never learned about it, so
the lead resolved clew against a STALE node with the silent-local default.

These tests prove ``propagate_remote_start`` records a lead-side
``instances`` row capturing the ACTUAL override host (``--on <peer>``),
the remote-resolved bound port, and ``remote=True``.

No-mocks: a real callable seam (``runner``) returns a real
``CompletedProcess`` carrying the JSON a remote ``agents start --json``
would emit; the registry write goes to an isolated on-disk state.db.
Conforms to STX-TQ002 (AAA markers), STX-TQ003 (descriptive names),
STX-TQ007 (one assertion per test).
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from scitex_agent_container.cli_pkg._on_start_propagate import (
    is_agents_start_argv,
    parse_started_agent_name,
    propagate_remote_start,
)


@pytest.fixture(autouse=True)
def _instances_store(pg_schema: str):
    """A throwaway ``instances`` store for every test in this file.

    ``instances`` moved to the shared PostgreSQL store on 2026-08-28 and the
    verbs driven here read ``list_active_instances`` on every path, so the
    dependency belongs to the VERB rather than to any one case. Autouse
    rather than per-signature for that reason, and for one more: it keeps a
    NEW test in this file from silently resolving whatever store the process
    happens to point at.
    """
    yield


@pytest.fixture
def isolated_state_db(tmp_path: Path):
    """Redirect state.db to a tmp path; reload the module so the
    module-level DEFAULT_DB_PATH picks it up (explicit save/restore)."""
    db = tmp_path / "state.db"
    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    os.environ[key] = str(db)
    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    try:
        yield db
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        importlib.reload(mod)


def _started_runner(name: str, *, host_workdir: str = "/work", port: int = 19123):
    """Return a runner seam that emits the JSON a successful remote start prints."""
    payload = {
        "name": name,
        "status": "started",
        "host": "peer-side-reported",
        "host_workdir": host_workdir,
        "container_workdir": "/container",
        "dry_run": False,
        "a2a_port": port,
        "started_at": "2026-05-24T08:00:00Z",
    }

    def _run(peer, full_argv):
        return subprocess.CompletedProcess(
            args=full_argv,
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        )

    return _run


# ---------------------------------------------------------------------------
# argv classification
# ---------------------------------------------------------------------------


class TestArgvClassification:
    def test_agents_start_argv_is_recognized_as_start(self) -> None:
        # Arrange
        argv = ["agents", "start", "clew", "--force"]
        # Act
        recognized = is_agents_start_argv(argv)
        # Assert
        assert recognized is True

    def test_agents_list_argv_is_not_recognized_as_start(self) -> None:
        # Arrange
        argv = ["agents", "list", "--json"]
        # Act
        recognized = is_agents_start_argv(argv)
        # Assert
        assert recognized is False

    def test_legacy_singular_agent_start_is_recognized(self) -> None:
        # Arrange
        argv = ["agent", "start", "clew"]
        # Act
        recognized = is_agents_start_argv(argv)
        # Assert
        assert recognized is True

    def test_positional_name_parsed_past_value_flag(self) -> None:
        # Arrange — --session takes a value that must NOT be mistaken
        # for the agent name.
        argv = ["agents", "start", "--session", "resume", "clew"]
        # Act
        name = parse_started_agent_name(argv)
        # Assert
        assert name == "clew"

    def test_positional_name_parsed_past_plain_flag(self) -> None:
        # Arrange
        argv = ["agents", "start", "--force", "--json", "neurovista"]
        # Act
        name = parse_started_agent_name(argv)
        # Assert
        assert name == "neurovista"

    def test_no_positional_returns_none(self) -> None:
        # Arrange
        argv = ["agents", "start", "--force"]
        # Act
        name = parse_started_agent_name(argv)
        # Assert
        assert name is None


# ---------------------------------------------------------------------------
# propagation records the ACTUAL override host
# ---------------------------------------------------------------------------


class TestPropagateRecordsOverrideHost:
    def test_records_lead_side_row_on_override_host(
        self, isolated_state_db, capsys
    ) -> None:
        # Arrange — a successful remote start on the override host.
        runner = _started_runner("clew", port=19123)
        # Act
        propagate_remote_start(
            "spartan-bm001", ["agents", "start", "clew"], runner=runner
        )
        # Assert — the recorded instances row's host IS the --on override.
        from scitex_agent_container._state.state_db import list_active_instances

        rows = [r for r in list_active_instances() if r["name"] == "clew"]
        assert rows[0]["host"] == "spartan-bm001"

    def test_records_row_with_remote_flag_set(self, isolated_state_db) -> None:
        # Arrange
        runner = _started_runner("clew", port=19123)
        # Act
        propagate_remote_start(
            "spartan-bm001", ["agents", "start", "clew"], runner=runner
        )
        # Assert
        from scitex_agent_container._state.state_db import list_active_instances

        rows = [r for r in list_active_instances() if r["name"] == "clew"]
        assert bool(rows[0]["remote"]) is True

    def test_records_remote_resolved_bound_port(self, isolated_state_db) -> None:
        # Arrange
        runner = _started_runner("clew", port=19123)
        # Act
        propagate_remote_start(
            "spartan-bm001", ["agents", "start", "clew"], runner=runner
        )
        # Assert
        from scitex_agent_container._state.state_db import list_active_instances

        rows = [r for r in list_active_instances() if r["name"] == "clew"]
        assert rows[0]["bound_port"] == 19123

    def test_appends_json_and_no_redispatch_to_remote_argv(
        self, isolated_state_db
    ) -> None:
        # Arrange — capture the argv the runner is invoked with.
        seen: list[list[str]] = []

        def _run(peer, full_argv):
            seen.append(full_argv)
            return subprocess.CompletedProcess(
                args=full_argv,
                returncode=0,
                stdout=json.dumps({"name": "clew", "status": "started", "a2a_port": 1}),
                stderr="",
            )

        # Act
        propagate_remote_start(
            "spartan-bm001", ["agents", "start", "clew"], runner=_run
        )
        # Assert — both control flags appended for a parseable, non-recursive remote start.
        assert {"--json", "--no-redispatch"}.issubset(set(seen[0]))

    def test_non_json_stdout_raises_loud_error(self, isolated_state_db) -> None:
        # Arrange — remote start succeeded (rc 0) but emitted non-JSON.
        def _run(peer, full_argv):
            return subprocess.CompletedProcess(
                args=full_argv, returncode=0, stdout="not-json", stderr=""
            )

        # Act
        ctx = pytest.raises(RuntimeError, match="non-JSON stdout")
        # Assert
        with ctx:
            propagate_remote_start(
                "spartan-bm001", ["agents", "start", "clew"], runner=_run
            )

    def test_remote_failure_records_no_row(self, isolated_state_db) -> None:
        # Arrange — remote start failed (rc 1); no live instance to record.
        def _run(peer, full_argv):
            return subprocess.CompletedProcess(
                args=full_argv, returncode=1, stdout="", stderr="boom"
            )

        propagate_remote_start(
            "spartan-bm001", ["agents", "start", "clew"], runner=_run
        )
        # Act
        from scitex_agent_container._state.state_db import list_active_instances

        rows = [r for r in list_active_instances() if r["name"] == "clew"]
        # Assert — nothing recorded for a failed remote start.
        assert rows == []


# ---------------------------------------------------------------------------
# Bug 1 fail-loud guards: silent-skip / silent-rc!=0 must surface to the operator.
#
# The lead's repro:
#   sac --on spartan-gpgpu011 agents start proj-paper-scitex-clew --force --no-preflight
# exited 0 with ZERO stdout/stderr because:
#   (a) propagate_remote_start returned 0 when the remote emitted
#       {"status": "skipped", ...}, dropping the skip reason on the floor;
#   (b) when the remote exited non-zero, _default_ssh_runner captured
#       stderr (capture_output=True) and propagate_remote_start returned
#       the rc WITHOUT echoing the captured stderr/stdout to the operator.
# Both paths produced operator-invisible no-ops. These tests pin the
# fail-loud contract so the regression is caught immediately.
# ---------------------------------------------------------------------------


class TestFailLoudOnRemoteNonStart:
    def test_skipped_status_returns_nonzero(self, isolated_state_db) -> None:
        # Arrange — remote start succeeded (rc 0) but skipped (host mismatch).
        def _run(peer, full_argv):
            return subprocess.CompletedProcess(
                args=full_argv,
                returncode=0,
                stdout=json.dumps(
                    {
                        "name": "clew",
                        "status": "skipped",
                        "reason": "singleton prefers 'bm043', current host is 'gpgpu011'",
                        "dry_run": False,
                    }
                ),
                stderr="",
            )

        # Act
        rc = propagate_remote_start(
            "spartan-gpgpu011", ["agents", "start", "clew"], runner=_run
        )
        # Assert — a skipped remote start MUST NOT look like success to the operator.
        assert rc != 0

    def test_skipped_status_prints_reason_to_stderr(
        self, isolated_state_db, capsys
    ) -> None:
        # Arrange
        def _run(peer, full_argv):
            return subprocess.CompletedProcess(
                args=full_argv,
                returncode=0,
                stdout=json.dumps(
                    {
                        "name": "clew",
                        "status": "skipped",
                        "reason": "singleton prefers 'bm043', current host is 'gpgpu011'",
                        "dry_run": False,
                    }
                ),
                stderr="",
            )

        # Act
        propagate_remote_start(
            "spartan-gpgpu011", ["agents", "start", "clew"], runner=_run
        )
        # Assert — the remote's skip reason MUST reach the operator.
        captured = capsys.readouterr()
        assert "singleton prefers 'bm043'" in (captured.err + captured.out)

    def test_skipped_status_records_no_row(self, isolated_state_db) -> None:
        # Arrange
        def _run(peer, full_argv):
            return subprocess.CompletedProcess(
                args=full_argv,
                returncode=0,
                stdout=json.dumps({"name": "clew", "status": "skipped"}),
                stderr="",
            )

        # Act
        propagate_remote_start(
            "spartan-gpgpu011", ["agents", "start", "clew"], runner=_run
        )
        # Assert — a skipped remote did NOT start anything, so no row.
        from scitex_agent_container._state.state_db import list_active_instances

        rows = [r for r in list_active_instances() if r["name"] == "clew"]
        assert rows == []

    def test_dry_run_ok_status_is_silent_success(
        self, isolated_state_db, capsys
    ) -> None:
        # Arrange — `--dry-run` propagated to the remote returns
        # status=dry_run_ok cleanly; this is the ONE non-"started" status
        # that legitimately returns 0 (no agent was meant to start).
        def _run(peer, full_argv):
            return subprocess.CompletedProcess(
                args=full_argv,
                returncode=0,
                stdout=json.dumps(
                    {"name": "clew", "status": "dry_run_ok", "dry_run": True}
                ),
                stderr="",
            )

        # Act
        rc = propagate_remote_start(
            "spartan-gpgpu011",
            ["agents", "start", "clew", "--dry-run"],
            runner=_run,
        )
        # Assert
        assert rc == 0

    def test_remote_failure_echoes_remote_output_to_operator(
        self, isolated_state_db, capsys
    ) -> None:
        # Arrange — remote rc != 0 (e.g. exception inside remote start);
        # captured stdout/stderr MUST surface so the operator can debug.
        def _run(peer, full_argv):
            return subprocess.CompletedProcess(
                args=full_argv,
                returncode=1,
                stdout=json.dumps(
                    {
                        "name": "clew",
                        "status": "error",
                        "error": "Permission denied loading spec.yaml",
                    }
                ),
                stderr="ssh-side: connection reset",
            )

        # Act
        propagate_remote_start(
            "spartan-gpgpu011", ["agents", "start", "clew"], runner=_run
        )
        # Assert — at least the remote's structured error must reach the operator.
        captured = capsys.readouterr()
        merged = captured.err + captured.out
        assert "Permission denied loading spec.yaml" in merged

    def test_remote_failure_returns_remote_rc(self, isolated_state_db) -> None:
        # Arrange
        def _run(peer, full_argv):
            return subprocess.CompletedProcess(
                args=full_argv,
                returncode=42,
                stdout="",
                stderr="boom",
            )

        # Act
        rc = propagate_remote_start(
            "spartan-gpgpu011", ["agents", "start", "clew"], runner=_run
        )
        # Assert
        assert rc == 42
