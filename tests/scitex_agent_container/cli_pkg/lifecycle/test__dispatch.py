"""Tests for cli_pkg.lifecycle._dispatch._dispatch_remote_start (step 4).

See ``~/proj/scitex-lead/GITIGNORED/WORKING/remote-agent-pipeline.md``.
Covers the drift-check / spec-handoff surface (step 3b) AND the remote
``sac agents start`` invocation + JSON parse + lead-side instances
row write (step 4).

No-mocks: real ``subprocess.run`` against a PATH-prepended fake ``ssh``
binary that answers as the peer would in each phase of the handoff.
Every leg is now an ssh call — the spec no longer travels by rsync, whose
exit code was not evidence of delivery (see ``_spec_handoff``). Conforms
to STX-TQ002 (AAA markers), STX-TQ003 (descriptive names), STX-TQ007
(one assert per test).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from scitex_agent_container._state.host_config import PeerSpec
from scitex_agent_container.cli_pkg.lifecycle._dispatch import (
    _dispatch_remote_start,
    lookup_remote_peer,
    try_dispatch,
    try_dispatch_remote,
)
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import HostsSpec

# ---------------------------------------------------------------------------
# Shim helpers — dual-behavior rsync (dry-run vs real) plus a fake ssh.
# ---------------------------------------------------------------------------


def _install_ssh_shim(
    bin_dir: Path,
    *,
    peer_manifest: str = "",
    manifest_exit: int = 0,
    manifest_stderr: str = "",
    landed_manifest: str | None = None,
    extract_exit: int = 0,
    extract_stderr: str = "",
    stdout: str = "{}",
    stderr: str = "",
    exit: int = 0,
) -> Path:
    """A fake peer, reached the way the real one is — over ``ssh``.

    Every leg of a dispatch is now an ssh call, so the shim answers as the
    peer would in each PHASE, recognised from the script it was handed:

      * ``md5sum``  — report the spec dir's digests. The FIRST such call is
        the pre-handoff read (``peer_manifest``); every later one is the
        post-transfer verification (``landed_manifest``, defaulting to
        ``peer_manifest`` so a peer that received nothing keeps saying so).
        Splitting the two is what lets a test model a transport that exits 0
        and delivers nowhere.
      * ``tar -C``  — receive the spec (``extract_exit``).
      * anything else — the ``sac agents start --json`` handoff (``stdout``).
    """
    log = bin_dir / "ssh.argv.jsonl"
    seen = bin_dir / "ssh.manifest.count"
    script = bin_dir / "ssh"
    landed = peer_manifest if landed_manifest is None else landed_manifest
    body = (
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"log, seen = {json.dumps(str(log))}, {json.dumps(str(seen))}\n"
        "argv = sys.argv[1:]\n"
        "with open(log, 'a') as fh:\n"
        "    fh.write(json.dumps(argv) + '\\n')\n"
        "joined = ' '.join(argv)\n"
        f"before, after = {json.dumps(peer_manifest)}, {json.dumps(landed)}\n"
        "if 'md5sum' in joined:\n"
        "    n = int(open(seen).read()) if os.path.exists(seen) else 0\n"
        "    open(seen, 'w').write(str(n + 1))\n"
        "    sys.stdout.write(before if n == 0 else after)\n"
        f"    sys.stderr.write({json.dumps(manifest_stderr)})\n"
        f"    sys.exit({int(manifest_exit)})\n"
        "if 'tar -C' in joined:\n"
        "    sys.stdin.buffer.read()\n"
        f"    sys.stderr.write({json.dumps(extract_stderr)})\n"
        f"    sys.exit({int(extract_exit)})\n"
        f"sys.stdout.write({json.dumps(stdout)})\n"
        f"sys.stderr.write({json.dumps(stderr)})\n"
        f"sys.exit({int(exit)})\n"
    )
    script.write_text(body)
    script.chmod(0o755)
    return script


def _ssh_invocations(bin_dir: Path) -> list[list[str]]:
    log = bin_dir / "ssh.argv.jsonl"
    if not log.exists():
        return []
    return [json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]


def _phase_count(bin_dir: Path, marker: str) -> int:
    return sum(1 for argv in _ssh_invocations(bin_dir) if marker in " ".join(argv))


# ---------------------------------------------------------------------------
# Fixtures: HOME redirection, spec dir, PATH-prepended shim bin, peer config.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: Path, env_save_restore):
    """Redirect HOME so Path.home() returns tmp_path."""
    env_save_restore.set("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def spec_dir(fake_home: Path) -> Path:
    """Populated spec dir at ``~/.scitex/agent-container/agents/alpha``."""
    d = fake_home / ".scitex" / "agent-container" / "agents" / "alpha"
    d.mkdir(parents=True)
    (d / "spec.yaml").write_text("name: alpha\n")
    return d


@pytest.fixture
def registered_peer(fake_home: Path, env_save_restore) -> Path:
    """Register ``peer-host`` in a real config.yaml.

    Needed by EVERY dispatch test now: each leg of the handoff — the
    manifest read, the transfer, the verification — is rendered by
    ``build_ssh_argv``, which resolves the peer from this config. Under the
    old rsync transport only the final ``sac agents start`` leg did.
    """
    return _write_peer_config(fake_home, env_save_restore)


@pytest.fixture
def shim_bin(tmp_path: Path, env_save_restore) -> Path:
    """Prepend a fresh bin dir to PATH for shim installation."""
    bin_dir = tmp_path / "_shim_bin"
    bin_dir.mkdir()
    saved_path = os.environ.get("PATH", "")
    env_save_restore.set("PATH", f"{bin_dir}{os.pathsep}{saved_path}")
    return bin_dir


@pytest.fixture
def state_db(fake_home: Path) -> Path:
    """Redirect state.db to a tmp path under fake_home.

    DEFAULT_DB_PATH is module-level and reads the env var at import
    time, so we reload the module after setting the env var. Tests
    that mutate state.db rely on this to stay isolated; without the
    reload each test would write to the user's real state.db.

    Both env-var manipulation and module reload are managed locally
    (no env_save_restore) so the teardown order is unambiguous: we
    first reset the env, THEN reload, so DEFAULT_DB_PATH lands back
    on the real path the user expects after the fixture exits.
    """
    import importlib
    import os as _os

    db = fake_home / "state.db"
    saved = _os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    _os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    import scitex_agent_container._state.state_db as _state_db_mod

    importlib.reload(_state_db_mod)
    try:
        yield db
    finally:
        if saved is None:
            _os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            _os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved
        importlib.reload(_state_db_mod)


def _write_peer_config(
    home: Path,
    env_save_restore,
    peer: str = "peer-host",
    env_preamble: list[str] | None = None,
) -> Path:
    """Write ``config.yaml`` registering ``peer`` (optional env_preamble)."""
    cfg = home / "config.yaml"
    body = f"host:\n  fallback: hostname-short\npeers:\n  {peer}:\n    ssh: {peer}\n"
    if env_preamble:
        body += "    env_preamble:\n"
        for line in env_preamble:
            body += f"      - {line}\n"
    cfg.write_text(body)
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
    return cfg


# ---------------------------------------------------------------------------
# Scenario builder.
# ---------------------------------------------------------------------------


@dataclass
class _Scenario:
    bin_dir: Path
    raised: BaseException | None = None
    returned: Any = None
    captured_stdout: str = ""
    captured_stderr: str = ""

    @property
    def message(self) -> str:
        return str(self.raised) if self.raised is not None else ""

    @property
    def shipped_count(self) -> int:
        """How many times the spec was actually handed to the peer."""
        return _phase_count(self.bin_dir, "tar -C")


def _act_dispatch(
    shim_bin: Path,
    capsys,
    *,
    ssh_kwargs: dict[str, Any] | None = None,
    name: str = "alpha",
    peer: str = "peer-host",
    dry_run: bool = False,
    force: bool = False,
) -> _Scenario:
    """Install the fake peer and invoke ``_dispatch_remote_start`` once."""
    _install_ssh_shim(shim_bin, **(ssh_kwargs or {}))
    scen = _Scenario(bin_dir=shim_bin)
    try:
        scen.returned = _dispatch_remote_start(name, peer, dry_run=dry_run, force=force)
    except BaseException as exc:
        scen.raised = exc
    captured = capsys.readouterr()
    scen.captured_stdout = captured.out
    scen.captured_stderr = captured.err
    return scen


#: The spec file the ``spec_dir`` fixture writes, and its real md5 — the
#: digest a peer that received it correctly must report back.
_ALPHA_BODY = "name: alpha\n"
_ALPHA_MD5 = hashlib.md5(_ALPHA_BODY.encode(), usedforsecurity=False).hexdigest()

#: A peer holding exactly what the lead has (post-transfer, or no drift).
_DELIVERED = f"{_ALPHA_MD5}  ./spec.yaml\n"
#: A peer holding a DIFFERENT spec.yaml — the drift gate's trigger.
_PEER_DRIFTED = f"{'0' * 32}  ./spec.yaml\n"

_OK_JSON = '{"a2a_port": 47213, "started_at": "2026-05-16T00:00:00Z"}'

def _peer_that_delivers(**start_kwargs) -> dict[str, Any]:
    """A peer whose handoff genuinely succeeds, varying only its start reply.

    Every step-4 test needs the spec to actually arrive before the remote
    ``sac agents start`` is reached at all — the handoff now refuses to
    continue past a mis-delivery — so the delivered digests are pinned here
    and each test overrides only the ``--json`` reply it cares about.
    """
    return dict(peer_manifest="", landed_manifest=_DELIVERED, **start_kwargs)


#: Reusable step-4 peer: empty before the handoff, correct after it, and a
#: well-formed ``sac agents start --json`` reply.
_SK_OK = _peer_that_delivers(stdout=_OK_JSON, exit=0)


# ---------------------------------------------------------------------------
# Drift / rsync gate behavior (step 3b).
# ---------------------------------------------------------------------------


class TestDispatchDriftBlocksWithoutForce:
    def test_drift_without_force_raises_runtime_error(self, spec_dir, shim_bin, registered_peer, capsys):
        # Arrange — the peer holds a DIFFERENT spec.yaml.
        sk = dict(peer_manifest=_PEER_DRIFTED)
        # Act
        scen = _act_dispatch(shim_bin, capsys, ssh_kwargs=sk)
        # Assert
        assert isinstance(scen.raised, RuntimeError)

    def test_drift_message_mentions_spec_drift(self, spec_dir, shim_bin, registered_peer, capsys):
        # Arrange
        sk = dict(peer_manifest=_PEER_DRIFTED)
        # Act
        scen = _act_dispatch(shim_bin, capsys, ssh_kwargs=sk)
        # Assert
        assert "Spec drift" in scen.message

    def test_drift_message_names_the_differing_file(self, spec_dir, shim_bin, registered_peer, capsys):
        # Arrange
        sk = dict(peer_manifest=_PEER_DRIFTED)
        # Act
        scen = _act_dispatch(shim_bin, capsys, ssh_kwargs=sk)
        # Assert
        assert "spec.yaml" in scen.message

    def test_drift_ships_nothing(self, spec_dir, shim_bin, registered_peer, capsys):
        # Arrange
        sk = dict(peer_manifest=_PEER_DRIFTED)
        # Act
        scen = _act_dispatch(shim_bin, capsys, ssh_kwargs=sk)
        # Assert
        assert scen.shipped_count == 0

    def test_a_peer_only_file_alone_does_not_block_a_start(
        self, pg_schema: str, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        """The handoff no longer deletes, so a file only the peer has is news
        rather than a conflict — losing scitex-nas-03's sidecar launcher to a
        mirroring delete is what that rule prevents."""
        # Arrange
        _write_peer_config(fake_home, env_save_restore)
        peer_only = _DELIVERED + f"{'1' * 32}  ./start-telegram-sidecar.sh\n"
        sk = dict(peer_manifest=peer_only, landed_manifest=peer_only, stdout=_OK_JSON)
        # Act
        scen = _act_dispatch(shim_bin, capsys, ssh_kwargs=sk)
        # Assert
        assert scen.raised is None


class TestDispatchDryRunMode:
    def test_dry_run_mode_does_not_raise(self, spec_dir, shim_bin, registered_peer, capsys):
        # Arrange
        sk = dict(peer_manifest="")
        # Act
        scen = _act_dispatch(shim_bin, capsys, ssh_kwargs=sk, dry_run=True)
        # Assert
        assert scen.raised is None

    def test_dry_run_mode_returns_zero_exit(self, spec_dir, shim_bin, registered_peer, capsys):
        # Arrange
        sk = dict(peer_manifest="")
        # Act
        scen = _act_dispatch(shim_bin, capsys, ssh_kwargs=sk, dry_run=True)
        # Assert
        assert scen.returned == 0

    def test_dry_run_mode_prints_dispatch_marker(self, spec_dir, shim_bin, registered_peer, capsys):
        # Arrange
        sk = dict(peer_manifest="")
        # Act
        scen = _act_dispatch(shim_bin, capsys, ssh_kwargs=sk, dry_run=True)
        # Assert
        assert "[dispatch] dry-run" in scen.captured_stdout

    def test_dry_run_mode_ships_nothing(self, spec_dir, shim_bin, registered_peer, capsys):
        # Arrange
        sk = dict(peer_manifest="")
        # Act
        scen = _act_dispatch(shim_bin, capsys, ssh_kwargs=sk, dry_run=True)
        # Assert
        assert scen.shipped_count == 0


class TestDispatchHandoffFailures:
    def test_unreadable_peer_manifest_raises_runtime_error(
        self, spec_dir, shim_bin, registered_peer, capsys
    ):
        # Arrange
        sk = dict(manifest_stderr="ssh: host unreachable\n", manifest_exit=255)
        # Act
        scen = _act_dispatch(shim_bin, capsys, ssh_kwargs=sk)
        # Assert
        assert isinstance(scen.raised, RuntimeError)

    def test_unreadable_peer_manifest_message_identifies_phase(
        self, spec_dir, shim_bin, registered_peer, capsys
    ):
        # Arrange
        sk = dict(manifest_stderr="ssh: host unreachable\n", manifest_exit=255)
        # Act
        scen = _act_dispatch(shim_bin, capsys, ssh_kwargs=sk)
        # Assert
        assert "Could not read the spec manifest" in scen.message

    def test_a_failed_transfer_message_identifies_the_extract_phase(
        self, spec_dir, shim_bin, registered_peer, capsys
    ):
        # Arrange — the peer is readable, then refuses the write.
        sk = dict(peer_manifest="", extract_stderr="broken pipe\n", extract_exit=12)
        # Act
        scen = _act_dispatch(shim_bin, capsys, ssh_kwargs=sk)
        # Assert
        assert "failed while extracting" in scen.message

    def test_a_transfer_that_exits_zero_but_delivers_nothing_raises(
        self, spec_dir, shim_bin, registered_peer, capsys
    ):
        """THE regression. scitex-nas-03's patched rsync exited 0 and wrote the
        spec one directory away; the old code then booted the agent from the
        stale spec and called the dispatch a success."""
        # Arrange — extraction "succeeds", peer still reports an empty dir.
        sk = dict(peer_manifest="", landed_manifest="", extract_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, ssh_kwargs=sk)
        # Assert
        assert "stale spec" in scen.message

    def test_a_silent_mis_delivery_never_reaches_the_remote_start(
        self, spec_dir, shim_bin, registered_peer, capsys
    ):
        """The consequence that made this urgent: a mis-delivered spec must
        not be followed by a start that reads whatever is at that path."""
        # Arrange
        sk = dict(peer_manifest="", landed_manifest="", extract_exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, ssh_kwargs=sk)
        # Assert
        assert _phase_count(shim_bin, "sac agents start") == 0


class TestDispatchMissingSpecDir:
    def test_missing_spec_dir_raises_file_not_found(self, fake_home, shim_bin, capsys):
        # Arrange — fake_home redirects HOME but NO spec dir is created.
        # Act
        scen = _act_dispatch(shim_bin, capsys, name="ghost")
        # Assert
        assert isinstance(scen.raised, FileNotFoundError)

    def test_missing_spec_dir_message_names_problem(self, fake_home, shim_bin, capsys):
        # Arrange
        # Act
        scen = _act_dispatch(shim_bin, capsys, name="ghost")
        # Assert
        assert "Spec dir for" in scen.message


# ---------------------------------------------------------------------------
# Step 4 — ssh handoff: success path writes lead-side row, returns 0,
# prints success line; ssh failure / non-JSON paths raise RuntimeError.
# ---------------------------------------------------------------------------


class TestDispatchSshSuccessPath:
    def test_dispatch_ssh_success_writes_instances_row(
        self, pg_schema: str, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange
        _write_peer_config(fake_home, env_save_restore)
        # Act
        _act_dispatch(shim_bin, capsys, ssh_kwargs=_SK_OK)
        # Assert — query state.db via the project API so schema is init'd.
        from scitex_agent_container._state.state_db import list_active_instances

        rows = [r for r in list_active_instances() if r["name"] == "alpha"]
        assert (rows[0]["host"], rows[0]["a2a_port"]) == ("peer-host", 47213)

    def test_dispatch_ssh_success_returns_zero(
        self, pg_schema: str, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange
        _write_peer_config(fake_home, env_save_restore)
        # Act
        scen = _act_dispatch(shim_bin, capsys, ssh_kwargs=_SK_OK)
        # Assert
        assert scen.returned == 0

    def test_dispatch_ssh_success_prints_started_message(
        self, pg_schema: str, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange
        _write_peer_config(fake_home, env_save_restore)
        # Act
        scen = _act_dispatch(shim_bin, capsys, ssh_kwargs=_SK_OK)
        # Assert
        assert "started on 'peer-host'" in scen.captured_stdout

    def test_dispatch_ssh_success_prints_assigned_port(
        self, pg_schema: str, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange
        _write_peer_config(fake_home, env_save_restore)
        # Act
        scen = _act_dispatch(shim_bin, capsys, ssh_kwargs=_SK_OK)
        # Assert
        assert "a2a_port=47213" in scen.captured_stdout

    def test_dispatch_ssh_success_marks_row_remote(
        self, pg_schema: str, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange — a cross-host dispatch must record remote=1 so
        # resolve_peer_url / agent_status know to reach the agent on the
        # peer (sac-agent-spawn design, Rule B/F).
        _write_peer_config(fake_home, env_save_restore)
        # Act
        _act_dispatch(shim_bin, capsys, ssh_kwargs=_SK_OK)
        # Assert
        from scitex_agent_container._state.state_db import list_active_instances

        rows = [r for r in list_active_instances() if r["name"] == "alpha"]
        assert rows[0]["remote"] == 1

    def test_dispatch_ssh_success_records_bound_port_from_peer_json(
        self, pg_schema: str, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange — the bound port captured back from the peer's --json
        # output is the concrete int the peer's allocator resolved (the
        # crux of the remote-port gap fix).
        _write_peer_config(fake_home, env_save_restore)
        # Act
        _act_dispatch(shim_bin, capsys, ssh_kwargs=_SK_OK)
        # Assert
        from scitex_agent_container._state.state_db import list_active_instances

        rows = [r for r in list_active_instances() if r["name"] == "alpha"]
        assert rows[0]["bound_port"] == 47213

    def test_dispatch_ssh_success_records_cli_spawned_by(
        self, pg_schema: str, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange — a bare lead dispatch (no SAC_NAME) records the
        # lineage edge as "cli".
        env_save_restore.set("SAC_NAME", "")
        _write_peer_config(fake_home, env_save_restore)
        # Act
        _act_dispatch(shim_bin, capsys, ssh_kwargs=_SK_OK)
        # Assert
        from scitex_agent_container._state.state_db import list_active_instances

        rows = [r for r in list_active_instances() if r["name"] == "alpha"]
        assert rows[0]["spawned_by"] == "cli"

    def test_dispatch_ssh_success_propagates_a2a_port_none_when_spec_omits_it(
        self, pg_schema: str, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange — when peer JSON has ``a2a_port: null`` (sidecar
        # disabled), lead MUST write NULL into the instances row
        # rather than substituting a default. Covers the cross-host
        # null-propagation seam.
        _write_peer_config(fake_home, env_save_restore)
        sk_null = _peer_that_delivers(
            stdout='{"a2a_port": null, "started_at": "2026-05-17T00:00:00Z"}',
            exit=0,
        )
        # Act
        _act_dispatch(shim_bin, capsys, ssh_kwargs=sk_null)
        # Assert
        from scitex_agent_container._state.state_db import list_active_instances

        rows = [r for r in list_active_instances() if r["name"] == "alpha"]
        assert rows[0]["a2a_port"] is None


class TestDispatchSshFailurePaths:
    def test_dispatch_ssh_failure_raises_runtime_error(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange
        _write_peer_config(fake_home, env_save_restore)
        sk = _peer_that_delivers(stdout="", stderr="connection refused\n", exit=255)
        # Act
        scen = _act_dispatch(shim_bin, capsys, ssh_kwargs=sk)
        # Assert
        assert isinstance(scen.raised, RuntimeError)

    def test_dispatch_ssh_failure_message_mentions_remote_failed(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange
        _write_peer_config(fake_home, env_save_restore)
        sk = _peer_that_delivers(stderr="connection refused\n", exit=255)
        # Act
        scen = _act_dispatch(shim_bin, capsys, ssh_kwargs=sk)
        # Assert
        assert "Remote `sac agents start alpha` failed" in scen.message

    def test_dispatch_ssh_non_json_stdout_raises_runtime_error(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange
        _write_peer_config(fake_home, env_save_restore)
        sk = _peer_that_delivers(stdout="OK\n", exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, ssh_kwargs=sk)
        # Assert
        assert isinstance(scen.raised, RuntimeError)

    def test_dispatch_ssh_non_json_message_mentions_phase(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange
        _write_peer_config(fake_home, env_save_restore)
        sk = _peer_that_delivers(stdout="OK\n", exit=0)
        # Act
        scen = _act_dispatch(shim_bin, capsys, ssh_kwargs=sk)
        # Assert
        assert "non-JSON stdout" in scen.message


# ---------------------------------------------------------------------------
# Step 4 — ssh argv assembly: --no-redispatch + env_preamble forwarding.
# ---------------------------------------------------------------------------


_LMOD_PREAMBLE = ["module load GCCcore/11.3.0", "module load Apptainer/1.3.3"]


class TestDispatchSshArgv:
    def test_dispatch_ssh_argv_includes_no_redispatch_flag(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange — peer-side MUST NOT re-trigger the dispatch branch.
        _write_peer_config(fake_home, env_save_restore)
        # Act
        _act_dispatch(shim_bin, capsys, ssh_kwargs=_SK_OK)
        # Assert
        assert "--no-redispatch" in " ".join(_ssh_invocations(shim_bin)[-1])

    def test_dispatch_ssh_argv_includes_json_flag(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange — peer must emit machine-parseable output.
        _write_peer_config(fake_home, env_save_restore)
        # Act
        _act_dispatch(shim_bin, capsys, ssh_kwargs=_SK_OK)
        # Assert
        assert "--json" in " ".join(_ssh_invocations(shim_bin)[-1])

    def test_dispatch_env_preamble_forwarded_via_build_ssh_argv(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange — peer with env_preamble; build_ssh_argv wraps in
        # `bash -c '<preamble> && <cmd>'`.
        _write_peer_config(fake_home, env_save_restore, env_preamble=_LMOD_PREAMBLE)
        # Act
        _act_dispatch(shim_bin, capsys, ssh_kwargs=_SK_OK)
        # Assert
        assert "module load Apptainer/1.3.3 && sac agents start" in " ".join(
            _ssh_invocations(shim_bin)[-1]
        )

    def test_dispatch_env_preamble_wrapper_uses_bash_lc(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange — bash -c wrapper is the explicit env_preamble shape.
        _write_peer_config(fake_home, env_save_restore, env_preamble=_LMOD_PREAMBLE)
        # Act
        _act_dispatch(shim_bin, capsys, ssh_kwargs=_SK_OK)
        # Assert
        assert any("bash -c" in tok for tok in _ssh_invocations(shim_bin)[-1])


# ---------------------------------------------------------------------------
# Bug 3 — TOFU policy: EVERY leg of a dispatch must carry ``-o
# StrictHostKeyChecking=accept-new``, so a first-touch peer (the most
# common dispatch failure mode on a freshly-configured cluster) does not
# silently rc-1 with no operator-actionable error. The spec handoff used
# to hand rsync its own ``-e ssh …`` string, which had to repeat the
# policy; it now goes through build_ssh_argv like everything else, so
# there is exactly one place the policy can be got wrong.
# ---------------------------------------------------------------------------


class TestDispatchStrictHostKeyChecking:
    def test_dispatch_ssh_argv_includes_accept_new_strict_host_key(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange
        _write_peer_config(fake_home, env_save_restore)
        # Act
        _act_dispatch(shim_bin, capsys, ssh_kwargs=_SK_OK)
        # Assert — the rendered ssh argv carries the TOFU policy.
        assert "StrictHostKeyChecking=accept-new" in " ".join(
            _ssh_invocations(shim_bin)[-1]
        )

    def test_every_dispatch_leg_carries_the_tofu_policy(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange — manifest read, transfer, verification and remote start.
        _write_peer_config(fake_home, env_save_restore)
        # Act
        _act_dispatch(shim_bin, capsys, ssh_kwargs=_SK_OK)
        # Assert
        assert all(
            "StrictHostKeyChecking=accept-new" in " ".join(argv)
            for argv in _ssh_invocations(shim_bin)
        )

    def test_the_spec_transfer_is_one_of_those_legs(
        self, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        """Guards the claim above from passing vacuously — if the transfer
        stopped going over ssh, `all(...)` would still be True."""
        # Arrange
        _write_peer_config(fake_home, env_save_restore)
        # Act
        _act_dispatch(shim_bin, capsys, ssh_kwargs=_SK_OK)
        # Assert
        assert _phase_count(shim_bin, "tar -C") == 1


# ---------------------------------------------------------------------------
# lookup_remote_peer + try_dispatch_remote: state.db-driven routing.
# ---------------------------------------------------------------------------


class TestLookupRemotePeer:
    def test_no_active_row_returns_none(self, pg_schema: str, fake_home, state_db, env_save_restore):
        # Arrange — fresh state.db with no instances row for "alpha".
        from scitex_agent_container._state.state_db import init_schema

        init_schema()
        # Act
        result = lookup_remote_peer("alpha")
        # Assert
        assert result is None

    def test_local_active_row_returns_none(self, pg_schema: str, fake_home, state_db, env_save_restore):
        # Arrange — write a row whose host matches the current_host (so
        # ``state_db._resolve_host`` will collapse to the same value).
        env_save_restore.set("SAC_HOST", "local-host-x")
        from scitex_agent_container._state.state_db import record_instance_start

        record_instance_start(name="alpha", host="local-host-x")
        # Act
        result = lookup_remote_peer("alpha")
        # Assert
        assert result is None

    def test_remote_active_row_returns_peer_and_row(
        self, pg_schema: str, fake_home, state_db, env_save_restore
    ):
        # Arrange — row's host differs from this run's current_host.
        env_save_restore.set("SAC_HOST", "lead-host")
        from scitex_agent_container._state.state_db import record_instance_start

        record_instance_start(name="alpha", host="peer-host", a2a_port=18888)
        # Act
        peer, row = lookup_remote_peer("alpha")  # type: ignore[misc]
        # Assert
        assert (peer, row["a2a_port"]) == ("peer-host", 18888)


class TestTryDispatchRemote:
    def _peers_with(self, *names):
        from scitex_agent_container._state.host_config import PeerSpec

        return {n: PeerSpec(name=n, ssh=n) for n in names}

    def test_no_active_row_returns_false(self, pg_schema: str, fake_home, state_db, env_save_restore):
        # Arrange — no row; caller proceeds local.
        from scitex_agent_container._state.state_db import init_schema

        init_schema()
        calls: list = []
        # Act
        dispatched = try_dispatch_remote(
            "ghost",
            "stop",
            self._peers_with("peer-host"),
            handler=lambda p, r, ps: calls.append((p, r)),
        )
        # Assert
        assert dispatched is False

    def test_remote_row_calls_handler_returns_true(
        self, pg_schema: str, fake_home, state_db, env_save_restore
    ):
        # Arrange
        env_save_restore.set("SAC_HOST", "lead-host")
        from scitex_agent_container._state.state_db import record_instance_start

        record_instance_start(name="alpha", host="peer-host", a2a_port=18888)
        calls: list = []
        # Act
        dispatched = try_dispatch_remote(
            "alpha",
            "stop",
            self._peers_with("peer-host"),
            handler=lambda p, r, ps: calls.append((p, r["a2a_port"])),
        )
        # Assert
        assert dispatched is True and calls == [("peer-host", 18888)]

    def test_remote_peer_not_in_peers_raises_runtime_error(
        self, pg_schema: str, fake_home, state_db, env_save_restore
    ):
        # Arrange — row points at a peer that the lead's config.yaml
        # does NOT define. Must surface, not silently skip.
        env_save_restore.set("SAC_HOST", "lead-host")
        from scitex_agent_container._state.state_db import record_instance_start

        record_instance_start(name="alpha", host="unknown-peer")

        # Act
        def _do() -> None:
            try_dispatch_remote(
                "alpha",
                "stop",
                self._peers_with("other-peer"),
                handler=lambda p, r, ps: None,
            )

        # Assert
        with pytest.raises(RuntimeError, match="NOT in"):
            _do()


# ---------------------------------------------------------------------------
# try_dispatch — concrete-host routing: local (no ssh) / remote (ssh argv) /
# unknown (fail loud). local + unknown reach no ssh; the remote path reuses
# the PATH-shim ssh + rsync (no live network) to assert the constructed argv.
# ``local_names`` is injected so routing is hermetic (no host_config read).
# ---------------------------------------------------------------------------


def _cfg_host(name: str, host) -> AgentConfig:
    """AgentConfig carrying a v3 ``spec.host`` pin (str / list / '')."""
    c = AgentConfig(name=name)
    c.hosts_spec = HostsSpec(host=host, hosts=[])
    return c


class TestTryDispatchClassification:
    def test_canonical_host_stays_local_and_skips_ssh(self, shim_bin, capsys):
        # Arrange — host == this machine; an ssh shim is present to prove it
        # is never invoked, and a peer map that would NOT rescue the name.
        _install_ssh_shim(shim_bin, stdout=_OK_JSON, exit=0)
        cfg = _cfg_host("alpha", "ywata-note-win")
        peers = {"peer-host": PeerSpec(name="peer-host", ssh="peer-host")}
        # Act
        out = try_dispatch(
            cfg,
            "ywata-note-win",
            peers,
            dry_run=False,
            force=False,
            local_names={"ywata-note-win"},
        )
        # Assert
        assert out is False and _ssh_invocations(shim_bin) == []

    def test_alias_of_self_that_is_also_a_peer_stays_local(self, shim_bin, capsys):
        # Arrange — the machine is ALSO a peer (ssh: localhost); an alias
        # spelling must resolve local, never ssh-dispatch to itself.
        _install_ssh_shim(shim_bin, stdout=_OK_JSON, exit=0)
        cfg = _cfg_host("alpha", "ywata-note-win")
        peers = {"ywata-note-win": PeerSpec(name="ywata-note-win", ssh="localhost")}
        # Act
        out = try_dispatch(
            cfg,
            "raw-short-name",
            peers,
            dry_run=False,
            force=False,
            local_names={"raw-short-name", "ywata-note-win"},
        )
        # Assert
        assert out is False and _ssh_invocations(shim_bin) == []

    def test_absent_host_stays_local(self, shim_bin, capsys):
        # Arrange — host: local / absent normalizes to '' upstream.
        _install_ssh_shim(shim_bin, stdout=_OK_JSON, exit=0)
        cfg = _cfg_host("alpha", "")
        peers = {"peer-host": PeerSpec(name="peer-host", ssh="peer-host")}
        # Act
        out = try_dispatch(
            cfg,
            "ywata-note-win",
            peers,
            dry_run=False,
            force=False,
            local_names={"ywata-note-win"},
        )
        # Assert
        assert out is False and _ssh_invocations(shim_bin) == []

    def test_unknown_host_raises_naming_the_registered_peers(self, capsys):
        # Arrange — host is a typo: neither this machine nor a peer key. It
        # must FAIL LOUD with the registered-peer list (operator directive
        # 2026-07-10), never silently start on the wrong machine.
        cfg = _cfg_host("alpha", "spartn-gpgpu")
        peers = {"peer-host": PeerSpec(name="peer-host", ssh="peer-host")}

        # Act
        def _do() -> None:
            try_dispatch(
                cfg,
                "ywata-note-win",
                peers,
                dry_run=False,
                force=False,
                local_names={"ywata-note-win"},
            )

        # Assert
        with pytest.raises(RuntimeError, match="peer-host"):
            _do()

    def test_unknown_host_never_dispatches_ssh(self, shim_bin, capsys):
        # Arrange — an ssh shim is present; the unknown path must not touch
        # it (negative-safety: an unknown host raises BEFORE any ssh; the
        # raise itself is asserted by the sibling test and only absorbed
        # here so this test's single assert stays the ssh log).
        _install_ssh_shim(shim_bin, stdout=_OK_JSON, exit=0)
        cfg = _cfg_host("alpha", "spartn-gpgpu")
        peers = {"peer-host": PeerSpec(name="peer-host", ssh="peer-host")}
        # Act
        try:
            try_dispatch(
                cfg,
                "ywata-note-win",
                peers,
                dry_run=False,
                force=False,
                local_names={"ywata-note-win"},
            )
        except RuntimeError:
            pass
        # Assert
        assert _ssh_invocations(shim_bin) == []

    def test_unknown_head_with_local_chain_tail_stays_local(self, shim_bin, capsys):
        # Arrange — fallback CHAIN whose tail names THIS machine: the
        # documented fallback-hosts semantics (singleton-skip accepts the
        # current host anywhere in the chain) must keep the local path
        # instead of failing loud on the dead head.
        _install_ssh_shim(shim_bin, stdout=_OK_JSON, exit=0)
        cfg = _cfg_host("alpha", ["dead-host", "ywata-note-win"])
        peers = {"peer-host": PeerSpec(name="peer-host", ssh="peer-host")}
        # Act
        out = try_dispatch(
            cfg,
            "ywata-note-win",
            peers,
            dry_run=False,
            force=False,
            local_names={"ywata-note-win"},
        )
        # Assert
        assert out is False

    def test_known_peer_dispatches_remote_with_expected_ssh_argv(
        self, pg_schema: str, spec_dir, shim_bin, state_db, fake_home, env_save_restore, capsys
    ):
        # Arrange — host is a known peer distinct from the caller; the PATH-shim
        # ssh stands in for the network across every phase of the handoff.
        # _dispatch_remote_start re-loads peers from the on-disk config for
        # build_ssh_argv, so register peer-host there too (real config.yaml).
        _write_peer_config(fake_home, env_save_restore, peer="peer-host")
        _install_ssh_shim(shim_bin, **_SK_OK)
        cfg = _cfg_host("alpha", "peer-host")
        peers = {"peer-host": PeerSpec(name="peer-host", ssh="peer-host")}
        # Act
        out = try_dispatch(
            cfg,
            "ywata-note-win",
            peers,
            dry_run=False,
            force=False,
            local_names={"ywata-note-win"},
        )
        # Assert — dispatched, and the LAST ssh argv runs the peer-side start
        # verb (the earlier calls are the manifest read, the transfer and the
        # post-transfer verification).
        ssh_calls = _ssh_invocations(shim_bin)
        assert out is True and ssh_calls[-1][-6:] == [
            "sac",
            "agents",
            "start",
            "alpha",
            "--no-redispatch",
            "--json",
        ]
