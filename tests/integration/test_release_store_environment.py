"""Exercise the release-only pre-import floor with hostile inherited libpq state."""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path


import pytest

from tests.integration import test_release_private_pg as pg_controls


@pytest.fixture()
def owned_pg16():
    yield from pg_controls.private_pg16.__wrapped__()


ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / ".github/ci/release-store-environment.sh"
FLOOR = "postgresql://sac_tests@127.0.0.1:1/tests_must_not_write_to_the_fleet_store"


def hostile_environment(tmp_path):
    env = {
        "PATH": os.defpath,
        "HOME": str(tmp_path),
        "PYTHONPATH": str(ROOT / "src") + os.pathsep + str(ROOT),
    }
    for prefix in ("", "APPTAINERENV_", "SINGULARITYENV_"):
        for key in (
            "SCITEX_STORE_DSN",
            "SAC_TEST_PG_DSN",
            "PGHOST",
            "PGPORT",
            "PGDATABASE",
            "PGUSER",
            "PGPASSWORD",
            "PGPASSFILE",
            "PGSERVICE",
            "PGSERVICEFILE",
            "PGOPTIONS",
        ):
            env[prefix + key] = "hostile-inherited-sentinel"
    return env


def child(tmp_path, program, *, context=""):
    path = tmp_path / "first_import.py"
    path.write_text(program)
    command = '. "$1"; sac_release_store_floor "$2"; ' + context + 'exec "$3" "$4"'
    return subprocess.run(
        [
            "bash",
            "-ec",
            command,
            "boundary",
            str(LIBRARY),
            str(tmp_path / "owned"),
            sys.executable,
            str(path),
        ],
        cwd=ROOT,
        env=hostile_environment(tmp_path),
        capture_output=True,
        text=True,
        timeout=25,
    )


def test_first_provider_import_sees_floor_and_owned_empty_passfile(tmp_path):
    # Arrange: deny every socket connection; this checks import/resolve, not SQL.
    program = """import json,os,stat,sys
from pathlib import Path
seen=[]
def audit(event,args):
 if event=='socket.connect':raise RuntimeError('network forbidden in readonly import proof')
 if event=='import' and args[0].startswith(('scitex_dev','scitex_cards','scitex_logging')):
  seen.append(os.environ['SCITEX_STORE_DSN'])
sys.addaudithook(audit)
from scitex_cards._store_target import resolve_store_target
resolved=str(resolve_store_target())
p=Path(os.environ['PGPASSFILE'])
assert p.is_file() and not p.is_symlink() and p.read_bytes()==b''
assert stat.S_IMODE(p.stat().st_mode)==0o600 and p.stat().st_uid==os.getuid()
assert not any('hostile-inherited-sentinel' in v for k,v in os.environ.items() if k.startswith(('PG','APPTAINERENV_PG','SINGULARITYENV_PG')))
assert all(k not in os.environ for k in ('SAC_TEST_PG_DSN','APPTAINERENV_SAC_TEST_PG_DSN','SINGULARITYENV_SAC_TEST_PG_DSN','SINGULARITYENV_SCITEX_STORE_DSN'))
print(json.dumps({'seen':seen,'resolved':resolved,'pgservice':os.environ.get('PGSERVICE'),'pgoptions':os.environ['PGOPTIONS']}))
"""
    # Act
    result = child(tmp_path, program)
    # Assert
    data = json.loads(result.stdout.splitlines()[-1]) if result.returncode == 0 else {}
    assert (
        result.returncode,
        bool(data.get("seen")),
        set(data.get("seen", [])),
        data.get("resolved"),
        data.get("pgservice"),
        data.get("pgoptions"),
    ) == (0, True, {FLOOR}, FLOOR, None, ""), result.stderr


def test_real_pytest_external_preflight_no_longer_observes_ambient_store(tmp_path):
    # Arrange: match the actual empty-directory plugin preflight outside rootdir.
    empty = tmp_path / "empty"
    empty.mkdir()
    program = """import os,sys
seen=[]
def audit(event,args):
 if event=='socket.connect':raise RuntimeError('network forbidden in plugin probe')
 if event=='import' and args[0].startswith(('scitex_dev','scitex_logging')):seen.append(os.environ['SCITEX_STORE_DSN'])
sys.addaudithook(audit)
import pytest
code=pytest.main(['--collect-only','-p','no:cacheprovider',%r])
assert code==5 and seen and all('tests_must_not_write_to_the_fleet_store' in value for value in seen)
print('EARLY-PLUGIN-FLOOR-OK')
""" % str(empty)
    # Act
    result = child(tmp_path, program)
    # Assert
    assert result.returncode == 0 and "EARLY-PLUGIN-FLOOR-OK" in result.stdout, (
        result.stderr
    )


def test_actual_owned_pg_fixture_can_override_floor_explicitly(tmp_path, owned_pg16):
    # Arrange: reuse the existing private cluster + real fixture, never ambient SQL.
    # Supply the Unix-socket fixture's actual port as part of its explicit owned context.
    context = (
        "export SAC_TEST_PG_REQUIRED=1 PGPORT=5432; export SAC_TEST_PG_DSN="
        + shlex.quote(owned_pg16)
        + "; "
    )
    program = (
        'import os\nassert "tests_must_not_write_to_the_fleet_store" in os.environ["SCITEX_STORE_DSN"]\n'
        + pg_controls.PROBE
    )
    # Act
    result = child(tmp_path, program, context=context)
    # Assert
    assert result.returncode == 0 and "PRIVATE-PG:42:16" in result.stdout, result.stderr


def test_release_floor_precedes_first_image_python_and_runtime_check():
    # Arrange
    source = (ROOT / ".github/ci/release-exec-in-sif.sh").read_text()
    # Act
    floor = source.index('sac_release_store_floor "$SAC_CI_TMPDIR_ROOT"')
    # Assert
    assert (
        floor
        < source.index("sac_release_runtime\n")
        < source.index('"/opt/venv-$V/bin/python"')
    )


def test_release_floor_refuses_aliased_scratch(tmp_path):
    # Arrange
    physical = tmp_path / "physical"
    physical.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(physical, target_is_directory=True)
    # Act
    result = subprocess.run(
        [
            "bash",
            "-ec",
            '. "$1"; sac_release_store_floor "$2"',
            "boundary",
            str(LIBRARY),
            str(alias),
        ],
        env=hostile_environment(tmp_path),
        capture_output=True,
        text=True,
        timeout=5,
    )
    # Assert
    assert result.returncode != 0 and not (physical / "postgres").exists()


def test_unfenced_child_reproduces_hostile_first_import_without_connecting(tmp_path):
    # Arrange: negative witness for the old preflight, with every socket denied.
    program = """import os,sys
seen=[]
def audit(event,args):
 if event=='socket.connect':raise RuntimeError('network forbidden')
 if event=='import' and args[0].startswith('scitex_cards'):seen.append(os.environ['SCITEX_STORE_DSN'])
sys.addaudithook(audit)
import scitex_cards._store_target
assert seen and set(seen)=={'hostile-inherited-sentinel'}
assert os.environ['PGSERVICE']=='hostile-inherited-sentinel'
print('UNFENCED-EXPOSURE-REPRODUCED')
"""
    # Act
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=ROOT,
        env=hostile_environment(tmp_path),
        capture_output=True,
        text=True,
        timeout=15,
    )
    # Assert
    assert result.returncode == 0 and "UNFENCED-EXPOSURE-REPRODUCED" in result.stdout, (
        result.stderr
    )
