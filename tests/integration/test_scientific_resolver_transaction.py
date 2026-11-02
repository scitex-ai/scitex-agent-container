"""Execute both real recipe branches; reject split installs and bad closures."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

_RECIPE = (
    Path(__file__).resolve().parents[2]
    / "src/scitex_agent_container/containers/apptainer-scitex.def"
)


@pytest.mark.parametrize("resolver", ["uv", "pip"])
@pytest.mark.parametrize("check_status", [0, 17])
def test_scientific_recipe_resolves_whole_graph_and_refuses_bad_closure(
    tmp_path, resolver, check_status
):
    # Arrange: execute the shipped shell branch with owned installer witnesses.
    text = _RECIPE.read_text()
    start = text.index("    if command -v uv >/dev/null; then")
    end = text.index("    # ---- 2b.", start)
    branch = text[start:end]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.jsonl"
    stub = (
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "record = [pathlib.Path(sys.argv[0]).name, *sys.argv[1:]]\n"
        'with open(os.environ["OWNED_CALLS"], "a") as stream:\n'
        '    stream.write(json.dumps(record) + "\\n")\n'
        'checking = sys.argv[1:] == ["-m", "pip", "check"]\n'
        'sys.exit(int(os.environ["OWNED_CHECK_STATUS"]) if checking else 0)\n'
    )
    for name in ("pip", "python", *(["uv"] if resolver == "uv" else [])):
        executable = bin_dir / name
        executable.write_text(stub)
        executable.chmod(0o700)
    # Substitute executable locations only; keep every requirement and flag.
    branch = branch.replace("/opt/venv-sac/bin/pip", str(bin_dir / "pip"))
    branch = branch.replace("/opt/venv-sac/bin/python", str(bin_dir / "python"))
    environment = {
        "PATH": str(bin_dir),
        "OWNED_CALLS": str(calls),
        "OWNED_CHECK_STATUS": str(check_status),
    }
    # Act: the marker represents later symbol checks/system provisioning.
    result = subprocess.run(
        ["/bin/sh", "-ec", branch + "\nprintf continued"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    observed = [json.loads(line) for line in calls.read_text().splitlines()]
    scientific = [argv for argv in observed if "scitex[all]>=2.30.9" in argv]
    coherent = len(scientific) == 1 and all(
        requirement in scientific[0]
        for requirement in (
            "/opt/scitex-agent-container-src[all,dev]",
            "/opt/scitex-cards-src[mcp,postgres]",
            "openai-agents>=0.19.0",
        )
    )
    bypasses = [argv for argv in observed if "--no-deps" in argv]
    # Assert: conflict detection runs before anything can promote the image.
    assert (coherent, bypasses, observed[-1][1:], result.returncode, result.stdout) == (
        True,
        [],
        ["-m", "pip", "check"],
        check_status,
        "continued" if check_status == 0 else "",
    )
