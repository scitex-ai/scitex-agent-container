"""Fleet-wide responsiveness doctrine — durable rollout invariants.

The operator's #1 UX pain (lead directive 2026-06-04): a Telegram
message must NOT wait until the agent's current heavy turn finishes.
The chosen primary fix is doctrine — never block the main turn on
long-running work; launch it in the background, end the turn promptly,
handle the result on completion. This file pins that doctrine into
two delivery surfaces so every agent on next restart inherits it:

  * The shipped skill ``30_responsiveness-background-work.md`` —
    auto-delivered to every agent's ``~/.claude/skills/scitex/
    scitex-agent-container/`` via the package's ``_skills`` tree.
  * The example agent ``to_home/CLAUDE.md`` template — the canonical
    reference fleet operators copy from.
  * The skill index ``SKILL.md`` — lists the new skill so it surfaces
    in the package's table of contents.

Each test asserts a single observable invariant. AAA layout. No mocks.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]

SKILL_FILE = (
    REPO_ROOT
    / "src"
    / "scitex_agent_container"
    / "_skills"
    / "scitex-agent-container"
    / "30_responsiveness-background-work.md"
)

SKILL_INDEX = (
    REPO_ROOT
    / "src"
    / "scitex_agent_container"
    / "_skills"
    / "scitex-agent-container"
    / "SKILL.md"
)

EXAMPLE_CLAUDE_MD = (
    REPO_ROOT / "examples" / "agents" / "full-agent" / "to_home" / "CLAUDE.md"
)


def test_skill_file_exists() -> None:
    # Arrange
    target = SKILL_FILE
    # Act
    present = target.is_file()
    # Assert
    assert present, f"expected shipped skill at {target}"


def test_skill_has_frontmatter_description() -> None:
    # Arrange
    text = SKILL_FILE.read_text(encoding="utf-8")
    # Act
    has_fm = text.startswith("---\n") and "\n---\n" in text[4:]
    has_desc = "description:" in text.split("\n---\n", 1)[0]
    # Assert
    assert has_fm and has_desc, (
        "skill must open with YAML frontmatter including description"
    )


def test_skill_has_tags() -> None:
    # Arrange
    text = SKILL_FILE.read_text(encoding="utf-8")
    front = text.split("\n---\n", 1)[0]
    # Act
    has_tags = "tags:" in front
    # Assert
    assert has_tags, "skill must declare tags in frontmatter"


def test_skill_mentions_background_launch() -> None:
    # Arrange
    text = SKILL_FILE.read_text(encoding="utf-8").lower()
    # Act
    mentions = "background" in text and "run_in_background" in text
    # Assert
    assert mentions, "skill must direct heavy work to background (run_in_background)"


def test_skill_mentions_short_turns_for_telegram() -> None:
    # Arrange
    text = SKILL_FILE.read_text(encoding="utf-8").lower()
    # Act
    has_short = "short" in text and "turn" in text
    has_telegram = "telegram" in text
    # Assert
    assert has_short and has_telegram, (
        "skill must explain SHORT turns so operator Telegram stays responsive"
    )


def test_skill_lists_heavy_work_examples() -> None:
    # Arrange
    text = SKILL_FILE.read_text(encoding="utf-8").lower()
    # Act — at least three canonical heavy operations must be called out
    examples = ("latex", "pytest", "figure", "training", "git")
    hits = sum(1 for kw in examples if kw in text)
    # Assert
    assert hits >= 3, f"expected ≥3 of {examples!r} in skill, found {hits}"


def test_skill_index_references_new_skill() -> None:
    # Arrange
    index_text = SKILL_INDEX.read_text(encoding="utf-8")
    # Act
    referenced = "30_responsiveness-background-work.md" in index_text
    # Assert
    assert referenced, "SKILL.md must link the new responsiveness skill"


def test_example_claude_md_has_responsiveness_section() -> None:
    # Arrange
    text = EXAMPLE_CLAUDE_MD.read_text(encoding="utf-8")
    # Act
    has_heading = "Responsiveness" in text
    # Assert
    assert has_heading, "example to_home/CLAUDE.md must carry a Responsiveness section"


def test_example_claude_md_directs_heavy_work_to_background() -> None:
    # Arrange
    text = EXAMPLE_CLAUDE_MD.read_text(encoding="utf-8").lower()
    # Act
    directs = "background" in text and (
        "run_in_background" in text or "long-running" in text
    )
    # Assert
    assert directs, "example CLAUDE.md must direct heavy work to the background"


def test_example_claude_md_calls_out_telegram_responsiveness() -> None:
    # Arrange
    text = EXAMPLE_CLAUDE_MD.read_text(encoding="utf-8").lower()
    # Act
    mentions = "telegram" in text
    # Assert
    assert mentions, "example CLAUDE.md must mention Telegram responsiveness as the why"


def test_example_claude_md_references_enforcement_hook() -> None:
    # Arrange — operator wants structural enforcement (hook), not just rule.
    text = EXAMPLE_CLAUDE_MD.read_text(encoding="utf-8")
    # Act
    referenced = "force_background_bash.sh" in text
    # Assert
    assert referenced, (
        "example CLAUDE.md must point at the enforcement hook so future "
        "agents know it's structural, not behavioural"
    )


def test_skill_references_enforcement_hook() -> None:
    # Arrange
    text = SKILL_FILE.read_text(encoding="utf-8")
    # Act
    referenced = "force_background_bash.sh" in text
    # Assert
    assert referenced, (
        "skill must point at the enforcement hook so rationale + wall live "
        "next to each other"
    )
