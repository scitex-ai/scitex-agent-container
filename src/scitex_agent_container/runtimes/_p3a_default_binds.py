"""Fleet-default bind helpers.

Two classes of fleet-wide bind live here today:

* **single-shared-store** — every agent's apptainer container mounts
  the host copy of a shared store, so a resolver keying off the
  AGENT's ``$HOME=/home/agent`` reaches the SAME data fleet-wide.
  Today that is ``~/.scitex/cards`` and
  ``~/.scitex/claude-code-telegrammer``; see each entry's own note
  for the incident that bought it.

  The original member of this class, ``~/.scitex/todo``, was RETIRED
  2026-08-19 and is archived in place at the end of the tuple rather
  than deleted. Its P3a-2 provenance (operator directive
  ``feedback_scitex_todo_single_shared_store``, lead-learnings/22,
  lead a2a ``214dd26d3fd24e088c75a34329895fa4``) is recorded there.

  This module remains the SOLE source of these binds — no fleet
  ``_shared/spec.yaml`` carries them explicitly (lead audit
  2026-06-13 a2a ``f33cbc78c2074594b513439d93748810``), so the helper
  here is what every sac-launched agent picks up at boot. That is
  itself under review: the operator ruled 2026-08-19 that a bind must
  be declared in the spec rather than injected from code
  (「必ずスペックで明示的に渡して下さい」), with sac's own wiring the
  stated exception. See card
  ``sac-remove-implicit-fleet-default-binds-20260819``.

* **2026-06-13 SAC overlay stopgap** — bind the host's working
  ``scitex_agent_container`` source over the in-SIF install so
  agents pick up new CLI surface (e.g., ``sac pytest spartan run``
  from PR #375) WITHOUT a 30-minute SIF rebuild. Read-only because
  the host-side tree is the source of truth; nothing inside the
  container should mutate it. Lead a2a ``b6f3916cdf3544a9`` opened
  this as the fast-path for the spartan-pytest hook rollout.
  Removable: delete the overlay entry once a SIF rebuild folds the
  new package version back into the canonical install.

* **2026-08-15 account registry** — the host's account store bound
  READ-ONLY so an agent can SEE the fleet's credential registry instead
  of resolving a private, near-empty copy from its own ``$HOME``. Unlike
  the entries above its host source is COMPUTED from the SSoT root
  (``_state/state_paths.py``) rather than expanded from a literal ``~``,
  so it lives in :func:`accounts_store_bind` instead of in
  :data:`_FLEET_DEFAULT_BINDS`. Card
  ``sac-container-home-splits-the-account-registry-20260815``.

Mechanism — see :func:`apply_default_binds`:
  * The list of default binds is :data:`_FLEET_DEFAULT_BINDS` —
    extend cautiously, every entry adds a host directory bind
    to every agent.
  * An EXPLICIT ``spec.apptainer.binds`` entry to the SAME
    destination path REPLACES the default (operator override
    wins; we de-dupe by destination, not by full string).
  * Missing host source dir → SKIP that default silently. The
    operator may not have a ``~/.scitex/cards/`` yet (clean
    install, fresh laptop), or a fresh deploy host may not have
    the canonical ``~/proj/scitex-agent-container/`` checkout —
    we don't create either from sac code.

This module is intentionally tiny so the sites that consume the
default-bind list (``_apptainer_runtime.py``) stay under the
512-line module limit.
"""

from __future__ import annotations

import logging
import socket
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

__all__ = [
    "ACCOUNTS_STORE_DST",
    "accounts_store_bind",
    "apply_default_binds",
    "default_binds_for_host",
]


