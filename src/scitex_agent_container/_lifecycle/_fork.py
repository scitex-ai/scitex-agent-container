"""Fork an agent into an independent, committed authority snapshot."""

from __future__ import annotations

import copy
from contextlib import closing
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile

import yaml


def derive_fork_spec(doc: dict, parent: str, name: str, task: str, overlay: Path,
                     *, fresh: bool = False) -> dict:
    """Keep capabilities and project identity, split runtime and bot identity."""
    result = copy.deepcopy(doc)
    spec = result["spec"]
    apptainer = spec.setdefault("apptainer", {})
    apptainer["overlay"] = str(overlay)
    apptainer["overlay_create_if_missing"] = True
    for env in (apptainer.setdefault("env", {}),):
        for key in list(env):
            if key.startswith("CCT_") or key in {"SAC_NAME", "SAC_TWIN_PARENT", "SCITEX_TODO_AGENT_ID"}:
                del env[key]
        env.update(SCITEX_CARDS_AGENT_ID=name, SAC_FORK_PARENT=parent,
                   CCT_BOT_TOKEN="", GIT_AUTHOR_NAME=name, GIT_COMMITTER_NAME=name)
    # Raw --env entries override declared env and could restore parent identity.
    raw = apptainer.get("raw_args", [])
    if any(str(v).startswith(("--env", "--env-file")) for v in raw):
        raise ValueError("fork requires identity settings in apptainer.env, not raw --env arguments")
    spec.setdefault("a2a", {})["port"] = "auto"
    for block in (spec.setdefault("comms", {}), spec.get("claude", {})):
        if "channels" in block:
            block["channels"] = [c for c in block["channels"] if c != "server:claude-code-telegrammer"]
    spec.get("mcp_servers", {}).pop("claude-code-telegrammer", None)
    spec.pop("telegram", None)
    for harness in spec.get("available_harnesses", {}).values():
        harness["session"] = {"mode": "fresh" if fresh else "continue", "max_age_minutes": None}
    spec.pop("claude", None)
    spec["lineage"] = {"group": "", "may_spawn": False}
    spec.setdefault("restart", {})["policy"] = "never"
    spec["startup_prompts"] = [
        f"You are {name}, a task-scoped child of {parent}. You are not the domain lead. "
        f"Use your own authorship; keep durable task ownership with {parent}. "
        "Report evidence, results and blockers through Cards and SAC/A2A. "
        "Do not use Telegram or create further agents. Follow repository AGENTS.md. "
        f"Your assignment: {task}"
    ]
    labels = result.setdefault("metadata", {}).setdefault("labels", {})
    labels.update(role="domain-subagent", purpose=task, parent=parent)
    labels["groups"] = [g for g in labels.get("groups", []) if g != "lead"]
    spec["extensions"] = {"fork": {"parent": parent, "inherit_context": not fresh}}
    return result


def copy_hermes_context(source: Path, dest: Path, parent: str, child: str) -> int:
    """Take a WAL-consistent private copy; never share parent runtime locks."""
    if dest.exists():
        raise ValueError(f"child conversation already exists: {dest}")
    if not source.is_file():
        raise ValueError(f"parent has no Hermes conversation database: {source}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = dest.with_suffix(".fork-tmp")
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
            with closing(sqlite3.connect(temporary)) as dst:
                src.backup(dst)
                dst.execute("PRAGMA journal_mode=DELETE")
                prefix = f"sac:{parent}"
                count = dst.execute(
                    "SELECT count(*) FROM sessions WHERE title=? OR substr(title,1,?)=?",
                    (prefix, len(prefix) + 1, prefix + ":"),
                ).fetchone()[0]
                if not count:
                    raise ValueError("parent has no named SAC Hermes conversation to inherit")
                dst.execute(
                    "UPDATE sessions SET title=? || substr(title,?) "
                    "WHERE title=? OR substr(title,1,?)=?",
                    (f"sac:{child}", len(prefix) + 1, prefix, len(prefix) + 1, prefix + ":"),
                )
                tables = {r[0] for r in dst.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in ("session_turn_leases", "compression_locks", "gateway_routing",
                              "gateway_heartbeats", "gateway_hygiene_state", "async_delegations"):
                    if table in tables:
                        dst.execute(f'DELETE FROM "{table}"')
                dst.commit()
        temporary.chmod(0o600)
        temporary.replace(dest)
        return count
    finally:
        temporary.unlink(missing_ok=True)


def prepare_fork(parent: str, name: str, task: str, *, fresh: bool = False) -> Path:
    """Validate the parent, snapshot context and publish a new child spec."""
    from ..config import load_config, resolve_config
    from .._drift._authority import validate_spec_authority
    from ..runtimes.tui_session import state_dir_for_config
    from ._twin import _container_home_dir

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name) or name == parent:
        raise ValueError("child name must be distinct and contain only letters, digits, _ or -")
    registry = Path.home() / ".scitex/agent-container/agents"
    link = registry / name
    if link.exists() or link.is_symlink():
        raise ValueError(f"agent {name!r} already exists")
    source = Path(resolve_config(parent)).resolve()
    authority = validate_spec_authority(source)
    cfg = load_config(str(source))
    if not fresh and cfg.harness != "hermes":
        raise ValueError("conversation inheritance currently supports Hermes; use --fresh for other harnesses")
    state = state_dir_for_config(cfg)
    child_state = state.parent / name
    if child_state.exists():
        raise ValueError(f"child runtime already exists: {child_state}")
    overlay = child_state / "overlay"
    doc = derive_fork_spec(yaml.safe_load(source.read_text()), parent, name, task, overlay, fresh=fresh)
    to_home = source.parent / str(doc["spec"].get("to_home", "./to_home"))
    if to_home.is_dir():
        doc["spec"]["to_home"] = str(to_home.resolve())
    root = Path(authority.repo)
    base = root.parent if root.parent.name == "sac-authority" else Path.home() / ".cache/sac-authority"
    base.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="fork-", dir=base))
    temporary.rmdir()

    def git(path, *args):
        return subprocess.check_output(["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE).strip()

    git(root, "worktree", "add", "--detach", str(temporary), authority.head)
    relative = source.relative_to(root).parent.parent / name / "spec.yaml"
    target = temporary / relative
    target.parent.mkdir(parents=True, exist_ok=False)
    try:
        target.write_text(yaml.safe_dump(doc, sort_keys=False))
        load_config(str(target))
        if not fresh:
            home = _container_home_dir(cfg, state, existing=True)
            copy_hermes_context(home / ".hermes/state.db",
                                overlay / "upper/home/agent/.hermes/state.db", parent, name)
        git(temporary, "add", "--", str(relative))
        git(temporary, "commit", "-m", f"feat(agents): fork {name} from {parent}")
        head = git(temporary, "rev-parse", "HEAD")
        snapshot = base / f"{authority.source_identity}-{head}"
        git(root, "worktree", "move", str(temporary), str(snapshot))
        target = snapshot / relative
        validate_spec_authority(target)
        registry.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target.parent)
        return target
    except Exception:
        # Retain copied context for diagnosis; never overwrite it on a retry.
        if temporary.exists():
            git(root, "worktree", "remove", "--force", str(temporary))
        raise
