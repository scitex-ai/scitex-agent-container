"""AGENTS.md exact-projection regression test (infra-canary-20261015).

Canary proof (2026-10-15): a ``to_home/AGENTS.md`` carrying a canary
instruction materialized into the agent home byte-identical, including a
v1 -> v2 source flip re-materializing to changed output
(evidence: /tmp/canary-evidence/infra-canary-20261015/).

``AGENTS.md`` flows through ``_deploy_plain_file`` (full overwrite, NOT
marker-protected), so the contract is exact projection: destination bytes
== source bytes on every deploy, and a changed source re-deploys to
changed output (no stale-copy skip).

PA-306 no-mocks: real filesystem staging against ``tmp_path`` via the
low-level ``materialize_to_home`` entrypoint (config=None -> verbatim
copy, no interpolation). Baseline env keys are pinned off so the fleet
``_shared`` baselines cannot leak into the assertion.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container.runtimes._to_home import materialize_to_home

V1 = (
    "# infra-canary-20261015 \u2014 to_home canary instruction (v1)\n"
    "\n"
    "CANARY-PHRASE-ALPHA-7Q2Z: the sky is striped green.\n"
    "\n"
    "When asked for the canary phrase, reply with the exact phrase above.\n"
)

V2 = (
    "# infra-canary-20261015 \u2014 to_home canary instruction (v2)\n"
    "\n"
    "CANARY-PHRASE-BETA-9K4M: the river hums copper at dawn.\n"
    "\n"
    "When asked for the canary phrase, reply with the exact phrase above.\n"
)


def _stage(tmp_path: Path, body: str) -> tuple[Path, Path]:
    """Stage ``to_home/AGENTS.md`` with ``body``; return (spec_dir, home)."""
    spec_dir = tmp_path / "spec"
    (spec_dir / "to_home").mkdir(parents=True)
    (spec_dir / "to_home" / "AGENTS.md").write_text(body)
    return spec_dir, tmp_path / "home"


def _isolate_baselines(tmp_path: Path, env_save_restore) -> None:
    """Pin both baseline layers off so only the per-agent layer deploys."""
    env_save_restore.delete("SAC_TO_HOME_BASELINE")
    env_save_restore.set(
        "SAC_USER_TO_HOME_BASELINE", str(tmp_path / "no-user-baseline")
    )


class TestAgentsMdMirror:
    def test_to_home_agents_md_projects_byte_identical(
        self, tmp_path, env_save_restore
    ):
        # Arrange
        _isolate_baselines(tmp_path, env_save_restore)
        spec_dir, home = _stage(tmp_path, V1)
        # Act
        materialize_to_home(spec_dir, home)
        # Assert — exact projection, byte-for-byte.
        assert (home / "AGENTS.md").read_bytes() == V1.encode()

    def test_changed_source_rematerializes_to_changed_output(
        self, tmp_path, env_save_restore
    ):
        # Arrange — v1 deployed first.
        _isolate_baselines(tmp_path, env_save_restore)
        spec_dir, home = _stage(tmp_path, V1)
        materialize_to_home(spec_dir, home)
        assert (home / "AGENTS.md").read_bytes() == V1.encode()
        # Act — source flips to v2, re-materialize.
        (spec_dir / "to_home" / "AGENTS.md").write_text(V2)
        materialize_to_home(spec_dir, home)
        # Assert — no stale copy; destination tracks the source.
        assert (home / "AGENTS.md").read_bytes() == V2.encode()
