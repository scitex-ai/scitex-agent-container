"""LIVE: a sealed Claude/Codex/OpenCode pane's agent tree runs under the mask.

Skipped unless ``SAC_SANDBOX_TEST_SIF`` and ``SAC_SANDBOX_TEST_ROOT`` are
set (see test_sandbox_apptainer_live.py). Drives the PRODUCTION pieces:
``begin_run`` (copies the mask launcher into the run's read-only bin dir),
``sandbox_flags`` + ``netgw_wrap`` and :func:`sealed_tui_inner_argv` — the
exact inner command ``build_inner_argv`` gives a sealed pane — with a probe
script in the harness's place and a probe ``startup_command``.

Direct pane (Claude Code / Codex shape): the startup command and the
"harness" both run inside one mask. Owner pane (Hermes / OpenCode shape):
the startup command runs in its own masked shell, the owner outside it
still sees ``/state/<name>``, and a mask that cannot hold refuses (125).
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import scitex_agent_container
from scitex_agent_container._sandbox import (
    begin_run,
    end_run,
    netgw_wrap,
    parse_sandbox,
    sandbox_flags,
    supervised_argv,
)
from scitex_agent_container._sandbox._mask import masked_argv, sealed_tui_inner_argv
from scitex_agent_container._sandbox._supervisor import main as supervise
from scitex_agent_container.config._types import StartupCommand
from scitex_agent_container.runtimes._apptainer_inner_argv import (
    _GIT_ENV_ALIAS_STEPS,
    _format_shell_steps,
)

SIF = os.environ.get("SAC_SANDBOX_TEST_SIF", "")
ROOT = os.environ.get("SAC_SANDBOX_TEST_ROOT", "")
pytestmark = pytest.mark.skipif(
    not (SIF and ROOT and Path(SIF).is_file() and shutil.which("apptainer")),
    reason="live sealed-run test: set SAC_SANDBOX_TEST_SIF and SAC_SANDBOX_TEST_ROOT",
)

NAME = "sealed-pane-live"
STATE = f"/state/{NAME}"
REPORT = "/home/agent/mask-report.txt"
SAC_PREFIX = Path(sys.prefix)
SAC_SRC = Path(scitex_agent_container.__file__).resolve().parents[1]

STARTUP = f"""echo "STARTUP_STATE_LS=[$(ls -A {STATE} 2>&1 | tr '\\n' ' ')]" >> {REPORT}
export STARTUP_EXPORT=reached-harness"""

PANE_PROBE = f"""
exec >> {REPORT} 2>&1
echo "STATE_LS=[$(ls -A {STATE} 2>&1 | tr '\\n' ' ')]"
echo "TOKEN_READ=$(cat {STATE}/secret-env/agent.token >/dev/null 2>&1 && echo yes || echo no)"
echo "SECRET_READ=$(cat {STATE}/home.env >/dev/null 2>&1 && echo yes || echo no)"
echo "SAC_ON_PATH=[$(command -v sac)]"
echo "SAC_RUN=$(/opt/venv-sac/bin/sac --version >/dev/null 2>&1 && echo yes || echo no)"
echo "SAC_LAUNCHING_RUN=$({SAC_PREFIX}/bin/sac --version >/dev/null 2>&1 && echo yes || echo no)"
echo "SAC_IMPORT=$(python3 -c 'import scitex_agent_container.cli' >/dev/null 2>&1 && echo yes || echo no)"
echo "SAC_VERSION=$(python3 -c 'from scitex_agent_container import __version__' >/dev/null 2>&1 && echo yes || echo no)"
echo "SAC_HIDE_IMPORT=$(python3 -c 'import scitex_agent_container._sandbox._hide' >/dev/null 2>&1 && echo yes || echo no)"
echo "SAC_SRC_LS=[$(ls -A {SAC_SRC}/scitex_agent_container 2>&1 | head -1)]"
echo "SOCK_LS=[$(ls -A /run/sac-sandbox/sock 2>&1 | tr '\\n' ' ')]"
echo "ALLOWED=$(curl -sS -m 20 -o /dev/null -w '%{{http_code}}' https://pypi.org/simple/ 2>&1)"
for h in claude opencode; do
  command -v $h >/dev/null && echo "HARNESS_$h=$($h --version >/dev/null 2>&1 && echo runs || echo fails)"
