"""An ISOLATED fleet root + board, for the ``sac agents rename`` tests.

A rename test that reached the real fleet would move a live agent's spec
dir, overlay and state dir, and REASSIGN ITS CARDS. So isolation is not a
tidiness concern here — it is the first requirement, and it is asserted,
not assumed (``tests/.../test__rename_isolation.py``).

Two escape routes exist and both are closed:

1. **Paths.** ``Layout`` derives every path from an injectable ``root``.
   That is deliberate: sac's own module-level defaults
   (``Registry.REGISTRY_DIR``, ``_session_state.DEFAULT_STATE_ROOT``,
   ``state_db.DEFAULT_DB_PATH``) are computed from ``$HOME`` at IMPORT
   time, so a fixture that only sets ``$HOME`` CANNOT redirect them — it
   would read and write the live fleet while looking isolated.

2. **The board.** Every scitex-cards call takes an explicit ``store=``.
   That explicit argument is the PRIMARY isolation and it is what these
   tests actually rely on.

   Belt and braces, :func:`isolated_board` ALSO points
   ``$SCITEX_TODO_TASKS_YAML_SHARED`` at the tmp store, so a call that
   forgot to pass ``store=`` would land in the tmp file rather than on the
   real board. WARNING, 2026-08-16: that second net is now INERT —
   scitex_cards does not read ``SCITEX_TODO_TASKS_YAML_SHARED`` (its axis
   is ``SCITEX_CARDS_DB``), so a forgotten ``store=`` would no longer be
   caught. The env var is left as-is rather than renamed on a guess:
   pointing it at the right variable without checking what scitex_cards
   actually resolves would restore the APPEARANCE of a safety net while a
   forgotten ``store=`` reached the live board. Verify the resolution
   first, then re-arm it.

No mocks: the store is a real YAML file that real ``scitex_todo`` reads
and writes, and the state.db is a real SQLite file with the real schema.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from scitex_agent_container._lifecycle._rename_plan import Layout

# A spec that names itself in every place the rename must find. Modelled
# on the live project-maintainer shape (relaxed apptainer + directory
# overlay + raw_args env block), with the comments kept so the tests also
# prove comment preservation.
SPEC_TEMPLATE = """\
# {name} — per-package maintainer agent.
#
# This comment block is LOAD-BEARING: it is the operator's record of why
# each flag below is set, and a rename must not destroy it.

apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    project: {name}
    purpose: {name}-maintainer
    role: project-maintainer
    groups: [developer, infra]

spec:
  runtime: tui
  host: test-host
  # The --pwd the agent opens at.
  workdir: {home}/proj/{name}

  apptainer:
    image: {home}/.scitex/agent-container/containers/sac-base.sif
    relaxed: true
    binds:
      - {home}:{home}:rw
      - {home}/.ssh:/home/agent/.ssh:ro
    raw_args:
      - --userns
      - --containall
      - --home
      - /home/agent
      - --overlay
      - {home}/.scitex/agent-container/containers/overlays/{name}/
      - --env
      - SCITEX_AGENT_CONTAINER_STATE_DB=/state/{name}/state.db
      # The board identity. Change this without migrating the cards and
      # every card the agent owns is orphaned. BOTH spellings are seeded
      # because both are on disk: the var was renamed to
      # SCITEX_CARDS_AGENT_ID and the fleet is half migrated (measured
      # 2026-08-25 on compute-04: 110 specs current, 21 retired). A
      # fixture carrying only the retired name tests the minority and
      # let a current-name blindness ship unnoticed.
      - --env
      - SCITEX_TODO_AGENT_ID={name}
      - --env
      - SCITEX_CARDS_AGENT_ID={name}
      - --env
      - GIT_AUTHOR_NAME=Yusuke Watanabe
    env: {{}}
    post: ""
    environment: {{}}
    def_file: ""
    nv: false
    rocm: false
    overlay: ""
    overlay_size: ""
    overlay_create_if_missing: true
    tmpfs_size: 2G
    fakeroot: false
    jail: false
    nested_build: false

  claude:
    model: opus[1m]
    flags:
      - --dangerously-skip-permissions
    channels: []
    raw_options: {{}}
    session:
    continue_max_age_minutes:
    resume_id: ""
    auto_accept: true
    account: ""
    credentials_file: ""
    credentials_files: []
    provider:

  health:
    enabled: true
    interval: 60
    timeout: 5
    method: sdk-alive

  restart:
    policy: on-failure
    max_retries: 3
    prune_on_stop: false
    backoff:
      initial: 30
      max: 300
      multiplier: 2

  # Explicit-fields ruling (2026-07-21): the remainder of the required
  # field set at its defaults — every spec field is written.
  provider: anthropic
  python-venv: ""
  user: ""
  to_home: ./to_home
  startup_commands: []
  startup_prompts: []
  listen: []
  extensions: {{}}
  mcp_servers: {{}}

  container:
    image: scitex-agent-container:latest
    volumes: []
    network: host
    mount_host_claude: false

  watchdog:
    enabled: false
    interval: 1.5
    responses:
      y_n: "1"
      y_y_n: "2"
      waiting: /speak-and-call

  autonomous:
    enabled: false
    drive_until: DONE
    max_turns: 50
    idle_kick_after_s: 120
    kick_text: Continue. Print DONE when finished.

  hooks:
    pre_start: []
    post_start: []
    pre_stop: []
    post_stop: []
    on_compact: []
    on_restart: []
    on_diff: []

  a2a:
    host: 127.0.0.1
    port: auto

  comms:
    outbound:
      siblings: allow
      parent: allow
    inbound:
      siblings: allow
      parent: allow
    a2a:
      listen: true

  lineage:
    group: ""
    may_spawn: true

