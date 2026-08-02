"""Tests for the to_home/ materialization pipeline (ADR-0006).

Every path under ``to_home/`` lands at the same relative path inside
``$HOME``. Marker-protection semantics for ``CLAUDE.md`` / ``state.md``
guard against silent data loss on a hand-edited file.

PA-306 no-mocks: real ``AgentConfig`` instances against ``tmp_path``.
Env-driven tests use the project-wide ``env_save_restore`` fixture
(POSIX-honest equivalent of ``monkeypatch.setenv`` /
``monkeypatch.delenv``).
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

import pytest

from scitex_agent_container.config._types import AgentConfig
from scitex_agent_container.runtimes._to_home import (
    END_MARKER,
    DanglingToHomeSymlinkError,
    WorkspaceCLAUDEMarkerError,
    WorkspaceCredentialLeakError,
    WorkspaceMcpMergeError,
    deploy_to_home,
    materialize_to_home,
    resolve_baseline_to_home_dir,
    resolve_to_home_dir,
)

START_MARKER_RE = re.compile(
    r"<!-- Start of scitex-agent-container generated section.*?-->"
)


# ---------------------------------------------------------------------------
# Real-AgentConfig builders.
# ---------------------------------------------------------------------------


def _build_cfg(
    tmp_path: Path,
    *,
    to_home: str = "",
    labels: dict | None = None,
) -> tuple[AgentConfig, Path]:
    """Build a real ``AgentConfig`` pointing at a ``to_home/`` next to
    ``spec.yaml`` inside ``tmp_path``. Returns (config, to_home_root).
    """
    agent_dir = tmp_path / "agent_def"
    to_home_dir = agent_dir / "to_home"
    to_home_dir.mkdir(parents=True, exist_ok=True)
    cfg = AgentConfig(name="test-agent")
    cfg.config_path = str(agent_dir / "spec.yaml")
    cfg.to_home = to_home
    if labels:
        cfg.labels = dict(labels)
    return cfg, to_home_dir


# ---------------------------------------------------------------------------
# resolve_to_home_dir — discovery semantics
# ---------------------------------------------------------------------------


class TestResolveToHomeDir:
    def test_auto_discovers_sibling_to_home_dir(self, tmp_path):
        # Arrange
        cfg, root = _build_cfg(tmp_path)
        # Act
        resolved = resolve_to_home_dir(cfg)
        # Assert
        assert resolved == root

    def test_returns_none_when_no_dir_present(self, tmp_path):
        # Arrange — config_path points at a dir without to_home/
        cfg = AgentConfig(name="ghost")
        cfg.config_path = str(tmp_path / "ghost" / "spec.yaml")
        cfg.to_home = ""
        # Act
        resolved = resolve_to_home_dir(cfg)
        # Assert
        assert resolved is None

    def test_honours_explicit_absolute_path_override(self, tmp_path):
        # Arrange
        elsewhere = tmp_path / "elsewhere" / "custom_home"
        elsewhere.mkdir(parents=True)
        cfg, _ = _build_cfg(tmp_path, to_home=str(elsewhere))
        # Act
        resolved = resolve_to_home_dir(cfg)
        # Assert
        assert resolved == elsewhere

    def test_honours_explicit_relative_path_override(self, tmp_path):
        # Arrange — relative path resolves against spec.yaml's dir.
        cfg, _ = _build_cfg(tmp_path, to_home="./to_home")
        # Act
        resolved = resolve_to_home_dir(cfg)
        # Assert — same as auto-discovery here.
        assert resolved == tmp_path / "agent_def" / "to_home"


# ---------------------------------------------------------------------------
# materialize_to_home — the low-level (no-config) signature.
# ---------------------------------------------------------------------------


class TestMaterializeToHomeBasics:
    def test_missing_to_home_dir_is_noop(self, tmp_path):
        # Arrange — spec_dir with no to_home/ subdir.
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (not home.exists()) or (not any(home.iterdir()))

    def test_plain_file_is_copied_into_home(self, tmp_path):
        # Arrange
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home").mkdir(parents=True)
        (spec_dir / "to_home" / ".bashrc").write_text("export FOO=1\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (home / ".bashrc").read_text() == "export FOO=1\n"

    def test_directory_structure_is_mirrored(self, tmp_path):
        # Arrange
        spec_dir = tmp_path / "spec"
        nested = spec_dir / "to_home" / ".config" / "gh"
        nested.mkdir(parents=True)
        (nested / "hosts.yml").write_text("github.com:\n  user: ywatanabe\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (home / ".config" / "gh" / "hosts.yml").exists()

    def test_nested_directory_content_preserved(self, tmp_path):
        # Arrange
        spec_dir = tmp_path / "spec"
        nested = spec_dir / "to_home" / "secrets"
        nested.mkdir(parents=True)
        (nested / "bot_token").write_text("abcd1234\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (home / "secrets" / "bot_token").read_text() == "abcd1234\n"

    def test_empty_to_home_dir_creates_workspace_home(self, tmp_path):
        # Arrange — empty to_home/.
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home").mkdir(parents=True)
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert home.is_dir()


class TestSkillsSubtreeMaterialization:
    """LOCK: ``to_home/.claude/skills/`` is materialized VERBATIM and DEEPLY.

    sac's only skills job is to copy the whole ``.claude/skills/`` subtree into
    the agent home; ``@``-imports live in each agent's OWN
    ``to_home/.claude/CLAUDE.md`` (author-controlled), never in sac. A
    paper-scitex-clew solver's ``@skills/<name>/SKILL.md`` import resolves
    in-container ONLY if ``_walk_and_apply`` recurses into
    ``.claude/skills/<name>/`` and copies ``SKILL.md`` byte-for-byte. These
    tests guard that dependency (a regression here silently breaks their run).
    """

    def test_skill_md_lands_verbatim(self, tmp_path):
        # Arrange — a staged skill dir with a SKILL.md.
        spec_dir = tmp_path / "spec"
        sk = spec_dir / "to_home" / ".claude" / "skills" / "scitexification"
        sk.mkdir(parents=True)
        body = "---\nname: scitexification\n---\nDo the thing.\n"
        (sk / "SKILL.md").write_text(body)
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert — byte-for-byte, at the in-home relative path.
        landed = home / ".claude" / "skills" / "scitexification" / "SKILL.md"
        assert landed.read_text() == body

    def test_nested_skill_resource_is_preserved(self, tmp_path):
        # Arrange — a resource nested BELOW the skill dir (deep recursion).
        spec_dir = tmp_path / "spec"
        sk = spec_dir / "to_home" / ".claude" / "skills" / "foo"
        (sk / "references").mkdir(parents=True)
        (sk / "SKILL.md").write_text("---\nname: foo\n---\n")
        (sk / "references" / "table.md").write_text("row\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        nested = home / ".claude" / "skills" / "foo" / "references" / "table.md"
        assert nested.read_text() == "row\n"

    def test_multiple_skill_dirs_all_materialize(self, tmp_path):
        # Arrange
        spec_dir = tmp_path / "spec"
        root = spec_dir / "to_home" / ".claude" / "skills"
        for nm in ("alpha", "beta"):
            (root / nm).mkdir(parents=True)
            (root / nm / "SKILL.md").write_text(f"---\nname: {nm}\n---\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert — both staged skills landed (host-curated extras allowed).
        got = {p.parent.name for p in (home / ".claude" / "skills").rglob("SKILL.md")}
        assert {"alpha", "beta"} <= got


class TestCredentialLeakGuard:
    """``to_home/`` must never carry a ``.credentials.json``.

    Lead-reported 2026-06-15: an EXPIRED ``.credentials.json`` was
    committed under ``proj-scitex-todo/to_home/.claude/.credentials.json``
    and re-deployed every ``sac agents start``. Credentials are
    operator-rotated runtime state, not workspace bootstrap content —
    they must come from the auth-stage rw bind, not a static copy.
    Letting a static cred file land in ``$HOME/.claude/.credentials.json``
    masks a missing bind: the agent appears to authenticate but uses
    a stale token, then 401s opaquely at the next refresh.

    The guard is loud (raises :class:`WorkspaceCredentialLeakError`)
    rather than silent-skip so the operator sees the offending path
    and fixes the source.
    """

    def test_credentials_json_at_to_home_root_raises(self, tmp_path):
        # Arrange
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home").mkdir(parents=True)
        (spec_dir / "to_home" / ".credentials.json").write_text('{"x":1}\n')
        home = tmp_path / "home"
        # Act
        # Assert — `match=` pins the relative-path text so the audit
        # sees one assertion (TQ007) with AAA markers on their own
        # lines (TQ002).
        with pytest.raises(WorkspaceCredentialLeakError, match=r"\.credentials\.json"):
            materialize_to_home(spec_dir, home)

    def test_credentials_json_under_dot_claude_raises(self, tmp_path):
        # Arrange — the lead-reported leak shape.
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home" / ".claude").mkdir(parents=True)
        (spec_dir / "to_home" / ".claude" / ".credentials.json").write_text(
            '{"oauthAccount":"old"}\n'
        )
        home = tmp_path / "home"
        # Act
        # Assert
        with pytest.raises(
            WorkspaceCredentialLeakError, match=r"\.claude/\.credentials\.json"
        ):
            materialize_to_home(spec_dir, home)

    def test_credentials_json_at_arbitrary_depth_raises(self, tmp_path):
        # Arrange — guard fires regardless of nesting.
        spec_dir = tmp_path / "spec"
        deep = spec_dir / "to_home" / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / ".credentials.json").write_text("{}\n")
        home = tmp_path / "home"
        # Act
        # Assert
        with pytest.raises(WorkspaceCredentialLeakError):
            materialize_to_home(spec_dir, home)

    def test_other_dotfiles_in_claude_dir_pass(self, tmp_path):
        # Arrange — settings.json, mcp.json etc. under .claude/ are fine,
        # only `.credentials.json` is forbidden.
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home" / ".claude").mkdir(parents=True)
        (spec_dir / "to_home" / ".claude" / "settings.json").write_text("{}\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (home / ".claude" / "settings.json").is_file()

    def test_error_message_names_the_offending_path(self, tmp_path):
        # Arrange
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home" / ".claude").mkdir(parents=True)
        leak = spec_dir / "to_home" / ".claude" / ".credentials.json"
        leak.write_text("{}\n")
        home = tmp_path / "home"
        # Act
        # Assert — the operator-visible relative path must appear in
        # the message so `rm` is one step away.
        with pytest.raises(
            WorkspaceCredentialLeakError, match=r"\.claude/\.credentials\.json"
        ):
            materialize_to_home(spec_dir, home)

    def test_no_partial_destination_on_reject(self, tmp_path):
        # Arrange — leak + other content; guard fires BEFORE any deploy.
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home" / ".claude").mkdir(parents=True)
        (spec_dir / "to_home" / ".claude" / ".credentials.json").write_text("{}")
        (spec_dir / "to_home" / "innocent.txt").write_text("hi")
        home = tmp_path / "home"
        try:
            materialize_to_home(spec_dir, home)
        except WorkspaceCredentialLeakError:
            pass
        # Act
        leaked = home / ".claude" / ".credentials.json"
        # Assert — the destination must NOT carry the leaked file even
        # though the guard fired during a deploy that touched siblings.
        assert not leaked.exists()


# ---------------------------------------------------------------------------
# Symlink preservation
# ---------------------------------------------------------------------------


@pytest.fixture
def rel_file_symlink_home(tmp_path):
    """Materialize a to_home/ with a relative symlink to a sibling file.

    Returns the container home dir after materialization.
    """
    spec_dir = tmp_path / "spec"
    (spec_dir / "to_home").mkdir(parents=True)
    (spec_dir / "to_home" / "real.txt").write_text("payload\n")
    os.symlink("real.txt", spec_dir / "to_home" / "link.txt")
    home = tmp_path / "home"
    materialize_to_home(spec_dir, home)
    return home


@pytest.fixture
def abs_file_symlink_home(tmp_path):
    """Materialize a to_home/ with an absolute symlink to a host file
    outside to_home/ (the explicit-pass case). Returns container home.
    """
    external = tmp_path / "external_target"
    external.write_text("external\n")
    spec_dir = tmp_path / "spec"
    (spec_dir / "to_home").mkdir(parents=True)
    os.symlink(str(external), spec_dir / "to_home" / "abs_link")
    home = tmp_path / "home"
    materialize_to_home(spec_dir, home)
    return home


@pytest.fixture
def dir_symlink_home(tmp_path):
    """Materialize a to_home/ with a symlink to a real directory tree
    that itself contains a nested symlink. Returns container home.
    """
    real = tmp_path / "real_payload"
    real.write_text("deep\n")
    src_tree = tmp_path / "src_tree"
    (src_tree / "nested").mkdir(parents=True)
    (src_tree / "a.txt").write_text("A\n")
    (src_tree / "nested" / "b.txt").write_text("B\n")
    os.symlink(str(real), src_tree / "inner_link")
    spec_dir = tmp_path / "spec"
    (spec_dir / "to_home").mkdir(parents=True)
    os.symlink(str(src_tree), spec_dir / "to_home" / "tree")
    home = tmp_path / "home"
    materialize_to_home(spec_dir, home)
    return home


@pytest.fixture
def dangling_symlink_spec(tmp_path):
    """A to_home/ containing a single dangling symlink.

    Returns ``(spec_dir, home)``; the caller drives materialization so
    it can assert on the raised error or on the (absent) destination.
    """
    spec_dir = tmp_path / "spec"
    (spec_dir / "to_home").mkdir(parents=True)
    os.symlink("nonexistent.txt", spec_dir / "to_home" / "dead_link")
    home = tmp_path / "home"
    return spec_dir, home


@pytest.fixture
def dangling_symlink_error(dangling_symlink_spec):
    """The :class:`DanglingToHomeSymlinkError` raised by materializing a
    to_home/ that holds a dangling symlink. Fails the test if none is
    raised.
    """
    spec_dir, home = dangling_symlink_spec
    with pytest.raises(DanglingToHomeSymlinkError) as exc_info:
        materialize_to_home(spec_dir, home)
    return exc_info.value


class TestSymlinkDereferenceCopy:
    """Materialize resolves EVERY to_home symlink to its real target
    content — the container home holds real, self-contained files (no
    symlinks), so the agent is reproducible from its definition alone
    and closed to apptainer regardless of host filesystem layout.
    """

    def test_relative_symlink_lands_as_real_file_not_symlink(
        self, rel_file_symlink_home
    ):
        # Arrange
        home = rel_file_symlink_home
        # Act
        is_link = (home / "link.txt").is_symlink()
        # Assert
        assert is_link is False

    def test_relative_symlink_lands_with_real_content(self, rel_file_symlink_home):
        # Arrange
        home = rel_file_symlink_home
        # Act
        content = (home / "link.txt").read_text()
        # Assert
        assert content == "payload\n"

    def test_absolute_symlink_lands_as_real_file_not_symlink(
        self, abs_file_symlink_home
    ):
        # Arrange
        home = abs_file_symlink_home
        # Act
        is_link = (home / "abs_link").is_symlink()
        # Assert
        assert is_link is False

    def test_absolute_symlink_lands_with_real_content(self, abs_file_symlink_home):
        # Arrange
        home = abs_file_symlink_home
        # Act
        content = (home / "abs_link").read_text()
        # Assert
        assert content == "external\n"

    def test_directory_symlink_lands_as_real_tree_not_symlink(self, dir_symlink_home):
        # Arrange
        home = dir_symlink_home
        # Act
        is_link = (home / "tree").is_symlink()
        # Assert
        assert is_link is False

    def test_directory_symlink_copies_top_level_file(self, dir_symlink_home):
        # Arrange
        home = dir_symlink_home
        # Act
        content = (home / "tree" / "a.txt").read_text()
        # Assert
        assert content == "A\n"

    def test_directory_symlink_copies_nested_file(self, dir_symlink_home):
        # Arrange
        home = dir_symlink_home
        # Act
        content = (home / "tree" / "nested" / "b.txt").read_text()
        # Assert
        assert content == "B\n"

    def test_nested_symlink_inside_resolved_dir_is_not_symlink(self, dir_symlink_home):
        # Arrange
        home = dir_symlink_home
        # Act
        is_link = (home / "tree" / "inner_link").is_symlink()
        # Assert
        assert is_link is False

    def test_nested_symlink_inside_resolved_dir_has_real_content(
        self, dir_symlink_home
    ):
        # Arrange
        home = dir_symlink_home
        # Act
        content = (home / "tree" / "inner_link").read_text()
        # Assert
        assert content == "deep\n"

    def test_dangling_symlink_raises_dedicated_error(self, dangling_symlink_error):
        # Arrange — fixture materializes a dangling-symlink to_home/ and
        # captures the raised error (failing the test if none is raised).
        error = dangling_symlink_error
        # Act
        is_dedicated = isinstance(error, DanglingToHomeSymlinkError)
        # Assert
        assert is_dedicated is True

    def test_dangling_symlink_message_names_path(self, dangling_symlink_error):
        # Arrange
        message = str(dangling_symlink_error)
        # Act
        names_path = "dead_link" in message
        # Assert
        assert names_path is True

    def test_dangling_symlink_message_names_target(self, dangling_symlink_error):
        # Arrange
        message = str(dangling_symlink_error)
        # Act
        names_target = "nonexistent.txt" in message
        # Assert
        assert names_target is True

    def test_dangling_symlink_leaves_no_partial_destination(
        self, dangling_symlink_spec, dangling_symlink_error
    ):
        # Arrange — dangling_symlink_error triggers the (failed) deploy.
        _spec_dir, home = dangling_symlink_spec
        # Act
        leftover = (home / "dead_link").exists() or (home / "dead_link").is_symlink()
        # Assert
        assert leftover is False


# ---------------------------------------------------------------------------
# .env tight-perm semantics
# ---------------------------------------------------------------------------


class TestEnvFileChmod:
    def test_env_file_lands_in_home(self, tmp_path):
        # Arrange
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home").mkdir(parents=True)
        (spec_dir / "to_home" / ".env").write_text("SECRET=xyz\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (home / ".env").exists()

    def test_env_file_chmod_is_owner_read_write_only(self, tmp_path):
        # Arrange
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home").mkdir(parents=True)
        (spec_dir / "to_home" / ".env").write_text("SECRET=xyz\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert — only owner read+write (0600).
        mode = stat.S_IMODE((home / ".env").stat().st_mode)
        assert mode == 0o600


# ---------------------------------------------------------------------------
# Marker-protected merge: CLAUDE.md
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_claude_md_under_home(tmp_path: Path) -> Path:
    """Materialize a single CLAUDE.md under to_home/.claude/ and return
    the resulting destination path. One-shot setup → many single-assert
    tests.
    """
    spec_dir = tmp_path / "spec"
    (spec_dir / "to_home" / ".claude").mkdir(parents=True)
    (spec_dir / "to_home" / ".claude" / "CLAUDE.md").write_text(
        "## Doctrine\nBe helpful.\n"
    )
    home = tmp_path / "home"
    materialize_to_home(spec_dir, home)
    return home / ".claude" / "CLAUDE.md"


class TestMarkerProtectedClaudeMd:
    def test_fresh_deploy_emits_start_marker(self, fresh_claude_md_under_home):
        # Arrange
        content = fresh_claude_md_under_home.read_text()
        # Act
        match = START_MARKER_RE.search(content)
        # Assert
        assert match is not None

    def test_fresh_deploy_emits_end_marker(self, fresh_claude_md_under_home):
        # Arrange
        content = fresh_claude_md_under_home.read_text()
        # Act
        # Assert
        assert END_MARKER in content

    def test_fresh_deploy_includes_source_body(self, fresh_claude_md_under_home):
        # Arrange
        content = fresh_claude_md_under_home.read_text()
        # Act
        # Assert
        assert "Be helpful." in content

    def test_redeploy_preserves_user_tail_past_end_marker(self, tmp_path):
        # Arrange
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home" / ".claude").mkdir(parents=True)
        (spec_dir / "to_home" / ".claude" / "CLAUDE.md").write_text(
            "## Doctrine\nBe helpful.\n"
        )
        home = tmp_path / "home"
        materialize_to_home(spec_dir, home)
        dst = home / ".claude" / "CLAUDE.md"
        dst.write_text(dst.read_text() + "\n### My notes\nremember me\n")
        # Act — redeploy.
        materialize_to_home(spec_dir, home)
        # Assert
        assert "remember me" in dst.read_text()

    def test_malformed_existing_markers_hard_abort(self, tmp_path):
        # Arrange
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home" / ".claude").mkdir(parents=True)
        (spec_dir / "to_home" / ".claude" / "CLAUDE.md").write_text(
            "## Doctrine\nBe helpful.\n"
        )
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        # Two Start markers — invariant violation.
        bad = (
            "<!-- Start of scitex-agent-container generated section (ts1) -->\n"
            f"{END_MARKER}\n"
            "<!-- Start of scitex-agent-container generated section (ts2) -->\n"
            f"{END_MARKER}\n"
        )
        (home / ".claude" / "CLAUDE.md").write_text(bad)
        # Act
        # Assert
        with pytest.raises(WorkspaceCLAUDEMarkerError, match="expected exactly 1"):
            materialize_to_home(spec_dir, home)


@pytest.fixture
def deployed_state_md(tmp_path: Path) -> Path:
    """Materialize a single state.md under to_home/ and return the path."""
    spec_dir = tmp_path / "spec"
    (spec_dir / "to_home").mkdir(parents=True)
    (spec_dir / "to_home" / "state.md").write_text("initial state\n")
    home = tmp_path / "home"
    materialize_to_home(spec_dir, home)
    return home / "state.md"


class TestMarkerProtectedStateMd:
    def test_state_md_emits_start_marker(self, deployed_state_md):
        # Arrange
        content = deployed_state_md.read_text()
        # Act
        match = START_MARKER_RE.search(content)
        # Assert
        assert match is not None

    def test_state_md_emits_end_marker(self, deployed_state_md):
        # Arrange
        content = deployed_state_md.read_text()
        # Act
        # Assert
        assert END_MARKER in content


# ---------------------------------------------------------------------------
# deploy_to_home — AgentConfig-driven entrypoint.
# ---------------------------------------------------------------------------


class TestDeployToHomeFromConfig:
    def test_metadata_name_is_interpolated_in_claude_md(self, tmp_path):
        # Arrange
        cfg, root = _build_cfg(tmp_path)
        (root / ".claude").mkdir()
        (root / ".claude" / "CLAUDE.md").write_text("Agent name: ${metadata.name}\n")
        home = tmp_path / "home"
        # Act
        deploy_to_home(cfg, str(home))
        # Assert
        assert "Agent name: test-agent" in (home / ".claude" / "CLAUDE.md").read_text()

    def test_metadata_label_is_interpolated_in_claude_md(self, tmp_path):
        # Arrange
        cfg, root = _build_cfg(tmp_path, labels={"role": "dev"})
        (root / ".claude").mkdir()
        (root / ".claude" / "CLAUDE.md").write_text("Role: ${metadata.labels.role}\n")
        home = tmp_path / "home"
        # Act
        deploy_to_home(cfg, str(home))
        # Assert
        assert "Role: dev" in (home / ".claude" / "CLAUDE.md").read_text()

    def test_env_var_is_interpolated_when_present(self, tmp_path, env_save_restore):
        # Arrange
        env_save_restore.set("MY_TEST_HOST", "spartan-gpgpu180")
        cfg, root = _build_cfg(tmp_path)
        (root / ".bashrc").write_text("export HOST=${MY_TEST_HOST}\n")
        home = tmp_path / "home"
        # Act
        deploy_to_home(cfg, str(home))
        # Assert
        assert "export HOST=spartan-gpgpu180" in (home / ".bashrc").read_text()

    def test_missing_to_home_is_noop_via_deploy_entrypoint(self, tmp_path):
        # Arrange — config_path points at a dir without to_home/.
        cfg = AgentConfig(name="ghost")
        cfg.config_path = str(tmp_path / "ghost" / "spec.yaml")
        cfg.to_home = ""
        home = tmp_path / "home"
        # Act
        deploy_to_home(cfg, str(home))
        # Assert
        assert (not home.exists()) or (not any(home.iterdir()))

    def test_binary_file_falls_back_to_byte_copy(self, tmp_path):
        # Arrange — embed a binary blob; interpolation must not error.
        cfg, root = _build_cfg(tmp_path)
        payload = bytes(range(256))
        (root / "blob.bin").write_bytes(payload)
        home = tmp_path / "home"
        # Act
        deploy_to_home(cfg, str(home))
        # Assert
        assert (home / "blob.bin").read_bytes() == payload


# ---------------------------------------------------------------------------
# Restart re-delivery: a deploy into a dest that ALREADY holds a stale copy
# of a to_home file must land the CURRENT content — whether the stale copy is
# a plain OLD file (the common case) or a leftover host-merge SYMLINK pointing
# at the operator's real host file (which must be REPLACED, never written
# through). Mirrors the fleet-restart to_home-staleness class.
# ---------------------------------------------------------------------------

_HOOK_REL = ".claude/hooks/pre-tool-use/deny_edit_on_main_branch.sh"
_HOST_ENV = "SAC_HOST_CLAUDE_DIR"
_USER_BASELINE_ENV = "SAC_USER_TO_HOME_BASELINE"


class TestDeployRedeliversChangedFilesOnRestart:
    def test_deploy_overwrites_old_real_file_in_dest(self, tmp_path):
        # Arrange — dest already holds an OLD real copy of a baseline hook.
        cfg, root = _build_cfg(tmp_path)
        src = root / _HOOK_REL
        src.parent.mkdir(parents=True)
        src.write_text("NEW-v2\n")
        os.chmod(src, 0o755)
        home = tmp_path / "home"
        old = home / _HOOK_REL
        old.parent.mkdir(parents=True)
        old.write_text("OLD-v1\n")
        os.chmod(old, 0o755)
        # Act — the restart-path materialization.
        deploy_to_home(cfg, str(home))
        # Assert — the stale content is replaced with the current source.
        assert (home / _HOOK_REL).read_text() == "NEW-v2\n"

    def test_deploy_replaces_leftover_hostmerge_symlink_with_real_file(
        self, tmp_path, env_save_restore
    ):
        # Arrange — developer agent; a prior host-merge left a SYMLINK at the
        # dest pointing at the operator's real host hook, and the hook has
        # since moved into the agent baseline (a real source file now exists).
        env_save_restore.set(_USER_BASELINE_ENV, str(tmp_path / "no-user-baseline"))
        host_root = tmp_path / "host_claude"
        host_hook = host_root / "hooks" / "pre-tool-use" / "deny_edit_on_main_branch.sh"
        host_hook.parent.mkdir(parents=True)
        host_hook.write_text("HOST-ORIGINAL\n")
        env_save_restore.set(_HOST_ENV, str(host_root))
        cfg, root = _build_cfg(tmp_path, labels={"role": "project-maintainer"})
        src = root / _HOOK_REL
        src.parent.mkdir(parents=True)
        src.write_text("NEW-baseline\n")
        os.chmod(src, 0o755)
        home = tmp_path / "home"
        stale = home / _HOOK_REL
        stale.parent.mkdir(parents=True)
        stale.symlink_to(host_hook)
        # Act
        deploy_to_home(cfg, str(home))
        # Assert — dest is a REAL file carrying the current baseline content.
        assert (
            not (home / _HOOK_REL).is_symlink()
            and (home / _HOOK_REL).read_text() == "NEW-baseline\n"
        )

    def test_deploy_over_leftover_symlink_does_not_corrupt_host_file(
        self, tmp_path, env_save_restore
    ):
        # Arrange — same as above; the danger is writing THROUGH the link.
        env_save_restore.set(_USER_BASELINE_ENV, str(tmp_path / "no-user-baseline"))
        host_root = tmp_path / "host_claude"
        host_hook = host_root / "hooks" / "pre-tool-use" / "deny_edit_on_main_branch.sh"
        host_hook.parent.mkdir(parents=True)
        host_hook.write_text("HOST-ORIGINAL\n")
        env_save_restore.set(_HOST_ENV, str(host_root))
        cfg, root = _build_cfg(tmp_path, labels={"role": "project-maintainer"})
        src = root / _HOOK_REL
        src.parent.mkdir(parents=True)
        src.write_text("NEW-baseline\n")
        os.chmod(src, 0o755)
        home = tmp_path / "home"
        stale = home / _HOOK_REL
        stale.parent.mkdir(parents=True)
        stale.symlink_to(host_hook)
        # Act
        deploy_to_home(cfg, str(home))
        # Assert — the operator's real host file is byte-for-byte untouched.
        assert host_hook.read_text() == "HOST-ORIGINAL\n"


# ---------------------------------------------------------------------------
# Baseline layer — shared/common to_home overlaid by per-agent to_home.
#
# Layout under tmp_path:
#   agents/<name>/spec.yaml   ← spec dir
#   agents/<name>/to_home/    ← per-agent layer (overlay, wins on conflict)
#   agents/_shared/to_home/   ← shared baseline (applied first)
# ---------------------------------------------------------------------------


def _build_layered(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build the agents-root layout used by baseline tests.

    Returns ``(spec_dir, per_agent_to_home, baseline_to_home)`` — all
    real directories rooted at ``tmp_path/agents``.
    """
    agents_root = tmp_path / "agents"
    spec_dir = agents_root / "test-agent"
    per_agent = spec_dir / "to_home"
    baseline = agents_root / "_shared" / "to_home"
    per_agent.mkdir(parents=True, exist_ok=True)
    baseline.mkdir(parents=True, exist_ok=True)
    return spec_dir, per_agent, baseline


