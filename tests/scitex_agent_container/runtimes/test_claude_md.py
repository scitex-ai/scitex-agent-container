"""Branch-coverage tests for ``runtimes.claude_md``.

PA-306 no-mocks: real ``AgentConfig`` instances + real ``tmp_path``
``CLAUDE.md`` files. No ``unittest.mock`` anywhere. Each test follows
AAA, asserts a single observable invariant, uses a 3+ word name.

Targets the section-merge generator branches and early-return loops
left uncovered by integration coverage:
  * ``_add_dir_paths``: each of three CLI forms + the trailing
    ``--add-dir`` (no value) skip branch
  * ``_read_frontmatter`` / ``_file_tags`` / ``_file_frontmatter_name``:
    nonexistent + missing-frontmatter + missing-tags/name branches
  * ``_walk_md`` / ``_resolve_skill``: hidden-dir prune, non-md skip,
    duplicate skill-id warn, fallback-to-default-roots
  * ``build_skills_lines``: block-mode early returns, at-import paths,
    unresolved-name placeholder
  * ``setup_claude_md`` + ``cleanup_claude_md``: existing-section
    replace, fresh-file create, missing-file early return
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from scitex_agent_container.config._types import AgentConfig
from scitex_agent_container.runtimes.claude_md import (
    _add_dir_paths,
    _file_frontmatter_name,
    _file_tags,
    _matches,
    _read_frontmatter,
    _resolve_skill,
    _walk_md,
    build_skills_lines,
    cleanup_claude_md,
    setup_claude_md,
)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _cfg(**kw) -> AgentConfig:
    cfg = AgentConfig(name=kw.pop("name", "agent-x"))
    if "flags" in kw:
        cfg.claude.flags = kw.pop("flags")
    if "skills" in kw:
        for k, v in kw.pop("skills").items():
            setattr(cfg.skills, k, v)
    return cfg


def _write_skill(root: Path, dirname: str, *, name=None, tags=None, body="") -> Path:
    d = root / dirname
    d.mkdir(parents=True, exist_ok=True)
    fm_lines = ["---"]
    if name is not None:
        fm_lines.append(f"name: {name}")
    if tags is not None:
        fm_lines.append(f"tags: [{', '.join(tags)}]")
    fm_lines.append("---")
    md = d / "SKILL.md"
    md.write_text("\n".join(fm_lines) + "\n" + body)
    return md


@pytest.fixture
def home_override(tmp_path):
    """Temporarily redirect $HOME to an empty tmp dir; restore on teardown."""
    # Arrange
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


# ---------------------------------------------------------------------------
# _add_dir_paths
# ---------------------------------------------------------------------------


def test_add_dir_space_joined_form(tmp_path):
    # Arrange
    cfg = _cfg(flags=[f"--add-dir {tmp_path}"])
    # Act
    out = _add_dir_paths(cfg)
    # Assert
    assert out == [Path(str(tmp_path))]


def test_add_dir_equals_form(tmp_path):
    # Arrange
    cfg = _cfg(flags=[f"--add-dir={tmp_path}"])
    # Act
    out = _add_dir_paths(cfg)
    # Assert
    assert out == [Path(str(tmp_path))]


def test_add_dir_two_token_form(tmp_path):
    # Arrange
    cfg = _cfg(flags=["--add-dir", str(tmp_path)])
    # Act
    out = _add_dir_paths(cfg)
    # Assert
    assert out == [Path(str(tmp_path))]


def test_add_dir_trailing_no_value_skipped():
    # Arrange
    cfg = _cfg(flags=["--add-dir"])
    # Act
    out = _add_dir_paths(cfg)
    # Assert
    assert out == []


def test_add_dir_ignores_unrelated_flag():
    # Arrange
    cfg = _cfg(flags=["--model", "opus", "--other"])
    # Act
    out = _add_dir_paths(cfg)
    # Assert
    assert out == []


def test_add_dir_handles_empty_flag_string():
    # Arrange
    cfg = _cfg(flags=["", "--add-dir=/tmp/x"])
    # Act
    out = _add_dir_paths(cfg)
    # Assert
    assert out == [Path("/tmp/x")]


# ---------------------------------------------------------------------------
# _read_frontmatter / _file_tags / _file_frontmatter_name
# ---------------------------------------------------------------------------


def test_read_frontmatter_missing_returns_none(tmp_path):
    # Arrange
    md = tmp_path / "no_fm.md"
    md.write_text("just body, no frontmatter\n")
    # Act
    fm = _read_frontmatter(md)
    # Assert
    assert fm is None


def test_read_frontmatter_strips_html_comment(tmp_path):
    # Arrange
    md = tmp_path / "lead.md"
    md.write_text("<!-- gen -->\n---\nname: foo\n---\nbody\n")
    # Act
    fm = _read_frontmatter(md)
    # Assert
    assert fm == "name: foo"


def test_read_frontmatter_nonexistent_returns_none(tmp_path):
    # Arrange
    md = tmp_path / "absent.md"
    # Act
    fm = _read_frontmatter(md)
    # Assert
    assert fm is None


def test_file_tags_no_frontmatter_empty(tmp_path):
    # Arrange
    md = tmp_path / "plain.md"
    md.write_text("body only\n")
    # Act
    tags = _file_tags(md)
    # Assert
    assert tags == []


def test_file_tags_frontmatter_without_tags(tmp_path):
    # Arrange
    md = tmp_path / "noname.md"
    md.write_text("---\nname: foo\n---\nbody\n")
    # Act
    tags = _file_tags(md)
    # Assert
    assert tags == []


def test_file_tags_parses_quoted_values(tmp_path):
    # Arrange
    md = tmp_path / "tagged.md"
    md.write_text('---\ntags: ["a", b, "c"]\n---\nbody\n')
    # Act
    tags = _file_tags(md)
    # Assert
    assert tags == ["a", "b", "c"]


def test_file_frontmatter_name_missing_returns_none(tmp_path):
    # Arrange
    md = tmp_path / "anon.md"
    md.write_text("---\ntags: [x]\n---\nbody\n")
    # Act
    nm = _file_frontmatter_name(md)
    # Assert
    assert nm is None


def test_file_frontmatter_name_no_frontmatter_none(tmp_path):
    # Arrange
    md = tmp_path / "raw.md"
    md.write_text("hello\n")
    # Act
    nm = _file_frontmatter_name(md)
    # Assert
    assert nm is None


def test_file_frontmatter_name_present_returns_value(tmp_path):
    # Arrange
    md = tmp_path / "nm.md"
    md.write_text('---\nname: "alpha"\n---\n')
    # Act
    nm = _file_frontmatter_name(md)
    # Assert
    assert nm == "alpha"


# ---------------------------------------------------------------------------
# _walk_md
# ---------------------------------------------------------------------------


def test_walk_md_skips_hidden_dirs(tmp_path):
    # Arrange
    (tmp_path / ".hidden").mkdir()
    (tmp_path / ".hidden" / "x.md").write_text("h")
    (tmp_path / "GITIGNORED").mkdir()
    (tmp_path / "GITIGNORED" / "y.md").write_text("h")
    (tmp_path / "ok.md").write_text("h")
    # Act
    found = {p.name for p in _walk_md(tmp_path)}
    # Assert
    assert found == {"ok.md"}


def test_walk_md_skips_non_md_files(tmp_path):
    # Arrange
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.md").write_text("x")
    # Act
    names = [p.name for p in _walk_md(tmp_path)]
    # Assert
    assert names == ["b.md"]


# ---------------------------------------------------------------------------
# _matches
# ---------------------------------------------------------------------------


def test_matches_empty_candidate_is_false():
    # Arrange
    value, candidate = "a", ""
    # Act
    result = _matches(value, candidate, "exact")
    # Assert
    assert result is False


def test_matches_partial_substring_true():
    # Arrange
    value, candidate = "foo", "the-foo-skill"
    # Act
    result = _matches(value, candidate, "partial")
    # Assert
    assert result is True


def test_matches_exact_requires_equality():
    # Arrange
    value, candidate = "foo", "foobar"
    # Act
    result = _matches(value, candidate, "exact")
    # Assert
    assert result is False


# ---------------------------------------------------------------------------
# _resolve_skill
# ---------------------------------------------------------------------------


def test_resolve_skill_by_dirname_when_no_name(tmp_path):
    # Arrange
    _write_skill(tmp_path, "myskill")
    # Act
    out = _resolve_skill("myskill", [tmp_path])
    # Assert
    assert len(out) == 1


def test_resolve_skill_by_frontmatter_name(tmp_path):
    # Arrange
    _write_skill(tmp_path, "dirname", name="canonical")
    # Act
    out = _resolve_skill("canonical", [tmp_path])
    # Assert
    assert len(out) == 1


def test_resolve_skill_by_tag_strategy(tmp_path):
    # Arrange
    md = tmp_path / "f.md"
    md.write_text("---\ntags: [shiny]\n---\n")
    # Act
    out = _resolve_skill("shiny", [tmp_path], strategies=["tag"])
    # Assert
    assert out == [md.resolve()]


def test_resolve_skill_filename_strategy(tmp_path):
    # Arrange
    (tmp_path / "abc.md").write_text("body")
    # Act
    out = _resolve_skill("abc", [tmp_path], strategies=["filename"])
    # Assert
    assert len(out) == 1


def test_resolve_skill_no_matches_empty(tmp_path):
    # Arrange
    # (empty root)
    # Act
    out = _resolve_skill("nope", [tmp_path])
    # Assert
    assert out == []


def test_resolve_skill_skips_nondir_root(tmp_path):
    # Arrange
    bogus = tmp_path / "does-not-exist"
    # Act
    out = _resolve_skill("x", [bogus])
    # Assert
    assert out == []


def test_resolve_skill_tag_ignores_nonmd_siblings(tmp_path):
    # Arrange: tag strategy must skip non-.md files in same dir
    (tmp_path / "note.txt").write_text("ignore me")
    md = tmp_path / "real.md"
    md.write_text("---\ntags: [keep]\n---\n")
    # Act
    out = _resolve_skill("keep", [tmp_path], strategies=["tag"])
    # Assert
    assert out == [md.resolve()]


def test_resolve_skill_duplicate_dedupes_to_one(tmp_path):
    # Arrange
    _write_skill(tmp_path, "a", name="dup")
    _write_skill(tmp_path, "b", name="dup")
    # Act
    out = _resolve_skill("dup", [tmp_path], strategies=["skill-id"])
    # Assert
    assert len(out) == 1


def test_resolve_skill_duplicate_emits_warning(tmp_path, caplog):
    # Arrange
    _write_skill(tmp_path, "a", name="dup")
    _write_skill(tmp_path, "b", name="dup")
    # Act
    with caplog.at_level(
        logging.WARNING, logger="scitex_agent_container.runtimes.claude_md"
    ):
        _resolve_skill("dup", [tmp_path], strategies=["skill-id"])
    # Assert
    assert any("skill-id candidates" in r.message for r in caplog.records)


def test_resolve_skill_empty_roots_uses_default(home_override):
    # Arrange
    # $HOME redirected to empty dir, so ~/.claude/skills does not exist;
    # the empty-roots branch + non-dir guard both execute.
    # Act
    out = _resolve_skill("x", [])
    # Assert
    assert out == []


# ---------------------------------------------------------------------------
# build_skills_lines — block-mode branches
# ---------------------------------------------------------------------------


def test_build_skills_block_mode_required_header():
    # Arrange
    cfg = _cfg(skills={"injection_mode": "block", "required": ["a", "b"]})
    # Act
    out = build_skills_lines(cfg)
    # Assert
    assert "### Required Skills" in out


def test_build_skills_block_mode_emits_fence():
    # Arrange
    cfg = _cfg(skills={"injection_mode": "block", "required": ["a"]})
    # Act
    out = build_skills_lines(cfg)
    # Assert
    assert "```skills" in out


def test_build_skills_block_mode_available_header():
    # Arrange
    cfg = _cfg(skills={"injection_mode": "block", "available": ["c"]})
    # Act
    out = build_skills_lines(cfg)
    # Assert
    assert "### Available Skills" in out


def test_build_skills_block_mode_empty_no_section():
    # Arrange
    cfg = _cfg(skills={"injection_mode": "block"})
    # Act
    out = build_skills_lines(cfg)
    # Assert
    assert out == []


# ---------------------------------------------------------------------------
# build_skills_lines — at-import branches
# ---------------------------------------------------------------------------


def test_build_skills_at_import_required_resolved(tmp_path):
    # Arrange
    _write_skill(tmp_path, "demo")
    cfg = _cfg(
        flags=[f"--add-dir={tmp_path}"],
        skills={"injection_mode": "at-import", "required": ["demo"]},
    )
    # Act
    out = build_skills_lines(cfg)
    # Assert
    assert any(line.startswith("@") and "SKILL.md" in line for line in out)


def test_build_skills_at_import_required_unresolved_placeholder(tmp_path):
    # Arrange
    cfg = _cfg(
        flags=[f"--add-dir={tmp_path}"],
        skills={"injection_mode": "at-import", "required": ["ghost"]},
    )
    # Act
    out = build_skills_lines(cfg)
    # Assert
    assert any("not resolved" in line for line in out)


def test_build_skills_at_import_available_resolved(tmp_path):
    # Arrange
    _write_skill(tmp_path, "soft")
    cfg = _cfg(
        flags=[f"--add-dir={tmp_path}"],
        skills={"injection_mode": "at-import", "available": ["soft"]},
    )
    # Act
    out = build_skills_lines(cfg)
    # Assert
    assert any(line.startswith("- soft:") for line in out)


def test_build_skills_at_import_available_unresolved(tmp_path):
    # Arrange
    cfg = _cfg(
        flags=[f"--add-dir={tmp_path}"],
        skills={"injection_mode": "at-import", "available": ["nope"]},
    )
    # Act
    out = build_skills_lines(cfg)
    # Assert
    assert any("- nope (not resolved)" in line for line in out)


def test_build_skills_at_import_empty_required_emits_nothing(tmp_path):
    # Arrange
    cfg = _cfg(
        flags=[f"--add-dir={tmp_path}"],
        skills={"injection_mode": "at-import"},
    )
    # Act
    out = build_skills_lines(cfg)
    # Assert
    assert out == []


# ---------------------------------------------------------------------------
# setup_claude_md
# ---------------------------------------------------------------------------


def test_setup_creates_fresh_claude_md(tmp_path):
    # Arrange
    cfg = _cfg(name="fresh")
    # Act
    setup_claude_md(cfg, str(tmp_path))
    # Assert
    assert 'id="fresh"' in (tmp_path / ".claude" / "CLAUDE.md").read_text()


def test_setup_replaces_existing_section_once(tmp_path):
    # Arrange
    cfg = _cfg(name="dup")
    setup_claude_md(cfg, str(tmp_path))
    # Act
    setup_claude_md(cfg, str(tmp_path))
    # Assert
    text = (tmp_path / ".claude" / "CLAUDE.md").read_text()
    assert text.count('agent-container:start id="dup"') == 1


def test_setup_replaces_section_with_new_role(tmp_path):
    # Arrange
    cfg = _cfg(name="rerun")
    setup_claude_md(cfg, str(tmp_path))
    cfg.labels = {"role": "head"}
    # Act
    setup_claude_md(cfg, str(tmp_path))
    # Assert
    assert "Role: head" in (tmp_path / ".claude" / "CLAUDE.md").read_text()


def test_setup_appends_after_existing_user_content(tmp_path):
    # Arrange
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "CLAUDE.md").write_text("# User content\n")
    cfg = _cfg(name="appended")
    # Act
    setup_claude_md(cfg, str(tmp_path))
    # Assert
    assert (claude_dir / "CLAUDE.md").read_text().startswith("# User content\n")


def test_setup_role_from_env_overrides_labels(tmp_path):
    # Arrange
    cfg = _cfg(name="e")
    cfg.env = {"SCITEX_AGENT_CONTAINER_ROLE": "boss"}
    cfg.labels = {"role": "ignored"}
    # Act
    setup_claude_md(cfg, str(tmp_path))
    # Assert
    assert "Role: boss" in (tmp_path / ".claude" / "CLAUDE.md").read_text()


def test_setup_no_role_when_unset(tmp_path):
    # Arrange
    cfg = _cfg(name="norole")
    # Act
    setup_claude_md(cfg, str(tmp_path))
    # Assert
    assert "Role:" not in (tmp_path / ".claude" / "CLAUDE.md").read_text()


def test_setup_emits_todo_store_conventions_section(tmp_path):
    # Arrange
    cfg = _cfg(name="todo-section")
    # Act
    setup_claude_md(cfg, str(tmp_path))
    # Assert
    assert (
        "### Todo Store Conventions" in (tmp_path / ".claude" / "CLAUDE.md").read_text()
    )


def test_setup_todo_scope_substitutes_agent_id(tmp_path):
    # Arrange
    cfg = _cfg(name="alpha")
    cfg.env = {"SCITEX_AGENT_CONTAINER_ID": "alpha-prime"}
    # Act
    setup_claude_md(cfg, str(tmp_path))
    # Assert
    assert (
        'scope="agent:alpha-prime"' in (tmp_path / ".claude" / "CLAUDE.md").read_text()
    )


# ---------------------------------------------------------------------------
# cleanup_claude_md
# ---------------------------------------------------------------------------


def test_cleanup_missing_file_does_not_raise(tmp_path):
    # Arrange
    cfg = _cfg(name="absent")
    # Act
    cleanup_claude_md(cfg, str(tmp_path))
    # Assert
    assert not (tmp_path / ".claude" / "CLAUDE.md").exists()


def test_cleanup_removes_managed_section(tmp_path):
    # Arrange
    cfg = _cfg(name="rm")
    setup_claude_md(cfg, str(tmp_path))
    # Act
    cleanup_claude_md(cfg, str(tmp_path))
    # Assert
    content = (tmp_path / ".claude" / "CLAUDE.md").read_text()
    assert 'agent-container:start id="rm"' not in content


def test_cleanup_preserves_user_content(tmp_path):
    # Arrange
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "CLAUDE.md").write_text("# Mine\n")
    cfg = _cfg(name="usr")
    setup_claude_md(cfg, str(tmp_path))
    # Act
    cleanup_claude_md(cfg, str(tmp_path))
    # Assert
    assert (claude_dir / "CLAUDE.md").read_text().startswith("# Mine")


def test_cleanup_no_section_unchanged(tmp_path):
    # Arrange
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "CLAUDE.md").write_text("only user\n")
    cfg = _cfg(name="x")
    # Act
    cleanup_claude_md(cfg, str(tmp_path))
    # Assert
    assert (claude_dir / "CLAUDE.md").read_text() == "only user\n"
