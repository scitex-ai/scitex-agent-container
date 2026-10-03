"""The base image build must not spill large wheel extraction into host /tmp."""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

RECIPE = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "scitex_agent_container"
    / "containers"
    / "apptainer-base.def"
)


def test_post_uses_image_backed_temporary_directory_before_package_installs():
    # Arrange
    text = RECIPE.read_text(encoding="utf-8")
    post = text.split("%post", 1)[1].split("%environment", 1)[0]

    # Act
    export = "export TMPDIR=/opt/.sac-build-tmp"
    install = "uv pip install --no-cache"
    contract = (
        export in post,
        post.index(export) < post.index(install),
        'mkdir -p "$TMPDIR"' in post,
        'chmod 1777 "$TMPDIR"' in post,
        "trap 'rm -rf /opt/.sac-build-tmp' EXIT" in post,
    )

    # Assert
    assert contract == (True, True, True, True, True)


def test_system_development_tools_use_uv_in_the_declared_system_interpreter(tmp_path):
    # Arrange
    post = RECIPE.read_text().split("%post", 1)[1].split("%environment", 1)[0]
    commands = [
        line.strip()
        for line in post.splitlines()
        if line.strip().endswith((" pipx", " pre-commit"))
        and not line.strip().startswith("#")
    ]
    recorder = tmp_path / "record-uv.py"
    recorder.write_text("import json,sys; print(json.dumps(sys.argv[1:]))\n")
    expected = [
        [
            "pip",
            "install",
            "--no-cache",
            "--python",
            "/usr/bin/python3",
            "--break-system-packages",
            tool,
        ]
        for tool in ("pipx", "pre-commit")
    ]
    replacements = []
    for command in commands:
        tokens = shlex.split(command)
        if tokens[0] != "/usr/local/bin/uv":
            raise AssertionError("image system tools must use the already-installed UV")
        replacements.append(shlex.join([sys.executable, str(recorder), *tokens[1:]]))

    # Act
    result = subprocess.run(
        ["/bin/sh", "-e", "-c", "\n".join(replacements)],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    observed = [json.loads(line) for line in result.stdout.splitlines()]

    # Assert
    assert (result.returncode, observed, result.stderr) == (0, expected, "")


def _run_base_test_section(tmp_path, sac_exit):
    section = RECIPE.read_text().split("%test", 1)[1].split("%labels", 1)[0]
    observed = tmp_path / "observed"
    later = tmp_path / "opencode-ran"
    fixture = tmp_path / "console.py"
    fixture.write_text(
        "import os,sys\n"
        "from pathlib import Path\n"
        "state=os.environ.get('SCITEX_DIR')\n"
        "row={'home':os.environ.get('HOME'),'state':state,'writable':False}\n"
        "if state:\n"
        " p=Path(state)/'logging/runtime/probe.log'\n"
        " p.parent.mkdir(parents=True,exist_ok=True)\n"
        " p.write_text('owned test log\\n');row['writable']=True\n"
        "out=Path(sys.argv[1]);out.mkdir()\n"
        "(out/'home').write_text(row['home'])\n"
        "(out/'state').write_text(row['state'] or '')\n"
        "if row['writable']:(out/'writable').touch()\n"
        "raise SystemExit(int(sys.argv[2]))\n"
    )
    successor = tmp_path / "later.py"
    successor.write_text(
        "from pathlib import Path;import sys;Path(sys.argv[1]).touch()\n"
    )
    section = section.replace(
        "/opt/venv-sac/bin/sac --version",
        shlex.join([sys.executable, str(fixture), str(observed), str(sac_exit)]),
    ).replace(
        "/opt/npm-global/bin/opencode --version",
        shlex.join([sys.executable, str(successor), str(later)]),
    )
    result = subprocess.run(
        ["/bin/sh", "-c", section],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"]},
        check=False,
        timeout=5,
    )
    row = {
        "home": (observed / "home").read_text(),
        "state": (observed / "state").read_text() or None,
        "writable": (observed / "writable").exists(),
    }
    return result, row, later.exists()


def test_base_validation_stops_before_later_tool_after_sac_failure(tmp_path):
    # Arrange
    expected_home = os.environ["HOME"]

    # Act
    result, observed, later_ran = _run_base_test_section(tmp_path, 23)
    state = Path(observed["state"]) if observed["state"] else None

    # Assert
    assert (
        result.returncode,
        later_ran,
        observed["home"],
        state is not None and state.exists(),
    ) == (23, False, expected_home, False)


def test_base_validation_uses_owned_logging_state_and_preserves_home(tmp_path):
    # Arrange
    expected_home = os.environ["HOME"]

    # Act
    result, observed, later_ran = _run_base_test_section(tmp_path, 0)
    state = Path(observed["state"]) if observed["state"] else None

    # Assert
    assert (
        result.returncode,
        later_ran,
        observed["home"],
        observed["writable"],
        state is not None and state.name.startswith("sac-base-test."),
        state is not None and state.exists(),
    ) == (0, True, expected_home, True, True, False)
