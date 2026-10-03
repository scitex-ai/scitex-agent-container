"""Drive the real CI shell boundaries with owned synthetic files and children."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

_CI = Path(__file__).resolve().parents[2] / ".github" / "ci"


def _control_receipt(name: str, result: dict) -> None:
    directory = os.environ.get('SAC_CI_CONTROL_RECEIPTS_DIR')
    if directory:
        path = Path(directory)
        path.mkdir(mode=0o700, exist_ok=True)
        with (path / (name + '.json')).open('x') as stream:
            json.dump(result, stream, indent=2)
            stream.write('\n')


def _box(root: Path) -> tuple[dict[str, str], Path, Path]:
    checkout = root / "checkout"
    target = checkout / ".github" / "ci"
    target.mkdir(parents=True)
    for source in _CI.glob("*.sh"):
        (target / source.name).write_bytes(source.read_bytes())
    (target / "inner.sh").write_text("exit 0\n", encoding="utf-8")
    image = root / "ci.sif"
    image.write_bytes(b"owned synthetic image\n")
    scratch = root / "scratch"
    scratch.mkdir(mode=0o700)
    home = root / "home"
    home.mkdir()
    observed = root / "observed.json"
    recorder = root / "apptainer"
    recorder.write_text(
        f"#!{sys.executable}\nimport json,os,sys\n"
        f"with open({str(observed)!r},'w') as f: json.dump({{'argv':sys.argv[1:],'keys':sorted(os.environ)}},f)\n",
        encoding="utf-8",
    )
    recorder.chmod(0o700)
    env = {
        "PATH": "/usr/bin:/bin", "HOME": str(home), "LC_ALL": "C",
        "SCITEX_CI_APPTAINER": str(recorder), "SCITEX_CI_SIF": str(image),
        "SCITEX_CI_SIF_SHA256": hashlib.sha256(image.read_bytes()).hexdigest(),
        "SAC_CI_TMPDIR_ROOT": str(scratch), "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
    }
    return env, checkout, observed


def _exec(env: dict[str, str], checkout: Path, inner: str = "inner.sh"):
    return subprocess.run(
        ["/bin/bash", ".github/ci/exec-in-sif.sh", inner, "3.12"],
        cwd=checkout, env=env, text=True, capture_output=True, timeout=6,
    )


@pytest.mark.parametrize("digest", ["", "f" * 64, "../wrong"])
def test_image_digest_refuses_before_the_runtime(tmp_path: Path, digest: str):
    # Arrange
    env, checkout, observed = _box(tmp_path)
    env["SCITEX_CI_SIF_SHA256"] = digest
    # Act
    result = _exec(env, checkout)
    # Assert
    assert result.returncode != 0 and not observed.exists(), result.stdout + result.stderr


def test_absent_image_digest_refuses_before_the_runtime(tmp_path: Path):
    # Arrange
    env, checkout, observed = _box(tmp_path)
    env.pop('SCITEX_CI_SIF_SHA256')
    # Act
    result = _exec(env, checkout)
    # Assert
    assert result.returncode != 0 and not observed.exists(), result.stdout + result.stderr


def test_verified_non_test_driver_preserves_runtime_exit(tmp_path: Path):
    # Arrange
    env, checkout, observed = _box(tmp_path)
    # Act
    result = _exec(env, checkout)
    # Assert
    assert result.returncode == 0 and observed.exists(), result.stdout + result.stderr


def test_private_environment_is_removed_before_runtime(tmp_path: Path):
    # Arrange
    env, checkout, observed = _box(tmp_path)
    forbidden = {"SCITEX_STORE_DSN", "PGPASSWORD", "CODEX_HOME", "CCT_AGENT_ID", "PYTHONPATH", "APPTAINERENV_SCITEX_STORE_DSN"}
    env.update(dict.fromkeys(forbidden, "synthetic-private-sentinel"))
    # Act
    result = _exec(env, checkout)
    keys = set(json.loads(observed.read_text())["keys"]) if observed.exists() else set()
    # Assert
    assert result.returncode == 0 and not (keys & forbidden), result.stdout + result.stderr


def test_runtime_declares_clean_environment_and_private_home(tmp_path: Path):
    # Arrange
    env, checkout, observed = _box(tmp_path)
    # Act
    result = _exec(env, checkout)
    argv = json.loads(observed.read_text())["argv"] if observed.exists() else []
    # Assert
    assert result.returncode == 0 and "--cleanenv" in argv and "--no-home" in argv and "--home" in argv


def test_test_driver_requires_separately_declared_postgres(tmp_path: Path):
    # Arrange
    env, checkout, observed = _box(tmp_path)
    # Act
    result = _exec(env, checkout, "run-in-sif.sh")
    # Assert
    assert result.returncode != 0 and not observed.exists(), result.stdout + result.stderr


def test_unowned_old_directory_survives_actual_prune(tmp_path: Path):
    # Arrange
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    old = scratch / "ci-scitex_agent_container-987-1-3.12"
    old.mkdir()
    os.utime(old, (1, 1))
    env = {"PATH": "/usr/bin:/bin", "SAC_CI_TMPDIR_ROOT": str(scratch), "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}
    # Act
    result = subprocess.run(["/bin/bash", "-c", '. "$1"; ci_tmpdir_prune', "--", str(_CI / "tmpdir-lib.sh")], env=env, capture_output=True, text=True, timeout=6)
    # Assert
    assert result.returncode == 0 and old.is_dir(), result.stdout + result.stderr


def test_unowned_current_directory_refuses_cleanup(tmp_path: Path):
    # Arrange
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    current = scratch / "ci-scitex_agent_container-123-1-3.12"
    current.mkdir()
    env = {"PATH": "/usr/bin:/bin", "SAC_CI_TMPDIR_ROOT": str(scratch), "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}
    # Act
    result = subprocess.run(["/bin/bash", "-c", '. "$1"; ci_tmpdir_cleanup "$2"', "--", str(_CI / "tmpdir-lib.sh"), str(current)], env=env, capture_output=True, text=True, timeout=6)
    # Assert
    assert result.returncode != 0 and current.is_dir(), result.stdout + result.stderr


def _pg_box(root: Path, fault: str = ""):
    env, checkout, observed = _box(root)
    image = root / "pg.sif"
    image.write_bytes(b"distinct owned PG image\n")
    env.update({"SCITEX_CI_PG_SIF": str(image), "SCITEX_CI_PG_SIF_SHA256": hashlib.sha256(image.read_bytes()).hexdigest()})
    calls = root / "calls.jsonl"
    record = root / "data.txt"
    script = root / "apptainer"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json,os,pathlib,signal,sys,time\n"
        f"a=sys.argv[1:]; calls=pathlib.Path({str(calls)!r}); record=pathlib.Path({str(record)!r}); fault={fault!r}\n"
        "with calls.open('a') as f: f.write(json.dumps(a)+'\\n')\n"
        "if 'postgres' in a and '--version' in a:\n print('postgres (PostgreSQL) '+('17.6' if fault=='version' else '18.6')); sys.exit(0)\n"
        "if 'initdb' in a:\n d=a[a.index('-D')+1]; record.write_text(d); pathlib.Path(d).mkdir(mode=0o700); sys.exit(0)\n"
        "if 'postgres' in a and '-D' in a:\n while True: time.sleep(0.05)\n"
        "if 'pg_isready' in a: sys.exit(0)\n"
        "if 'psql' in a:\n"
        " q=a[a.index('-c')+1]\n"
        " if q.startswith('SELECT'):\n"
        "  d=record.read_text(); sid='7672112238472680366' if fault=='fleet' else '1234567890123456'\n"
        "  if fault=='path': d='/not/owned'\n"
        "  print('180006|'+d+'|'+sid+'|false|off|ci_tests')\n"
        " elif fault=='write': sys.exit(42)\n"
        " sys.exit(0)\n"
        "if '-I' in a: print('45678'); sys.exit(0)\n"
        f"pathlib.Path({str(observed)!r}).write_text(json.dumps({{'argv':a,'env':{{k:v for k,v in os.environ.items() if k.startswith('APPTAINERENV_SAC_TEST_PG')}}}}))\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    return env, checkout, observed, calls


@pytest.mark.parametrize("fault", ["version", "path", "fleet", "write"])
def test_pg_admission_failure_never_reaches_the_test_body(tmp_path: Path, fault: str):
    # Arrange
    env, checkout, observed, calls = _pg_box(tmp_path, fault)
    # Act
    result = _exec(env, checkout, "run-in-sif.sh")
    argv = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
    writes = [row for row in argv if "psql" in row and "BEGIN;" in row[-1]]
    # Assert
    assert (result.returncode != 0, observed.exists(), fault == 'write' or not writes) == (True, False, True), result.stdout + result.stderr


def test_owned_pg_admission_sets_required_only_after_identity_and_write(tmp_path: Path):
    # Arrange
    env, checkout, observed, calls = _pg_box(tmp_path)
    # Act
    result = _exec(env, checkout, "run-in-sif.sh")
    actual = json.loads(observed.read_text()) if observed.exists() else {}
    rows = [json.loads(line) for line in calls.read_text().splitlines()]
    select = next(i for i, row in enumerate(rows) if "psql" in row and row[-1].startswith("SELECT"))
    write = next(i for i, row in enumerate(rows) if "psql" in row and row[-1].startswith("BEGIN;"))
    # Assert
    assert (result.returncode, actual.get('env', {}).get('APPTAINERENV_SAC_TEST_PG_REQUIRED'), select < write < len(rows) - 1) == (0, '1', True), result.stdout + result.stderr


@pytest.mark.parametrize("name", ["run-in-sif.sh", "run-on-hosted.sh"])
def test_failed_full_dependency_install_has_no_reduced_or_pip_retry(tmp_path: Path, name: str):
    # Arrange
    source = (_CI / name).read_text()
    first = source.index("uv pip install", source.index('export PATH=') if name == 'run-in-sif.sh' else 0)
    last = source.index('export PYTHONPATH=', first) if name == 'run-in-sif.sh' else source.index('export PATH=', first)
    block = source[first:last]
    calls = tmp_path / "uv-args"
    executable = tmp_path / "uv"
    executable.write_text(f'#!/bin/bash\nprintf "%s\\n" "$*" >> {str(calls)!r}\nexit 42\n')
    executable.chmod(0o700)
    env = {"PATH": f"{tmp_path}:/usr/bin:/bin", "VENV": str(tmp_path / 'venv'), "PY": sys.executable, "TMPDIR": str(tmp_path)}
    # Act
    result = subprocess.run(['bash', '-c', 'set -euo pipefail\n' + block], env=env, capture_output=True, text=True, timeout=6)
    attempts = calls.read_text().splitlines()
    # Assert
    assert (result.returncode, len(attempts), '.[all,dev]' in attempts[0]) == (42, 1, True)


@pytest.mark.parametrize('name', ['run-in-sif.sh', 'run-on-hosted.sh'])
def test_both_direct_test_drivers_refuse_missing_pg_before_installer(tmp_path: Path, name: str):
    # Arrange
    calls = tmp_path / 'calls'
    executable = tmp_path / 'uv'
    executable.write_text(f'#!/bin/bash\necho called >> {str(calls)!r}\nexit 0\n')
    executable.chmod(0o700)
    env = {'PATH': f'{tmp_path}:/usr/bin:/bin', 'HOME': str(tmp_path)}
    # Act
    result = subprocess.run(['bash', str(_CI / name), '3.12'], env=env, capture_output=True, text=True, timeout=6)
    # Assert
    assert (result.returncode != 0, calls.exists(), 'qualified owned PG18 prerequisite' in result.stderr) == (True, False, True)


def test_declared_canonical_pg_identity_refuses_before_client_transport(tmp_path: Path):
    # Arrange
    env = {'PATH':'/usr/bin:/bin', 'SAC_TEST_PG_REQUIRED':'1', 'PGUSER':'ci_tests',
           'SAC_TEST_PG_DSN':'postgresql://127.0.0.1:54321/postgres', 'SAC_CI_PG_DATA':'/unread',
           'SAC_CI_PG_DATA_ID':'1:2', 'SAC_CI_PG_SYSTEM_ID':'7672112238472680366',
           'SAC_CI_PG_PID':'1', 'SAC_CI_PG_START':'1'}
    # Act
    result = subprocess.run(['bash', '-c', '. "$1"; ci_test_pg_require', '--', str(_CI/'sif-runtime-lib.sh')], env=env, capture_output=True, text=True, timeout=6)
    # Assert
    assert result.returncode != 0 and 'canonical' in result.stderr


def test_owned_live_group_survives_prune_even_when_old(tmp_path: Path):
    # Arrange
    root = tmp_path / 'scratch'
    root.mkdir(mode=0o700)
    target = root / 'ci-scitex_agent_container-999-1-3.12'
    env = {'PATH':'/usr/bin:/bin', 'SAC_CI_TMPDIR_ROOT':str(root), 'GITHUB_RUN_ID':'999', 'GITHUB_RUN_ATTEMPT':'1'}
    source = 'set -e; . "$1"; export SAC_CI_GROUP_PID=$BASHPID; ci_tmpdir_prepare "$2"; exec sleep 10'
    child = subprocess.Popen(['setsid','bash','-c',source,'--',str(_CI/'tmpdir-lib.sh'),str(target)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if (target/'.sac-ci-owner').exists():
                break
            time.sleep(.01)
        os.utime(target,(1,1))
        env['GITHUB_RUN_ID']='123'
        # Act
        result = subprocess.run(['bash','-c','. "$1"; ci_tmpdir_prune','--',str(_CI/'tmpdir-lib.sh')],env=env,capture_output=True,text=True,timeout=6)
        # Assert
        assert result.returncode == 0 and target.exists() and child.poll() is None
    finally:
        child.terminate()
        child.wait(timeout=2)


@pytest.mark.parametrize('status', [0, 42])
def test_child_status_survives_wrapper_cleanup(tmp_path: Path, status: int):
    # Arrange
    env, checkout, _ = _box(tmp_path)
    (tmp_path/'apptainer').write_text(f'#!/bin/bash\nexit {status}\n')
    # Act
    result = _exec(env, checkout)
    # Assert
    assert (result.returncode, list((tmp_path/'scratch').iterdir())) == (status, [])


def _birth(pid: int) -> str | None:
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()[19]
    except FileNotFoundError:
        return None


def _reap_owned(pid: int, birth: str) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            waited = 0
        if waited == pid or _birth(pid) != birth:
            return
        time.sleep(.01)
    raise AssertionError(f'owned fixture PID {pid} was not reaped')


@pytest.fixture
def owned_child_reaper():
    libc = ctypes.CDLL(None, use_errno=True)
    prior = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(prior), 0, 0, 0) != 0 or libc.prctl(36, 1, 0, 0, 0) != 0:
        raise RuntimeError('fixture subreaper state could not be enabled')
    try:
        yield
    finally:
        if libc.prctl(36, prior.value, 0, 0, 0) != 0:
            raise RuntimeError('fixture subreaper state could not be restored')


@pytest.mark.parametrize('signum', [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize('stubborn', [False, True])
def test_shell_only_signal_reaps_owned_grandchild_before_scratch_removal(
    tmp_path: Path, signum: int, stubborn: bool, owned_child_reaper,
):
    # Arrange: ordinary descendants remain in the exact owned group. There is
    # deliberately no escaped-process-group claim in this fixture.
    env, checkout, _ = _box(tmp_path)
    ready = tmp_path / 'descendants.json'
    removed_live = tmp_path / 'removed-while-live'
    removal = tmp_path / 'removal-witness.json'
    tools = tmp_path / 'tools'
    tools.mkdir()
    remover = tools / 'rm'
    remover.write_text(
        f'#!{sys.executable}\n'
        'import json,os,pathlib,sys\n'
        f'members=json.loads(pathlib.Path({str(ready)!r}).read_text())\n'
        + 'live=[]; dead=[]\n'
        + 'for label,(pid,birth) in members.items():\n'
        + ' try: fields=pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(") ",1)[1].split()\n'
        + ' except FileNotFoundError: continue\n'
        + ' if fields[19] == birth: (dead if fields[0] == "Z" else live).append(label)\n'
        + f'pathlib.Path({str(removal)!r}).write_text(json.dumps({{"live_members_before_rm":live,"dead_nonchild_before_rm":dead}}))\n'
        + 'os.execv("/bin/rm",["/bin/rm",*sys.argv[1:]])\n',
        encoding='utf-8',
    )
    remover.chmod(0o700)
    env['PATH'] = f'{tools}:/usr/bin:/bin'
    body = tmp_path / 'apptainer'
    body.write_text(
        f'#!{sys.executable}\n'
        'import json,os,pathlib,signal,time\n'
        'def birth(pid): return pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(") ",1)[1].split()[19]\n'
        'signal.signal(signal.SIGINT, signal.default_int_handler)\n'
        'child=os.fork()\n'
        'if child == 0:\n'
        f' if {stubborn!r}:\n'
        '  signal.signal(signal.SIGINT,signal.SIG_IGN); signal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
        ' while True:\n'
        f'  if not pathlib.Path(os.environ["HOME"]).exists(): pathlib.Path({str(removed_live)!r}).touch()\n'
        '  time.sleep(.005)\n'
        'else:\n'
        ' group=int(os.environ["APPTAINERENV_SAC_CI_GROUP_PID"])\n'
        f' pathlib.Path({str(ready)!r}).write_text(json.dumps({{"group":[group,birth(group)],"body":[os.getpid(),birth(os.getpid())],"grandchild":[child,birth(child)]}}))\n'
        ' while True: time.sleep(.05)\n',
        encoding='utf-8',
    )
    wrapper = subprocess.Popen(
        ['bash', '.github/ci/exec-in-sif.sh', 'inner.sh', '3.12'],
        cwd=checkout, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    unrelated = subprocess.Popen(['sleep', '30'], start_new_session=True)
    owned: dict[str, list[int | str]] = {}
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and wrapper.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        if not ready.exists():
            raise RuntimeError('owned descendant witness did not start')
        owned = json.loads(ready.read_text())
        wrapper_birth = _birth(wrapper.pid)
        # Act: signal the wrapper PID only, never its process group.
        started = time.monotonic()
        os.kill(wrapper.pid, signum)
        stdout, stderr = wrapper.communicate(timeout=6)
        # Assert: terminal status, bounded completion and independently reaped
        # ordinary descendants precede deletion; foreign processes survive.
        elapsed = time.monotonic() - started
        for pid, birth in owned.values():
            _reap_owned(int(pid), str(birth))
        actual = {
            'status': wrapper.returncode, 'bounded': elapsed < 5,
            'removed_while_live': removed_live.exists(),
            'scratch': list((tmp_path / 'scratch').iterdir()),
            'unrelated_status': unrelated.poll(),
            'all_fixture_pids_reaped': all(_birth(int(pid)) != str(birth) for pid, birth in owned.values()),
            'live_at_rm': json.loads(removal.read_text())['live_members_before_rm'] if removal.exists() else None,
        }
        _control_receipt(f'shell-only-{signum}-{stubborn}', {
            'source_sha256': hashlib.sha256((_CI / 'exec-in-sif.sh').read_bytes()).hexdigest(),
            'signal_target': 'wrapper PID only', 'signal': int(signum),
            'wrapper': [wrapper.pid, wrapper_birth], 'owned_fixture_members': owned,
            'elapsed_s': elapsed, 'actual': actual,
            'group_observation': stderr,
            'removal': json.loads(removal.read_text()) if removal.exists() else None,
            'driver_wait_owned_child': owned['group'],
            'fixture_adopted_reaping_is_separate': True, 'escaped_groups_unqualified': True,
        })
        assert actual == {
            'status': 130 if signum == signal.SIGINT else 143, 'bounded': True,
            'removed_while_live': False, 'scratch': [], 'unrelated_status': None,
            'all_fixture_pids_reaped': True,
            'live_at_rm': [],
        }, stdout + stderr
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait(timeout=2)
        for pid, birth in owned.values():
            if _birth(int(pid)) == str(birth):
                os.kill(int(pid), signal.SIGKILL)
                _reap_owned(int(pid), str(birth))
        unrelated.terminate()
        unrelated.wait(timeout=2)


def test_ordinary_success_with_live_descendant_refuses_and_retains_scratch(
    tmp_path: Path, owned_child_reaper,
):
    # Arrange
    env, checkout, _ = _box(tmp_path)
    record = tmp_path / 'retained-grandchild.json'
    (tmp_path / 'apptainer').write_text(
        f'#!{sys.executable}\n'
        'import json,os,pathlib,signal,time\n'
        'child=os.fork()\n'
        'if child == 0:\n'
        ' null=os.open(os.devnull,os.O_RDWR); os.dup2(null,1); os.dup2(null,2)\n'
        ' signal.signal(signal.SIGINT,signal.SIG_IGN); signal.signal(signal.SIGTERM,signal.SIG_IGN)\n'
        ' while True: time.sleep(.05)\n'
        'else:\n'
        ' birth=pathlib.Path(f"/proc/{child}/stat").read_text().rsplit(") ",1)[1].split()[19]\n'
        f' pathlib.Path({str(record)!r}).write_text(json.dumps([child,birth]))\n',
        encoding='utf-8',
    )
    owned = None
    try:
        # Act
        result = _exec(env, checkout)
        owned = json.loads(record.read_text()) if record.exists() else None
        # Assert
        actual = (
            result.returncode, owned is not None and _birth(int(owned[0])) == owned[1],
            bool(list((tmp_path / 'scratch').iterdir())), 'not quiescent' in result.stderr,
        )
        assert actual == (1, True, True, True), result.stdout + result.stderr
    finally:
        if owned is None and record.exists():
            owned = json.loads(record.read_text())
        if owned is not None and _birth(int(owned[0])) == owned[1]:
            os.kill(int(owned[0]), signal.SIGKILL)
            _reap_owned(int(owned[0]), owned[1])


@pytest.mark.parametrize('suite', ['matrix', 'nightly'])
def test_typed_suite_keeps_whole_tests_and_only_matrix_coverage(tmp_path: Path, suite: str):
    # Arrange: execute the actual final shell argv boundary, without installing
    # dependencies or substituting any pytest or PostgreSQL implementation.
    source = (_CI / 'run-in-sif.sh').read_text()
    block = source[source.index('PYTEST_ARGS='):]
    args = tmp_path / 'pytest-argv.json'
    executable = tmp_path / 'python'
    executable.write_text(
        f'#!{sys.executable}\nimport json,pathlib,sys\npathlib.Path({str(args)!r}).write_text(json.dumps(sys.argv[1:]))\n',
        encoding='utf-8',
    )
    executable.chmod(0o700)
    # Act
    result = subprocess.run(
        ['bash', '-c', 'set -euo pipefail\n' + block],
        env={'PATH': f'{tmp_path}:/usr/bin:/bin', 'WORKERS': '7', 'SUITE': suite},
        capture_output=True, text=True, timeout=6,
    )
    actual = json.loads(args.read_text()) if args.exists() else []
    # Assert
    receipt = (result.returncode, actual[:5], '--dist' in actual and '-p' in actual and 'no:cacheprovider' in actual, '--cov=src/scitex_agent_container' in actual)
    assert receipt == (0, ['-m', 'pytest', 'tests/', '-n', '7'], True, suite == 'matrix'), result.stdout + result.stderr


def test_unknown_suite_refuses_before_image_or_pg_execution(tmp_path: Path):
    # Arrange
    env, checkout, observed = _box(tmp_path)
    # Act
    result = subprocess.run(
        ['bash', '.github/ci/exec-in-sif.sh', 'run-in-sif.sh', '3.12', 'reduced'],
        cwd=checkout, env=env, capture_output=True, text=True, timeout=6,
    )
    # Assert
    assert (result.returncode != 0, observed.exists(), 'unknown test suite profile' in result.stderr) == (True, False, True)


@pytest.mark.parametrize(('key', 'value'), [
    ('SAC_TEST_PG_DSN', 'postgresql://127.0.0.1:1/postgres'),
    ('SAC_TEST_PG_DSN', 'postgresql://127.0.0.1:65536/postgres'),
    ('SAC_TEST_PG_DSN', 'postgresql://127.0.0.1:123456789012345/postgres'),
    ('SAC_CI_PG_PID', '0'),
    ('SAC_CI_PG_START', 'not-a-birth'),
    ('SAC_CI_PG_DATA_ID', 'unproven'),
    ('SAC_CI_PG_SYSTEM_ID', '0'),
    ('SAC_CI_PG_DATA', 'relative-data'),
])
def test_malformed_pg_prerequisite_refuses_before_client_or_installer(
    tmp_path: Path, key: str, value: str,
):
    # Arrange
    env = {
        'PATH': '/usr/bin:/bin', 'SAC_TEST_PG_REQUIRED': '1', 'PGUSER': 'ci_tests',
        'SAC_TEST_PG_DSN': 'postgresql://127.0.0.1:54321/postgres',
        'SAC_CI_PG_DATA': str(tmp_path / 'not-read'), 'SAC_CI_PG_DATA_ID': '1:2',
        'SAC_CI_PG_SYSTEM_ID': '1234567890123456', 'SAC_CI_PG_PID': '1', 'SAC_CI_PG_START': '1',
    }
    env[key] = value
    # Act
    result = subprocess.run(
        ['bash', '-c', '. "$1"; ci_test_pg_require', '--', str(_CI / 'sif-runtime-lib.sh')],
        env=env, capture_output=True, text=True, timeout=6,
    )
    # Assert
    assert result.returncode != 0 and 'metadata is malformed' in result.stderr


@pytest.mark.parametrize(('fault', 'message'), [
    ('inode', 'directory incarnation changed'),
    ('mode', 'directory ownership mismatch'),
    ('symlink', 'physical directory is unproven'),
    ('birth', 'process incarnation changed'),
])
def test_real_client_guard_refuses_changed_physical_proof_before_connection(
    tmp_path: Path, fault: str, message: str,
):
    # Arrange: real psycopg imports; no PostgreSQL, fake client, Store or server.
    data = tmp_path / 'owned-data'
    data.mkdir(mode=0o700)
    child = subprocess.Popen(['sleep', '20'])
    env = {
        'PATH': '/usr/bin:/bin', 'SAC_CI_PG_DATA': str(data),
        'SAC_CI_PG_DATA_ID': f'{data.stat().st_dev}:{data.stat().st_ino}',
        'SAC_CI_PG_PID': str(child.pid), 'SAC_CI_PG_START': _birth(child.pid),
        'SAC_CI_PG_SYSTEM_ID': '1234567890123456',
        'SAC_TEST_PG_DSN': 'postgresql://127.0.0.1:1/postgres',
    }
    try:
        _change_physical_proof(data, env, fault)
        # Act
        result = subprocess.run(
            ['bash', '-c', '. "$1"; ci_test_pg_verify "$2"', '--', str(_CI / 'sif-runtime-lib.sh'), sys.executable],
            env=env, capture_output=True, text=True, timeout=6,
        )
        # Assert
        assert result.returncode != 0 and message in result.stderr
    finally:
        child.terminate()
        child.wait(timeout=2)


def _change_physical_proof(data: Path, env: dict[str, str], fault: str) -> None:
    if fault == 'inode':
        env['SAC_CI_PG_DATA_ID'] = '0:0'
    elif fault == 'mode':
        data.chmod(0o755)
    elif fault == 'symlink':
        link = data.parent / 'copied-proof'
        link.symlink_to(data)
        env['SAC_CI_PG_DATA'] = str(link)
    else:
        env['SAC_CI_PG_START'] = '1'


def test_unknown_group_metadata_retains_live_group(tmp_path: Path):
    # Arrange: fail only the process metadata observation tool.
    observer = tmp_path / 'ps'
    observer.write_text('#!/bin/sh\nexit 42\n', encoding='utf-8')
    observer.chmod(0o700)
    child = subprocess.Popen(['sleep', '20'], start_new_session=True)
    try:
        # Act
        result = subprocess.run(
            ['bash', '-c', '. "$1"; ci_group_alive "$2"', '--', str(_CI / 'sif-runtime-lib.sh'), str(child.pid)],
            env={'PATH': f'{tmp_path}:/usr/bin:/bin'}, capture_output=True, text=True, timeout=6,
        )
        # Assert
        assert (result.returncode, 'process metadata unknown' in result.stderr, child.poll()) == (0, True, None)
    finally:
        child.terminate()
        child.wait(timeout=2)


def test_owned_inner_scratch_with_only_nonchild_zombie_is_quiescent(
    tmp_path: Path, owned_child_reaper,
):
    # Arrange: the actual producer owns this directory and process group.
    root = tmp_path / 'scratch'
    root.mkdir(mode=0o700)
    target = root / 'ci-scitex_agent_container-123-1-3.12'
    record = tmp_path / 'adopted-pid'
    env = {'PATH': '/usr/bin:/bin', 'SAC_CI_TMPDIR_ROOT': str(root), 'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1'}
    producer = subprocess.Popen(
        ['setsid', 'bash', '-c', '. "$1"; export SAC_CI_GROUP_PID=$BASHPID; ci_tmpdir_prepare "$2"; sleep .05 & printf "%s" "$!" > "$3"', '--', str(_CI / 'tmpdir-lib.sh'), str(target), str(record)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    pid, birth = None, None
    try:
        producer.wait(timeout=2)
        pid = int(record.read_text())
        birth = _birth(pid)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()[0]
            if state == 'Z':
                break
            time.sleep(.01)
        # Act: fixture reaping intentionally follows, not precedes, cleanup.
        result = subprocess.run(
            ['bash', '-c', '. "$1"; ci_tmpdir_cleanup "$2"', '--', str(_CI / 'tmpdir-lib.sh'), str(target)],
            env=env, capture_output=True, text=True, timeout=6,
        )
        # Assert
        assert (state, result.returncode, target.exists()) == ('Z', 0, False), result.stdout + result.stderr
    finally:
        if producer.poll() is None:
            producer.kill()
            producer.wait(timeout=2)
        if pid is not None and birth is not None:
            _reap_owned(pid, birth)
