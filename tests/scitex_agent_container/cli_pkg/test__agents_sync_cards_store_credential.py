from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scitex_agent_container.cli_pkg._agents_provision_cards_notify import (
    Endpoint,
    NotifyCredentialError,
)
from scitex_agent_container.cli_pkg._agents_sync_cards_store_credential import (
    install_exact_row,
    sync_to_peers,
)

ENDPOINT = Endpoint("scitex-primary", "55432", "scitex")
ROLE = "alice__scitex-lead"


def _private(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_install_preserves_unrelated_rows_and_is_idempotent(tmp_path: Path) -> None:
    # Arrange
    path = _private(tmp_path / ".pgpass", "other:5432:db:somebody:keep-me\n")

    # Act
    first = install_exact_row(
        path, endpoint=ENDPOINT, role=ROLE, raw_password="s\\:ecret", apply_changes=True
    )
    after = path.read_text(encoding="utf-8")
    second = install_exact_row(
        path, endpoint=ENDPOINT, role=ROLE, raw_password="s\\:ecret", apply_changes=True
    )

    # Assert
    assert (
        first,
        second,
        path.read_text(encoding="utf-8"),
        path.stat().st_mode & 0o777,
        "other:5432:db:somebody:keep-me\n" in after,
        after.count(f"scitex-primary:55432:scitex:{ROLE}:"),
    ) == (
        "provisioned",
        "ready",
        after,
        0o600,
        True,
        1,
    )


def test_install_replaces_only_conflicting_exact_rows(tmp_path: Path) -> None:
    # Arrange
    path = _private(
        tmp_path / ".pgpass",
        f"scitex-primary:55432:scitex:{ROLE}:old-a\n"
        f"scitex-primary:55432:scitex:{ROLE}:old-b\n"
        f"*:55432:*:{ROLE}:wildcard-stays\n",
    )

    # Act
    status = install_exact_row(
        path, endpoint=ENDPOINT, role=ROLE, raw_password="new", apply_changes=True
    )

    # Assert
    text = path.read_text(encoding="utf-8")
    assert (status, "old-a" in text, "old-b" in text, "wildcard-stays" in text) == (
        "updated",
        False,
        False,
        True,
    )


def test_dry_run_does_not_change_passfile(tmp_path: Path) -> None:
    # Arrange
    path = _private(tmp_path / ".pgpass", "unrelated:1:x:y:z\n")
    before = path.read_bytes()

    # Act
    status = install_exact_row(
        path, endpoint=ENDPOINT, role=ROLE, raw_password="secret", apply_changes=False
    )

    # Assert
    assert (status, path.read_bytes()) == ("planned", before)


def test_sync_uses_stdin_not_argv_and_preflights_all_before_apply(
    tmp_path: Path,
) -> None:
    # Arrange
    source = _private(tmp_path / ".pgpass", f"*:55432:*:{ROLE}:never-print-this\n")
    calls: list[tuple[list[str], str]] = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs["input"]))
        status = "provisioned" if "--apply" in argv else "planned"
        return subprocess.CompletedProcess(argv, 0, json.dumps({"status": status}), "")

    # Act
    result = sync_to_peers(
        ("compute-01", "compute-03"),
        role=ROLE,
        source_passfile=source,
        apply_changes=True,
        runner=runner,
    )

    # Assert
    assert (
        [item.status for item in result],
        ["--apply" in argv for argv, _ in calls],
        all("never-print-this" not in " ".join(argv) for argv, _ in calls),
        all(json.loads(stdin)["password"] == "never-print-this" for _, stdin in calls),
    ) == (
        ["provisioned", "provisioned"],
        [False, False, True, True],
        True,
        True,
    )


def test_failed_preflight_prevents_every_apply(tmp_path: Path) -> None:
    # Arrange
    source = _private(tmp_path / ".pgpass", f"*:55432:*:{ROLE}:secret\n")
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if "compute-03" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "receiver refused")
        return subprocess.CompletedProcess(
            argv, 0, json.dumps({"status": "planned"}), ""
        )

    # Act
    caught: NotifyCredentialError | None = None
    try:
        sync_to_peers(
            ("compute-01", "compute-03"),
            role=ROLE,
            source_passfile=source,
            apply_changes=True,
            runner=runner,
        )
    except NotifyCredentialError as exc:
        caught = exc

    # Assert
    assert (
        isinstance(caught, NotifyCredentialError),
        "compute-03" in str(caught),
        any("--apply" in argv for argv in calls),
    ) == (True, True, False)


def test_ambiguous_source_credentials_are_refused(tmp_path: Path) -> None:
    # Arrange
    source = _private(
        tmp_path / ".pgpass",
        f"*:55432:*:{ROLE}:one\nscitex-primary:55432:scitex:{ROLE}:two\n",
    )

    # Act
    # Assert
    with pytest.raises(NotifyCredentialError, match="ambiguous"):
        sync_to_peers(
            ("compute-01",),
            role=ROLE,
            source_passfile=source,
            apply_changes=False,
        )
