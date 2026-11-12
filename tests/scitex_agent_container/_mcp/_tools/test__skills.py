"""Skill retrieval admits only direct files within the owning package root."""

import pytest

from scitex_agent_container._mcp._tools import _skills


@pytest.fixture
def skills_root(tmp_path):
    """Select a real owned directory, restoring package configuration afterward."""
    root = tmp_path / "skills"
    root.mkdir()
    previous = _skills._SKILLS_ROOT
    _skills._SKILLS_ROOT = root
    try:
        yield root
    finally:
        _skills._SKILLS_ROOT = previous


@pytest.mark.parametrize("name", ["../decoy", "nested/../../decoy", "absolute"])
def test_skills_get_refuses_paths(skills_root, name):
    # Arrange
    root = skills_root
    (root / "safe.md").write_text("safe skill", encoding="utf-8")
    (root.parent / "decoy.md").write_text("outside decoy", encoding="utf-8")
    requested = str(root.parent / "decoy") if name == "absolute" else name

    # Act
    result = _skills.skills_get(requested)

    # Assert
    assert result == {
        "error": "skill not found",
        "name": requested,
        "available": ["safe"],
    }


@pytest.mark.parametrize("operation", ["get", "list"])
def test_skills_refuse_external_symlink(skills_root, operation):
    # Arrange
    root = skills_root
    (root / "safe.md").write_text("safe skill", encoding="utf-8")
    decoy = root.parent / "decoy.md"
    decoy.write_text("outside decoy", encoding="utf-8")
    (root / "outside-link.md").symlink_to(decoy)
    expected = (
        {"error": "skill not found", "name": "outside-link", "available": ["safe"]}
        if operation == "get"
        else {
            "count": 1,
            "skills": [
                {
                    "name": "safe",
                    "path": str(root / "safe.md"),
                    "description": "safe skill",
                }
            ],
        }
    )

    # Act
    result = (
        _skills.skills_get("outside-link")
        if operation == "get"
        else _skills.skills_list()
    )

    # Assert
    assert result == expected


def test_skills_get_preserves_direct_and_internal_link_files(skills_root):
    # Arrange
    root = skills_root
    (root / "safe.md").write_text("safe skill", encoding="utf-8")
    (root / "alias.md").symlink_to(root / "safe.md")
    (root / "directory.md").mkdir()
    (root / "missing.md").symlink_to(root / "absent.md")

    # Act
    listed = _skills.skills_list()
    contents = [_skills.skills_get(row["name"])["content"] for row in listed["skills"]]

    # Assert
    assert ([row["name"] for row in listed["skills"]], contents) == (
        ["alias", "safe"],
        ["safe skill", "safe skill"],
    )


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("description: |\n  First line\n  Second line", "First line\nSecond line"),
        ("description: >-\n  First line\n  second line", "First line second line"),
        ('description: "A colon: and \\"quotes\\""', 'A colon: and "quotes"'),
        ("description: ''", ""),
        ("description: 42", "Fallback body"),
        ("description: [unfinished", "Fallback body"),
        ("name: example", "Fallback body"),
        ("- not a mapping", "Fallback body"),
    ],
)
def test_skills_list_decodes_yaml_description(skills_root, header, expected):
    # Arrange
    text = f"---\n{header}\n---\n# Heading\n\nFallback body\n"
    (skills_root / "example.md").write_text(text, encoding="utf-8")

    # Act
    result = _skills.skills_list()

    # Assert
    assert result["skills"][0]["description"] == expected


def test_skills_list_accepts_crlf_frontmatter_and_exact_closing_fence(skills_root):
    # Arrange
    text = "---\r\ndescription: |-\r\n  First\r\n  ---inside description\r\n---\r\nBody"
    (skills_root / "example.md").write_bytes(text.encode("utf-8"))

    # Act
    result = _skills.skills_list()

    # Assert
    assert result["skills"][0]["description"] == "First\n---inside description"
