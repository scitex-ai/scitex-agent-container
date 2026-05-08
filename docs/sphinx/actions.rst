Pane Actions
============

The action subsystem gives scitex-agent-container a small, typed
vocabulary for *doing things to a pane* (probe it for liveness, send a
``/compact``, etc.) while recording every attempt for later debugging
and manual revision. It is deliberately decoupled from policy: the
core ships an ABC, an execution engine, a SQLite attempt store, and a
CLI. Scheduling *when* to run an action is left to a higher layer.

Why
---

A pane is a noisy, asynchronous surface — typing into it and then
deciding whether "it worked" is subtle. The action subsystem
centralises that loop so each concrete action only has to answer four
questions (snapshot / precheck / send / is_complete), and every
attempt lands in a SQLite log with elapsed time, outcome, and the
before / after pane snapshots.

Architecture
------------

``liveness_probe`` (pure observer)
    ``generate_nonce``, ``pane_has_nonce_echo``, ``pane_is_busy``,
    ``classify_probe``, ``wait_for_nonce_echo``, and the
    ``ProbeState`` enum. No side effects — this layer only reads
    captured pane text.

``action_store`` (SQLite attempt log)
    One host-wide DB at ``~/.scitex/agent-container/actions.db``;
    ``agent`` is a column, not a path. Public API:
    ``append_attempt``, ``query`` (filters: agent / action / outcome /
    since / limit), ``stats`` (p95 by action and outcome),
    ``summarize``, ``purge_old``. Pane snapshots are wrapped as
    ``{"format": "full", "text": ...}`` to leave room for a
    diff-encoded variant later.

``action_base`` (ABC + engine)
    ``PaneAction`` is an abstract base with four methods:
    ``snapshot(ctx)``, ``precheck(before)``, ``send(ctx)``,
    ``is_complete(before, now)``. ``run_action`` threads them into
    one attempt and classifies it with one of five outcomes:
    ``SUCCESS``, ``PRECONDITION_FAIL``, ``SEND_ERROR``,
    ``COMPLETION_TIMEOUT``, ``SKIPPED_BY_POLICY``.

``actions/nonce_probe.NonceProbeAction``
    Functional-liveness probe. Sends ``Repeat <nonce>`` into the
    pane and confirms the nonce is echoed back by the model.

``actions/compact.CompactAction``
    Sends ``/compact`` and confirms by watching
    ``context_pct`` drop by at least ``min_drop_pct`` (default 20).

CLI
---

.. code-block:: bash

    # Run one attempt. Non-zero exit on any non-SUCCESS / non-SKIPPED
    # outcome, so schedulers can branch on it.
    scitex-agent-container actions run nonce-probe <agent>
    scitex-agent-container actions run compact <agent> \
        --min-drop-pct 30 --timeout 60 --json

    # List recent attempts (most recent first).
    scitex-agent-container actions query \
        --agent <agent> --action compact --since 2h --limit 20

    # Aggregate by (action, outcome): count, mean, p95 elapsed.
    scitex-agent-container actions stats --agent <agent> --since 7d

    # Retention cleanup (default 30 days; override with
    # SAC_ACTION_RETENTION_DAYS or --days).
    scitex-agent-container actions purge --days 14

``run`` builds an ``ActionContext`` from the local ``Registry`` entry
and the multiplexer's ``capture_content``; ``query`` / ``stats`` /
``purge`` are thin wrappers over ``action_store``.

Runtime timing
--------------

Reliable ``send_keys`` requires an inter-key delay plus a settle
window between text and ``Enter``. Both ``runtimes/tmux.py`` and
``runtimes/screen.py`` honour two env vars, read at import time:

================================= ======= ==================================
Env var                           Default Meaning
================================= ======= ==================================
``SAC_KEY_DELAY_S``      ``0.1`` Delay between individual keys.
``SCITEX_AGENT_SUBMIT_SETTLE_S``  ``0.3`` Settle after text, before Enter.
================================= ======= ==================================

A ``send_text_and_submit(session, text)`` helper wraps the two-step
"type the payload, then submit" pattern used by every action ``send``.

Status integration
------------------

``agent_meta.collect_rich()`` folds the latest
``action_store.summarize()`` into the status payload, so
``scitex-agent-container show-status <name> --json`` exposes action history
alongside the pane / hook fields. See :doc:`status_and_hooks` for the
six new fields.

Extending
---------

Add a new concrete action by subclassing ``PaneAction``, implementing
the four methods, and registering it in
``cli_pkg/action_cmds._ACTION_FACTORIES``. The engine, the store, the
CLI, and the status surfacing all pick it up automatically.
