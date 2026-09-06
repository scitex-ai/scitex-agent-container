"""Whether an EMPTY account listing means "no accounts" or "could not look".

``list_accounts()`` is documented to never raise and to return ``[]`` when the
store directory does not exist or is unreadable. That is a deliberate,
long-standing contract and this module does not change it — but it collapses
two facts into one value, and the collapse is the bug:

    []  because this host genuinely has no accounts yet
    []  because we looked somewhere the store is not

MEASURED 2026-08-17, inside the scitex-agent-container agent on compute-04.
``sac accounts list --no-fanout`` printed

    No accounts stored or active. Use: scitex-agent-container account save <name>

while the identical command run as the operator listed FOUR healthy accounts.
The container's ``$HOME`` is ``/home/agent``; the store lives under
``/home/ywatanabe/.scitex/agent-container/accounts`` and only the operator's
home is bound, so ``Path.home()/.scitex/...`` resolved a path that does not
exist. The credential inventory then advised creating an account that already
existed four times over.

In the FLEET view the same collapse is worse than a wrong message: the local
host silently vanishes from the table. My reading showed 16 rows across four
hosts; the operator's showed 20 across five. The missing host was the one the
command was running on. Nothing in the output marked the difference, and I
came within one sentence of reporting the controller's credentials as gone.

So this module answers the question the list cannot: WAS THE STORE READ? It
is three-valued on purpose (§2 of the constitution — "Every signal is
three-valued: true, false, and unknown. Collapsing unknown into either pole
is the most common bug we ship"), and ``account_count`` is ``None`` in every
state except ``readable``, so an unattributable count is not merely undrawn
but unrepresentable.

Named after :func:`~.._account_usage_state.classify_usage`, which already
does this for usage percentages — same shape, same reason, adjacent problem.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["StoreState", "classify_store", "READABLE", "ABSENT", "UNREADABLE"]

READABLE = "readable"
ABSENT = "absent"
UNREADABLE = "unreadable"

_STATES = (READABLE, ABSENT, UNREADABLE)


@dataclass(frozen=True)
class StoreState:
    """sac's STANDING to say how many accounts a host has.

    Attributes
    ----------
    path
        The directory that was actually consulted. Reported even when the
        read failed, because "which path did you look at" is the first
        question a wrong answer raises — and on a container the answer is
        usually the whole explanation.
    state
        ``readable`` / ``absent`` / ``unreadable``.
    account_count
        Number of accounts found, or ``None`` when ``state`` is not
        ``readable``. A count that nobody could take must not be
        expressible as a number.
    """

    path: Path
    state: str
    account_count: int | None

    def __post_init__(self) -> None:
        if self.state not in _STATES:
            raise ValueError(f"state must be one of {_STATES}, got {self.state!r}")
        if self.state == READABLE and self.account_count is None:
            raise ValueError("a readable store must report a count")
        if self.state != READABLE and self.account_count is not None:
            raise ValueError(
                f"a {self.state} store cannot report a count "
                f"(got {self.account_count}) — that is the collapse this "
                f"class exists to prevent"
            )

    @property
    def empty_is_trustworthy(self) -> bool:
        """True iff an empty listing may be reported as "this host has none"."""
        return self.state == READABLE


def classify_store(store_dir: Path | None = None, home: Path | None = None) -> StoreState:
    """Report whether the account store could be read, and how many it holds.

    Resolves the SAME path :func:`.._state.account_store.list_accounts` uses,
    so the two can never disagree about where they looked.
    """
    from .account_store import _store_path

    resolved = _store_path(store_dir, home or Path.home())
    if not resolved.exists():
        return StoreState(path=resolved, state=ABSENT, account_count=None)
    if not resolved.is_dir():
        return StoreState(path=resolved, state=UNREADABLE, account_count=None)
    # stx-allow: fallback (reason: an unreadable dir is a REPORTED state here,
    # not a swallowed error — the return value says so and carries no count)
    try:
        entries = [p for p in resolved.iterdir() if p.is_dir()]
    except OSError:
        return StoreState(path=resolved, state=UNREADABLE, account_count=None)

    from .account_store import list_accounts

    return StoreState(
        path=resolved,
        state=READABLE,
        account_count=len(list_accounts(store_dir=store_dir, home=home)),
    )


def populated_stores_elsewhere(
    exclude: Path, homes_root: Path | None = None
) -> list[Path]:
    """Other account stores on this machine that DO hold accounts.

    THE CASE THIS EXISTS FOR, and it is not the one the three states above
    describe. Measured 2026-08-17 inside an agent container: the resolved
    store was ``/home/agent/.scitex/agent-container/accounts`` — it EXISTED,
    it was readable, and it held zero accounts. So it is not absent and not
    unreadable; it is an empty SHADOW, and a shadow answers "how many
    accounts does this host have" with a confident, well-formed, wrong zero
    while four healthy accounts sit in the operator's home on the same
    machine.

    No classification of the resolved path can catch that, because nothing
    about the resolved path is malformed. The only way to know the zero is
    misleading is to look somewhere else and find the accounts — which is
    what this does.

    Deliberately generic: it scans the home directories that exist on this
    machine rather than naming one. A hardcoded ``/home/ywatanabe`` would
    work here and be wrong on every other host, which is the same
    vantage-point error in a new place.

    ``homes_root`` defaults to ``/home`` and exists so this is TESTABLE.
    Without it the function reads the real machine, and its own unit test
    could not construct a "genuinely empty, no shadow" case on a host that
    has accounts — which is exactly how the first version failed: the test
    asserting the friendly message got the shadow message instead, because
    on this machine the tmp_path store really WAS a shadow. A hidden
    dependency on the live filesystem is not a detail; it is the difference
    between a test that pins behaviour and one that reports the host.
    """
    from .account_store import _DEFAULT_STORE_SUBDIR

    root = homes_root if homes_root is not None else Path("/home")
    found: list[Path] = []
    # stx-allow: fallback (reason: a machine without /home is not an error to
    # report here — it simply has no neighbour stores to offer)
    try:
        homes = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return found
    for home in homes:
        candidate = home / _DEFAULT_STORE_SUBDIR
        if candidate == exclude or not candidate.is_dir():
            continue
        # stx-allow: fallback (reason: an unreadable neighbour home is not
        # this function's problem to report; it simply cannot contribute)
        try:
            if any(p.is_dir() for p in candidate.iterdir()):
                found.append(candidate)
        except OSError:
            continue
    return found


def no_accounts_message(state: StoreState, homes_root: Path | None = None) -> str:
    """The line to print when a listing came back empty.

    Two different sentences for two different facts, which is the whole
    point: telling an operator to create an account, when the truth is that
    sac looked in the wrong place, sends them to fix something that is not
    broken.
    """
    if state.state == ABSENT:
        return (
            f"Account store NOT FOUND at {state.path} — this is NOT the same as "
            f"having no accounts, and nothing here should be read as a count. "
            f"Inside a container the store is usually on the host and not bound; "
            f"check $HOME (this process sees {Path.home()})."
        )
    if state.state == UNREADABLE:
        return (
            f"Account store at {state.path} is UNREADABLE (exists, could not be "
            f"listed) — no count can be taken. Check permissions."
        )
    elsewhere = populated_stores_elsewhere(exclude=state.path, homes_root=homes_root)
    if elsewhere:
        others = ", ".join(str(p) for p in elsewhere)
        return (
            f"No accounts in {state.path} — but accounts DO exist elsewhere on "
            f"this machine: {others}. This store is an empty shadow, not an "
            f"empty fleet; $HOME here is {Path.home()}. Do NOT create a new "
            f"account to fix this — point at the populated store instead."
        )
    return (
        "No accounts stored or active. Use: "
        "scitex-agent-container account save <name>"
    )
