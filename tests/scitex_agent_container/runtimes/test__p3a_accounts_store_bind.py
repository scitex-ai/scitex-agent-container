"""The account registry must read the SAME on both sides of a container.

Card ``sac-container-home-splits-the-account-registry-20260815``. On
2026-08-15 an agent inside a container ran ``sac accounts list``, resolved
``/home/agent/.scitex/agent-container/accounts`` from its own ``$HOME``,
and found ONE account. The host store held FOUR and was readable from
inside the container the whole time. When that one account's weekly quota
ran out, five delegates died within a minute and nothing in the container
could list, diagnose or pick a replacement.

WHAT THE CENTRAL TEST ASSERTS, AND WHY IT IS NOT A RESTATEMENT
---------------------------------------------------------------
A test that asserts ``default_binds_for_host()`` contains a particular
string would restate the change and would pass for a bind pointing
anywhere. The defect was never "the list lacks an entry" — it was "the two
vantage points disagree". So
``test_an_agent_with_a_container_home_sees_the_host_account_registry``
never names the accounts bind at all. It:

  1. builds a realistic HOST registry (provider dirs, legacy flat
     symlinks, the store's own ``_rotations/`` bookkeeping);
  2. takes the fleet-default bind list as production emits it and
     MATERIALISES every entry whose destination lands under the container
     home — deriving that container home from a SIBLING bind, so the test
     hardcodes neither ``/home/agent`` nor the accounts destination;
  3. asks ``list_accounts`` from the CONTAINER's ``$HOME``;
  4. asserts it agrees with a direct read of the host store.

It fails on the pre-change code (nothing is materialised, so the container
view is empty), and it keeps failing if a later edit points the bind at
the wrong source, drops it, or spells the destination one component away
from what ``account_store._store_path`` computes.

WHAT IT DOES NOT PROVE — stated plainly rather than implied
------------------------------------------------------------
A symlink stands in for the bind mount, because creating a mount needs
privileges CI does not have. The substitution is faithful for the ONE
thing under test — path resolution on every ``open`` — and for nothing
else. It does not prove apptainer emits the mount, that ``--containall``
preserves it, or that ``:ro`` is honoured by the kernel. Those are one
in-container measurement after a restart onto the new HOST build:
``stat -c %d:%i`` of the container path must equal the host directory's,
and ``list_accounts()`` from inside must return the host's account set.
The module comment in ``_p3a_default_binds`` says the same thing: never
verify a bind by reading the argv.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._state.account_store import list_accounts
from scitex_agent_container.runtimes._p3a_default_binds import (
    accounts_store_bind,
    default_binds_for_host,
)

_ACCOUNTS = (
    "scitex-01-scitex-ai",
    "wyusuuke-gmail-com",
    "ywata1989-gmail-com",
    "ywatanabe-scitex-ai",
)


@pytest.fixture
def host_home(tmp_path: Path) -> Iterator[Path]:
    """A sandboxed HOST ``$HOME`` with ``$SCITEX_DIR`` neutralised.

    Clearing ``SCITEX_DIR`` matters: the accounts bind resolves its source
    through ``state_paths.agent_container_root()``, which reads that
    variable per call. An ambient value would silently point the test at
    the real fleet registry — the same "resolved somewhere else than you
    think" family as the bug under test.
    """
    home = tmp_path / "host-home"
    home.mkdir()
    saved = {k: os.environ.get(k) for k in ("HOME", "SCITEX_DIR")}
    os.environ["HOME"] = str(home)
    os.environ.pop("SCITEX_DIR", None)
    try:
        yield home
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def container_home(tmp_path: Path) -> Path:
    """A stand-in for the container's private ``$HOME`` (``/home/agent``)."""
    home = tmp_path / "container-home"
    home.mkdir()
    return home


_ROOT_SKIP = pytest.mark.skipif(
    os.geteuid() == 0,
    reason="running as root traverses any directory, so EACCES cannot be staged",
)


@pytest.fixture
def unreadable_registry_parent(host_home: Path) -> Iterator[Path]:
    """A registry whose parent cannot be traversed, so its stat is EACCES."""
    parent = host_home / ".scitex" / "agent-container"
    (parent / "accounts").mkdir(parents=True)
    parent.chmod(0o000)
    try:
        yield parent
    finally:
        parent.chmod(0o700)


