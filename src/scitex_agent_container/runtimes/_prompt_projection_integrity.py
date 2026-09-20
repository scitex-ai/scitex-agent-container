"""Fail-closed provenance for instructions projected into an agent home."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

from ..config import AgentConfig
from ._to_home_resolve import materialization_layer_dirs

SCHEMA = "scitex-agent-container/prompt-projections/v1"
MANIFEST_RELATIVE_PATH = Path(".sac") / "prompt-projections.json"
HERMES_INSTRUCTION_RELATIVE_PATH = Path("AGENTS.md")
_INSTRUCTION_NAMES = frozenset({"AGENTS.md", "CLAUDE.md"})
_PROMPT_DIRECTORIES = frozenset({"commands", "skills"})


class PromptProjectionDriftError(RuntimeError):
    """A prompt source or its materialized runtime projection changed."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_prompt_path(relative: Path) -> bool:
    return relative.name in _INSTRUCTION_NAMES or (
        relative.suffix == ".md"
        and any(part in _PROMPT_DIRECTORIES for part in relative.parts[:-1])
    )


def _walk_prompt_files(root: Path) -> Iterable[tuple[Path, Path]]:
    """Yield ``(relative, file)`` deterministically, following skill links."""
    if not root.is_dir():
        return
    seen: set[Path] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        current = Path(dirpath)
        resolved = current.resolve()
        if resolved in seen:
            dirnames[:] = []
            continue
        seen.add(resolved)
        dirnames[:] = sorted(dirnames)
        for filename in sorted(filenames):
            path = current / filename
            relative = path.relative_to(root)
            if path.is_file() and _is_prompt_path(relative):
                yield relative, path


def _file_record(path: Path, **identity: str) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        **identity,
        "sha256": _sha256(data),
        "size": len(data),
    }


def capture_prompt_sources(config: AgentConfig) -> list[dict[str, Any]]:
    """Capture every declared ``to_home`` instruction/skill source."""
    records: list[dict[str, Any]] = []
    for layer, root in materialization_layer_dirs(config):
        if root is None:
            continue
        for relative, path in _walk_prompt_files(root):
            records.append(
                _file_record(
                    path,
                    layer=layer,
                    relative_path=relative.as_posix(),
                    source_path=str(path.resolve()),
                )
            )
    return records


def _startup_prompt_records(config: AgentConfig) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, prompt in enumerate(config.startup_prompts):
        data = str(prompt).encode("utf-8")
        records.append({"index": index, "sha256": _sha256(data), "size": len(data)})
    return records


def _runtime_projection_records(home: Path) -> list[dict[str, Any]]:
    return [
        _file_record(path, relative_path=relative.as_posix())
        for relative, path in _walk_prompt_files(home)
    ]


def _canonical_payload(config: AgentConfig, home: Path) -> dict[str, Any]:
    sources = capture_prompt_sources(config)
    projections = _runtime_projection_records(home)
    consumers: dict[str, dict[str, Any]] = {}
    has_declared_neutral_source = any(
        source["relative_path"] == HERMES_INSTRUCTION_RELATIVE_PATH.as_posix()
        for source in sources
    )
    if config.harness == "hermes" and has_declared_neutral_source:
        for projection in projections:
            if (
                projection["relative_path"]
                == HERMES_INSTRUCTION_RELATIVE_PATH.as_posix()
            ):
                consumers["hermes"] = {
                    "projection": projection["relative_path"],
                    "sha256": projection["sha256"],
                    "transport": "agent.system_prompt",
                }
                break
    return {
        "schema": SCHEMA,
        "agent": config.name,
        "spec_path": str(Path(config.config_path).resolve()),
        "declared_layers": list(config.to_home_layers or []),
        "startup_prompts": _startup_prompt_records(config),
        "sources": sources,
        "projections": projections,
        "consumers": consumers,
    }


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def write_prompt_projection_manifest(
    config: AgentConfig,
    home: str | Path,
    sources_before_materialization: list[dict[str, Any]],
) -> Path:
    """Write provenance only when source bytes stayed stable during assembly."""
    home_path = Path(home)
    sources_after = capture_prompt_sources(config)
    if sources_after != sources_before_materialization:
        raise PromptProjectionDriftError(
            f"prompt sources for agent {config.name!r} changed during home "
            "materialization; refusing to publish mixed provenance"
        )
    payload = _canonical_payload(config, home_path)
    manifest = home_path / MANIFEST_RELATIVE_PATH
    _atomic_write_json(manifest, payload)
    verify_prompt_projection_manifest(config, home_path)
    return manifest