done
echo "OUTER_PROCS=$(grep -l -a 'netg[w]' /proc/[0-9]*/cmdline 2>/dev/null | wc -l)"
echo "PIDS=$(ls /proc | grep -c '^[0-9]')"
echo "UNMOUNT=$(umount {STATE} >/dev/null 2>&1 && echo yes || echo no)"
echo "NESTED_UNMOUNT=$(unshare --user --map-root-user --mount sh -c 'umount {STATE} && ls -A {STATE}' >/dev/null 2>&1 && echo yes || echo no)"
echo "CAPS=$(awk '/^CapEff/{{print $2}}' /proc/self/status)"
echo "HOME_LS=$(test -r /home/agent/mask-report.txt && echo yes || echo no)"
echo "STARTUP_EXPORT=$STARTUP_EXPORT"
exit 7
"""

#: Runs OUTSIDE the mask in the owner shape (as an owner would).
OWNER_PROBE = f"""
echo "OWNER_STATE_LS=[$(ls -A {STATE} | tr '\\n' ' ')]" >> {REPORT}
mkdir -p /home/agent/bin && printf '#!/bin/sh\\nexit 0\\n' > /home/agent/bin/sac
chmod +x /home/agent/bin/sac
PATH=/home/agent/bin:$PATH {{refused}}
echo "REFUSED_RC=$?" >> {REPORT}
echo "REFUSED_RAN=$(test -e /home/agent/refused-but-ran && echo yes || echo no)" >> {REPORT}
"""


def _cfg(root: Path) -> SimpleNamespace:
    sb = parse_sandbox(
        {
            "sandbox": {
                "egress": {"allow": ["pypi.org:443"]},
                "env": {"allow": ["PYTHONPATH"], "model_key": "MODEL_KEY_X"},
                "run": {"root": str(root / "runs"), "size": "256M", "after": "archive"},
            }
        }
    )
    return SimpleNamespace(name=NAME, sandbox=sb, apptainer=None, hermes_failover=None)


def _pane_run(root: Path, *, owner: bool) -> str:
    cfg = _cfg(root)
    state_dir = root / "state" / NAME
    (state_dir / "secret-env").mkdir(parents=True, exist_ok=True)
    (state_dir / "secret-env" / "agent.token").write_text("not-a-real-token-0123\n")
    (state_dir / "home.env").write_text("MODEL_KEY_X=not-a-real-key\n")
    run_dir = begin_run(cfg, state_dir)
    if owner:
        refused = masked_argv(cfg, ["touch", "/home/agent/refused-but-ran"])
        probe = OWNER_PROBE.replace(
            "{refused}", " ".join(shlex.quote(a) for a in refused)
        )
        tail = ["bash", "-c", probe]
    else:
        (run_dir / "home" / "pane-probe.sh").write_text(PANE_PROBE)
        tail = ["bash", "/home/agent/pane-probe.sh"]
    inner = sealed_tui_inner_argv(
        cfg,
        alias_steps=_GIT_ENV_ALIAS_STEPS,
        startup_steps=_format_shell_steps([StartupCommand(command=STARTUP)]),
        runner_tail=tail,
        owner_masks_harness=owner,
    )
    python_root = Path(sys.base_prefix).parent
    argv = [
        "apptainer", "exec", "--containall", "--cleanenv", "--home", "/home/agent",
        "--bind", f"{state_dir}/home:/home/agent",
        "--bind", f"{state_dir}:{STATE}",
        "--bind", f"{SAC_PREFIX}:{SAC_PREFIX}:ro",
        "--bind", f"{python_root}:{python_root}:ro",
        "--bind", f"{SAC_SRC}:{SAC_SRC}:ro",
        "--env", f"PYTHONPATH={SAC_SRC}",
        "--env", "MODEL_KEY_X=not-a-real-key",
        *sandbox_flags(cfg, state_dir), SIF,
        *netgw_wrap(["bash", "-c", "exec " + " ".join(shlex.quote(a) for a in inner)]),
    ]  # fmt: skip
    full = supervised_argv(cfg, argv, state_dir=state_dir, image=SIF)
    rc = supervise(full[full.index("supervise") + 1 :])
    end_run(cfg, state_dir)
    report = run_dir / "home" / "mask-report.txt"
    return f"PANE_RC={rc}\n" + (report.read_text() if report.exists() else "")


@pytest.fixture(scope="module")
def direct():
    return _pane_run(Path(ROOT) / f"pane-direct-{os.getpid()}", owner=False)


@pytest.fixture(scope="module")
def owner():
    return _pane_run(Path(ROOT) / f"pane-owner-{os.getpid()}", owner=True)


def _line(report: str, key: str) -> str:
    return next(
        (line for line in report.splitlines() if line.startswith(key + "=")),
        f"{key}=<absent>\n{report}",
    )


DENIED = f"[ls: cannot open directory '{STATE}': Permission denied ]"


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("STARTUP_STATE_LS", f"STARTUP_STATE_LS={DENIED}"),
        ("STATE_LS", f"STATE_LS={DENIED}"),
        ("TOKEN_READ", "TOKEN_READ=no"),
        ("SECRET_READ", "SECRET_READ=no"),
        ("SAC_ON_PATH", "SAC_ON_PATH=[]"),
        ("SAC_RUN", "SAC_RUN=no"),
        ("SAC_LAUNCHING_RUN", "SAC_LAUNCHING_RUN=no"),
        ("SAC_IMPORT", "SAC_IMPORT=no"),
        ("SAC_VERSION", "SAC_VERSION=no"),
        ("SAC_HIDE_IMPORT", "SAC_HIDE_IMPORT=no"),
        ("ALLOWED", "ALLOWED=200"),
        ("OUTER_PROCS", "OUTER_PROCS=0"),
        ("UNMOUNT", "UNMOUNT=no"),
        ("NESTED_UNMOUNT", "NESTED_UNMOUNT=no"),
        ("CAPS", "CAPS=0000000000000000"),
        ("HOME_LS", "HOME_LS=yes"),
        ("STARTUP_EXPORT", "STARTUP_EXPORT=reached-harness"),
        ("PANE_RC", "PANE_RC=7"),
    ],
)
def test_direct_pane_probe_line_matches(direct, key, expected):
    # Arrange
    report = direct
    # Act
    line = _line(report, key)
    # Assert
    assert line == expected


def test_direct_pane_cannot_list_the_sac_source(direct):
    # Arrange
    report = direct
    # Act
    line = _line(report, "SAC_SRC_LS")
    # Assert
    assert "Permission denied" in line


def test_direct_pane_cannot_reach_the_proxy_sockets(direct):
    # Arrange
    report = direct
    # Act
    line = _line(report, "SOCK_LS")
    # Assert
    assert "Permission denied" in line


def test_image_harness_binaries_still_run_under_the_mask(direct):
    # Arrange -- whichever real harness CLIs the image carries.
    report = direct
    # Act
    states = [ln for ln in report.splitlines() if ln.startswith("HARNESS_")]
    # Assert
    assert all(ln.endswith("=runs") for ln in states)


def test_direct_pane_sees_only_its_own_processes(direct):
    # Arrange
    report = direct
    # Act
    count = int(_line(report, "PIDS").split("=")[1])
    # Assert
    assert count < 8


def test_owner_pane_startup_command_ran_under_the_mask(owner):
    # Arrange
    report = owner
    # Act
    line = _line(report, "STARTUP_STATE_LS")
    # Assert
    assert line == f"STARTUP_STATE_LS={DENIED}"


def test_owner_outside_the_mask_still_sees_the_state_dir(owner):
    # Arrange
    report = owner
    # Act
    line = _line(report, "OWNER_STATE_LS")
    # Assert
    assert "secret-env" in line


def test_mask_refuses_while_a_sac_stays_runnable(owner):
    # Arrange
    report = owner
    # Act
    line = _line(report, "REFUSED_RC")
    # Assert
    assert line == "REFUSED_RC=125"


def test_refused_command_never_ran(owner):
    # Arrange
    report = owner
    # Act
    line = _line(report, "REFUSED_RAN")
    # Assert
    assert line == "REFUSED_RAN=no"