def _write_account(account_dir: Path, *, email: str) -> None:
    """Create one account in the store's REAL on-disk shape."""
    account_dir.mkdir(parents=True, exist_ok=True)
    (account_dir / "account.json").write_text(
        json.dumps({"name": account_dir.name, "email_address": email}),
        encoding="utf-8",
    )
    (account_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": f"token-for-{email}"}}),
        encoding="utf-8",
    )


def _populate_host_registry(home: Path) -> Path:
    """Build the store as measured on scitex-compute-04, and return it.

    ``accounts/<provider>/<name>/`` with legacy flat names as symlinks
    beside the provider dirs, plus the store's own ``_rotations/``. Using
    the real shape rather than four flat directories is what keeps the
    test honest about the reader: provider dirs and underscore dirs must
    NOT be counted as accounts.
    """
    store = home / ".scitex" / "agent-container" / "accounts"
    for name in _ACCOUNTS:
        _write_account(store / "anthropic" / name, email=f"{name}@example.test")
        (store / name).symlink_to(Path("anthropic") / name)
    _write_account(store / "openai" / "some-openai-account", email="oai@example.test")
    (store / "_rotations").mkdir(parents=True, exist_ok=True)
    return store


def _container_home_from_siblings(binds: tuple[str, ...]) -> str:
    """Derive the fleet's container home from the cards bind's destination.

    Reading it off a SIBLING rather than hardcoding ``/home/agent`` means
    this helper cannot silently agree with a wrong constant in the module
    it is testing.
    """
    cards = next(b for b in binds if "/.scitex/cards:" in b)
    return str(Path(cards.split(":")[1]).parents[1])