# ---------------------------------------------------------------------------
# .mcp.json deep-merge across the two-pass overlay (W1 / operator 2026-06-17)
# ---------------------------------------------------------------------------


def test_mcp_json_two_pass_unions_baseline_and_agent_servers(tmp_path):
    # Arrange — baseline ships the defaults; the agent ships its own server.
    spec_dir, per_agent, baseline = _build_layered(tmp_path)
    (baseline / ".mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"sac": {"command": "sac"}, "todo": {"command": "todo"}}}
        )
    )
    (per_agent / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"figrecipe": {"command": "fr"}}})
    )
    home = tmp_path / "home"
    home.mkdir()
    # Act
    materialize_to_home(spec_dir, home)
    merged = json.loads((home / ".mcp.json").read_text())
    # Assert
    assert set(merged["mcpServers"]) == {"sac", "todo", "figrecipe"}


def test_mcp_json_conflicting_server_across_layers_per_agent_wins(tmp_path):
    # Arrange — same server name, different command in baseline vs agent.
    spec_dir, per_agent, baseline = _build_layered(tmp_path)
    (baseline / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"sac": {"command": "A"}}})
    )
    (per_agent / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"sac": {"command": "B"}}})
    )
    home = tmp_path / "home"
    home.mkdir()
    # Act
    materialize_to_home(spec_dir, home)
    merged = json.loads((home / ".mcp.json").read_text())
    # Assert
    assert merged["mcpServers"]["sac"]["command"] == "B"


