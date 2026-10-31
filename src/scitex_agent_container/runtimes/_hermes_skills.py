"""Project enabled installed-package skills into each isolated Hermes home."""

from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace


def installed_skill_records(config, *, packages=None, list_pages=None):
    """Use scitex-dev discovery and central opt-outs; SAC exclusions win too."""
    from scitex_dev._core.config import load_config, _load_knob_state
    from scitex_dev._core.discovery import discover_packages
    from scitex_dev._ecosystem._skills.skills import list_skills
    from scitex_dev.skill_registry import SkillRecord, hash_skill_tree

    if packages is None:
        enabled = {item.name: item.skills_enabled for item in load_config().packages}
        enabled.update(_load_knob_state().get("skills", {}))
        packages = [
            SimpleNamespace(name=name) for name in sorted(discover_packages())
            if enabled.get(name, True)
        ]
    list_pages = list_skills if list_pages is None else list_pages
    excluded = getattr(config, "exclude_skills", ()) or ()
    records = {}
    for package in packages:
        if any(pattern in package.name for pattern in excluded):
            continue
        for name, pages in list_pages(package=package.name).items():
            if any(pattern in name for pattern in excluded):
                continue
            indexes = [Path(page["path"]) for page in pages if page["name"] == "SKILL"]
            if len(indexes) != 1 or not indexes[0].is_file():
                raise ValueError(f"Installed skill {name!r} has no unique SKILL.md")
            path = indexes[0].parent.resolve()
            record = SkillRecord(name, package.name, path, path / "SKILL.md", hash_skill_tree(path))
            if name in records and records[name].path != path:
                raise ValueError(f"Installed skill {name!r} has conflicting sources")
            records[name] = record
    return tuple(records[name] for name in sorted(records))


def materialize_installed_skills(config, profiles, *, records=None):
    """Copy canonical sources, record hashes, and audit before exposing them."""
    from scitex_dev.skill_registry import (
        MANIFEST_NAME, RegistryReport, audit_projection, write_projection_manifest,
    )

    records = installed_skill_records(config) if records is None else tuple(records)
    registry = RegistryReport(records, ())
    for profile in profiles:
        profile.mkdir(parents=True, exist_ok=True)
        target = profile / "scitex-skills"
        if target.exists() and not (target / MANIFEST_NAME).is_file():
            raise ValueError(f"Refusing to replace unmanaged skill directory: {target}")
        with tempfile.TemporaryDirectory(prefix=".scitex-skills-", dir=profile) as scratch:
            staging = Path(scratch) / "projection"
            staging.mkdir()
            for record in records:
                shutil.copytree(
                    record.path, staging / record.name,
                    ignore=shutil.ignore_patterns("__pycache__", ".git"),
                )
            write_projection_manifest(records, "hermes", staging)
            findings = audit_projection(registry, staging)
            if not findings.ok:
                raise ValueError(f"Hermes skill projection audit failed: {findings.to_dict()}")
            previous = Path(scratch) / "previous"
            if target.exists():
                target.rename(previous)
            try:
                staging.rename(target)
            except OSError:
                if previous.exists():
                    previous.rename(target)
                raise
    # Relative to HERMES_HOME in Hermes, so both host backings compile to the
    # same container-visible location without depending on host source binds.
    return ["scitex-skills"]