def _materialise(binds: tuple[str, ...], into: Path, container_root: str) -> int:
    """Stand in for apptainer: re-root each bind's destination under ``into``.

    Returns how many entries were materialised, so a caller can tell
    "mounted nothing" from "mounted the wrong thing".
    """
    made = 0
    for bind in binds:
        parts = bind.split(":")
        if len(parts) < 2:
            continue
        source, destination = parts[0], parts[1]
        if not destination.startswith(container_root + "/"):
            continue
        target = into / Path(destination).relative_to(container_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            continue
        target.symlink_to(source)
        made += 1
    return made


# ---------------------------------------------------------------------------
# The central assertion — the two vantage points must agree
# ---------------------------------------------------------------------------


def test_an_agent_with_a_container_home_sees_the_host_account_registry(
    host_home: Path, container_home: Path
) -> None:
    # Arrange — a populated HOST registry, plus the cards store so the
    # container root can be derived from a sibling bind rather than typed.
    store = _populate_host_registry(host_home)
    (host_home / ".scitex" / "cards").mkdir(parents=True)
    binds = default_binds_for_host()
    root = _container_home_from_siblings(binds)
    _materialise(binds, container_home, root)
    # Act — the SAME reader, from the container's own $HOME.
    container_view = {a["name"] for a in list_accounts(home=container_home)}
    # Assert — agreement with a direct read of the host store.
    assert container_view == {a["name"] for a in list_accounts(store_dir=store)}


def test_the_container_view_is_the_four_real_accounts_not_an_empty_set(
    host_home: Path, container_home: Path
) -> None:
    # Arrange — guards the test above against passing by mutual emptiness:
    # two vantage points that both see NOTHING also "agree".
    _populate_host_registry(host_home)
    (host_home / ".scitex" / "cards").mkdir(parents=True)
    binds = default_binds_for_host()
    _materialise(binds, container_home, _container_home_from_siblings(binds))
    # Act
    container_view = {a["name"] for a in list_accounts(home=container_home)}
    # Assert — provider dirs and _rotations/ excluded, legacy symlinks kept.
    assert container_view == set(_ACCOUNTS)


# ---------------------------------------------------------------------------
# The bind itself — source resolution, mode, blast radius
# ---------------------------------------------------------------------------


def test_the_accounts_bind_source_follows_a_relocated_scitex_root(
    host_home: Path, tmp_path: Path
) -> None:
    # Arrange — the sin state_paths.py exists to end: a root resolved
    # correctly in one module and re-expanded as `~` in the next. A literal
    # `~/.scitex/...` tuple entry would resolve under host_home and miss
    # this store entirely.
    relocated = tmp_path / "relocated-scitex"
    store = relocated / "agent-container" / "accounts"
    _write_account(store / "anthropic" / "only-here", email="only@example.test")
    os.environ["SCITEX_DIR"] = str(relocated)
    # Act
    bind = accounts_store_bind()
    # Assert
    assert bind is not None and bind.split(":")[0] == str(store)


def test_the_accounts_bind_is_read_only(host_home: Path) -> None:
    # Arrange — deliberately unlike its three :rw sibling stores. Their
    # single writer is the agent; this store's single writer is the HOST
    # (`sac accounts save` / `sync-live` / the refresh timer). :ro keeps
    # today's write topology byte-identical and keeps this change
    # independent of the identity-verification work on the same card.
    _populate_host_registry(host_home)
    # Act
    bind = accounts_store_bind()
    # Assert
    assert bind is not None and bind.endswith(":ro")


def test_the_accounts_bind_does_not_expose_the_agent_container_parent(
    host_home: Path,
) -> None:
    # Arrange — the module comment forbids widening to a parent, and names
    # THIS store as the reason. The parent also holds runtime/, containers/
    # and agents/; binding it would drag them in by accident.
    _populate_host_registry(host_home)
    (host_home / ".scitex" / "cards").mkdir(parents=True)
    # Act
    destinations = {b.split(":")[1] for b in default_binds_for_host() if ":" in b}
    # Assert
    assert not destinations & {
        "/home/agent/.scitex",
        "/home/agent/.scitex/agent-container",
    }


def test_an_explicit_spec_bind_still_overrides_the_accounts_default(
    host_home: Path,
) -> None:
    # Arrange — the operator's spec is the operator's last word; the
    # computed entry must stay an ordinary default under de-dup-by-dest.
    _populate_host_registry(host_home)
    from scitex_agent_container.runtimes._p3a_default_binds import (
        ACCOUNTS_STORE_DST,
        apply_default_binds,
    )

    override = f"/somewhere/else:{ACCOUNTS_STORE_DST}:rw"
    # Act
    merged = apply_default_binds([override])
    # Assert
    assert [b for b in merged if b.split(":")[1] == ACCOUNTS_STORE_DST] == [override]


# ---------------------------------------------------------------------------
# The skip must be LOUD — measured live on one of the three siblings
# ---------------------------------------------------------------------------


def test_a_missing_host_registry_is_reported_not_skipped_silently(
    host_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange — no store on this host. The generic skip-if-missing filter
    # would drop the entry without a word; measured 2026-08-15, the
    # claude-code-telegrammer bind was absent from /proc/self/mountinfo
    # while its directory existed, so the silent no-op is real. A registry
    # bind that skips silently reproduces the outage while looking
    # configured.
    caplog.set_level(logging.WARNING)
    # Act
    bind = accounts_store_bind()
    # Assert — the message must name the RESOLVED path, since a wrong root
    # is the most likely cause and the one an argv cannot show.
    assert bind is None and str(host_home / ".scitex/agent-container/accounts") in (
        caplog.text
    )


def test_a_registry_that_holds_no_accounts_is_reported(
    host_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange — source-exists is a different question from
    # capability-delivered (_apptainer_bind_guard's whole lesson): an empty
    # registry directory mounts perfectly and hands the agent nothing.
    (host_home / ".scitex" / "agent-container" / "accounts").mkdir(parents=True)
    caplog.set_level(logging.WARNING)
    # Act
    accounts_store_bind()
    # Assert
    assert "ACCOUNT REGISTRY IS EMPTY" in caplog.text


@_ROOT_SKIP
def test_an_unverifiable_registry_does_not_ground_the_start(
    unreadable_registry_parent: Path,
) -> None:
    # Arrange — Path.is_dir() RE-RAISES EACCES rather than answering False,
    # and this runs inside build_run_argv, whose callers read a raise as
    # "refuse this start". A permission or transient-I/O error must not
    # ground an agent.
    expected_when_unanswerable = None
    # Act
    bind = accounts_store_bind()
    # Assert
    assert bind is expected_when_unanswerable


@_ROOT_SKIP
def test_an_unverifiable_registry_names_the_errno(
    unreadable_registry_parent: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange — continuing silently here would be the silent skip again,
    # one layer down.
    caplog.set_level(logging.ERROR)
    # Act
    accounts_store_bind()
    # Assert
    assert "PermissionError" in caplog.text


def test_a_bound_registry_records_the_resolved_path_and_the_account_count(
    host_home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange — the boot log is what a diagnostician reads at 03:00; it has
    # to say WHERE the registry came from and HOW MANY accounts it carries.
    _populate_host_registry(host_home)
    caplog.set_level(logging.INFO)
    # Act
    accounts_store_bind()
    # Assert
    assert f"({len(_ACCOUNTS)} account(s))" in caplog.text