# The CONTAINER-side path of the account-registry bind.
#
# This is NOT a choice. It is the path the in-container resolver ALREADY
# computes: ``_state.account_store._store_path(None, Path("/home/agent"))``
# is ``/home/agent/.scitex/agent-container/accounts`` for an agent whose
# ``$HOME`` is ``/home/agent`` (every sac container). Writing the literal
# here rather than importing the resolver keeps this module free of a
# ``_state`` import at module scope and matches the three sibling store
# entries, which also spell their destination literally.
#
# Guarded by ``test__p3a_accounts_store_bind.py``, which does NOT assert
# this string — it materialises the emitted bind under a fake container
# home and then asks ``list_accounts`` from that vantage point, so a
# destination that drifts from the resolver fails the test by returning
# an empty registry rather than by mismatching a hardcoded literal.
ACCOUNTS_STORE_DST = "/home/agent/.scitex/agent-container/accounts"


# Fleet-wide default binds. Each entry is the string form
# ``host:container[:mode]`` apptainer's ``--bind`` consumes.
# ``~`` is expanded against the host's ``$HOME`` at resolution time.
_FLEET_DEFAULT_BINDS: tuple[str, ...] = (
    # S6 store migration (scitex-todo -> scitex-cards). The reason this bind
    # has to exist at all is NOT obvious: the store
    # resolver keys off the AGENT's $HOME=/home/agent, so host-side reach is
    # not enough. An agent whose spec binds the operator's ENTIRE home rw
    # still cannot resolve ~/.scitex/cards — the data is present and
    # unreachable at the same time, because the resolver never looks there.
    # Measured 2026-07-16 by scitex-cards: `db import` SUCCEEDED into the
    # container overlay and reported success, because /home/agent/.scitex/todo
    # was a bind but /home/agent/.scitex itself was overlay-local.
    #
    # NARROW ON PURPOSE — do NOT widen to "~/.scitex". That parent also holds
    # ~/.scitex/agent-container/accounts (the credential store); a one-line
    # widening would expose it to every agent. One bind per store; each new
    # store pays its own explicit line. That cost is the feature.
    #
    # 2026-08-15 UPDATE — the accounts store has now paid that line, in
    # ``accounts_store_bind()`` below, after the split registry it describes
    # cost a fleet outage. Read this paragraph as it was written: the ban is
    # on WIDENING TO THE PARENT, which would drag `runtime/`, `containers/`
    # and `agents/` in by accident, and it is untouched. The accounts entry
    # names its own directory, resolves its source from the SSoT root rather
    # than `~`, and is `:ro` — which is what turns the incidental exposure
    # this paragraph warns about into a deliberate, argued and narrower one.
    #
    # AND THE PARENT IS NOT ONLY "overlay-local" — 2026-08-08 measured a SECOND
    # way it moves under you, which none of these binds defend against. A
    # dotfiles deploy ran inside a container, treated ~/.scitex as a DOTFILE,
    # moved the real tree aside as .scitex_back_<timestamp> and symlinked its
    # own copy in:
    #     /home/agent/.scitex -> ~/.dotfiles/.worktrees/<branch>/src/.scitex
    # The agent then booted into the substituted tree with a month-stale message
    # store and reported healthy, and the credential path resolved to a
    # directory that does not exist. The operator lost an hour of his evening to
    # it (card sac-cct-store-diverges-across-restart-two-dbs-20260808).
    #
    # WHY THAT MATTERS TO WHOEVER EDITS THIS LIST: a per-store bind here cannot
    # survive its PARENT being replaced. These lines make each store
    # host-persistent, which is necessary and not sufficient. Do not read a
    # green bind as proof the agent is using the store you think it is —
    # `readlink -f` the path from INSIDE the container, which is the only check
    # that sees a substituted parent.
    #
    # Skip-if-missing applies (see default_binds_for_host): on a host with no
    # ~/.scitex/cards this entry is a SILENT no-op — safe, but NOT a signal.
    # Verify a rollout by comparing dev:inode from INSIDE a booted container
    # against the host; never by reading the argv, which cannot tell a bind
    # that landed from one that was skipped.
    # Piloted per-agent first (dotfiles e2b72e8a, scitex-cards spec); its
    # in-container stat confirmed one directory / two names before this went
    # fleet-wide.
    "~/.scitex/cards:/home/agent/.scitex/cards:rw",
    # The operator's OWN MESSAGE HISTORY — third store to pay the toll above,
    # and the one whose absence he felt directly. On 2026-08-08 he asked whether
    # I remembered eleven things he had sent an hour earlier; my previous run had
    # answered every one of them, and my restarted run could find none. He
    # forwarded the whole conversation back by hand: 「忘れている、思い出せない、
    # となると結構辛いです。」
    #
    # cct had done its part correctly — it migrated that day to the
    # scitex-standard deterministic path (ts/lib/config.ts::resolveStateDir,
    # ~/.scitex/claude-code-telegrammer/runtime/<agent>), whose own docstring
    # names "a drifting default path opened a fresh empty DB and lost the
    # operator's message history" as the reason it exists. sac never grew the
    # matching bind, so that store landed OVERLAY-LOCAL — the exact shape the
    # cards note above records from 2026-07-16, one store later.
    #
    # Bound at the PACKAGE root (not .../runtime/<agent>) so the per-agent
    # subdir cct creates on first boot lands on the host rather than in the
    # overlay; a bind of a not-yet-existing leaf would skip silently.
    "~/.scitex/claude-code-telegrammer:/home/agent/.scitex/claude-code-telegrammer:rw",
    # 2026-06-13 STOPGAP (lead a2a b6f3916c) — bind the host's working
    # ``scitex_agent_container`` source over the in-SIF install so
    # agents pick up new CLI surface (e.g., ``sac pytest spartan run``
    # from PR #375) WITHOUT a 30-minute SIF rebuild. Read-only because
    # the host-side tree is the source of truth; nothing inside the
    # container should mutate it.
    #
    # Removable: delete this entry once a SIF rebuild folds the new
    # package version back into the canonical install. The
    # ``default_binds_for_host`` skip-if-missing filter makes the
    # entry a no-op on hosts that don't carry the canonical repo
    # path (e.g., a fresh deploy box). Per-agent spec overrides via
    # ``apptainer.binds`` for the SAME destination still win
    # through ``apply_default_binds``'s de-dup-by-destination merge.
    #
    # Pinned to python3.12 because every SAC SIF def
    # (apptainer-base.def + apptainer-scitex.def) uses ``/opt/venv-sac``
    # with Python 3.12 today; the bind silently skips if a future SIF
    # moves to 3.13 (the destination dir won't exist inside that SIF,
    # apptainer surfaces a benign warning) — operator notices and
    # either updates the entry or drops it after the SIF refresh.
    "~/proj/scitex-agent-container/src/scitex_agent_container"
    ":/opt/venv-sac/lib/python3.12/site-packages/scitex_agent_container:ro",
    # HAZARD — a dev-source bind MUST target a destination that EXISTS
    # in the SIF. ``default_binds_for_host`` filters ONLY by host-source
    # existence; it cannot see inside the SIF. apptainer normally
    # auto-creates a missing bind destination, BUT under ``--containall``
    # + a directory overlay that is slow to mount (host contention) the
    # auto-create loses the race and apptainer FATALs the WHOLE boot
    # ("destination ... doesn't exist in container") — a silent-looking,
    # NON-DETERMINISTIC death (empty pane, session vanishes at t=0).
    #
    # A second bind to ``/opt/venv-agent/lib/.../scitex_agent_container``
    # used to live here (2026-06-15) to shadow a broken stub that an
    # OLDER SIF build shipped at that path. The canonical sac-base.sif
    # now installs sac under ``/opt/venv-sac`` ONLY — there is NO
    # ``/opt/venv-agent`` in the SIF at all (verified by probe) — so that
    # bind targeted a nonexistent destination and FATAL-killed every boot
    # whose overlay was contended (proj-paper-scitex-clew died instantly
    # 3× while neurovista, winning the same race, came up). Removed
    # 2026-06-23. The ``/opt/venv-sac`` bind above already covers the
    # canonical install. If a future SIF reintroduces a second venv
    # prefix, add its bind ONLY after confirming the destination dir
    # exists in that SIF.
    #
    # Operator handoff path (card sac-bind-host-tmp-emacs-handoff) — under
    # ``--containall`` the host ``/tmp`` is isolated, so agents cannot read
    # the UI debug + screenshot context the operator hands over at
    # ``/tmp/emacs-claude-code/`` (``Element_Debug_Info_*.txt`` etc.), and
    # ``ssh ywata-note-win`` from inside the container is refused. Bind the
    # handoff dir READ-ONLY so an agent reads the file at the SAME path the
    # operator names, with no manual copy-into-home step. Destination is a
    # ``/tmp`` tmpfs path (writable under --containall), so apptainer's
    # bind-dest auto-create cannot lose the overlay race the HAZARD above
    # describes. ``default_binds_for_host`` skips it silently on hosts/times
    # where the source dir does not exist (remote hosts, fresh boot before
    # emacs writes) — no FATAL, no surprise mount.
    "/tmp/emacs-claude-code:/tmp/emacs-claude-code:ro",
    # GENERAL HOST-/tmp HANDOFF — generalise the narrow emacs entry above
    # into an operator->agent file-handoff channel: anything the operator
    # drops in host ``/tmp`` becomes readable in-container at
    # ``/tmp/host/...``. READ-ONLY because host ``/tmp`` holds other
    # processes' tempfiles + live sockets — an agent must NEVER clobber it.
    # Destination ``/tmp/host`` is a SUBPATH under the container's writable
    # ``/tmp`` tmpfs (writable under --containall), so apptainer's bind-dest
    # auto-create cannot lose the overlay race the HAZARD above describes
    # (same reasoning as the emacs entry). Host ``/tmp`` ALWAYS exists, so
    # this default always applies. Do NOT bind over ``/tmp`` ITSELF — that
    # would clobber the container's relocated scratch, the ``/tmp/sac-claude``
    # credentials bind, and the nested-apptainer cache; mounting at the
    # ``/tmp/host`` subpath sits harmlessly inside the tmpfs.
    "/tmp:/tmp/host:ro",
    # REMOVED — the persistent-testmon-cache bind
    # (``~/.cache/scitex-testmon:/home/agent/.cache/scitex-testmon:rw``), and
    # with it the ``SCITEX_TESTMON_CACHE_ROOT`` injection in
    # ``_apptainer_listen_env.listen_env_flags``.
    #
    # It bound a host directory RW into EVERY agent container to serve a
    # scitex-dev pre-commit-hook wrapper that this comment described, in the
    # future tense, as something a peer "IS BUILDING". The package that owns
    # that wrapper has since declared it DEAD:
    #
    #   scitex_dev/_skills/general/05_development/15_pre-commit-policy.md —
    #   "scitex-dev-testmon exists and is BROKEN ... it is referenced by ZERO
    #    repos, and scitex-dev does not dogfood it. Do not build another one."
    #
    # and its audit rule PS-HOOK-001 (severity E) mechanically FORBIDS that
    # hook's shape: ``python3 -m pytest --testmon`` under ``language: system``
    # is a bare $PATH lookup, so it resolves to a different interpreter on
    # every machine.
    #
    # sac's own ``.pre-commit-config.yaml`` never referenced testmon at all. So
    # this was speculative plumbing for someone else's unshipped feature, kept
    # alive by a comment written in the future tense — a rw host bind and an
    # env var on every agent in the fleet, serving nothing.
    #
    # The superseding policy is not sac's to restate: pre-commit runs fast,
    # bounded, deterministic checks and does NOT run the test suite. CI runs
    # the tests. There is no test suite in the commit path for a testmon cache
    # to accelerate.
    #
    # RETIRED 2026-08-19 — "~/.scitex/todo:/home/agent/.scitex/todo:rw"
    #   (was: P3a-2, scitex-todo single shared store, operator directive
    #    feedback_scitex_todo_single_shared_store)
    #
    # Archived rather than deleted, on the operator's instruction that a
    # retired thing should stay findable: 「消すというよりアーカイブでいい
    # んじゃないですかね。すなわち探そうと思えば探せるみたいな」. A silent
    # absence tells the next reader nothing about why the entry went, and
    # invites someone to "fix" its absence by adding it back.
    #
    # WHY IT WENT: scitex-todo was superseded by scitex-cards, and the
    # HOME-level store the bind targets is no longer read by anything.
    # Measured 2026-08-19 with rg --no-ignore over every checkout under
    # ~/proj, carrying a control term in the same pass so a zero could not
    # mean "the search did not run":
    #     scitex-cards       0 hits in *.py (docs/logs only)
    #     scitex-live-paper  prose in docs/research/, and a PROJECT-LOCAL
    #                        .scitex/todo/tasks.yaml, not ~/.scitex/todo
    #     scitex-hub         LIVE CODE at apps/workspace/todo_app/
    #                        middleware.py:302 — but it builds
    #                        base / project.slug / ".scitex" / "todo" /
    #                        "tasks.yaml" where base is a WORKSPACE root, not
    #                        $HOME, and refuses any store escaping it
    # The host directory itself held one 4-byte board.pid naming a pid that
    # is not running.
    #
    # THE TRAP, recorded because the naive check gets it backwards: the
    # string ".scitex/todo" names TWO unrelated things — a per-project store
    # under a workspace, and this HOME-level one. A substring search cannot
    # tell them apart, and hub's five hits argued for keeping the bind until
    # the paths were actually read. Matching the string is not matching the
    # dependency.
    #
    # If something turns out to need it, prefer an EXPLICIT bind in that
    # agent's spec over restoring a fleet-wide default (operator, same day:
    # 「必ずスペックで明示的に渡して下さい」).
)


