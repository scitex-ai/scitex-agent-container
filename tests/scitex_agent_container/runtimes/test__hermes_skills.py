"""Enabled installed skills reach both container home backings without source binds."""

from types import SimpleNamespace

from scitex_agent_container.runtimes._hermes_skills import (
    installed_skill_records, materialize_installed_skills,
)
from scitex_dev.skill_registry import RegistryReport, audit_projection


def test_projection_copies_enabled_skills_and_respects_sac_opt_out(tmp_path):
    # Arrange: dispatcher results point to actual package-owned skill trees.
    config = SimpleNamespace(exclude_skills=["excluded"])
    packages = [SimpleNamespace(name=name) for name in ("scitex-dev", "excluded-package")]
    source = tmp_path / "package/_skills/scitex-dev"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: scitex-dev\ndescription: Registry\n---\n")
    (source / "24_host-registry.md").write_text("Hosts registry reference\n")

    def pages(*, package):
        assert package == "scitex-dev"
        return {package: [{"name": "SKILL", "path": str(source / "SKILL.md")}]}

    records = installed_skill_records(config, packages=packages, list_pages=pages)
    homes = [tmp_path / "runtime/.hermes", tmp_path / "overlay/.hermes"]
    # Act
    dirs = materialize_installed_skills(config, homes, records=records)
    # Assert: source-independent copies, same contents and audited manifest.
    assert dirs == ["scitex-skills"]
    for home in homes:
        copied = home / "scitex-skills/scitex-dev/24_host-registry.md"
        assert copied.read_text() == (source / copied.name).read_text()
        assert not copied.is_symlink()
        assert audit_projection(RegistryReport(records, ()), home / "scitex-skills").ok


def test_projection_audit_detects_source_drift(tmp_path):
    # Arrange
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("Skill version one\n")
    config = SimpleNamespace(exclude_skills=[])
    pages = lambda **_: {"example": [{"name": "SKILL", "path": str(source / "SKILL.md")}]}
    records = installed_skill_records(config, packages=[SimpleNamespace(name="example")], list_pages=pages)
    profile = tmp_path / "profile"
    materialize_installed_skills(config, [profile], records=records)
    # Act
    (profile / "scitex-skills/example/SKILL.md").write_text("stale or edited copy\n")
    report = audit_projection(RegistryReport(records, ()), profile / "scitex-skills")
    # Assert
    assert not report.ok
