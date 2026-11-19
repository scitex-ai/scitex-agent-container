"""The control-plane mask wiring of sealed TUI panes (``_sandbox._mask``).

The namespace work itself is proven in-container by
tests/integration/test_sandbox_apptainer_live.py; these cover the argv
shapes, the hidden path list, and — with REAL subprocesses — that no
SAC-side launcher imports a module planted in its working directory or on
``PYTHONPATH`` (both are agent-influenced inside a sealed container).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scitex_agent_container._sandbox._hide import hide_argv
from scitex_agent_container._sandbox._layout import C_HIDE, C_SOCK
from scitex_agent_container._sandbox._mask import (
    IMAGE_SAC_SOURCE,
    SAC_IMPORT_ROOT,
    control_plane_paths,
    isolated_module_argv,
    masked_argv,
    sealed_tui_inner_argv,
)

CFG = SimpleNamespace(name="sealed-probe")
#: Stdlib / SAC names every launcher imports; each plant records itself.
PLANTED = (
    "json",
    "argparse",
    "ctypes",
    "runpy",
    "signal",
    "shutil",
    "glob",
    "asyncio",
    "socket",
    "subprocess",
)


def _plant(directory: Path, marker: Path) -> None:
    code = f"open({str(marker)!r}, 'a').write(__name__ + '\\n')\n"
    directory.mkdir(parents=True, exist_ok=True)
    for name in PLANTED:
        (directory / f"{name}.py").write_text(code)
    pkg = directory / "scitex_agent_container"
    pkg.mkdir(exist_ok=True)
    (pkg / "__init__.py").write_text(code)


def _run_planted(tmp_path: Path, argv: list[str]) -> str:
    """Run ``argv`` with plants in its cwd AND on PYTHONPATH; return what ran."""
    marker = tmp_path / "imported.txt"
    cwd, pypath = tmp_path / "cwd", tmp_path / "pypath"
    _plant(cwd, marker)
    _plant(pypath, marker)
    subprocess.run(
        argv,
        cwd=cwd,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": f"{pypath}::"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=60,
        check=False,
    )
    return marker.read_text() if marker.exists() else ""


def test_hidden_list_covers_the_agent_state_dir():
    # Arrange
    cfg = CFG
    # Act
    paths = control_plane_paths(cfg)
    # Assert
    assert paths[0] == "/state/sealed-probe"


def test_hidden_list_covers_the_image_sac_source_and_sockets():
    # Arrange
    cfg = CFG
    # Act
    paths = control_plane_paths(cfg)
    # Assert
    assert {IMAGE_SAC_SOURCE, C_SOCK, "/opt/venv-sac"} <= set(paths)


def test_hidden_list_covers_the_launching_installation():
    # Arrange
    expected = sys.prefix if sys.prefix != sys.base_prefix else None
    # Act
    paths = control_plane_paths(CFG)
    # Assert
    assert expected is None or expected in paths


def test_masked_argv_runs_the_run_copy_by_path_isolated():
    # Arrange
    inner = ["/bin/bash", "-lc", "exec claude"]
    # Act
    argv = masked_argv(CFG, inner)
    # Assert
    assert (argv[:3], argv[-4:]) == (["python3", "-I", C_HIDE], ["--", *inner])


def test_masked_argv_hides_every_control_plane_path():
    # Arrange
    expected = control_plane_paths(CFG)
    # Act
    argv = masked_argv(CFG, ["true"])
    # Assert
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--hide"] == expected


def _direct(startup: list[str]) -> list[str]:
    return sealed_tui_inner_argv(
        CFG,
        alias_steps=["ALIAS"],
        startup_steps=startup,
        runner_tail=["claude", "--model", "m"],
        owner_masks_harness=False,
    )


def test_direct_harness_pane_starts_under_the_mask():
    # Arrange
    startup = ["set -e", "touch /work/x"]
    # Act
    argv = _direct(startup)
    # Assert
    assert argv[:3] == ["python3", "-I", C_HIDE]


def test_direct_harness_startup_commands_run_inside_the_mask():
    # Arrange
    startup = ["set -e", "touch /work/x"]
    # Act
    argv = _direct(startup)
    # Assert
    assert argv[-3:] == [
        "/bin/bash",
        "-lc",
        "ALIAS; set -e; touch /work/x; exec claude --model m",
    ]


def _owner(startup: list[str]) -> list[str]:
    return sealed_tui_inner_argv(
        CFG,
        alias_steps=["ALIAS"],
        startup_steps=startup,
        runner_tail=["tini", "owner", "--hide", "/state/x"],
        owner_masks_harness=True,
    )


def test_owner_pane_runs_startup_commands_in_their_own_masked_shell():
    # Arrange
    startup = ["set -e", "touch /work/x"]
    # Act
    inline = _owner(startup)[2]
    # Assert
    assert f"set -e; python3 -I {C_HIDE} --hide /state/sealed-probe" in inline


def test_owner_pane_starts_the_owner_only_after_the_masked_startup():
    # Arrange
    startup = ["set -e", "touch /work/x"]
    # Act
    inline = _owner(startup)[2]
    # Assert
    assert inline.endswith("'set -e; touch /work/x'; exec tini owner --hide /state/x")


def test_owner_pane_without_startup_commands_runs_no_extra_shell():
    # Arrange
    startup: list[str] = []
    # Act
    argv = _owner(startup)
    # Assert
    assert argv == ["/bin/bash", "-lc", "ALIAS; exec tini owner --hide /state/x"]


def test_isolated_module_argv_never_uses_dash_m():
    # Arrange
    module = "scitex_agent_container.runtimes._hermes_tui_owner"
    # Act
    argv = isolated_module_argv("/py", module, ["--state-dir", "/state/a"])
    # Assert
    assert (argv[:3], argv[4:]) == (
        ["/py", "-I", "-c"],
        [str(SAC_IMPORT_ROOT), module, "--state-dir", "/state/a"],
    )


@pytest.mark.parametrize(
    "module",
    [
        "scitex_agent_container.runtimes._hermes_tui_owner",
        "scitex_agent_container.runtimes._opencode_tui_owner",
    ],
)
def test_sealed_owner_launch_imports_nothing_planted(tmp_path, module):
    # Arrange
    argv = isolated_module_argv(sys.executable, module, ["--help"])
    # Act
    imported = _run_planted(tmp_path, argv)
    # Assert
    assert imported == ""


def test_sealed_owner_launch_really_runs_the_sac_module(tmp_path):
    # Arrange
    module = "scitex_agent_container.runtimes._opencode_tui_owner"
    argv = isolated_module_argv(sys.executable, module, ["--help"])
    # Act
    out = subprocess.run(
        argv, cwd=tmp_path, env={"PATH": "/usr/bin:/bin"}, capture_output=True,
        text=True, timeout=60, check=False,
    ).stdout  # fmt: skip
    # Assert
    assert "sac-opencode-tui-owner" in out


def test_mask_launcher_imports_nothing_planted(tmp_path):
    # Arrange
    argv = hide_argv(sys.executable, [str(tmp_path / "absent")], ["true"])
    # Act
    imported = _run_planted(tmp_path, argv)
    # Assert
    assert imported == ""


def test_egress_gateway_imports_nothing_planted(tmp_path):
    # Arrange
    import scitex_agent_container._sandbox as sandbox_pkg

    netgw = Path(sandbox_pkg.__file__).with_name("netgw.py")
    # Act
    imported = _run_planted(tmp_path, [sys.executable, "-I", str(netgw), "--help"])
    # Assert
    assert imported == ""


def test_dash_m_launch_would_import_the_plant(tmp_path):
    # Arrange -- the control: proves the plants are live, so the "nothing
    # imported" results above are not vacuous.
    argv = [sys.executable, "-m", "json.tool", "--help"]
    # Act
    imported = _run_planted(tmp_path, argv)
    # Assert
    assert "json" in imported