def _bind_destination(bind_str: str) -> str:
    """Return the container-side destination path of a bind string.

    Accepts ``host:container`` and ``host:container:mode`` shapes
    (the only two apptainer ``--bind`` consumes). Falls back to
    the whole string for a malformed entry so the caller's de-dup
    set still gets a stable key.
    """
    if ":" not in bind_str:
        return bind_str
    _, _, rest = bind_str.partition(":")
    return rest.split(":", 1)[0]


def accounts_store_bind() -> str | None:
    """Return the READ-ONLY bind of the HOST account registry, or ``None``.

    The fourth store to pay the toll the ``cards`` entry above states in
    general terms, and the first one whose absence cost a fleet outage.

    2026-08-15, measured on scitex-compute-04: in-container ``sac accounts
    list`` read ``/home/agent/.scitex/agent-container/accounts`` and found
    ONE account — the one that had just been created there. The host store
    at ``/home/ywatanabe/.scitex/agent-container/accounts`` held FOUR and
    was readable from inside the container the whole time; the resolver
    simply never looked there, because it keys off the AGENT's ``$HOME``.
    When the weekly quota on the container's single account ran out, five
    delegates died within a minute and nothing inside the container could
    list, diagnose, or pick a replacement — the registry was present and
    unreachable at the same time. That is the ``cards`` comment's sentence,
    written for a different store on 2026-07-16, coming true verbatim.

    WHY THE SOURCE IS COMPUTED AND NOT A LITERAL ``~``
    --------------------------------------------------
    The three sibling entries hardcode ``~``, which silently misses a
    ``$SCITEX_DIR``-relocated root — the precise sin
    ``_state/state_paths.py``'s own docstring was written to end ("resolving
    the root and then ignoring it in the next module ... produces state
    SPLIT across two roots, which is harder to reason about than state
    consistently in one wrong place"). A credential registry resolved from
    the wrong root is the same outage in a new costume, so this entry goes
    through :func:`_state.state_paths.agent_container_root` and is built at
    call time rather than living as a literal tuple string.

    WHY ``:ro``, DELIBERATELY UNLIKE ITS THREE SIBLINGS
    ---------------------------------------------------
    Those stores' single writer IS the agent. This store's single writer is
    the HOST — ``sac accounts save`` / ``sync-live`` / the
    ``sac-accounts-refresh`` timer (ADR-0017, one-account-one-refresher).
    ``:ro`` leaves today's write topology byte-identical: the credential
    the agent actually runs on is a SEPARATE ``:rw`` bind emitted by
    ``_apptainer_auth_bind.credentials_file_bind`` (host snapshot file ->
    ``<container_home>/.claude/.credentials.json``) or by
    ``_apptainer_auth.auth_argv`` (account dir -> ``/tmp/sac-claude``), and
    a per-mount option here cannot reach either of them. So token refresh
    recording is untouched and this change is purely additive on the read
    side.

    It also keeps this change INDEPENDENT of the identity-verification work
    (direction (c) on the card): a shared registry plus today's unverified
    ``sac accounts save <anything>`` would let any container file a
    mislabeled credential under any account name on the HOST. ``:ro`` means
    that mistake stays where it is today — contained.

    LOUD, NOT SILENT
    ----------------
    :func:`default_binds_for_host` skips a missing host source SILENTLY,
    which is documented there as deliberate and is correct for a suggestion
    like the todo store. It is NOT correct for a credential registry:
    measured in the same container on the same morning, the
    ``~/.scitex/claude-code-telegrammer`` bind was ABSENT from
    ``/proc/self/mountinfo`` although its directory existed in the overlay —
    the silent no-op is real and was live on one of the three siblings. A
    registry bind that skips silently reproduces the outage while looking
    configured, so this one says so, names the RESOLVED host path (the root
    is the thing most likely to be wrong) and the account count, and points
    at the remedy.

    The count is not decoration. ``_apptainer_bind_guard``'s whole lesson is
    that source-exists is a different question from capability-delivered: a
    registry directory that holds zero accounts mounts perfectly and hands
    the agent nothing.
    """
    from .._state.state_paths import agent_container_root

    src = agent_container_root() / "accounts"
    # ``Path.is_dir()`` is not total: it swallows only
    # ``pathlib._IGNORED_ERRNOS`` (ENOENT/ENOTDIR/EBADF/ELOOP) and RE-RAISES
    # the rest, so an EACCES from a 0700 parent or an ESTALE/ETIMEDOUT from
    # an autofs/NFS hiccup would escape into ``build_run_argv`` — a pure
    # function whose callers treat a raise as "refuse this start". Grounding
    # an agent on an unanswerable stat is the outage
    # ``_apptainer_bind_guard`` is explicitly unwilling to trade for, so the
    # verdict here matches that module's: name the errno, and continue
    # without the bind.
    try:
        present = src.is_dir()
    except OSError as exc:  # stx-allow: fallback (reason: see inline comment)
        logger.error(
            "ACCOUNT REGISTRY UNVERIFIABLE: could not check %s on host %s "
            "(%s: %s), so this process can prove neither that the registry "
            "is there nor that it is missing. NOT refusing the start — a "
            "refusal must be earned by proof, and an unanswerable stat "
            "proves nothing. The agent will resolve its own private %s and "
            "see an EMPTY registry; if `sac accounts list` reports nothing "
            "inside the container, START HERE.",
            src,
            socket.gethostname(),
            exc.__class__.__name__,
            exc,
            ACCOUNTS_STORE_DST,
        )
        return None
    if not present:
        logger.warning(
            "ACCOUNT REGISTRY NOT BOUND: no host account store at %s (host "
            "%s), so every agent launched from here resolves its own private "
            "%s and will see an EMPTY registry. Consequence: the agent cannot "
            "list accounts, cannot tell a quota-exhausted credential from an "
            "unconfigured one, and cannot pick a replacement — it can only "
            "die and wait for a human (measured 2026-08-15, five delegates "
            "lost in one minute). FIX: run `sac accounts save <name>` on THIS "
            "host to create the store, or correct $SCITEX_DIR if the registry "
            "lives under a different root than the one resolved above.",
            src,
            socket.gethostname(),
            ACCOUNTS_STORE_DST,
        )
        return None
    # ``list_accounts`` never raises and takes no locks; with an explicit
    # ``store_dir`` it also skips the ``_ensure_short_name_alias`` side
    # effect, so this stays a pure read of a directory we just statted.
    from .._state.account_store import list_accounts

    count = len(list_accounts(store_dir=src))
    if count == 0:
        logger.warning(
            "ACCOUNT REGISTRY IS EMPTY: %s exists and will be bound read-only "
            "at %s, but holds NO accounts. The mount will succeed and deliver "
            "nothing — inside the container that is indistinguishable from "
            "'this agent was never granted an account'. FIX: `sac accounts "
            "save <name>` on host %s.",
            src,
            ACCOUNTS_STORE_DST,
            socket.gethostname(),
        )
    else:
        logger.info(
            "account registry bound read-only: %s -> %s (%d account(s)). "
            "Verify from INSIDE the container with `stat -c %%d:%%i %s` "
            "against the host path — never by reading this argv, which "
            "cannot tell a bind that landed from one that was skipped.",
            src,
            ACCOUNTS_STORE_DST,
            count,
            ACCOUNTS_STORE_DST,
        )
    return f"{src}:{ACCOUNTS_STORE_DST}:ro"


