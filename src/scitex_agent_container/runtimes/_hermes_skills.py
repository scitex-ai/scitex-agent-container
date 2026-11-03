"""Project enabled installed-package skills into each isolated Hermes home."""

from pathlib import Path
import shutil
import tempfile


def installed_skill_records(config, *, packages=None, list_pages=None):
    """Consume the public enabled registry; SAC exclusions narrow its records."""
    import scitex_dev.skill_registry as skill_registry

    excluded = getattr(config, "exclude_skills", ()) or ()
    if packages is None and list_pages is None:
        capability = "installed-provider-registry-v1"
        if capability not in getattr(skill_registry, "INSTALL_CAPABILITIES", ()):
            raise ValueError(
                "Installed skill discovery requires scitex-dev capability "
                f"{capability!r}; refusing the legacy discovery path"
            )
        candidates = skill_registry.discover_installed_skills().require_valid()
    else:
        selected = (
            [
                package
                for package in packages
                if not any(pattern in package.name for pattern in excluded)
            ]
            if packages is not None
            else None
        )
        candidates = _explicit_page_records(selected, list_pages)
    records = {}
    for record in candidates:
        if any(
            pattern in record.owner or pattern in record.name for pattern in excluded
        ):
            continue
        previous = records.get(record.name)
        if previous is not None and (
            previous.owner != record.owner
            or previous.path != record.path
            or previous.source_hash != record.source_hash
        ):
            raise ValueError(f"Installed skill {record.name!r} has conflicting sources")
        records[record.name] = record
    return tuple(records[name] for name in sorted(records))


def _explicit_page_records(packages, list_pages):
    """Retain explicit filesystem-test seams without implicit provider discovery."""
    from scitex_dev.skill_registry import SkillRecord, hash_skill_tree

    if packages is None or list_pages is None:
        raise ValueError("explicit skill pages require both packages and list_pages")
    records = []
    for package in packages:
        for name, pages in list_pages(package=package.name).items():
            indexes = [page for page in pages if page["name"] == "SKILL"]
            if len(indexes) != 1 or not Path(indexes[0]["path"]).is_file():
                raise ValueError(f"Installed skill {name!r} has no unique SKILL.md")
            path = Path(indexes[0]["path"]).parent.resolve()
            records.append(
                SkillRecord(
                    name,
                    indexes[0].get("owner", package.name),
                    path,
                    path / "SKILL.md",
                    hash_skill_tree(path),
                )
            )
    return records


def materialize_installed_skills(config, profiles, *, records=None):
    """Copy canonical sources, record hashes, and audit before exposing them."""
    from scitex_dev.skill_registry import (
        MANIFEST_NAME,
        RegistryReport,
        audit_projection,
        write_projection_manifest,
    )

    records = installed_skill_records(config) if records is None else tuple(records)
    registry = RegistryReport(records, ())
    for profile in profiles:
        profile.mkdir(parents=True, exist_ok=True)
        target = profile / "scitex-skills"
        if target.exists() and not (target / MANIFEST_NAME).is_file():
            raise ValueError(f"Refusing to replace unmanaged skill directory: {target}")
        with tempfile.TemporaryDirectory(
            prefix=".scitex-skills-", dir=profile
        ) as scratch:
            staging = Path(scratch) / "projection"
            staging.mkdir()
            for record in records:
                shutil.copytree(
                    record.path,
                    staging / record.name,
                    ignore=shutil.ignore_patterns("__pycache__", ".git"),
                )
            write_projection_manifest(records, "hermes", staging)
            findings = audit_projection(registry, staging)
            if not findings.ok:
                raise ValueError(
                    f"Hermes skill projection audit failed: {findings.to_dict()}"
                )
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