def verify_prompt_projection_manifest(
    config: AgentConfig, home: str | Path
) -> dict[str, Any]:
    """Verify source, effective startup prompt, and runtime projection hashes."""
    home_path = Path(home)
    manifest = home_path / MANIFEST_RELATIVE_PATH
    if not manifest.is_file():
        raise PromptProjectionDriftError(
            f"prompt projection manifest is missing for agent {config.name!r}: "
            f"{manifest}"
        )
    try:
        recorded = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromptProjectionDriftError(
            f"prompt projection manifest is unreadable for agent {config.name!r}: "
            f"{manifest}: {exc}"
        ) from exc
    if recorded.get("schema") != SCHEMA:
        raise PromptProjectionDriftError(
            f"prompt projection manifest schema mismatch for agent {config.name!r}: "
            f"expected {SCHEMA!r}, got {recorded.get('schema')!r}"
        )
    current = _canonical_payload(config, home_path)
    for key, label in (
        ("declared_layers", "declared to_home layers"),
        ("startup_prompts", "effective startup prompts"),
        ("sources", "prompt sources"),
        ("projections", "runtime projection"),
        ("consumers", "harness prompt consumers"),
    ):
        if recorded.get(key) != current[key]:
            raise PromptProjectionDriftError(
                f"{label} drift for agent {config.name!r}; rematerialize from the "
                "authoritative spec before launch"
            )
    return recorded


def resolve_hermes_instruction_projection(
    config: AgentConfig, home: str | Path
) -> tuple[str, dict[str, Any]]:
    """Return the verified neutral instruction projection Hermes must consume.

    Hermes discovers repository context from its terminal working directory,
    not from the isolated container home where SAC materializes ``to_home``.
    SAC therefore passes the verified root ``AGENTS.md`` projection through
    Hermes' explicit ``agent.system_prompt`` surface.  Claude-specific files
    are deliberately not translated.
    """
    if config.harness != "hermes":
        raise PromptProjectionDriftError(
            f"Hermes prompt projection requested for harness {config.harness!r}"
        )
    home_path = Path(home)
    recorded = verify_prompt_projection_manifest(config, home_path)
    consumer = recorded.get("consumers", {}).get("hermes")
    if not isinstance(consumer, dict):
        legacy = any(
            item.get("relative_path") in {"CLAUDE.md", ".claude/CLAUDE.md"}
            for item in recorded.get("projections", [])
            if isinstance(item, dict)
        )
        legacy_hint = (
            " A Claude-specific instruction file exists, but SAC will not "
            "silently translate it."
            if legacy
            else ""
        )
        raise PromptProjectionDriftError(
            f"Hermes agent {config.name!r} has no verified neutral AGENTS.md "
            "projection. Add AGENTS.md to a directory explicitly named by "
            f"spec.to_home_layers, then rematerialize before launch.{legacy_hint}"
        )
    relative = consumer.get("projection")
    if relative != HERMES_INSTRUCTION_RELATIVE_PATH.as_posix():
        raise PromptProjectionDriftError(
            f"Hermes agent {config.name!r} has unsupported instruction projection "
            f"{relative!r}; expected {HERMES_INSTRUCTION_RELATIVE_PATH.as_posix()!r}"
        )
    projection = home_path / HERMES_INSTRUCTION_RELATIVE_PATH
    try:
        data = projection.read_bytes()
    except OSError as exc:
        raise PromptProjectionDriftError(
            f"Hermes instruction projection is unreadable for agent "
            f"{config.name!r}: {projection}: {exc}"
        ) from exc
    digest = _sha256(data)
    if digest != consumer.get("sha256"):
        raise PromptProjectionDriftError(
            f"Hermes instruction projection drift for agent {config.name!r}; "
            "rematerialize from the authoritative spec before launch"
        )
    return data.decode("utf-8"), dict(consumer)


__all__ = [
    "MANIFEST_RELATIVE_PATH",
    "HERMES_INSTRUCTION_RELATIVE_PATH",
    "PromptProjectionDriftError",
    "SCHEMA",
    "capture_prompt_sources",
    "resolve_hermes_instruction_projection",
    "verify_prompt_projection_manifest",
    "write_prompt_projection_manifest",
]
