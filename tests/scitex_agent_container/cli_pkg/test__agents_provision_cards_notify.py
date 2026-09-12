from __future__ import annotations

import getpass
import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._agents_provision_cards_notify import (
    Endpoint,
    NotifyCredentialError,
    Request,
    provision,
    provision_cards_notify,
)


def _private(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def _request(path: Path, *, role: str = "alice__hub") -> Request:
    return Request(
        agent="hub",
        role=role,
        passfile=path,
        source=Endpoint("scitex-primary", "55432", "scitex"),
        target=Endpoint("scitex-primary", "55433", "scitex"),
    )


def _spec(root: Path, *, notify_port: int = 55433, pguser: str | None = None) -> Path:
    directory = root / "hub"
    directory.mkdir(parents=True)
    env = {
        "SCITEX_CARDS_AGENT_ID": "hub",
        "SCITEX_CARDS_DB": "postgresql://scitex-primary:55432/scitex",
        "SCITEX_CARDS_NOTIFY_DSN": (
            f"postgresql://scitex-primary:{notify_port}/scitex"
        ),
        "PGPASSFILE": str(root / ".pgpass"),
    }
    if pguser:
        env["PGUSER"] = pguser
    path = directory / "spec.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "metadata": {"labels": {"project": "scitex-hub"}},
                "spec": {"apptainer": {"env": env}},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_dry_run_derives_exact_direct_row_without_changing_passfile(tmp_path: Path) -> None:
    path = _private(
        tmp_path / ".pgpass",
        r"scitex-primary:55432:*:alice__hub:p\:a\\ss" + "\n",
    )
    before = path.read_bytes()

    rows = provision([_request(path)], apply_changes=False)

    assert rows[0].status == "planned"
    assert path.read_bytes() == before


def test_apply_is_atomic_private_and_idempotent(tmp_path: Path) -> None:
    path = _private(tmp_path / ".pgpass", "scitex-primary:55432:*:alice__hub:secret\n")

    first = provision([_request(path)], apply_changes=True)
    after_first = path.read_text(encoding="utf-8")
    second = provision([_request(path)], apply_changes=True)

    assert first[0].status == "provisioned"
    assert second[0].status == "ready"
    assert after_first == path.read_text(encoding="utf-8")
    assert "scitex-primary:55433:scitex:alice__hub:secret\n" in after_first
    assert stat_mode(path) == 0o600
    assert not list(tmp_path.glob(".pgpass-notify-*"))


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_conflicting_direct_row_refuses_the_entire_write(tmp_path: Path) -> None:
    path = _private(
        tmp_path / ".pgpass",
        "scitex-primary:55432:*:alice__hub:new\n"
        "scitex-primary:55433:scitex:alice__hub:old\n",
    )
    before = path.read_bytes()

    with pytest.raises(NotifyCredentialError, match="conflicting existing"):
        provision([_request(path)], apply_changes=True)

    assert path.read_bytes() == before


def test_missing_exact_role_source_refuses(tmp_path: Path) -> None:
    path = _private(tmp_path / ".pgpass", "scitex-primary:55432:*:*:secret\n")

    with pytest.raises(NotifyCredentialError, match="no matching port-55432"):
        provision([_request(path)], apply_changes=False)


def test_symlink_passfile_is_refused(tmp_path: Path) -> None:
    real = _private(tmp_path / "real", "scitex-primary:55432:*:alice__hub:secret\n")
    link = tmp_path / ".pgpass"
    link.symlink_to(real)

    with pytest.raises(NotifyCredentialError, match="non-symlink regular file"):
        provision([_request(link)], apply_changes=False)


def test_cli_derives_role_from_project_and_emits_no_secret(tmp_path: Path) -> None:
    _spec(tmp_path)
    role = f"{getpass.getuser()}__scitex-hub"
    path = _private(
        tmp_path / ".pgpass",
        f"scitex-primary:55432:*:{role}:never-print-this\n",
    )

    result = CliRunner().invoke(
        provision_cards_notify,
        ["--root", str(tmp_path), "--agent", "hub", "--pgpass", str(path), "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["rows"][0]["role"] == role
    assert payload["rows"][0]["status"] == "planned"
    assert "never-print-this" not in result.output


def test_cli_honours_explicit_pguser(tmp_path: Path) -> None:
    _spec(tmp_path, pguser="shared_role")
    path = _private(tmp_path / ".pgpass", "scitex-primary:55432:*:shared_role:secret\n")

    result = CliRunner().invoke(
        provision_cards_notify,
        ["--root", str(tmp_path), "--agent", "hub", "--pgpass", str(path), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["rows"][0]["role"] == "shared_role"


def test_cli_refuses_non_direct_notify_dsn_without_writing(tmp_path: Path) -> None:
    _spec(tmp_path, notify_port=55432)
    path = _private(tmp_path / ".pgpass", "scitex-primary:55432:*:alice__scitex-hub:secret\n")
    before = path.read_bytes()

    result = CliRunner().invoke(
        provision_cards_notify,
        ["--root", str(tmp_path), "--agent", "hub", "--pgpass", str(path), "--apply"],
        env={"USER": "alice"},
    )

    assert result.exit_code != 0
    assert "port 55433" in result.output
    assert path.read_bytes() == before


def test_check_reports_missing_row(tmp_path: Path) -> None:
    _spec(tmp_path, pguser="shared_role")
    path = _private(tmp_path / ".pgpass", "scitex-primary:55432:*:shared_role:secret\n")

    result = CliRunner().invoke(
        provision_cards_notify,
        ["--root", str(tmp_path), "--agent", "hub", "--pgpass", str(path), "--check"],
    )

    assert result.exit_code == 1
    assert path.read_text(encoding="utf-8").count("55433") == 0


def test_apply_and_check_are_mutually_exclusive(tmp_path: Path) -> None:
    result = CliRunner().invoke(provision_cards_notify, ["--apply", "--check"])

    assert result.exit_code == 2


def test_source_and_target_hosts_must_match(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    document = yaml.safe_load(spec.read_text(encoding="utf-8"))
    document["spec"]["apptainer"]["env"]["SCITEX_CARDS_NOTIFY_DSN"] = (
        "postgresql://another-primary:55433/scitex"
    )
    spec.write_text(yaml.safe_dump(document), encoding="utf-8")
    path = _private(tmp_path / ".pgpass", "scitex-primary:55432:*:alice__scitex-hub:secret\n")

    result = CliRunner().invoke(
        provision_cards_notify,
        ["--root", str(tmp_path), "--agent", "hub", "--pgpass", str(path), "--apply"],
        env={"USER": "alice"},
    )

    assert result.exit_code != 0
    assert "same host and database" in result.output