def default_binds_for_host() -> tuple[str, ...]:
    """Return the fleet-default binds whose host source EXISTS today.

    Walks :data:`_FLEET_DEFAULT_BINDS`, expands ``~`` against the
    operator's ``$HOME``, and FILTERS each entry by whether the
    host-side source path resolves to an existing directory. Missing
    host source = the default skips silently — sac does NOT mkdir on
    the host (the bound layout's ownership lives with whoever owns
    the source tree, e.g. scitex-todo for ``~/.scitex/todo/``).

    The returned tuple uses the EXPANDED absolute host path —
    apptainer's ``--bind`` does NOT expand ``~`` (it resolves it as a
    literal dir relative to CWD, causing a FATAL mount failure), so we
    expand against ``$HOME`` here before handing it to ``--bind``.
    """
    out: list[str] = []
    for bind_str in _FLEET_DEFAULT_BINDS:
        if ":" not in bind_str:
            continue
        host_src, _, rest = bind_str.partition(":")
        expanded = Path(host_src).expanduser()
        if expanded.is_dir():
            # Return the EXPANDED absolute host path. apptainer's
            # ``--bind`` does NOT expand ``~`` (it treats it as a
            # literal dir relative to CWD -> FATAL mount failure), so
            # we must hand it an absolute source. Bug fix 2026-06-13:
            # the literal ``~/.scitex/todo`` form broke every agent's
            # boot on restart.
            out.append(f"{expanded}:{rest}")
    # The account registry is appended rather than listed in
    # :data:`_FLEET_DEFAULT_BINDS` because its HOST source must be COMPUTED
    # from the SSoT root instead of expanded from a literal ``~`` — see
    # :func:`accounts_store_bind` for why that distinction is load-bearing
    # for this particular store. It is still an ordinary default in every
    # other respect: ``apply_default_binds`` de-dups it by destination, so
    # an explicit ``spec.apptainer.binds`` entry to the same destination
    # still overrides it (the operator's spec is the operator's last word).
    accounts = accounts_store_bind()
    if accounts is not None:
        out.append(accounts)
    return tuple(out)


def apply_default_binds(spec_binds: Iterable[str]) -> list[str]:
    """Merge fleet-default binds with the spec's explicit binds.

    Returns a list of bind strings (apptainer ``--bind`` ready) with
    fleet defaults PREPENDED and any explicit spec entry to the SAME
    destination path overriding the default (de-dup by destination —
    the operator's spec is the operator's last word).

    The fleet defaults are filtered by host-source existence via
    :func:`default_binds_for_host` BEFORE merge, so a missing
    ``~/.scitex/todo/`` (operator hasn't initialised the store)
    produces NO bind, NO crash, no surprise mount.
    """
    spec_binds_list = list(spec_binds)
    spec_destinations = {_bind_destination(b) for b in spec_binds_list}
    defaults_that_apply = [
        b
        for b in default_binds_for_host()
        if _bind_destination(b) not in spec_destinations
    ]
    return defaults_that_apply + spec_binds_list
