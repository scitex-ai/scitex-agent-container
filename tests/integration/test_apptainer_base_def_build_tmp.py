"""The base image build must not spill large wheel extraction into host /tmp."""

import json
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
