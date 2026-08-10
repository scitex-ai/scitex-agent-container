"""Real hook trees reproducing the 2026-08-10 single-layer-vs-effective split.

PA-306 no-mocks: these build actual directories and actual files under
``tmp_path``, so the code under test does the same ``os.scandir`` it does in
production. The numbers are the MEASURED ones, not round figures:

    layer 1  (<root>/runtime/<agent>/home/.claude/hooks)   67 pre /  8 post
    effective ($HOME inside the container)                 71 pre / 13 post

and ``log_post_tool_use.sh`` is the hook the layer-1 read called MISSING while
it was present in the container. It is named explicitly in the fixture because
"a count differs" and "a specific guarantee is absent" are different failures
and the second one is the one that hurt.
"""

from __future__ import annotations

from pathlib import Path

#: The four pre-tool-use hooks present in the container but not in layer 1.
EFFECTIVE_ONLY_PRE = (
    "enforce_find_maxdepth.sh",
    "enforce_periodic_report_metrics.sh",
    "log_pre_tool_use.sh",
    "tag_operator_messages.sh",
)

#: The five post-tool-use hooks present in the container but not in layer 1.
#: ``log_post_tool_use.sh`` is the one the host-side read reported as missing.
EFFECTIVE_ONLY_POST = (
    "check_ci_status.sh",
    "check_develop_branch.sh",
    "log_post_tool_use.sh",
    "log_post_tool_use_v01.sh",
    "orochi_activity_post.sh",
)

LAYER_PRE_COUNT = 67
LAYER_POST_COUNT = 8
EFFECTIVE_PRE_COUNT = LAYER_PRE_COUNT + len(EFFECTIVE_ONLY_PRE)  # 71
EFFECTIVE_POST_COUNT = LAYER_POST_COUNT + len(EFFECTIVE_ONLY_POST)  # 13


def _shared(prefix: str, count: int) -> "list[str]":
    return [f"{prefix}_{i:02d}.sh" for i in range(count)]


def write_hooks(home: Path, tree: "dict[str, list[str]]") -> Path:
    """Materialise ``{event dir: [script names]}`` under ``home/.claude/hooks``."""
    for event_dir, scripts in tree.items():
        target = home / ".claude" / "hooks" / event_dir
        target.mkdir(parents=True, exist_ok=True)
        for script in scripts:
            (target / script).write_text("#!/bin/sh\nexit 0\n")
    return home


def layer_only_home(base: Path) -> Path:
    """The UNDERCOUNTING view: one of the two stacked home layers, alone."""
    home = base / "layer1-home"
    return write_hooks(
        home,
        {
            "pre-tool-use": _shared("pre", LAYER_PRE_COUNT),
            "post-tool-use": _shared("post", LAYER_POST_COUNT),
        },
    )


def effective_home(base: Path) -> Path:
    """The view a process INSIDE the container gets — the resolved mount stack."""
    home = base / "effective-home"
    return write_hooks(
        home,
        {
            "pre-tool-use": _shared("pre", LAYER_PRE_COUNT) + list(EFFECTIVE_ONLY_PRE),
            "post-tool-use": (
                _shared("post", LAYER_POST_COUNT) + list(EFFECTIVE_ONLY_POST)
            ),
        },
    )


__all__ = [
    "EFFECTIVE_ONLY_POST",
    "EFFECTIVE_ONLY_PRE",
    "EFFECTIVE_POST_COUNT",
    "EFFECTIVE_PRE_COUNT",
    "LAYER_POST_COUNT",
    "LAYER_PRE_COUNT",
    "effective_home",
    "layer_only_home",
    "write_hooks",
]
