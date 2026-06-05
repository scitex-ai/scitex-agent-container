"""Listen-side ``agents_start`` handler shells the canonical CLI argv.

Regression test for the singular-vs-plural bug exposed by the
SAC-from-SAC live test (operator-mandated 2026-06-01): the handler in
:mod:`_listen._agent_exec` used to shell ``["sac", "agent", "start",
name]`` (singular ``agent``), but the F-CS13 rename removed the
singular group — the host's ``sac`` binary now responds with
``Error: No such command 'agent'.`` and exits non-zero. That single
character broke every brokered spawn end-to-end.

The fix is one token (``agent`` → ``agents``). This test pins the argv
shape so the regression cannot return silently: a future refactor that
flips the form back to singular will fail here, not at 3am when an
in-SIF agent tries to spawn a child and the host listen returns 502.

NO MOCKS — uses the :func:`subprocess_shim` helper from the package
conftest. A real fake ``sac`` binary is dropped on ``$PATH``; the
handler's real ``shutil.which("sac")`` resolves to the shim; the shim
records its argv; the test reads the argv back. This is the same
no-mocks pattern :mod:`tests/scitex_agent_container/cli_pkg/lifecycle`
already uses.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._state import registry as _reg
from scitex_agent_container._state import state_db

_TOKEN = "test-token-agent-exec"


@pytest.fixture
def isolated_listen_env(tmp_path: Path):
    """Isolated state.db + registry/runtime dirs (mirrors test__acl.py shape)."""
    db = tmp_path / "state.db"
    saved_env_db = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_default_db = state_db.DEFAULT_DB_PATH
    saved_home = os.environ.get("HOME")
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    state_db.DEFAULT_DB_PATH = db
    state_db.init_schema(db)
    os.environ["HOME"] = str(tmp_path)
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"
    try:
        yield tmp_path
    finally:
        state_db.DEFAULT_DB_PATH = saved_default_db
        _reg.REGISTRY_DIR = saved_reg_const
        _ss.DEFAULT_STATE_ROOT = saved_state_const
        if saved_env_db is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_env_db
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home


def test_agents_start_shells_plural_agents_form(
    isolated_listen_env, env_save_restore, subprocess_shim
) -> None:
    """The handler MUST shell ``["sac", "agents", "start", <name>]``.

    The singular ``"agent"`` form was removed in F-CS13; using it here
    makes the host's CLI exit non-zero with "No such command 'agent'",
    which manifests as a 502 from every brokered SAC-from-SAC spawn.
    """
    # Arrange — drop a fake ``sac`` on PATH that records its argv.
    # Disable the post-ack liveness probe (PR7) for this test — the
    # shim doesn't simulate the apptainer-pid write; the probe's
    # contract is asserted by the dedicated probe tests below.
    env_save_restore.set("SAC_LISTEN_POST_ACK_LIVENESS_TIMEOUT_S", "0")
    subprocess_shim.install("sac", stdout="ok", exit=0)
    app = create_app(token=_TOKEN)
    # Act — admin spawn (no caller) so the gate trivially allows and we
    # reach the subprocess shell-out. Body is the minimum the handler
    # accepts.
    with TestClient(app) as client:
        client.post(
            "/agents",
            json={"name": "broker-child"},
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
    argv = subprocess_shim.argv_for("sac")
    # Assert — the canonical PLURAL form. A regression to singular
    # (``"agent"``) would fail here loudly.
    assert argv == ["agents", "start", "broker-child"], argv


def test_agents_start_does_not_use_singular_agent_form(
    isolated_listen_env, env_save_restore, subprocess_shim
) -> None:
    """Explicit negative — the buggy singular form must NOT recur."""
    # Arrange
    env_save_restore.set("SAC_LISTEN_POST_ACK_LIVENESS_TIMEOUT_S", "0")
    subprocess_shim.install("sac", stdout="ok", exit=0)
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        client.post(
            "/agents",
            json={"name": "broker-child"},
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
    argv = subprocess_shim.argv_for("sac")
    # Assert — single-token regression guard.
    assert argv is not None and argv[0] != "agent", argv


# ---------------------------------------------------------------------------
# --broker-self recursive re-entry fix (clew dogfood repro 2026-06-06,
# lead msg d8f61055): when the listen runs inside a parent SAC's SIF on
# a SLURM allocation, the child `sac agents start` it execs inherited
# APPTAINER_CONTAINER / SINGULARITY_CONTAINER from the listen env,
# is_in_sif() returned True on the child, and maybe_broker_in_sif_spawn
# re-entered → recursive InSifBrokerError loop. The handler MUST strip
# those env markers before exec'ing the child so the child takes the
# bare-host path (direct apptainer-exec the sibling SIF).
#
# Test seam: a custom env-recording sac shim writes the child's
# os.environ snapshot for the two markers to a sibling log file; the
# test reads it back and asserts neither marker survived.
# ---------------------------------------------------------------------------


def _install_env_recording_sac_shim(bin_dir: Path) -> Path:
    """Like subprocess_shim's apptainer shim but ALSO records the env.

    Writes ``<bin_dir>/sac.env.jsonl`` with one JSON object per
    invocation, ``{"APPTAINER_CONTAINER": <value-or-None>,
    "SINGULARITY_CONTAINER": <value-or-None>}``. Returns the path to
    the env log file.
    """
    import json
    import sys

    env_log = bin_dir / "sac.env.jsonl"
    script = bin_dir / "sac"
    body = (
        f"#!{sys.executable}\n"
        "import json, os\n"
        f"with open({json.dumps(str(env_log))}, 'a') as fh:\n"
        "    fh.write(json.dumps({\n"
        "        'APPTAINER_CONTAINER': os.environ.get('APPTAINER_CONTAINER'),\n"
        "        'SINGULARITY_CONTAINER': os.environ.get('SINGULARITY_CONTAINER'),\n"
        "    }) + '\\n')\n"
        "import sys; sys.exit(0)\n"
    )
    script.write_text(body)
    script.chmod(0o755)
    return env_log


def test_agents_start_strips_apptainer_container_env_from_child(
    isolated_listen_env, env_save_restore, tmp_path: Path
) -> None:
    """Pin the recursive-re-entry fix for APPTAINER_CONTAINER."""
    # Arrange — parent has APPTAINER_CONTAINER set (as the listen
    # process would inside the parent SAC's SIF on a SLURM allocation);
    # install a sac shim that records the CHILD subprocess's env.
    import json

    bin_dir = tmp_path / "sac_env_shim_bin"
    bin_dir.mkdir()
    env_log = _install_env_recording_sac_shim(bin_dir)
    env_save_restore.set("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # Disable the PR7 post-ack liveness probe — these tests assert
    # env-strip semantics, not apptainer-instance health.
    env_save_restore.set("SAC_LISTEN_POST_ACK_LIVENESS_TIMEOUT_S", "0")
    env_save_restore.set("APPTAINER_CONTAINER", "/path/to/parent-sac.sif")
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        client.post(
            "/agents",
            json={"name": "broker-child"},
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
    recorded = json.loads(env_log.read_text().splitlines()[-1])
    # Assert — the marker must be stripped from the child's env;
    # otherwise is_in_sif() returns True downstream and we hit the
    # recursive InSifBrokerError loop clew reproduced on Spartan.
    assert recorded["APPTAINER_CONTAINER"] is None


def test_agents_start_strips_singularity_container_env_from_child(
    isolated_listen_env, env_save_restore, tmp_path: Path
) -> None:
    """Pin the recursive-re-entry fix for SINGULARITY_CONTAINER.

    Clew's actual repro on Spartan jobid 25666081 hit this variant
    (the legacy Singularity-named env var, set by operator bashrc /
    SLURM prologue / generate.py._default_sif_path hint). Pinned
    separately from APPTAINER_CONTAINER so a future refactor that
    only strips one of the two trips a red test.
    """
    # Arrange
    import json

    bin_dir = tmp_path / "sac_env_shim_bin2"
    bin_dir.mkdir()
    env_log = _install_env_recording_sac_shim(bin_dir)
    env_save_restore.set("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # Disable the PR7 post-ack liveness probe — these tests assert
    # env-strip semantics, not apptainer-instance health.
    env_save_restore.set("SAC_LISTEN_POST_ACK_LIVENESS_TIMEOUT_S", "0")
    env_save_restore.set("SINGULARITY_CONTAINER", "/path/to/parent-sac.sif")
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        client.post(
            "/agents",
            json={"name": "broker-child"},
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
    recorded = json.loads(env_log.read_text().splitlines()[-1])
    # Assert
    assert recorded["SINGULARITY_CONTAINER"] is None


def test_agents_start_preserves_unrelated_env_vars_to_child(
    isolated_listen_env, env_save_restore, tmp_path: Path
) -> None:
    """The strip MUST be surgical — only the two in-SIF markers.

    Stripping everything else would orphan downstream env-dependent
    behavior (credentials, account routing, channel bearer, etc.).
    Pin one canary unrelated var to guard against an over-broad
    fix (e.g. accidentally passing env={} or os.environ.clear()).
    """
    # Arrange
    import json

    bin_dir = tmp_path / "sac_env_shim_bin3"
    bin_dir.mkdir()
    env_log = bin_dir / "sac.canary.jsonl"
    script = bin_dir / "sac"
    body = (
        f"#!{__import__('sys').executable}\n"
        "import json, os\n"
        f"with open({__import__('json').dumps(str(env_log))}, 'a') as fh:\n"
        "    fh.write(json.dumps({\n"
        "        'CANARY': os.environ.get('SAC_BROKER_SELF_TEST_CANARY'),\n"
        "    }) + '\\n')\n"
        "import sys; sys.exit(0)\n"
    )
    script.write_text(body)
    script.chmod(0o755)
    env_save_restore.set("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    # Disable the PR7 post-ack liveness probe — these tests assert
    # env-strip semantics, not apptainer-instance health.
    env_save_restore.set("SAC_LISTEN_POST_ACK_LIVENESS_TIMEOUT_S", "0")
    env_save_restore.set("SAC_BROKER_SELF_TEST_CANARY", "must-survive-strip")
    env_save_restore.set("APPTAINER_CONTAINER", "/parent.sif")
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        client.post(
            "/agents",
            json={"name": "broker-child"},
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
    recorded = json.loads(env_log.read_text().splitlines()[-1])
    # Assert
    assert recorded["CANARY"] == "must-survive-strip"


# ---------------------------------------------------------------------------
# Layer-3 fail-loud post-spawn-ack liveness probe (PR7, clew dogfood
# repro 2026-06-06, lead msg 57f1632a). The listen's /agents handler
# was returning 200 "SUCC: started" on `sac agents start` rc=0 without
# verifying the apptainer instance actually came up; clew's repro on
# Spartan jobid 25666081 saw the listen ack the spawn, then the
# instance died silently (empty stdout.log, dead apptainer_pid, no
# fresh STARTUP_FAILED). The probe (waits up to N seconds for an
# alive apptainer_pid) makes the silent death LOUD: STARTUP_FAILED
# marker + 502 response.
#
# Each test installs a custom sac shim that simulates one of the three
# branches (no pid file written / pid file written but pid dead / pid
# file written and pid alive) so the probe's contract is exercised
# end-to-end against a real subprocess + real handler + real marker
# write. No MagicMock.
# ---------------------------------------------------------------------------


def _install_sac_shim_writing_pid_file(
    bin_dir: Path, *, runtime_dir: Path, pid_value: int | None
) -> None:
    """Install a sac shim that writes ``<runtime_dir>/apptainer_pid``.

    ``pid_value=None`` → don't write the file (simulates the "child
    never reached apptainer runtime" branch). Otherwise write the int.
    ``runtime_dir`` is created before the file is written so the shim
    can run on a fresh isolated_listen_env.
    """
    import json as _json
    import sys as _sys

    script = bin_dir / "sac"
    if pid_value is None:
        body = f"#!{_sys.executable}\nimport sys\nsys.exit(0)\n"
    else:
        runtime_dir_s = _json.dumps(str(runtime_dir))
        body = (
            f"#!{_sys.executable}\n"
            "import pathlib, sys\n"
            f"rd = pathlib.Path({runtime_dir_s})\n"
            "rd.mkdir(parents=True, exist_ok=True)\n"
            f"(rd / 'apptainer_pid').write_text('{int(pid_value)}\\n')\n"
            "sys.exit(0)\n"
        )
    script.write_text(body)
    script.chmod(0o755)


def test_agents_start_writes_startup_failed_when_no_apptainer_pid(
    isolated_listen_env, env_save_restore, tmp_path: Path
) -> None:
    """Probe must fail loud when the child returned rc=0 without writing pid."""
    # Arrange — shim returns 0 but never touches the runtime_dir.
    bin_dir = tmp_path / "sac_bin_no_pid"
    bin_dir.mkdir()
    _install_sac_shim_writing_pid_file(
        bin_dir, runtime_dir=tmp_path / "unused", pid_value=None
    )
    env_save_restore.set("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    env_save_restore.set("SAC_LISTEN_POST_ACK_LIVENESS_TIMEOUT_S", "0.5")
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        client.post(
            "/agents",
            json={"name": "broker-child"},
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
    from scitex_agent_container._lifecycle._startup_failed import read_marker
    from scitex_agent_container._runners._session_state import state_dir_for

    marker = read_marker(state_dir_for("broker-child"))
    # Assert
    assert marker is not None and marker["kind"] == "post_ack_no_apptainer_pid"


def test_agents_start_writes_startup_failed_when_apptainer_pid_dead(
    isolated_listen_env, env_save_restore, tmp_path: Path
) -> None:
    """Probe must fail loud when the pid file points at a reaped pid."""
    # Arrange — shim writes apptainer_pid with a pid that is definitely
    # not alive (a very high value the kernel won't have allocated to
    # any running process in this test session).
    from scitex_agent_container._runners._session_state import state_dir_for

    runtime_dir = state_dir_for("broker-child")
    bin_dir = tmp_path / "sac_bin_dead_pid"
    bin_dir.mkdir()
    _install_sac_shim_writing_pid_file(
        bin_dir, runtime_dir=runtime_dir, pid_value=2147483646
    )
    env_save_restore.set("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    env_save_restore.set("SAC_LISTEN_POST_ACK_LIVENESS_TIMEOUT_S", "0.5")
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        client.post(
            "/agents",
            json={"name": "broker-child"},
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
    from scitex_agent_container._lifecycle._startup_failed import read_marker

    marker = read_marker(runtime_dir)
    # Assert
    assert marker is not None and marker["kind"] == "post_ack_apptainer_pid_dead"


def test_agents_start_returns_200_when_apptainer_pid_is_alive(
    isolated_listen_env, env_save_restore, tmp_path: Path
) -> None:
    """Probe happy path — alive apptainer_pid → 200, no STARTUP_FAILED."""
    # Arrange — shim writes apptainer_pid pointing at the pytest
    # process pid (alive for the entire test session).
    from scitex_agent_container._runners._session_state import state_dir_for

    runtime_dir = state_dir_for("broker-child")
    bin_dir = tmp_path / "sac_bin_live_pid"
    bin_dir.mkdir()
    _install_sac_shim_writing_pid_file(
        bin_dir, runtime_dir=runtime_dir, pid_value=os.getpid()
    )
    env_save_restore.set("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    env_save_restore.set("SAC_LISTEN_POST_ACK_LIVENESS_TIMEOUT_S", "2.0")
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        resp = client.post(
            "/agents",
            json={"name": "broker-child"},
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
    # Assert
    assert resp.status_code == 200


def test_agents_start_post_ack_failure_returns_502(
    isolated_listen_env, env_save_restore, tmp_path: Path
) -> None:
    """The loud signal: a post-ack failure must downgrade the response.

    The pre-PR7 behaviour returned 200 with "SUCC: started" even when
    the apptainer instance was stillborn — clew's Spartan repro hit
    this. Pin the new contract: failure → 502 so the operator-side
    recv path sees a real diagnostic.
    """
    # Arrange
    bin_dir = tmp_path / "sac_bin_502"
    bin_dir.mkdir()
    _install_sac_shim_writing_pid_file(
        bin_dir, runtime_dir=tmp_path / "unused", pid_value=None
    )
    env_save_restore.set("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    env_save_restore.set("SAC_LISTEN_POST_ACK_LIVENESS_TIMEOUT_S", "0.5")
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        resp = client.post(
            "/agents",
            json={"name": "broker-child"},
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
    # Assert
    assert resp.status_code == 502
