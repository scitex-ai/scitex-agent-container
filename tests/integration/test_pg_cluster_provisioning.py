"""A newly provisioned card-store cluster must be CLOSED on the unix socket.

Incident 2026-08-11/12: ``scripts/pg/setup_cluster.sh``'s ancestor ran
``initdb --auth-local=trust``. Every agent container binds
``/home/ywatanabe``, the socket is ``srwxrwxrwx``, every agent runs as uid
1000, and the login role is the bootstrap SUPERUSER -- so any agent could
connect as any role with no password and drop the board. Measured open on
scitex-compute-01, -02, -03 and -04.

The initdb call is guarded by PGDATA absence, so fixing the running clusters
did NOT fix the script: every newly provisioned cluster was born with the hole
again. This test is what makes the one-word fix verifiable.

It is deliberately a REAL cluster, not a parse of the script text (STX-NM002,
no mocks): the failure mode is a libpq lookup-key rule that no amount of
reading the file can confirm. ``--auth-local=scram-sha-256`` alone would lock
every socket user out, so the suite asserts BOTH halves --

    no password over the socket  -> refused
    .pgpass over the socket      -> connects

-- plus a wrong-password mutation control, because a checker that cannot go
red proves nothing about the green.

AAA markers (TQ002); one assertion per test (TQ007); 3+-word names (TQ003).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SETUP = _REPO_ROOT / "scripts" / "pg" / "setup_cluster.sh"

# The SIF is a 157 MB build artifact, not a repo file; it lives beside the
# real cluster. Without it there is nothing to stand up.
_SIF = Path(os.environ.get("SCITEX_PG_SIF", Path.home() / ".scitex/pg/postgres18.sif"))
_FALLBACK_SIF = Path("/home/ywatanabe/.scitex/pg/postgres18.sif")
if not _SIF.is_file() and _FALLBACK_SIF.is_file():
    _SIF = _FALLBACK_SIF

# Deliberately NOT pytest.mark.integration: that marker is deselected by the
# default addopts and is documented for tests that spawn a real Claude Code
# agent. A security regression test that never runs is not a test. Gating is
# by CAPABILITY instead -- it runs wherever apptainer and the SIF exist, and
# skips cleanly everywhere else, exactly like tests/integration/
# test_sac_listen_health_probe.py gates on curl.
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        shutil.which("apptainer") is None, reason="provisioning needs apptainer"
    ),
    pytest.mark.skipif(not _SIF.is_file(), reason=f"no postgres SIF at {_SIF}"),
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _psql(pgpass, *args: str) -> subprocess.CompletedProcess:
    """Run psql in the container. PGPASSFILE is injected via APPTAINERENV_."""
    env = dict(os.environ)
    env["APPTAINERENV_PGPASSFILE"] = str(pgpass)
    env["APPTAINERENV_PGPASSWORD"] = ""
    return subprocess.run(
        ["apptainer", "exec", str(_SIF), "psql", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


@pytest.fixture(scope="module")
def throwaway_cluster(tmp_path_factory):
    """Provision + start a real cluster under a tmp dir; always tear it down."""
    tmp_path = tmp_path_factory.mktemp("pgprov")
    pgroot = tmp_path / "pg"
    pgroot.mkdir()
    pgpass = tmp_path / "pgpass"
    port = _free_port()
    role = "scitex_cards"

    env = dict(os.environ)
    env.update(
        SCITEX_PG_ROOT=str(pgroot),
        SCITEX_PG_SIF=str(_SIF),
        SCITEX_PG_PORT=str(port),
        SCITEX_PG_ROLE=role,
        SCITEX_PGPASS=str(pgpass),
        HOME=str(tmp_path),
    )
    setup = subprocess.run(
        ["bash", str(_SETUP)], capture_output=True, text=True, env=env, timeout=600
    )
    if setup.returncode != 0:
        pytest.fail(f"setup_cluster.sh failed:\n{setup.stdout}\n{setup.stderr}")

    pgdata = pgroot / "18" / "main"
    rundir = pgroot / "run"
    proc = subprocess.Popen(
        ["apptainer", "exec", str(_SIF), "postgres", "-D", str(pgdata)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    try:
        for _ in range(120):
            ready = subprocess.run(
                ["apptainer", "exec", str(_SIF), "pg_isready",
                 "-h", str(rundir), "-p", str(port), "-q"],
                capture_output=True, env=env, timeout=30,
            )
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail("throwaway cluster never accepted connections")
        yield {
            "rundir": rundir, "port": port, "role": role,
            "pgpass": pgpass, "pgdata": pgdata, "setup_output": setup.stdout,
        }
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()


def _conn_args(cluster) -> list[str]:
    return [
        "-h", str(cluster["rundir"]), "-p", str(cluster["port"]),
        "-U", cluster["role"], "-d", "postgres", "-w",
        "-tAc", "select current_user",
    ]


@pytest.fixture(scope="module")
def no_password_attempt(throwaway_cluster):
    """Socket connection offering NO credential at all."""
    empty = throwaway_cluster["pgpass"].parent / "empty.pgpass"
    empty.write_text("")
    empty.chmod(0o600)
    return _psql(empty, *_conn_args(throwaway_cluster))


@pytest.fixture(scope="module")
def pgpass_attempt(throwaway_cluster):
    """Socket connection using the .pgpass the script itself provisioned."""
    return _psql(throwaway_cluster["pgpass"], *_conn_args(throwaway_cluster))


@pytest.fixture(scope="module")
def wrong_password_attempt(throwaway_cluster):
    """Socket connection with a well-formed .pgpass carrying a wrong secret."""
    wrong = throwaway_cluster["pgpass"].parent / "wrong.pgpass"
    wrong.write_text(f"*:*:*:{throwaway_cluster['role']}:not-the-password\n")
    wrong.chmod(0o600)
    return _psql(wrong, *_conn_args(throwaway_cluster))


@pytest.fixture(scope="module")
def provisioned_pgpass_rows(throwaway_cluster):
    """The .pgpass rows split into fields. Field 5 is never inspected."""
    text = throwaway_cluster["pgpass"].read_text()
    return [ln.split(":") for ln in text.splitlines() if ln.strip()]


@pytest.fixture(scope="module")
def generated_local_rules(throwaway_cluster):
    """The non-comment ``local`` rules of the generated pg_hba.conf."""
    hba = (throwaway_cluster["pgdata"] / "pg_hba.conf").read_text()
    return [
        ln for ln in hba.splitlines()
        if ln.strip().startswith("local") and not ln.strip().startswith("#")
    ]


# --- the hole itself -------------------------------------------------------


def test_fresh_cluster_refuses_a_passwordless_socket_connection(no_password_attempt):
    # Arrange
    attempt = no_password_attempt
    # Act
    connected = attempt.returncode == 0
    # Assert
    assert not connected, (
        "a freshly provisioned cluster accepted a NO-PASSWORD socket "
        f"connection -- the trust hole is back. stdout={attempt.stdout!r}"
    )


def test_passwordless_socket_refusal_demands_a_password(no_password_attempt):
    # Arrange
    attempt = no_password_attempt
    # Act
    reason = attempt.stderr.lower()
    # Assert
    assert "no password supplied" in reason, attempt.stderr


# --- the half that a bare --auth-local=scram-sha-256 would break ------------


def test_fresh_cluster_accepts_a_pgpass_socket_connection(pgpass_attempt):
    # Arrange
    attempt = pgpass_attempt
    # Act
    connected = attempt.returncode == 0
    # Assert
    assert connected, (
        "the script closed the socket but did not provision a usable .pgpass "
        f"entry, locking socket users out. stderr={attempt.stderr!r}"
    )


def test_pgpass_socket_connection_authenticates_as_the_role(
    pgpass_attempt, throwaway_cluster
):
    # Arrange
    attempt = pgpass_attempt
    # Act
    who = attempt.stdout.strip()
    # Assert
    assert who == throwaway_cluster["role"], attempt.stderr


# --- mutation control: the guard must VALIDATE, not merely demand -----------


def test_fresh_cluster_rejects_a_wrong_socket_password(wrong_password_attempt):
    # Arrange
    attempt = wrong_password_attempt
    # Act
    connected = attempt.returncode == 0
    # Assert
    assert not connected, "a wrong password was accepted over the socket"


def test_wrong_socket_password_fails_authentication(wrong_password_attempt):
    # Arrange
    attempt = wrong_password_attempt
    # Act
    reason = attempt.stderr.lower()
    # Assert
    assert "password authentication failed" in reason, attempt.stderr


# --- the two .pgpass traps that broke compute-04 and compute-03 -------------


def test_provisioned_pgpass_is_keyed_on_the_literal_socket_path(
    provisioned_pgpass_rows, throwaway_cluster
):
    # Arrange: libpq rewrites a socket host to "localhost" ONLY when the socket
    # dir is its compiled-in default. This cluster's is not, so the LITERAL
    # path must be present -- a localhost-only line pre-flights fine over TCP
    # and still fails on the socket (scitex-compute-04, 2026-08-11).
    keys = {row[0] for row in provisioned_pgpass_rows}
    # Act
    socket_key = str(throwaway_cluster["rundir"])
    # Assert
    assert socket_key in keys, f"no .pgpass entry keyed on {socket_key}; got {keys}"


def test_provisioned_pgpass_file_is_owner_only_readable(throwaway_cluster):
    # Arrange
    pgpass = throwaway_cluster["pgpass"]
    # Act
    mode = oct(pgpass.stat().st_mode)[-3:]
    # Assert
    assert mode == "600", f".pgpass must be 0600, got {mode} -- libpq ignores it"


def test_socket_keyed_pgpass_entries_use_a_wildcard_database(
    provisioned_pgpass_rows, throwaway_cluster
):
    # Arrange: a db-specific entry does not cover a connection to another db.
    # scitex-compute-03 carried db=scitex_cards lines and the admin path to
    # db=postgres failed with "no password supplied".
    socket_key = str(throwaway_cluster["rundir"])
    # Act
    databases = {row[2] for row in provisioned_pgpass_rows if row[0] == socket_key}
    # Assert
    assert databases == {"*"}, (
        f"socket .pgpass rows must use a wildcard database, got {databases}"
    )


# --- the script must refuse to hand back an open cluster -------------------


def test_generated_pg_hba_declares_local_rules(generated_local_rules):
    # Arrange
    rules = generated_local_rules
    # Act
    count = len(rules)
    # Assert
    assert count > 0, "no local rules at all in the generated pg_hba.conf"


def test_generated_pg_hba_has_no_local_trust_rule(generated_local_rules):
    # Arrange
    rules = generated_local_rules
    # Act
    trusting = [ln for ln in rules if ln.split()[-1] == "trust"]
    # Assert
    assert not trusting, f"a 'local ... trust' rule survived provisioning: {trusting}"


def test_setup_script_reports_completion(throwaway_cluster):
    # Arrange
    output = throwaway_cluster["setup_output"]
    # Act
    finished = "SETUP: done" in output
    # Assert
    assert finished, output
