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

2. **The board.** Every scitex-todo call takes an explicit ``store=``.
   Belt and braces, :func:`isolated_board` ALSO points
   ``$SCITEX_TODO_TASKS_YAML_SHARED`` at the tmp store, so even a call
   that forgot to pass ``store=`` lands in the tmp file rather than on the
   real 1,400-card board.

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
      # every card the agent owns is orphaned.
      - --env
      - SCITEX_TODO_AGENT_ID={name}
      - --env
      - GIT_AUTHOR_NAME=Yusuke Watanabe

  claude:
    model: opus[1m]
    flags:
      - --dangerously-skip-permissions

  health:
    enabled: true
    interval: 60

  restart:
    policy: on-failure
    max_retries: 3

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


def make_state_db(layout: Layout) -> Path:
    """Create a REAL state.db with the REAL schema under ``layout.root``.

    Uses sac's own ``init_schema`` so the tables (and therefore the
    columns the rename walks) are exactly the production ones — a
    hand-rolled fixture schema would drift and the tests would stop
    proving anything.
    """
    from scitex_agent_container._state.state_db import init_schema

    db_path = layout.state_db
    db_path.parent.mkdir(parents=True, exist_ok=True)
    init_schema(db_path)
    return db_path


def seed_db_rows(db_path: Path, statements: list[tuple[str, tuple]]) -> Path:
    """Execute seeding INSERTs against a real state.db, committing once.

    Lives HERE rather than inline in a fixture on purpose. STX-TQ005 (the
    ecosystem test-quality rule) forbids a fixture that opens an external
    resource — ``sqlite3.connect(...)`` — and hands it back with ``return``
    instead of ``yield``, because a returned connection is never closed.
    These fixtures never hand the connection back at all; they open it,
    write, and close it. Extracting that into a plain helper keeps the
    fixture bodies resource-free and the rule satisfied for the right
    reason rather than by suppression.
    """
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            for sql, args in statements:
                conn.execute(sql, args)
    finally:
        conn.close()
    return db_path


COMMS_NODE_SQL = (
    "INSERT INTO comms_nodes (name, host, a2a_port, registered_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?)"
)
TURN_SQL = (
    "INSERT INTO turns (turn_id, name, host, status, ts) VALUES (?, ?, ?, ?, ?)"
)


def seed_identity_and_history(layout: Layout, name: str) -> Path:
    """Give ``name`` one identity row (comms_nodes) and one history row (turns).

    The two halves the rename must both carry: the live A2A directory entry,
    and the agent's past.
    """
    db_path = make_state_db(layout)
    return seed_db_rows(
        db_path,
        [
            (COMMS_NODE_SQL, (name, "h", 9001, 1.0, 1.0)),
            (TURN_SQL, ("t1", name, "h", "ok", 1.0)),
        ],
    )


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

    THREE things must be isolated, not one. Each is a real production
    opt-out, not a mock — the code paths stay exactly as they ship; the
    test simply does not ride them.

    * **the store** — ``$SCITEX_TODO_TASKS_YAML_SHARED`` points at a tmp
      YAML file, so even a call that forgot ``store=`` lands in tmp rather
      than on the live 1,400-card board.

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

    yield from _yield_value(
        store,
        _env_overrides(
            {
                "SCITEX_TODO_TASKS_YAML_SHARED": str(store),
                "SAC_CARD_EVENT_DELIVERY_DISABLED": "1",
                "SCITEX_TODO_STORE_GIT_AUTOCOMMIT": "0",
                # `list_tasks(scope=None)` falls back to this. A stray value
                # would silently AND every owner query with the caller's own
                # slice — the exact orphaning the rename exists to prevent.
                "SCITEX_TODO_SCOPE": None,
            }
        ),
    )


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
    from scitex_todo import _store

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
    return [
        add_card(store, f"{owner}-card-{i}", owner=owner) for i in range(count)
    ]
