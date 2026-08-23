"""A host can pass every installation check and still make agents useless.

WHY THIS FILE EXISTS. On 2026-08-23 scitex-compute-01 reported sac installed
and current, listen alive since boot, 121 specs on disk, images present and
egress working — and an agent started there came up with ZERO MCP servers and
no error. The operator discovered it because a colleague stopped answering on
Telegram and he asked whether it was down or dead.

Every test below reproduces a state MEASURED on a real host that night. Not
one is hypothetical:

  * no baseline                 compute-01 AND spartan — two of four measured
                                hosts, so this is a setup step nobody owns
  * dangling .claude/skills     introduced by copying the baseline from a host
                                where its target existed; every deploy then
                                died with DanglingToHomeSymlinkError
  * command missing             the telegrammer entry pointed at a repo
                                checkout that had never been cloned there
  * /usr/bin/true stub          what business actually read, from which they
                                inferred a decision nobody had made and told
                                the operator Telegram was disabled for them

NO MOCKS AND NOTHING PATCHED. Every test builds a real directory tree in
tmp_path and passes its path in, because the production function takes the
baseline directory as a parameter. A test that had to patch something would be
testing the patch.
"""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container._readiness import assess_node_readiness


def _write_baseline(root: Path, servers: dict) -> Path:
    """A minimal but REAL to_home baseline: a directory holding .mcp.json."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    return root


def _servable(tmp_path: Path, name: str) -> dict:
    """A server whose command genuinely exists, so it is honestly servable."""
    exe = tmp_path / f"bin-{name}"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return {"command": str(exe)}


def test_absent_baseline_is_cannot_deploy_not_ready(tmp_path) -> None:
    # Arrange: compute-01's actual state — no to_home tree at all.
    missing_root = tmp_path / "never-provisioned"

    # Act
    result = assess_node_readiness(missing_root)

    # Assert: the whole point is that this host looked healthy by every other
    # measure, so the readiness verdict must be the one that refuses.
    assert result.verdict == "cannot-deploy"


def test_absent_baseline_reports_zero_tools(tmp_path) -> None:
    # Arrange
    missing_root = tmp_path / "never-provisioned"

    # Act
    result = assess_node_readiness(missing_root)

    # Assert: "how many tools would an agent get here" is the number the
    # operator asked for, and it must be 0 rather than unknown.
    assert result.tool_count == 0


def test_dangling_symlink_blocks_deploy_even_when_servers_are_fine(tmp_path) -> None:
    # Arrange: a baseline that is otherwise perfect, plus the exact symlink
    # shape that killed every deploy on compute-01 — .claude/skills pointing
    # at a path that does not exist on this host.
    root = _write_baseline(tmp_path / "to_home", {"cards": _servable(tmp_path, "cards")})
    (root / ".claude").mkdir()
    (root / ".claude" / "skills").symlink_to(tmp_path / "nowhere" / "skills")

    # Act
    result = assess_node_readiness(root)

    # Assert: a dangling link ABORTS the deploy, so it must outrank a healthy
    # server list rather than being averaged with it.
    assert result.verdict == "cannot-deploy"


def test_dangling_symlink_is_named_so_it_can_be_fixed(tmp_path) -> None:
    # Arrange
    root = _write_baseline(tmp_path / "to_home", {"cards": _servable(tmp_path, "cards")})
    (root / ".claude").mkdir()
    (root / ".claude" / "skills").symlink_to(tmp_path / "nowhere" / "skills")

    # Act
    result = assess_node_readiness(root)

    # Assert: naming the offending path is what makes the check actionable
    # rather than merely correct.
    assert any("skills" in entry for entry in result.dangling_links)


def test_declared_but_missing_command_is_broken_not_absent(tmp_path) -> None:
    # Arrange: the telegrammer case — a real command path that simply was
    # never installed on this host.
    root = _write_baseline(
        tmp_path / "to_home",
        {
            "cards": _servable(tmp_path, "cards"),
            "telegrammer": {"command": str(tmp_path / "no-such-binary")},
        },
    )

    # Act
    result = assess_node_readiness(root)

    # Assert: it must appear in broken_servers. Silently dropping it would
    # reproduce the defect — the caller would see a shorter list and no reason.
    assert [v.name for v in result.broken_servers] == ["telegrammer"]


def test_stub_command_is_reported_as_stub(tmp_path) -> None:
    # Arrange: what was actually deployed — a declared channel wired to a
    # no-op. business read this and concluded a decision had been made.
    root = _write_baseline(
        tmp_path / "to_home",
        {
            "cards": _servable(tmp_path, "cards"),
            "telegrammer": {"command": "/usr/bin/true"},
        },
    )

    # Act
    result = assess_node_readiness(root)

    # Assert: "stub" must be distinguishable from "command-missing". They look
    # identical in outcome and are completely different faults — one is an
    # unprovisioned host, the other is a config asserting a false decision.
    assert [v.state for v in result.broken_servers] == ["stub"]


def test_a_host_with_one_broken_server_is_crippled_not_ready(tmp_path) -> None:
    # Arrange
    root = _write_baseline(
        tmp_path / "to_home",
        {
            "cards": _servable(tmp_path, "cards"),
            "telegrammer": {"command": "/usr/bin/true"},
        },
    )

    # Act
    result = assess_node_readiness(root)

    # Assert: partial capability is its own verdict. Calling it "ready"
    # because SOME servers work is how an agent ends up unable to reach the
    # operator while the host reports healthy.
    assert result.verdict == "crippled"


def test_fully_provisioned_host_is_ready(tmp_path) -> None:
    # Arrange: the positive control. Without it, a check that always says
    # "cannot-deploy" would pass every test above and be worthless.
    root = _write_baseline(
        tmp_path / "to_home",
        {
            "cards": _servable(tmp_path, "cards"),
            "telegrammer": _servable(tmp_path, "telegrammer"),
        },
    )

    # Act
    result = assess_node_readiness(root)

    # Assert
    assert (result.verdict, result.tool_count) == ("ready", 2)


def test_malformed_mcp_json_refuses_rather_than_reporting_zero_servers(tmp_path) -> None:
    # Arrange: an unparseable baseline must not be indistinguishable from a
    # baseline that declares nothing — same shape as the empty-vs-absent
    # confusion this whole check exists to remove.
    root = tmp_path / "to_home"
    root.mkdir()
    (root / ".mcp.json").write_text("{ this is not json")

    # Act
    result = assess_node_readiness(root)

    # Assert
    assert result.verdict == "cannot-deploy"