# EOF
"""


def make_spec(name: str, home: str = "/home/tester") -> str:
    """Render a self-referencing spec for ``name``."""
    return SPEC_TEMPLATE.format(name=name, home=home)


def make_fleet(
    root: Path,
    name: str,
    *,
    spec: str | None = None,
    overlay: bool = True,
    runtime: bool = True,
    registry: bool = True,
) -> Layout:
    """Materialise a complete, isolated fleet root holding one agent.

    Creates the spec dir + spec.yaml, and (by default) the overlay dir,
    the runtime/state dir, and the JSON registry entry — the same five
    on-disk locations the real fleet has.
    """
    layout = Layout(root=root)

    spec_dir = layout.spec_dir(name)
    spec_dir.mkdir(parents=True)
    (spec_dir / "to_home").mkdir()
    layout.spec_file(name).write_text(spec if spec is not None else make_spec(name))

    if overlay:
        overlay_dir = layout.overlay_dir(name)
        (overlay_dir / "upper" / "home" / "agent").mkdir(parents=True)
        (overlay_dir / "work").mkdir(parents=True)
        (overlay_dir / "upper" / "home" / "agent" / "marker").write_text(name)

    if runtime:
        runtime_dir = layout.runtime_dir(name)
        runtime_dir.mkdir(parents=True)
        (runtime_dir / "session.jsonl").write_text('{"marker": "%s"}\n' % name)

    if registry:
        registry_dir = layout.registry_json(name).parent
        registry_dir.mkdir(parents=True, exist_ok=True)
        layout.registry_json(name).write_text(
            '{\n  "name": "%s",\n  "config": "%s",\n  "screen": "sac-%s"\n}\n'
            % (name, layout.spec_file(name), name)
        )

    return layout


# ``make_state_db`` was here until 2026-08-29. It called sac's own
# ``init_schema`` on ``Layout.state_db`` so the rename suites walked the
# production SQLite schema rather than a hand-rolled one. Both halves of that
# sentence stopped being true: ``init_schema`` issues ZERO ``CREATE TABLE``,
# and ``Layout.state_db`` is gone with ``_lifecycle/_rename_db.py`` — there is
# no state.db path for a rename to touch, so there is nothing for a helper to
# create. Its only surviving caller went with it.
#
# ``seed_db_rows`` had gone the same way on 2026-08-28, and before it
# ``COMMS_NODE_SQL`` / ``DEFINITION_SQL`` / ``INSTANCE_SQL`` /
# ``CHANNEL_EVENT_SQL`` — four raw INSERTs, each retired as its table left
# SQLite, the last of them announcing the move as 147 setup ERRORs across
# three rename suites. Both halves a rename must carry are now seeded through
# their REAL production writers below, which is a better seed than any INSERT
# was: it exercises the production id allocation and the production merge
# rules rather than hand-writing a row into a shape the code never uses.


#: The counterparty of the grant :func:`seed_identity_and_history` writes.
#: Named once so the suites asserting the carry cannot drift from the seed.
GRANT_PEER = "grant-peer"


def seed_identity_and_history(name: str) -> None:
    """Identity record, history row and ACL grant — all three in PostgreSQL.

    Every part a rename must carry. They stopped sharing a database on
    2026-08-28 and then, later the same day, stopped being in SQLite at all:
    ``sac``'s ``init_schema`` now issues ZERO ``CREATE TABLE``. Writing all
    three here is what keeps any one of them from moving again unnoticed.

    The identity half has moved three times. It was ``comms_nodes.name``
    until the ADR-0014 directory left SQLite for the shared store, then
    ``definitions.name`` until that table was deleted for having no writer,
    then ``instances.name`` — which left the same day for the shared store
    as well. It is written here through ``record_instance_start``, the same
    verb ``sac agents start`` uses, and carried by
    ``state_db_instances_rename.rename_instance_rows`` as its own step in
    ``_rename.apply_plan``.

    The history half moved four times: ``turns`` (the diary trio, to
    per-host PostgreSQL), then ``attempts`` (deleted, zero writers), then
    ``channel_events.target``, now ``sac_channel_events`` in the shared
    PostgreSQL (ADR-0023). It is written through the real ``persist_event``
    and carried by ``state_db_channel.rename_channel_events``.

    The AUTHORISATION half was the last of the three to need seeding: it rode
    inside ``_rename_db``'s ``comms_grants`` pairs until 2026-08-29, which is
    to say it was not carried at all once the table left SQLite. It is written
    through the real ``grant_send`` and carried by
    ``state_db_grants_rename.rename_comms_grants``.

    NO ``layout`` ARGUMENT, and no ``state.db``. It took one only to build
    that file, ``Layout.state_db`` was deleted on 2026-08-29 with the rename
    step that was its last reader, and a parameter kept past its last use is
    the shape that made ``_open_instance_pid`` answer "not running" for a live
    agent. Nothing here is rooted on disk any more; every record it writes is
    keyed by ``name`` in the shared store.

    CALLERS MUST TAKE ``pg_schema``: all three write to a real PostgreSQL
    schema, so a caller without it resolves the unreachable DSN and fails.
    """
    from scitex_agent_container._state.state_db_channel import persist_event
    from scitex_agent_container._state.state_db_grants import grant_send
    from scitex_agent_container._state.state_db_instances import (
        record_instance_start,
    )

    # ``workdir`` carries the name as a whole path component on purpose: it is
    # the PATH half of the rename, rewritten component-wise by
    # ``rename_instance_rows``, and the only seeded field that proves it.
    record_instance_start(name, workdir=f"/home/u/proj/{name}")
    persist_event(target=name, event={"msg_id": f"seed-{name}", "content": "hi"})
    # A THIRD half, added 2026-08-29 with the ``acl-grants`` step. It is here
    # rather than in one test because the rollback matrix photographs whatever
    # this helper seeds: an ACL grant that a failed rename does not hand back
    # is a permission silently withdrawn, and that has to be checked at every
    # injection point, not at one.
    grant_send(sender=name, target=GRANT_PEER, note=f"seed for {name}")


def _env_overrides(pairs: dict[str, str | None]) -> Iterator[None]:
    """Set (or clear) env vars for the duration, then restore them.

    Real ``os.environ`` mutation with a real teardown — the ecosystem bans
    ``monkeypatch``, and rightly: a fixture that rewrites production
    internals proves nothing about production.
    """
    previous = {key: os.environ.get(key) for key in pairs}
    for key, value in pairs.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, before in previous.items():
            if before is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = before


def isolated_board(tmp_path: Path) -> Iterator[Path]:
    """Yield a tmp scitex-todo store, fully cut off from the live fleet.

    FOUR things must be isolated, not one. Each is a real production
    opt-out, not a mock — the code paths stay exactly as they ship; the
    test simply does not ride them.

    * **the store** — ``$SCITEX_TODO_TASKS_YAML_SHARED`` points at a tmp
      YAML file, so even a call that forgot ``store=`` lands in tmp rather
      than on the live 1,400-card board.

    * **the SQLite shadow** — ``$SCITEX_CARDS_DB`` (+ its pre-rename alias
      ``$SCITEX_TODO_DB``) points at a tmp DB. This one is not belt-and-
      braces, it is load-bearing, and its absence destroyed the live board
      on 2026-07-20: the dual-write mirror resolves its own path and
      RECONCILES, so isolating only the YAML meant a five-card tmp doc
      deleted 2,772 real cards. See the inline note below.

    * **the notification rail** — sac registers a ``scitex_todo.hooks``
      consumer (``_listen._card_event_delivery``), so every real
      ``reassign_task`` emits a ``reassigned`` event which that consumer
      turns into an HTTP POST to the LOCAL ``sac listen`` daemon on
      127.0.0.1:7878, which publishes it onto a live agent's a2a bus. A
      test that reassigns 5 cards would ping five REAL agents — and block
      5 s per POST when the daemon is absent. ``SAC_CARD_EVENT_DELIVERY_-
      DISABLED`` is sac's own shipped kill-switch for exactly this.

    * **the git autocommit** — ``scitex_todo._store_write`` git-commits the
      store after every write (two ``subprocess.run`` forks). Against a tmp
      store that is pure cost, and inside a repo it would be a real commit.
      ``SCITEX_TODO_STORE_GIT_AUTOCOMMIT=0`` is scitex-todo's documented
      opt-out.

    Generator — call from a fixture with ``yield from``.
    """
    store = tmp_path / "board" / "tasks.yaml"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("tasks: []\n")
    cards_db = tmp_path / "board" / "cards.db"
    _materialise_cards_db(cards_db)

    yield from _yield_value(
        store,
        _env_overrides(
            {
                "SCITEX_TODO_TASKS_YAML_SHARED": str(store),
                # *** THE SQLITE SHADOW — isolating the YAML IS NOT ENOUGH. ***
                #
                # Redirecting the store above protects the YAML and nothing
                # else. scitex-cards mirrors every write into a SQLite shadow
                # whose path it resolves ITSELF, ignoring the store you wrote
                # to: `_dual_write.mirror_after_save` calls
                # `mirror_doc_incremental(doc, resolve_db_path(), ...)` with NO
                # explicit path, so it lands on `$SCITEX_CARDS_DB` or, failing
                # that, the live `~/.scitex/cards/cards.db`.
                #
                # And the mirror RECONCILES rather than appends
                # (`_db_mirror.py:208`): every card in the DB that is absent
                # from the doc is DELETED. So a tmp store of five seeded cards
                # does not pollute the real board, it REPLACES it. On
                # 2026-07-20 that took the fleet board from ~2,777 cards to
                # six, five of which were this module's own fixtures.
                #
                # tests/conftest.py force-sets the same variable as a floor
                # under the whole suite. This is the belt to that braces: a
                # test using this helper is isolated even if the floor is not
                # there (a bare `pytest` from another rootdir, a subprocess
                # with a scrubbed env, a future refactor of conftest).
                "SCITEX_CARDS_DB": str(cards_db),
                # Pre-rename name of the same knob, still honoured by
                # `resolve_db_path` for direct callers that never imported the
                # scitex_cards root. Set both — this is the variable whose
                # absence destroyed the board; do not bet on a transition
                # window closing cleanly.
                "SCITEX_TODO_DB": str(cards_db),
                "SAC_CARD_EVENT_DELIVERY_DISABLED": "1",
                "SCITEX_TODO_STORE_GIT_AUTOCOMMIT": "0",
                # `list_tasks(scope=None)` falls back to this. A stray value
                # would silently AND every owner query with the caller's own
                # slice — the exact orphaning the rename exists to prevent.
                "SCITEX_TODO_SCOPE": None,
            }
        ),
    )


def _materialise_cards_db(cards_db: Path) -> None:
    """Create the tmp cards store, schema and all, before any test touches it.

    Pointing ``$SCITEX_CARDS_DB`` at a path is no longer enough. scitex-cards
    REFUSES a target that does not exist rather than creating one, and says why:
    the exporter answers a missing database with an empty document, and that
    empty document is written back as the WHOLE store — every card replaced by
    nothing. Refusing is the correct behaviour and it is the direct lesson of
    2026-07-20, when this fixture's own five seeded cards replaced ~2,777 real
    ones. See the long note on ``$SCITEX_CARDS_DB`` above.

    So the isolation now has to build a real store, not merely name one:
    ``open_db`` resolves, connects, and runs ``init_schema`` (a no-op on an
    existing file).

    A MISSING scitex-cards IS THE ONE SAFE FAILURE, and it is caught NARROWLY.
    The CI SIF does not install scitex-cards (`ModuleNotFoundError: No module
    named 'scitex_cards'`, measured on PR #897). That case is safe for a precise
    reason rather than by hope: the dual-write mirror that endangers the live
    board IS scitex-cards code, so if the package cannot be imported there is no
    mirror to run and no board to reconcile away. Nothing to isolate FROM.

    EVERY OTHER FAILURE STILL RAISES. The catch is `ImportError` only, never a
    bare `except`. If scitex-cards IS present and the store cannot be built, the
    tests that follow would run against whatever `$SCITEX_CARDS_DB` resolves to
    next — the LIVE fleet board, which the mirror reconciles by DELETING. A
    helper that cannot isolate must stop the run: "could not isolate" and
    "isolated" must never look the same from the caller's side.
    """
    try:
        from scitex_cards._db import open_db
    except ImportError:
        return

    open_db(cards_db).close()


def _yield_value(value, guard: Iterator[None]) -> Iterator:
    """Run ``guard`` (a setup/teardown generator) while yielding ``value``."""
    for _ in guard:
        yield value


def isolated_root(tmp_path: Path) -> Iterator[Path]:
    """Yield a tmp sac root, with ``$SCITEX_AGENT_CONTAINER_ROOT`` set to it.

    This is what lets a CliRunner test drive the REAL ``sac agents rename``
    command — which resolves its own ``Layout.default()`` — without the
    command reaching the live fleet.
    """
    from scitex_agent_container._lifecycle._rename_plan import ROOT_ENV

    root = tmp_path / "fleet"
    root.mkdir(parents=True, exist_ok=True)
    yield from _yield_value(root, _env_overrides({ROOT_ENV: str(root)}))


def no_root_override(tmp_path: Path) -> Iterator[None]:
    """Yield with ``$SCITEX_AGENT_CONTAINER_ROOT`` cleared."""
    from scitex_agent_container._lifecycle._rename_plan import ROOT_ENV

    yield from _env_overrides({ROOT_ENV: None})


def add_card(
    store: Path,
    task_id: str,
    *,
    owner: str,
    scope: str | None = None,
) -> str:
    """Write ONE real card through ``scitex_todo._store.add_task``.

    ``entry_points=[]`` is scitex-todo's documented in-process injection
    seam. It is used HERE — on the test's own seeding writes, never on the
    code under test — because the real plugin dispatch re-runs
    ``importlib.metadata.entry_points()`` on EVERY card write (uncached: it
    re-reads ~126 ``entry_points.txt`` files, measured at 2.2 s a call in
    this container). Seeding a fixture is not the behaviour under test, and
    paying that toll on every seed makes the suite unrunnable. The rename's
    OWN ``reassign_task`` calls keep real discovery — the code under test is
    never short-circuited.

    ``scitex-todo`` rejects an owner-less card outright ("creator+assignee
    are mandatory ... no silent fallback"), so ``owner`` is required.
    """
    # scitex_cards, not scitex_todo: v0.41.0 deleted that module outright.
    # This is a HARD import on purpose — the callers guard with
    # importorskip("scitex_cards"), so reaching here means the package IS
    # installed and a failure here is a real broken path, not an absent
    # optional peer.
    from scitex_cards import _store

    _store.add_task(
        store,
        id=task_id,
        title=f"card {task_id}",
        status="in_progress",
        agent=owner,
        assignee=owner,
        scope=scope if scope is not None else f"agent:{owner}",
        entry_points=[],
    )
    return task_id


def seed_cards(store: Path, owner: str, count: int) -> list[str]:
    """Add ``count`` real cards owned by ``owner`` to the tmp store."""
    return [add_card(store, f"{owner}-card-{i}", owner=owner) for i in range(count)]
