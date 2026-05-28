# ADR-0015 — Cross-host push via ssh-transport selector

## Status

Proposed (2026-05-28).

## Context

ADR-0014 (Stage 1, merged in PR #234) shipped the symmetric federated
`comms_nodes` table so any host can resolve a non-local node to a
``{host, a2a_port}`` pair. With that in place, ``resolve_node_host('lead')``
on a Spartan agent's listen returns ``{host: 'ywata-note-win', a2a_port: N}``
once ``sac registry sync`` has run from the lead side.

The remaining structural gap blocks Stage 2 acceptance:
``_listen/_node_channel._forward_to_remote`` posts plain HTTP to
``http://{target_host}:{target_port}/agents/{name}/message:send``. For
the WAN topology the lead actually operates (Spartan agents reaching a
Windows laptop named ``ywata-note-win``), that URL is unroutable —
there is no overlay net between the hosts and the operator does not
want to add one. The forwarder has a fixture-only loopback rewrite for
hostnames starting with ``host-`` (used by the five existing two-listen
tests at ``tests/scitex_agent_container/_listen/test_server.py:591-950``),
but no production transport.

The codebase already has a working ssh-tunnel pattern at
``_network/peer.py::_post_turn_via_ssh`` (lines 264-323 prior to this
change): ``ssh -o BatchMode=yes -o ConnectTimeout=15 [control-opts...]
HOST "curl -sS --max-time T -X POST -H 'Content-Type: application/json'
-d @- http://127.0.0.1:PORT/PATH"``, with the JSON body piped through
ssh stdin into the remote curl's stdin. That path proves the operator's
existing ssh trust to every peer host is enough to deliver a JSON POST
without an overlay net — but it only services ``/v1/turn``.

## Decision

Add an **ssh leg parallel to the existing HTTP forwarder**, with a
**per-host transport selector keyed on ``host_config.peers`` membership**.

Concretely:

1. **Generalize the ssh-curl wrapper.** Extract the ssh + remote curl
   shell-out into ``_network/_ssh_curl.py::_post_via_ssh_curl``:

   ```python
   def _post_via_ssh_curl(
       *,
       host: str,
       port: int,
       path: str,
       body: bytes,
       bearer: str | None = None,
       timeout_s: float = 15.0,
   ) -> tuple[int, bytes, bytes]:
       """ssh + remote curl POST. Returns (rc, stdout, stderr)."""
   ```

   ``_post_turn_via_ssh`` is refactored to delegate to this helper with
   ``path='/v1/turn', bearer=None``. The ssh argv shape and the
   ControlMaster options stay byte-identical — every existing /v1/turn
   test continues to pass unchanged.

2. **Transport selector in ``_forward_to_remote``.** When
   ``target_host`` is a member of ``host_config.peers`` (via the
   ``PeersMap`` glob-aware ``__contains__`` — so ``spartan-*: { ssh:
   spartan-login1 }`` matches ``spartan-bm043`` etc.), the forward leg
   is ssh-curl. The destination's host bearer
   (``peer-tokens/<target_host>.token``) is passed to the helper as
   ``bearer=`` so the remote curl carries
   ``Authorization: Bearer <peer-token>``. Non-zero ssh exit / curl
   failure surfaces as the same 502 shape the existing HTTP branch
   produces, preserving the "add-peer fix" message.

   When ``target_host`` is NOT in ``peers:``, the legacy HTTP path runs
   verbatim — including the ``host-*`` loopback-alias rewrite used by
   the existing five two-listen tests. That rewrite is **not** broadened
   or tightened in this PR; see P-5 below.

3. **Receiver-side ACL: unchanged.** The destination's
   ``NodeAuthMiddleware`` admits the request as an administrative caller
   (``authenticated_node=None`` because the *destination's* host bearer
   was presented, not a per-node token). ``check_send_acl`` then runs
   against the original ``metadata.from_agent`` carried in the body,
   gating on the destination's local ``comms_grants`` table. Cross-group
   denials fire at the receiving host. The Stage-2 ACL work in this PR
   is purely *test-side* — five real-network e2e tests that prove this
   end-to-end without mocks.

4. **Test strategy: ssh-shim binary on $PATH performing real httpx
   POST.** A shim ``ssh`` executable is materialised under
   ``tmp_path/_shim_bin/ssh`` and PATH-prepended (env restore via
   yield/finally — no ``monkeypatch`` fixture parameter, no
   ``unittest.mock``). When production code invokes ``subprocess.run(
   ["ssh", ...])`` the shim:

   * Logs the production argv.
   * Reads the JSON body from its stdin.
   * Parses the inner curl invocation for URL / port / path / bearer.
   * Performs a real ``httpx.Client().post(...)`` to
     ``http://127.0.0.1:<port><path>`` with the same body + bearer.
     The destination is a real ``uvicorn`` listen on a loopback port
     — same shape as the existing Stage-1 cross-host tests.
   * Prints curl-shaped stdout to its own stdout and exits 0 / non-zero
     accordingly.

   The shim substitutes the ssh *transport* with a localhost call;
   nothing else is mocked. End-to-end the SSE subscriber on the
   destination receives the published event through the broker, the
   ACL fires on the destination's local ``comms_grants`` table, and
   the originating sender sees a real 200 / 403 status code.

5. **Backward compat.** Five existing Stage-1 cross-host HTTP tests
   (``test_server.py:707-947``) remain untouched. Their target
   hostnames (``host-a``, ``host-b``) are NOT in any ``peers:`` block
   in those fixtures, so they naturally take the HTTP path and the
   ``host-*`` loopback alias rewrite still applies. ``ruff`` and the
   ``/v1/turn`` direct-ssh tests (45 tests in
   ``test_ssh_control_options.py``) also pass unchanged — the
   ``_post_turn_via_ssh`` refactor preserves the argv shape verbatim.

## Non-goals

* **Do not change ``is_local_node`` semantics.** The current behaviour
  ("unknown name → treat as local") is documented in ADR-0014 and
  surfaces as P-3 below.
* **Do not modify ``import_state``'s ``INSERT OR IGNORE`` conflict
  bypass.** Tombstone propagation (P-1) and conflict detection (P-2)
  remain pre-existing problems; see Open follow-ups.
* **Do not broaden or tighten the ``host-*`` loopback rewrite** in
  ``_forward_via_http``. That rewrite is preserved as-is so the five
  Stage-1 two-listen tests stay green; see P-5.
* **No grant federation.** Grants minted on host A are not pushed to
  host B; cross-host grants must be added on the destination's db
  explicitly. This is Stage 3 scope.
* **No container→host grant scope fix.** Grants minted inside an agent
  container land in the container's ``state.db``, not the host's
  ``state.db`` that gates inbound pushes. Documented in ADR-0014;
  Stage 3 scope.

## Open follow-ups (numbered options surfaced in PR body)

* **P-1: Tombstone non-convergence.** ``state_db_export.import_state``
  uses ``INSERT OR IGNORE`` on ``comms_nodes`` so ``ended_at`` UPDATEs
  do not propagate across hosts. Options: (a) route comms_nodes import
  through ``register_comms_node``-shaped UPSERT including tombstone
  propagation; (b) ship a separate tombstone-log table for deletions;
  (c) accept eventual divergence + add ``sac registry prune``.

* **P-2: Sync bypasses ``register_comms_node`` conflict detection.**
  ``INSERT OR IGNORE`` silently keeps the existing row when two hosts
  claim the same name with different ``(host, port)``, contradicting
  ADR-0014's "fail-loud on conflict" claim. Options: (a) route the
  ``comms_nodes`` branch through ``register_comms_node`` with
  ``source_host`` from ``_stamp_source_host``; (b) accept silent
  divergence + add ``sac registry doctor`` diff tool.

* **P-3: ``is_local_node`` returns True for unknown names.** A Spartan
  agent firing ``a2a_send target='lead'`` BEFORE sync completes silently
  lands in Spartan's own inbox. Documented in ADR-0014 as "registry
  must be sync'd first". Options: (a) gate ``sac listen`` ready signal
  on ``_maybe_sync_on_start`` completion (synchronous startup sync);
  (b) change ``is_local_node`` to fail-loud for unknown explicit
  targets (behaviour break — needs test migration); (c) status quo,
  document loudly.

* **P-4: Container→host grant-scope bug (ADR-0014 deferred to Stage 3).**
  Grants minted inside an agent container write the container's
  ``state.db``, not the host listen's ``state.db`` that gates inbound
  pushes. Out of scope here.

* **P-5: Loopback alias over-matches.**
  ``_node_channel_forwarders._forward_via_http`` rewrites any hostname
  starting with ``host-`` to ``127.0.0.1``. A production hostname with
  that prefix would mis-route. Options: (a) gate behind explicit
  ``SAC_TEST_LOOPBACK_ALIASES`` env; (b) hard-list ``host-a`` /
  ``host-b`` only; (c) move into a test-only fixture hook.
