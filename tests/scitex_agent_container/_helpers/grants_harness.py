"""Harness-variant specs for the spec.grants render / infer tests.

Each variant is the scitex-scholar fixture switched to one harness path
(codex TUI, Hermes TUI, OpenCode TUI, sealed Hermes, Claude SDK), written
into the ``world`` fake host (``tests/.../_grants/conftest.py``).
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["HARNESSES", "harness_variant"]

_FIXTURE = (
    Path(__file__).parents[1]
    / "runtimes"
    / "_fixtures"
    / "scitex-scholar"
    / "spec.yaml"
)
HARNESSES = {
    "codex-tui": {"harness": "codex"},
    "hermes-tui": {"harness": "hermes", "engine": "scitex-free"},
    "opencode-tui": {
        "harness": "opencode",
        "engine": "opencode-go-deepseek-v4.1-flash",
    },
    "sealed-hermes": {"harness": "hermes", "engine": "scitex-free", "sandbox": {}},
    "claude-sdk": {
        "harness": "anthropic",
        "runtime": "claude-agent-sdk",
        "engine": "claude-opus",
    },
}


def harness_variant(
    world, name: str, *, agents_md: bool = True
) -> tuple[dict, Path, Path]:
    """A spec for harness ``name`` built from the scitex-scholar fixture."""
    import yaml

    raw = yaml.safe_load(_FIXTURE.read_text())
    _raw, spec, root = world
    raw["spec"].update(HARNESSES[name])
    raw["spec"]["host"] = "${HOSTNAME}"
    raw["spec"]["workdir"] = str(root.parent / "work" / name)
    if name == "opencode-tui":
        raw["spec"]["available_harnesses"]["opencode"] = {
            "session": {"mode": "continue", "max_age_minutes": None},
            "approval_policy": "never",
        }
    # No PostgreSQL wiring: the pg-credential preflight needs a provisioned
    # role, which is orthogonal to grants.
    raw["spec"]["mcp_servers"] = {}
    raw["spec"]["apptainer"]["env"] = {
        k: v
        for k, v in raw["spec"]["apptainer"]["env"].items()
        if k != "PGPASSFILE" and "postgresql://" not in str(v)
    }
    if "sandbox" in raw["spec"]:
        # A sealed run owns its home and tmp: no overlay, no relaxed mode.
        raw["spec"]["apptainer"].update(overlay="", relaxed=False, raw_args=[])
        raw["spec"]["sandbox"] = {
            "egress": {"allow": ["opencode.ai:443"]},
            "env": {"allow": [], "model_key": "OPENCODE_GO_API_KEY"},
            "run": {"root": str(root.parent / "runs"), "size": "1G", "after": "remove"},
        }
    path = spec.parent.parent / f"h-{name}{'' if agents_md else '-bare'}" / "spec.yaml"
    (path.parent / "to_home").mkdir(parents=True, exist_ok=True)
    if agents_md:
        (path.parent / "to_home" / "AGENTS.md").write_text("# agent\n", encoding="utf-8")
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return raw, path, root.parent / f"render-{path.parent.name}"