def test_mcp_json_invalid_agent_json_fails_loud(tmp_path):
    # Arrange — the agent's .mcp.json is not valid JSON.
    spec_dir, per_agent, _baseline = _build_layered(tmp_path)
    (per_agent / ".mcp.json").write_text("not json{")
    home = tmp_path / "home"
    home.mkdir()
    # Act
    # Assert
    with pytest.raises(WorkspaceMcpMergeError):
        materialize_to_home(spec_dir, home)


class TestResolveBaselineToHomeDir:
    def test_resolves_sibling_base_dir_under_agents_root(self, tmp_path):
        # Arrange
        spec_dir, _, baseline = _build_layered(tmp_path)
        # Act
        resolved = resolve_baseline_to_home_dir(spec_dir)
        # Assert
        assert resolved == baseline

    def test_returns_none_when_no_base_dir_present(self, tmp_path):
        # Arrange — spec dir without a sibling _shared/to_home.
        spec_dir = tmp_path / "agents" / "lonely-agent"
        spec_dir.mkdir(parents=True)
        # Act
        resolved = resolve_baseline_to_home_dir(spec_dir)
        # Assert
        assert resolved is None

    def test_resolves_legacy_base_dir_as_fallback(self, tmp_path):
        # Arrange — only the legacy ``_base`` sibling exists.
        agents_root = tmp_path / "agents"
        spec_dir = agents_root / "test-agent"
        spec_dir.mkdir(parents=True)
        legacy = agents_root / "_base" / "to_home"
        legacy.mkdir(parents=True)
        # Act
        resolved = resolve_baseline_to_home_dir(spec_dir)
        # Assert
        assert resolved == legacy

    def test_shared_dir_wins_over_legacy_base_dir(self, tmp_path):
        # Arrange — both ``_shared`` and legacy ``_base`` siblings exist.
        agents_root = tmp_path / "agents"
        spec_dir = agents_root / "test-agent"
        spec_dir.mkdir(parents=True)
        shared = agents_root / "_shared" / "to_home"
        shared.mkdir(parents=True)
        (agents_root / "_base" / "to_home").mkdir(parents=True)
        # Act
        resolved = resolve_baseline_to_home_dir(spec_dir)
        # Assert
        assert resolved == shared

    def test_returns_none_when_spec_dir_is_none(self):
        # Arrange — no spec dir, no env override.
        # Act
        resolved = resolve_baseline_to_home_dir(None)
        # Assert
        assert resolved is None

    def test_env_override_takes_precedence(self, tmp_path, env_save_restore):
        # Arrange — an explicit baseline elsewhere wins over the sibling.
        spec_dir, _, sibling = _build_layered(tmp_path)
        custom = tmp_path / "custom_baseline" / "to_home"
        custom.mkdir(parents=True)
        env_save_restore.set("SAC_TO_HOME_BASELINE", str(custom))
        # Act
        resolved = resolve_baseline_to_home_dir(spec_dir)
        # Assert
        assert resolved == custom

    def test_env_override_pointing_at_missing_dir_returns_none(
        self, tmp_path, env_save_restore
    ):
        # Arrange — override path that does not exist.
        spec_dir, _, _ = _build_layered(tmp_path)
        env_save_restore.set("SAC_TO_HOME_BASELINE", str(tmp_path / "does_not_exist"))
        # Act
        resolved = resolve_baseline_to_home_dir(spec_dir)
        # Assert
        assert resolved is None


