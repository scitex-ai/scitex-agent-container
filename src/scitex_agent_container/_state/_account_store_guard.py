#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Write guards for the account store once it is SHARED with the host.

Why this module exists (2026-08-15)
-----------------------------------
Until now every agent container resolved its OWN private account store
from its own ``$HOME``, so an in-container write into that store could
only damage a directory nobody else read. Card
``sac-container-home-splits-the-account-registry-20260815`` traced a
five-delegate outage to exactly that privacy — the container could not
SEE the fleet's real registry — and
``runtimes/_p3a_default_binds.accounts_store_bind`` now binds the HOST
registry into every container to fix it.

That fix changes what a write means. Three of ``account_store``'s
operations were written against a private directory and answer wrongly
against a shared one mounted read-only:

``save_account``
    raises a bare ``OSError: [Errno 30] Read-only file system`` from
    inside a ``json.dump``, naming a tmp file rather than the fact that
    the caller is a container trying to write the host's registry.

``delete_account``
    is the worse of the three, because it does not fail at all:
    ``shutil.rmtree(..., ignore_errors=True)`` swallows the refusal and
    the function returns ``True``. "Deleted" and "could not delete and
    did not check" have the SAME return value — the failure shape this
    fleet has paid for repeatedly.

``switch_account``
    copies into ``~/.claude/``, and for a PINNED agent that destination
    is not a private file: ``_apptainer_auth_bind.credentials_file_bind``
    binds the host snapshot ``:rw`` at
    ``<container_home>/.claude/.credentials.json``, so the live path and
    ``<store>/<provider>/<account>/.credentials.json`` are ONE inode
    under two names (measured in ``/proc/self/mountinfo`` on
    scitex-compute-04). Switching to account B therefore aims account B's
    credential at account A's registry entry.

    That footgun PRE-DATES the registry bind — the container's private
    store already held one account — but the bind widens its source set
    from one account to every account on the host, which is what makes it
    worth a guard rather than a comment.

What the guard asserts, and what it does not
--------------------------------------------
:func:`snapshot_alias_refusal` answers ONE falsifiable question: does the
file we are about to overwrite share a ``(st_dev, st_ino)`` with a file
inside the account store? Inode identity is the exact property that makes
the write dangerous, not a proxy for it — a bind mount of a file and a
hard link both present the same underlying inode under two names, and
both are caught.

It deliberately does NOT try to predict WHICH wrong outcome follows.
Renaming over a mount point returns ``EBUSY`` on Linux, so the copy may
well fail rather than clobber; but this process cannot prove which of the
two happens on an arbitrary kernel and mount topology, and it does not
need to. Both outcomes are wrong, and both are better replaced by one
refusal that names the account whose snapshot was at risk. Claiming more
than that would be asserting an untested mechanism.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Iterable, Iterator

__all__ = [
    "SharedStoreWriteRefused",
    "removal_did_not_happen_message",
    "snapshot_alias_refusal",
    "store_write_refused",
]

# Depth is bounded because the store's real shape is shallow — measured
# 2026-08-15: ``accounts/<provider>/<account>/<files>`` with legacy flat
# names as symlinks beside the provider dirs, plus the store's own
# ``_rotations/`` bookkeeping. Three levels covers every account file; an
# unbounded walk over an operator's home-adjacent tree is not something a
# credential switch should ever start.
_MAX_STORE_DEPTH = 3


class SharedStoreWriteRefused(OSError):
    """A write into the account store was refused, or provably did not land.

    Subclasses :class:`OSError` on purpose: the call sites it is raised
    from already propagate ``OSError`` today (``save_account`` lets a
    failing ``json.dump`` escape), so existing ``except OSError`` handlers
    keep working and only the message improves.
    """


