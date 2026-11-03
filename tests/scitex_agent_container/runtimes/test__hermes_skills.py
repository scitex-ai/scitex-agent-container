"""Enabled installed skills reach both container home backings without source binds."""

import os
import sys
from types import SimpleNamespace

import pytest

from scitex_agent_container.runtimes._hermes_skills import (
    installed_skill_records,
    materialize_installed_skills,
)
from scitex_dev.skill_registry import RegistryReport, audit_projection


def test_projection_copies_enabled_skills_and_respects_sac_opt_out(tmp_path):
    # Arrange: dispatcher results point to actual package-owned skill trees.
    config = SimpleNamespace(exclude_skills=["excluded"])
    packages = [
        SimpleNamespace(name=name) for name in ("scitex-dev", "excluded-package")
    ]
    source = tmp_path / "package/_skills/scitex-dev"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: scitex-dev\ndescription: Registry\n---\n"
    )
    (source / "24_host-registry.md").write_text("Hosts registry reference\n")

    def pages(*, package):
        if package != "scitex-dev":
            raise ValueError("Disabled package reached discovery")
        return {package: [{"name": "SKILL", "path": str(source / "SKILL.md")}]}

    records = installed_skill_records(config, packages=packages, list_pages=pages)
    homes = [tmp_path / "runtime/.hermes", tmp_path / "overlay/.hermes"]
    # Act
    dirs = materialize_installed_skills(config, homes, records=records)
    # Assert: source-independent copies, same contents and audited manifest.
    observations = []
    for home in homes:
        copied = home / "scitex-skills/scitex-dev/24_host-registry.md"
        observations.append(
            (
                copied.read_text() == (source / copied.name).read_text(),
                copied.is_symlink(),
                audit_projection(
                    RegistryReport(records, ()), home / "scitex-skills"
                ).ok,
            )
        )
    assert (dirs, observations) == (["scitex-skills"], [(True, False, True)] * 2)


def test_projection_audit_detects_source_drift(tmp_path):
    # Arrange
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("Skill version one\n")
    config = SimpleNamespace(exclude_skills=[])
    pages = lambda **_: {
        "example": [{"name": "SKILL", "path": str(source / "SKILL.md")}]
    }
    records = installed_skill_records(
        config, packages=[SimpleNamespace(name="example")], list_pages=pages
    )
    profile = tmp_path / "profile"
    materialize_installed_skills(config, [profile], records=records)
    # Act
    (profile / "scitex-skills/example/SKILL.md").write_text("stale or edited copy\n")
    report = audit_projection(RegistryReport(records, ()), profile / "scitex-skills")
    # Assert
    assert not report.ok


@pytest.fixture
def installed_hermes_provider(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    owner = "scitex-audit-hermes"
    names = [owner, "audit-hermes-general", "audit-hermes-scientific"]
    module = site / "audit_hermes"
    module.mkdir()
    marker = tmp_path / "consumer-imported"
    (module / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        "raise RuntimeError('consumer must not import')\n"
    )
    for name in names:
        skill = module / "_skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\n# {name}\n")
    dist = site / "scitex_audit_hermes-1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text(f"Name: {owner}\nVersion: 1.0\n")
    (dist / "entry_points.txt").write_text(
        "[scitex_dev.skills]\n" + "".join(f"{name} = audit_hermes\n" for name in names)
    )
    before_path = sys.path[:]
    keys = {
        "SCITEX_DIR": str(tmp_path / "state"),
        "SCITEX_DEV_KNOB_STATE": str(tmp_path / "knobs.json"),
    }
    before_env = {key: os.environ.get(key) for key in keys}
    sys.path.insert(0, str(site))
    os.environ.update(keys)
    try:
        yield owner, names, marker
    finally:
        sys.path[:] = before_path
        for key, value in before_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_skills_only_alias_provider_materializes_once_with_real_owner(
    installed_hermes_provider, tmp_path
):
    # Arrange
    owner, names, marker = installed_hermes_provider
    config = SimpleNamespace(exclude_skills=[])
    records = tuple(
        record for record in installed_skill_records(config) if record.owner == owner
    )
    homes = [tmp_path / "runtime" / ".hermes", tmp_path / "overlay" / ".hermes"]
    # Act
    materialize_installed_skills(config, homes, records=records)
    observations = [
        (
            audit_projection(RegistryReport(records, ()), home / "scitex-skills").ok,
            sorted(
                path.name
                for path in (home / "scitex-skills").iterdir()
                if path.is_dir()
            ),
        )
        for home in homes
    ]
    # Assert
    assert (
        {record.name: record.owner for record in records},
        observations,
        marker.exists(),
    ) == ({name: owner for name in names}, [(True, sorted(names))] * 2, False)


def test_runtime_registry_respects_real_central_opt_out(installed_hermes_provider):
    # Arrange
    from scitex_dev._core.config import set_package_knob

    owner, _, marker = installed_hermes_provider
    set_package_knob(owner, "skills", False)
    # Act
    records = installed_skill_records(SimpleNamespace(exclude_skills=[]))
    # Assert
    assert (any(record.owner == owner for record in records), marker.exists()) == (
        False,
        False,
    )


@pytest.mark.parametrize(
    "exclude, remaining",
    [
        (["scitex-audit-hermes"], set()),
        (["audit-hermes-general"], {"scitex-audit-hermes", "audit-hermes-scientific"}),
    ],
)
def test_runtime_spec_excludes_owner_or_namespace(
    installed_hermes_provider, exclude, remaining
):
    # Arrange
    owner, _, marker = installed_hermes_provider
    # Act
    records = installed_skill_records(SimpleNamespace(exclude_skills=exclude))
    # Assert
    assert (
        {record.name for record in records if record.owner == owner},
        marker.exists(),
    ) == (remaining, False)
