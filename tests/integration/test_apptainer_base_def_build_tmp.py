"""The base image build must not spill large wheel extraction into host /tmp."""

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
    export = 'export TMPDIR=/opt/.sac-build-tmp'
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