def _identity(path: Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` for ``path``, or ``None`` if unanswerable.

    Follows symlinks, because that is what the subsequent write does.
    A path that cannot be statted yields ``None`` rather than an
    exception: "I could not find out" must never be reported as "this is
    dangerous", and it must never break the operation being guarded.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _iter_store_files(store: Path) -> Iterator[Path]:
    """Yield regular files under ``store``, bounded and never raising."""
    stack: list[tuple[Path, int]] = [(store, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir():
                    if depth < _MAX_STORE_DEPTH:
                        stack.append((entry, depth + 1))
                elif entry.is_file():
                    yield entry
            except OSError:
                continue


def snapshot_alias_refusal(
    *,
    claude_dir: Path,
    account_dir: Path,
    skip_names: Iterable[str],
    store: Path,
    to_account: str,
) -> str | None:
    """Refusal message if a live path IS a stored snapshot, else ``None``.

    Enumerates the files ``switch_account`` is about to copy out of
    ``account_dir`` (minus ``skip_names``) and checks each corresponding
    destination in ``claude_dir``. A destination that does not exist yet
    cannot alias anything, so the common case costs one failed ``stat``
    per file and no walk of the store at all.

    Never raises — ``switch_account``'s contract is that it returns a
    result dict for every outcome, so a guard that could throw would
    break the very callers it protects.
    """
    try:
        sources = list(account_dir.iterdir())
    except OSError:
        return None
    skip = set(skip_names)
    wanted: dict[tuple[int, int], str] = {}
    for source in sources:
        if source.name in skip:
            continue
        ident = _identity(claude_dir / source.name)
        if ident is not None:
            wanted[ident] = source.name
    if not wanted:
        return None
    for stored in _iter_store_files(store):
        ident = _identity(stored)
        if ident is None or ident not in wanted:
            continue
        filename = wanted[ident]
        # The account is the store-relative parent of the matched file
        # (``<provider>/<account>/.credentials.json`` -> that account).
        try:
            owner = stored.relative_to(store).parts[-2]
        except (ValueError, IndexError):
            owner = stored.parent.name
        return (
            f"refusing to switch to '{to_account}': {claude_dir / filename} "
            f"and {stored} are the SAME FILE (one inode, two names — a bind "
            f"mount or a hard link), so writing '{to_account}' there would "
            f"land on the stored snapshot of account '{owner}' in the shared "
            f"account registry at {store}.\n"
            f"  host:  {socket.gethostname()}\n"
            f"  WHY THIS IS REFUSED RATHER THAN ATTEMPTED: a pinned agent's "
            f"credential is bound :rw from its own snapshot into ~/.claude/, "
            f"so the live file IS a registry entry. The write would either "
            f"overwrite '{owner}''s registered credential with "
            f"'{to_account}''s, or fail with an opaque errno from the kernel "
            f"refusing to rename over a mount point. Both are wrong; this "
            f"message is the third option.\n"
            f"  IF you meant to change which account this agent runs on: "
            f"that is a LAUNCH-time decision (spec.claude.account / "
            f"spec.claude.credentials_files) applied by the host — restart "
            f"the agent through the host rather than swapping the file under "
            f"a running container.\n"
            f"  IF '{owner}' and '{to_account}' are the same account: nothing "
            f"needed doing; this agent already runs on it."
        )
    return None


def store_write_refused(
    *, store: Path, action: str, target: Path, error: OSError
) -> SharedStoreWriteRefused:
    """Build the refusal for a store write the filesystem rejected."""
    return SharedStoreWriteRefused(
        f"cannot {action} in the account registry at {store}: writing "
        f"{target} failed ({error.__class__.__name__}: {error}).\n"
        f"  host:  {socket.gethostname()}\n"
        "  IF YOU ARE INSIDE AN AGENT CONTAINER this is expected and is not "
        "a broken install: the HOST's account registry is bound read-only "
        "into every container so agents can READ the fleet's accounts, "
        "while the host stays its single writer (`sac accounts save` / "
        "`sync-live` / the sac-accounts-refresh timer). See "
        "runtimes/_p3a_default_binds.accounts_store_bind.\n"
        "  FIX: run this command on the host — directly, or from a container "
        "via the sac listen bypass (`host_exec_local`)."
    )


def removal_did_not_happen_message(*, store: Path, account_dir: Path) -> str:
    """Message for a delete that reported nothing and removed nothing."""
    return (
        f"account '{account_dir.name}' was NOT deleted: {account_dir} still "
        f"exists after the removal attempt, so the registry at {store} "
        f"rejected the write.\n"
        f"  host:  {socket.gethostname()}\n"
        "  This is reported rather than swallowed because the removal path "
        "uses `ignore_errors=True`, which would otherwise return the SAME "
        "value for 'deleted it' and 'could not delete it and did not "
        "check'.\n"
        "  IF YOU ARE INSIDE AN AGENT CONTAINER: the host registry is bound "
        "read-only by design — run `sac accounts delete` on the host, or "
        "from a container via the sac listen bypass (`host_exec_local`)."
    )


# EOF