class TestMaterializeBaselineOverlay:
    def test_baseline_only_file_lands_in_home(self, tmp_path):
        # Arrange — baseline provides a shared file the agent does not.
        spec_dir, _, baseline = _build_layered(tmp_path)
        (baseline / ".bashrc").write_text("export BASE=1\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (home / ".bashrc").read_text() == "export BASE=1\n"

    def test_per_agent_file_overrides_baseline_file_of_same_name(self, tmp_path):
        # Arrange — same relative path in both layers; per-agent must win.
        spec_dir, per_agent, baseline = _build_layered(tmp_path)
        (baseline / "shared.txt").write_text("from baseline\n")
        (per_agent / "shared.txt").write_text("from agent\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (home / "shared.txt").read_text() == "from agent\n"

    def test_distinct_baseline_file_lands_alongside_per_agent(self, tmp_path):
        # Arrange — distinct files in each layer.
        spec_dir, per_agent, baseline = _build_layered(tmp_path)
        (baseline / "base_only.txt").write_text("base\n")
        (per_agent / "agent_only.txt").write_text("agent\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (home / "base_only.txt").read_text() == "base\n"

    def test_distinct_per_agent_file_lands_alongside_baseline(self, tmp_path):
        # Arrange — distinct files in each layer.
        spec_dir, per_agent, baseline = _build_layered(tmp_path)
        (baseline / "base_only.txt").write_text("base\n")
        (per_agent / "agent_only.txt").write_text("agent\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (home / "agent_only.txt").read_text() == "agent\n"

    def test_absent_baseline_is_unchanged_current_behavior(self, tmp_path):
        # Arrange — no _shared/ sibling at all; only a per-agent to_home.
        # (Matches the historical single-layer layout.)
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home").mkdir(parents=True)
        (spec_dir / "to_home" / ".bashrc").write_text("export FOO=1\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert — identical to the legacy single-layer test.
        assert (home / ".bashrc").read_text() == "export FOO=1\n"

    def test_baseline_only_with_no_per_agent_to_home(self, tmp_path):
        # Arrange — baseline exists but the agent has NO to_home/ of its own.
        agents_root = tmp_path / "agents"
        spec_dir = agents_root / "test-agent"
        spec_dir.mkdir(parents=True)
        baseline = agents_root / "_shared" / "to_home"
        baseline.mkdir(parents=True)
        (baseline / "common.txt").write_text("shared\n")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (home / "common.txt").read_text() == "shared\n"


class TestDeployBaselineFromConfig:
    def test_baseline_lands_via_config_entrypoint(self, tmp_path):
        # Arrange — config_path under an agents root with a _base sibling.
        agents_root = tmp_path / "agents"
        spec_dir = agents_root / "test-agent"
        (spec_dir / "to_home").mkdir(parents=True)
        baseline = agents_root / "_shared" / "to_home"
        baseline.mkdir(parents=True)
        (baseline / ".bashrc").write_text("export BASE=1\n")
        cfg = AgentConfig(name="test-agent")
        cfg.config_path = str(spec_dir / "spec.yaml")
        cfg.to_home = ""
        home = tmp_path / "home"
        # Act
        deploy_to_home(cfg, str(home))
        # Assert
        assert (home / ".bashrc").read_text() == "export BASE=1\n"

    def test_per_agent_overrides_baseline_via_config_entrypoint(self, tmp_path):
        # Arrange
        agents_root = tmp_path / "agents"
        spec_dir = agents_root / "test-agent"
        per_agent = spec_dir / "to_home"
        per_agent.mkdir(parents=True)
        baseline = agents_root / "_shared" / "to_home"
        baseline.mkdir(parents=True)
        (baseline / "shared.txt").write_text("from baseline\n")
        (per_agent / "shared.txt").write_text("from agent\n")
        cfg = AgentConfig(name="test-agent")
        cfg.config_path = str(spec_dir / "spec.yaml")
        cfg.to_home = ""
        home = tmp_path / "home"
        # Act
        deploy_to_home(cfg, str(home))
        # Assert
        assert (home / "shared.txt").read_text() == "from agent\n"


@pytest.fixture
def overlaid_claude_md(tmp_path: Path) -> Path:
    """Materialize a marker-protected CLAUDE.md from BOTH layers and return
    the destination. The layers COMPOSE: baseline first, per-agent after.
    One-shot setup feeds several single-assert tests.
    """
    spec_dir, per_agent, baseline = _build_layered(tmp_path)
    (baseline / ".claude").mkdir()
    (baseline / ".claude" / "CLAUDE.md").write_text("## Base doctrine\n")
    (per_agent / ".claude").mkdir()
    (per_agent / ".claude" / "CLAUDE.md").write_text("## Agent doctrine\n")
    home = tmp_path / "home"
    materialize_to_home(spec_dir, home)
    return home / ".claude" / "CLAUDE.md"


class TestMarkerProtectedOverlay:
    def test_per_agent_body_wins(self, overlaid_claude_md):
        # Arrange
        content = overlaid_claude_md.read_text()
        # Act
        # Assert
        assert "Agent doctrine" in content

    def test_baseline_body_is_preserved(self, overlaid_claude_md):
        # Arrange — INVERTED 2026-08-02. This asserted `not in`: that the
        # per-agent layer DROPS the baseline body. That is the general
        # plain-file overlay rule ("per-agent wins on conflict") applied to a
        # MERGE-class file, and it is wrong for the same reason .mcp.json is
        # already exempt from it — full overwrite "would silently drop the
        # defaults". Here the defaults are the shared safety baseline: the
        # prompt-injection rules, hook doctrine and task-board obligations. An
        # agent gaining its first non-empty per-agent CLAUDE.md silently lost
        # them while its spec looked clean. This test is why that survived —
        # it made the correct behaviour look like the regression.
        content = overlaid_claude_md.read_text()
        # Act
        # Assert
        assert "Base doctrine" in content

    def test_result_has_single_marker_section(self, overlaid_claude_md):
        # Arrange
        content = overlaid_claude_md.read_text()
        # Act
        # Assert
        assert content.count(END_MARKER) == 1


# ---------------------------------------------------------------------------
# Read-only destination overwrite (FIX 2) — hooks are commonly mode 0755 /
# read-only; deploy must overwrite them in place without PermissionError.
# ---------------------------------------------------------------------------


class TestReadOnlyDestinationOverwrite:
    def _deploy_over_readonly(
        self, tmp_path: Path, basename: str, *, mode: int = 0o555
    ) -> Path:
        """Materialize a source file over a pre-existing read-only dest.

        Returns the destination path. Uses the no-config
        ``materialize_to_home`` path so the plain-file ``shutil.copy2``
        branch is exercised directly.
        """
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home").mkdir(parents=True)
        (spec_dir / "to_home" / basename).write_text("new content\n")
        home = tmp_path / "home"
        home.mkdir()
        dst = home / basename
        dst.write_text("old content\n")
        os.chmod(dst, mode)
        materialize_to_home(spec_dir, home)
        return dst

    def test_plain_file_overwrite_succeeds_over_readonly(self, tmp_path):
        # Arrange — read-only existing dest (the #142 PermissionError case).
        # Act
        dst = self._deploy_over_readonly(tmp_path, "hook_switch_helper.sh")
        # Assert — no PermissionError raised; content updated.
        assert dst.read_text() == "new content\n"

    def test_plain_file_content_is_updated(self, tmp_path):
        # Arrange
        # Act
        dst = self._deploy_over_readonly(tmp_path, "hook_switch_helper.sh")
        # Assert
        assert "old content" not in dst.read_text()

    def test_marker_protected_overwrite_succeeds_over_readonly(self, tmp_path):
        # Arrange — CLAUDE.md gets marker-protected merge; a prior deploy
        # left a valid marker section that is now read-only.
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home").mkdir(parents=True)
        (spec_dir / "to_home" / "CLAUDE.md").write_text("## doctrine\n")
        home = tmp_path / "home"
        home.mkdir()
        dst = home / "CLAUDE.md"
        dst.write_text(
            "<!-- Start of scitex-agent-container generated section (old) -->\n"
            "## stale\n"
            f"{END_MARKER}\n"
        )
        os.chmod(dst, 0o444)
        # Act
        materialize_to_home(spec_dir, home)
        # Assert — wrapped section refreshed, no PermissionError.
        assert "doctrine" in dst.read_text()

    def test_tight_perm_file_overwrite_succeeds_over_readonly(self, tmp_path):
        # Arrange — .env gets chmod 0600; dest starts read-only.
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home").mkdir(parents=True)
        (spec_dir / "to_home" / ".env").write_text("KEY=new\n")
        home = tmp_path / "home"
        home.mkdir()
        dst = home / ".env"
        dst.write_text("KEY=old\n")
        os.chmod(dst, 0o400)
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert dst.read_text() == "KEY=new\n"


# ---------------------------------------------------------------------------
# Isolation: the delivery path NEVER auto-reads host state. Host content
# enters ONLY via an EXPLICIT symlink the operator places under to_home/
# (e.g. ``_shared/to_home/.claude/skills -> ~/.claude/skills``), which the
# materialize walk resolves to real content. The old unconditional host
# ``~/.claude/skills`` auto-read is gone — these tests lock that.
# ---------------------------------------------------------------------------


class TestNoHostAutoRead:
    """The runtime must be reproducible from the definition alone. There
    is no module that sources the host ``~/.claude/skills`` and no
    ``deploy_to_home`` / ``materialize_to_home`` call that reads it.
    """

    def test_skills_resolve_module_is_removed(self):
        # Arrange
        import importlib.util

        module_name = "scitex_agent_container.runtimes._skills_resolve"
        # Act
        spec = importlib.util.find_spec(module_name)
        # Assert — the host-sourcing module no longer exists.
        assert spec is None

    def test_materialize_does_not_read_real_home_claude_skills(
        self, tmp_path, env_save_restore
    ):
        # Arrange — a HOME whose ``.claude/skills`` exists with content
        # that must NOT be auto-read; spec has no to_home referencing it.
        fake_home = tmp_path / "fake_home"
        host_skills = fake_home / ".claude" / "skills" / "leaked"
        host_skills.mkdir(parents=True)
        (host_skills / "SKILL.md").write_text("must not leak\n")
        env_save_restore.set("HOME", str(fake_home))
        spec_dir = tmp_path / "spec"
        (spec_dir / "to_home").mkdir(parents=True)
        (spec_dir / "to_home" / "marker.txt").write_text("ok\n")
        container_home = tmp_path / "container_home"
        # Act
        materialize_to_home(spec_dir, container_home)
        # Assert — host skills must NOT have been auto-materialized.
        assert not (container_home / ".claude" / "skills" / "leaked").exists()

    def test_explicit_to_home_symlink_to_host_skills_is_resolved(self, tmp_path):
        # Arrange — operator EXPLICITLY links host skills into to_home/;
        # the walk must resolve it to real content (explicit-pass).
        host_skills = tmp_path / "host" / ".claude" / "skills" / "general"
        host_skills.mkdir(parents=True)
        (host_skills / "SKILL.md").write_text("explicit\n")
        spec_dir = tmp_path / "spec"
        link_parent = spec_dir / "to_home" / ".claude"
        link_parent.mkdir(parents=True)
        os.symlink(str(host_skills.parent), link_parent / "skills")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert (
            home / ".claude" / "skills" / "general" / "SKILL.md"
        ).read_text() == "explicit\n"

    def test_explicit_to_home_symlink_lands_as_real_content_not_symlink(self, tmp_path):
        # Arrange
        host_skills = tmp_path / "host" / ".claude" / "skills" / "general"
        host_skills.mkdir(parents=True)
        (host_skills / "SKILL.md").write_text("explicit\n")
        spec_dir = tmp_path / "spec"
        link_parent = spec_dir / "to_home" / ".claude"
        link_parent.mkdir(parents=True)
        os.symlink(str(host_skills.parent), link_parent / "skills")
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec_dir, home)
        # Assert
        assert not (home / ".claude" / "skills").is_symlink()

    def test_per_agent_to_home_overrides_baseline_on_conflict(self, tmp_path):
        # Arrange — baseline ships a skill; per-agent to_home ships its
        # own at the same path. Per-agent must win (overlay order).
        agents_root = tmp_path / "agents"
        base_skill = (
            agents_root
            / "_shared"
            / "to_home"
            / ".claude"
            / "skills"
            / "general"
            / "SKILL.md"
        )
        base_skill.parent.mkdir(parents=True)
        base_skill.write_text("from baseline\n")
        agent_skill = (
            agents_root
            / "ghost"
            / "to_home"
            / ".claude"
            / "skills"
            / "general"
            / "SKILL.md"
        )
        agent_skill.parent.mkdir(parents=True)
        agent_skill.write_text("from per-agent\n")
        cfg = AgentConfig(name="ghost")
        cfg.config_path = str(agents_root / "ghost" / "spec.yaml")
        cfg.to_home = ""
        home = tmp_path / "home"
        # Act
        deploy_to_home(cfg, str(home))
        # Assert
        assert (
            home / ".claude" / "skills" / "general" / "SKILL.md"
        ).read_text() == "from per-agent\n"


# ---------------------------------------------------------------------------
# .envrc — verbatim deploy + host-side fold into .env (sac respects .envrc)
# ---------------------------------------------------------------------------


def test_deploy_to_home_folds_envrc_into_env(tmp_path: Path) -> None:
    # Arrange — a to_home carrying a .envrc (direnv shell script).
    cfg, to_home = _build_cfg(tmp_path)
    (to_home / ".envrc").write_text("export FOLDED_FROM_ENVRC=yes\n", encoding="utf-8")
    home = tmp_path / "home"
    # Act
    deploy_to_home(cfg, str(home))
    # Assert — the .envrc was evaluated and folded into $HOME/.env.
    assert "FOLDED_FROM_ENVRC=yes" in (home / ".env").read_text()


def test_deploy_to_home_envrc_deployed_verbatim(tmp_path: Path) -> None:
    # Arrange — a .envrc whose own ${...} syntax must NOT be sac-interpolated.
    cfg, to_home = _build_cfg(tmp_path)
    (to_home / ".envrc").write_text(
        "export KEEP='${NOT_INTERPOLATED}'\n", encoding="utf-8"
    )
    home = tmp_path / "home"
    # Act
    deploy_to_home(cfg, str(home))
    # Assert — the deployed .envrc retains its literal shell ${...} syntax.
    assert "${NOT_INTERPOLATED}" in (home / ".envrc").read_text()


# ---------------------------------------------------------------------------
# .mcp.json deep-merge idempotency — a CHANGED per-agent definition must
# re-deploy cleanly (no false McpMergeConflict vs last deploy's stale result).
# ---------------------------------------------------------------------------


def test_deploy_to_home_remerges_changed_mcp_json_without_conflict(
    tmp_path: Path,
) -> None:
    # Arrange — deploy a per-agent .mcp.json once, then CHANGE it (the
    # figrecipe scenario: an updated telegrammer command).
    cfg, to_home = _build_cfg(tmp_path)
    mcp = to_home / ".mcp.json"
    mcp.write_text('{"mcpServers": {"x": {"command": "old"}}}', encoding="utf-8")
    home = tmp_path / "home"
    deploy_to_home(cfg, str(home))
    mcp.write_text('{"mcpServers": {"x": {"command": "new"}}}', encoding="utf-8")
    # Act — redeploy with the changed definition (must NOT McpMergeConflict).
    deploy_to_home(cfg, str(home))
    # Assert — the deployed .mcp.json reflects the NEW definition (re-derived).
    deployed = json.loads((home / ".mcp.json").read_text())
    assert deployed["mcpServers"]["x"]["command"] == "new"


# ---------------------------------------------------------------------------
# Tokenless-telegrammer prune, END-TO-END through deploy_to_home.
#
# The unit tests for prune_tokenless_telegrammer_mcp live in
# test__cct_token_pool.py. These exist because a correct function wired at the
# WRONG point in deploy_to_home would still pass those: the prune reads the
# .mcp.json the walk materialises and the token ensure_cct_bot_token resolves,
# so it must run after BOTH. Asserting through the real entry-point is what
# makes the ordering testable rather than merely commented.
# ---------------------------------------------------------------------------


class TestTokenlessTelegrammerPrune:
    _TELEGRAMMER = "claude-code-telegrammer"

    def _mcp_servers(self, home: Path) -> dict:
        import json

        return json.loads((home / ".mcp.json").read_text())["mcpServers"]

    def _seed(self, tmp_path: Path):
        """A to_home carrying the shared baseline's telegrammer entry."""
        cfg, root = _build_cfg(tmp_path)
        (root / ".mcp.json").write_text(
            '{"mcpServers": {"claude-code-telegrammer": '
            '{"command": "cct", "env": {"CCT_BOT_TOKEN": "${CCT_BOT_TOKEN}"}}, '
            '"scitex-cards": {"command": "scitex-cards"}}}\n'
        )
        return cfg, root

    def test_tokenless_agent_gets_no_telegrammer_entry(self, tmp_path):
        # Arrange — no .envrc, no pool: nothing can resolve a token.
        cfg, _root = self._seed(tmp_path)
        home = tmp_path / "home"
        # Act
        deploy_to_home(cfg, str(home))
        # Assert
        assert self._TELEGRAMMER not in self._mcp_servers(home)

    def test_tokenless_prune_keeps_the_other_servers(self, tmp_path):
        # Arrange
        cfg, _root = self._seed(tmp_path)
        home = tmp_path / "home"
        # Act
        deploy_to_home(cfg, str(home))
        # Assert
        assert "scitex-cards" in self._mcp_servers(home)

    def test_agent_with_a_token_keeps_the_telegrammer_entry(self, tmp_path):
        # Arrange — a real .envrc supplying the token, exactly as a bot-owning
        # project does; the fold lands it in dest/.env before the prune reads it.
        cfg, root = self._seed(tmp_path)
        (root / ".envrc").write_text("export CCT_BOT_TOKEN=123:abc\n")
        home = tmp_path / "home"
        # Act
        deploy_to_home(cfg, str(home))
        # Assert
        assert self._TELEGRAMMER in self._mcp_servers(home)
