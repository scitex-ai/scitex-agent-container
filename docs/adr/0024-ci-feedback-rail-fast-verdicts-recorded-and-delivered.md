# ADR-0024 — The CI feedback rail: fast verdicts, recorded on cards, delivered to the agent that pushed

* **Status**: Proposed — design only, nothing implemented
* **Date**: 2026-08-12
* **Vantage point**: measured from inside the `scitex-agent-container` agent on host `scitex-compute-04`. Container `$HOME` is `/home/agent`; the host's is `/home/ywatanabe`. Where a fact differs between the two, both are given.
* **Operator ruling (2026-08-12), verbatim**:

  > 効率的なテストのインフラです。普段の PR では Python の 3.11 と 3.13 だけとか、先に速いテストを走らせて全部の子でですよ。テストが失敗したらすぐ返すとか。あとはプッシュに連動して、必ずフックでカードにその情報を書かないといけない、登録しないといけないってことですね。で、そうすると今度 GitHub から信号が帰ってきたときに、フックで今度はカードに書き込む、で、それがエージェントに通知が行くっていうような経路を作らないといけない

* **Consumers**: scitex-agent-container, scitex-cards, scitex-dev
* **Builds on**: ADR-0022 (state → PostgreSQL, configuration → git), scitex-cards ADR-0012 (work receipts), ADR-0016 (many transports, one store)

---

## 1. The four parts, and their true state

The headline correction: **this is not four greenfield builds. Three of the four are already designed, written, and shipped — and are inert because nothing installs or schedules them.** The gap is deployment, not construction.

| # | What the operator asked for | Prior reading | **Measured state** | Decisive evidence |
|---|---|---|---|---|
| 1 | PR gate runs 3.11 + 3.13 only | DONE (#982) | **DONE** — and enforced by a derived-invariant test, not by an edited list. Branch protection already narrowed to the two contexts. One caveat: 3.12 still runs on every PR as the *tool* interpreter for lint/docs/quality/import-smoke/guard | `.github/workflows/`, `tests/integration/test_ci_python_matrix_shape.py`, #982 merged 02:05:34Z |
| 2 | Fast tests first, fail fast | NOT STARTED | **NOT STARTED, and far more expensive than it looks.** The markers it would select on (`slow`, `e2e`, `smoke`) are registered and **never applied** — the current `-m` filter deselects 23 of 15,292 tests (0.15%). There is no seam; one must be built across 15k tests. And the latency is dominated by queueing, not running — see §3.3 | `pyproject.toml` markers, `--collect-only -q`, job-level timings |
| 3 | push → hook → card records it | NOT STARTED | **BUILT AND SHIPPED, INSTALLED NOWHERE.** `scitex-cards/.githooks/{post-commit,pre-push}` call `scitex-todo hook push`; an idempotent installer exists. `core.hooksPath` is unset in sac and points at a non-existent container path in scitex-cards | `/home/ywatanabe/proj/scitex-cards/.githooks/`, `scripts/install-todo-git-hooks.sh`, `.git/config` |
| 4 | GitHub signal → hook → card → agent | NOT STARTED, rail broken | **BUILT TWICE, DELIVERED ZERO TIMES.** Two independent lanes by operator ruling of 2026-06-15. sac's delivery lane is wired into the `sac listen` lifespan and has never recorded a verdict; cards' record lane has never completed a sweep | `verdict_delivered` table **absent** from the host state.db; `ci-state.json` **absent** |

Three corrections to the prior reading, in order of importance:

1. **Part 4 is not unstarted — it is unfired.** The operator already ruled on this architecture on 2026-06-15 (the "decoupled-pollers override", dev msg `96afacc7`): *todo's lane = RECORD, SAC's lane = DELIVERY, neither depends on the other.* Both lanes are implemented. Neither has ever produced an artifact on this host.
2. **"The rail is broken" is half right, and the half that matters is the opposite of the half that was measured.** There are **two** notification rails with **opposite** health (§2). The one that was measured (scitex-cards' file sidecar) is indeed dead. The one that part 4 should use (sac's `channel_events` + a2a bus) is alive, durable, and replayable — 1599 rows, most recent delivery minutes before this was written.
3. **Part 3's producer already exists** as shipped git hooks with an installer. Turning it on is a config line per repo, not a build.

---

## 2. Two rails, opposite health

This is the structural fact the design turns on. "The notification rail" is not one thing.

| | **Rail A — sac a2a / channel** | **Rail B — scitex-cards inbox** |
|---|---|---|
| Durable store | `channel_events` in the host `state.db` | file sidecar `runtime/todo.db` |
| Rows | **1599**, newest delivered minutes ago | **365**, newest `2026-08-11T07:05:27Z`, zero-length WAL |
| Unseen backlog | 0 pending | **149 unseen, frozen** |
| Publish path | `POST /v1/notify` → `publish_to_agent` → persist **then** `broker.publish` | `_notify/_dispatch` → `_inbox.enqueue` (fail-soft, swallows errors) |
| Replay after a drop | yes — SSE `Last-Event-ID` replays from the persisted row id | no |
| Reaches a mid-turn process | yes (§4) | only via the MCP channel push, whose poll loop is gated off here |
| Health | **live** | **dead since 07:05** |

Rail B's failure is diagnosed by the package's own doctor, which I ran:

> `backend_mode` — **SPLIT BACKENDS** — cards are on postgres … but the notification inbox is on yaml (`/home/agent/.scitex/cards/runtime/inboxes.json`). The inbox rail is a file sidecar located from the store PATH, so pointing the store at a server does not move it. Card writes and notification writes therefore land in different engines, fail independently, and a green card-side check says nothing about whether notifications are being delivered.

The mechanism is exact and worth naming because it explains the "five broken tools" observation. `resolve_tasks_path()` (`scitex_cards/_paths.py:100-131`) deliberately returns `~/.scitex/cards/tasks.yaml` whenever the store target is a DSN. Every caller then takes `.parent` as the local state directory — so the lock path, the runtime dir, and therefore the **inbox** are all derived from a YAML file that does not exist, while the cards themselves come from PostgreSQL. The artifact is visible on disk: `/home/agent/.scitex/cards/.tasks.yaml.lock` and `.inboxes.json.lock` exist, and **neither `tasks.yaml` nor `inboxes.json` does.** Locks are being taken on files nobody reads.

**On PR #806.** It is **merged** (2026-08-11T23:59:04Z), not in flight. Its stated acceptance criterion — *no verb can still open the sidecar afterwards* — **is not met, and the PR says so itself**: "The file/SQLite inbox path is not deleted yet … deleting it makes every test require a PostgreSQL that CI does not yet have. That is the next PR, and CI's postgres service is its prerequisite." Seven surfaces still construct `inbox_db_path()`: `inbox migrate-to-sqlite`, `inbox info`, `_inbox_sqlite.{enqueue,poll_inbox,ack}`, `_inbox_migrate`, `_dm_receipt_state`, `_health_backend_mode`, `_inbox_maint.collapse_digests`. Critically, `_inbox._use_sqlite()` — the two-valued predicate that #806 was written to replace — is **still the live gate for `poll_notifications` and `drain_once`**. So #806 fixed the receipt path and left the read path on the corpse.

**Therefore #806 does not cover what parts 3 and 4 need, and parts 3 and 4 should not wait for it.** Rail A is already durable, already has a card-event bridge, and is already delivering. The correct move is to route the CI feedback loop over Rail A and let Rail B's migration finish on its own schedule.

Two further live defects, measured here and consistent with ADR-0022 §Deferred:

* **One container, two stores.** `$SCITEX_CARDS_DB` in this agent's session names port **55432**; the scitex-cards MCP server process (started 01:45) resolves port **5442**, the NAS-tunnel clone. Same container, same variable name, two values, no error. ADR-0022 measured this independently and by a different method (writing a card and finding it in one store and not the other) — this ADR confirms it, and does not re-litigate it.
* **The channel poll loop's own gate is broken.** `_mcp_channel` runs tools-only with **zero channel pushes** when `$SCITEX_TODO_AGENT_ID` is unset. In this container it is unset in the shell, and in the cards MCP server process it is set to the literal, unexpanded string `${SCITEX_TODO_AGENT_ID}`. The doctor's `delivery_confirmed` check agrees with the consequence: *"no channel push has ever been recorded for scitex-agent-container (0 inbox records, 0 with a push receipt)."*

---

## 3. Parts 1 and 2 — the gate, and why not to split the suite yet

**Part 1 is done, and the mechanism is better than "someone edited a list".** The gate expresses the split as one expression:

```yaml
python-version: >-
  ${{ fromJSON(github.event_name == 'pull_request'
               && '["3.11","3.13"]'
               || '["3.11","3.12","3.13"]') }}
```

and `tests/integration/test_ci_python_matrix_shape.py` **derives** the invariant from it — the PR list must be exactly `[oldest, newest]` of the full list, the nightly exactly the full list minus the PR list, the release matrix exactly the full list, and the full list must agree with pyproject's classifiers. A bump that edits one place turns that test red instead of half-applying in silence. **This is the pattern part 2 must copy** (see below).

*One honest caveat:* several other PR-triggered workflows (`lint`, `quality-audit`, `import-smoke`, `rtd-sphinx-build`, `newb-docs-quality`) still provision `uv venv --python 3.12`. That is a **tool runtime**, not a test-matrix leg, so the claim "the PR gate runs 3.11 + 3.13 only" is true of the gate and slightly false of the phrase "PRs only touch 3.11 and 3.13". Worth knowing; not worth changing.

### 3.2 Part 2 — what is already there, and what the numbers say

Before designing anything, four things already exist and three of my own first instincts were therefore wrong:

| Instinct | Reality |
|---|---|
| "add `concurrency: cancel-in-progress`" | **already present** on the gate (`concurrency:` … `cancel-in-progress: true`), with a comment explaining why it is safe unconditionally |
| "set `fail-fast: false` so one leg doesn't hide the other" | **already set** |
| "parallelise the suite" | **already done** — the SIF runner "runs the suite under xdist" |
| "assert the split is honest" | **already the house pattern**, in `test_ci_python_matrix_shape.py` |

So the cheap wins are taken. What remains is the actual question: does splitting the suite reduce the time to first red?

**The arithmetic that decides it.** Running the fast tests first is only a *latency* win if time is spent **running**. If time is spent **waiting for a runner**, splitting one job into two does not divide the wall-clock — it **adds a queue entry**, and the first signal arrives later than before, because the work now sits behind two waits instead of one. Splitting pays only when **queue < run**.

### 3.3 Measured — queue versus run

Gate-workflow jobs only (`tests`), completed `success|failure`, window **2026-08-11T11:26Z → 2026-08-12T02:54Z (15 h 28 m)**. Measured twice, independently, by two different collectors:

| | n | QUEUE med | QUEUE p90 | RUN med | RUN p90 | **Q:R med** |
|---|---|---|---|---|---|---|
| collection A | 249 | 717 s | 2263 s | 259 s | 485 s | **2.77** |
| collection B (replication) | 255 | 706 s | 2325 s | 263 s | 484 s | **2.68** |

**QUEUE is 73–76% of total job latency. 186 of 255 jobs spent longer waiting for a runner than running.** Per leg the ratio is flat (py3.11 2.79, py3.12 2.55, py3.13 2.66) — this is not one slow leg, it is admission cost.

And it is getting worse, not better, in the most recent window:

| 2-hour bucket (UTC) | n | QUEUE med | RUN med | Q:R |
|---|---|---|---|---|
| 08-11 1x | 86 | 652 s | 228 s | 2.86 |
| 08-11 2x | 81 | 603 s | 267 s | 2.26 |
| **08-12 0x** | 88 | **1138 s** | 324 s | **3.51** |

*A correction worth recording, because it nearly went into this ADR as fact.* A first, smaller pull (193 jobs, ~2 h, **all** workflows) reported Q:R = 0.59 and would have recommended splitting the suite. It was wrong twice over: it mixed 12-second lint jobs into the RUN median, and it queried a different endpoint. Two independent collections over the same 15-hour window using the same method agree to within 2%. The small sample was the outlier.

**Why the pool cannot absorb it.** The eligible self-hosted pool is **4 runners**, all `busy`. One PR fires **7 self-hosted jobs** (2 test legs + lint + import-smoke + docs + quality + hosted-runner guard) — a 7:4 oversubscription before a second PR exists. Three `spartan-cpu` runners were online and **idle** throughout, ineligible because they do not carry the label `CI_RUNS_ON` currently names. And 21.9% of jobs ended `cancelled`, burning 32% of machine-minutes.

> **Decision: do not split the suite. Splitting converts one 4.3-minute run into two jobs that each pay a ~12-minute median (~38-minute p90) admission cost.** It makes the first red arrive later. The operator's instinct is right in general and wrong for this pool; it becomes right the moment queue < run.

**What actually reduces time-to-first-red here, in order of cost:**

1. **Widen `CI_RUNS_ON` to admit the three idle Spartan runners** — supply, not scheduling. (Note the fleet lesson: a label is not a pool; widening a label edits every workflow naming it, so this needs a deliberate check that Spartan is an acceptable host for these jobs.)
2. **Drop `--cov` from one test leg.** ~15% of RUN, and the precedent is already set — the hosted nightly drops it on the stated grounds that coverage is uploaded from the gate anyway.
3. **Reduce jobs per PR**, which is exactly what #982 did (8 → 7). If 3.12 coverage is genuinely meant to be nightly-only, the five PR-side `uv venv --python 3.12` jobs are the next candidates.

### 3.4 If the pool is ever fixed and the split is built, three rules keep it honest

**First, the cost nobody has priced: there is no seam to split on.** The markers a fast lane would select are declared in `pyproject.toml` (`integration`, `docker_smoke`, `slow`, `e2e`, `smoke`) and are **almost never applied** — only **9 files** in the entire tree carry any of them, and the gate's default `-m 'not integration and not docker_smoke'` deselects **23 of 15,292 collected tests (0.15%)**. Building a fast lane therefore means classifying ~15,000 tests, not adding a workflow job. (Relatedly, `run-in-sif.sh` and the nightly both describe the suite as "~2460 tests" — off by a factor of six, and worth fixing wherever the split is discussed.)

The failure mode to fear is a fast lane that is green because it selected nothing — this fleet found three gates-that-cannot-fail in a single night.

The failure mode to fear is a fast lane that is green because it selected nothing — this fleet found three gates-that-cannot-fail in a single night.

* **Select by explicit marker, never by path or name matching.** A test is slow because it sleeps, spawns a subprocess, touches the network, or needs apptainer/docker/ssh. Mark those `@pytest.mark.slow`. The fast lane runs `-m "not slow"`; the full lane runs the **entire** suite with no `-m` at all, so it is a superset by construction and cannot develop a hole.
* **Prove the split with an assertion, not a convention.** Copy `test_ci_python_matrix_shape.py`: assert `count(-m "not slow") + count(-m slow) == count(unfiltered)` from `--collect-only -q`. A marker typo then fails the arithmetic instead of silently shrinking the gate. **This rule is the whole reason the split is safe to build at all.**
* **Only the fast lane gets `--maxfail`.** It exists to return a verdict quickly, so `-x` belongs there. The full lane must run to completion — a truncated full lane reports one failure when there are forty, and the next push fixes one and re-queues.

---

## 4. Part 4 — the delivery mechanism, and where the signal enters

### 4.1 How GitHub's signal reaches the host

Three options exist; two are commonly assumed and both are wrong here.

* **Inbound webhook — not available.** `sac listen` binds loopback only (`127.0.0.1:7878`, the sole `0.0.0.0` listener on the box is sshd), and this is policy, not accident: the CLI refuses a non-loopback bind without `--allow-non-loopback`, and the auth module states the bearer is defence-in-depth because "sac listen binds loopback or to a private tunnel-only interface". A `cloudflared` tunnel process does run on the host, but its ingress map is managed remotely and cannot be enumerated from here, and it currently holds no local TCP connections. So: **no public route to `sac listen` is configured, and its absence cannot be confirmed from this host** — which is not the same as "impossible", and the ADR should not claim it is.
* **Polling — available, already built, and unnecessary.** Both lanes are pollers today. Polling is what you do when the signal cannot reach you.
* **The self-hosted runner — available, already trusted, and the right answer.** A self-hosted runner long-polls *outward* to GitHub and then executes the job **on this host**, where `127.0.0.1:7878` is directly reachable and the listen bearer token is a local file. **The GitHub→host path already exists and is outbound-initiated.** A final workflow step (`if: always()`) can post the verdict directly.

**Decision: the CI verdict enters via a step on the self-hosted runner, not a webhook and not a poller.** This matches the operator's word 「フック」 more closely than polling does — it is a hook on the GitHub side, firing on the event, rather than a sweep that notices later. It removes a poll interval (currently 300 s) from the latency budget, and it removes the `gh`-auth dependency that makes the existing pollers fail closed.

The pollers should be **kept as the reconciler**, not deleted: a runner step is lost if the job is cancelled or the runner dies mid-step, and a 5-minute sweep that fills gaps is the correct backstop. Emit-then-reconcile, never emit-only.

### 4.2 How the signal reaches a *running* agent

A card write is not a notification. The concrete mechanism, which already exists end to end:

```
runner step (on this host)
  └─POST /v1/notify  (loopback, bearer)          ← sac listen, already routed
       └─ publish_to_agent()
            ├─ persist_event() → channel_events   ← DURABILITY FIRST, then publish
            └─ broker.publish(agent, event)
                 └─ agent's outbound SSE subscription → live session
```

Two properties make this the right seam, both already in the code:

* **Durable before live.** `publish_to_agent` persists to `channel_events` *before* publishing, and the returned row id is the SSE `id:`, which a reconnecting client replays from via `Last-Event-ID`. The bus is an in-memory fan-out in front of a durable, replayable log — not the log itself.
* **Outbound subscription, not inbound POST.** A containerized agent's own `turn_url` is unreachable from outside its container; the agent subscribes outward and the daemon publishes down that stream. scitex-cards already removed its direct turn-URL POST from the dispatch critical path for exactly this reason.

**Failure modes, stated honestly:**

| Agent state | What happens | Recovery |
|---|---|---|
| **Busy mid-turn** | Delivered. The event lands in the agent's bounded `asyncio.Queue` (cap 64) and is consumed at the turn boundary. | none needed |
| **Queue overflowing** | **Oldest is dropped** to make room. A flood silently loses the front of the queue. | the persisted `channel_events` row survives; replay by `Last-Event-ID` |
| **Stopped / dead** | Nothing is published — the broker holds no subscriber. | the row is still in `channel_events`; delivered on next subscribe |
| **Silently deaf** | The worst case: the agent believes it is subscribed, the broker holds no subscriber, every message lands on an empty bus, **no error is raised anywhere**. | keepalive beat forces a write so a bounded read deadline fires and the client re-dials |

That last row is not hypothetical, and it is the single most important number in this ADR:

> **Measured now: of 15 registered agents, 6 are `inbox_reachable` and 9 are `unreachable` (0 subscribers).** Every reachable agent was started after 2026-08-11T17:59. Every unreachable one was started 2026-08-10T12:4x. **Subscription reach decays with uptime**, and a notification aimed at 60% of the fleet is currently delivered to nobody, with no error anywhere.

**This is the thing that must be fixed before anything writes CI results anywhere.** A verdict rail built on top of a bus that reaches 40% of the fleet produces exactly the "fuller dead letter box" outcome. The fix is not new architecture — it is making a missing subscriber an *alarm* rather than a silent state. `a2a_peers` already computes `inbox_reachable`; nothing acts on it.

### 4.3 Why the existing lane has delivered zero verdicts

sac's delivery lane is fully wired: `_listen_lifespan` launches `github_ci_poll_loop` unconditionally at boot; each tick hands `(repo, pr, head_sha, conclusion)` to `_ci_deliver.deliver_verdict`, which dedups → resolves owner → delivers to pusher → climbs the lineage → records via `record_verdict_delivered`.

> **Measured: the `verdict_delivered` table does not exist in the host `state.db`.** A `SELECT name FROM sqlite_master WHERE name LIKE '%verdict%'` returns empty against `/home/ywatanabe/.scitex/agent-container/runtime/state.db`. Since recording happens *after* a delivery that found an owner, the absence of the table is proof that **not one CI verdict has ever been delivered on this host.**

**One defect is confirmed by measurement.** `_ci_owner.tracked_repos()` builds each watched repo as `<org>/<project>`, where `org` is `$SAC_CI_POLL_ORG` and then the hard-coded default `ywatanabe1989` — annotated in the source as "(the SciTeX GitHub org)". It is not: the org is **`scitex-ai`**, and `SAC_CI_POLL_ORG` is unset here. Measured:

| constructed name | result |
|---|---|
| `ywatanabe1989/scitex-agent-container` | resolves — GitHub redirects a transferred repo to `scitex-ai/scitex-agent-container` |
| `ywatanabe1989/scitex-cards` | **404 Not Found** |

So the default is **silently half-right**: repos that were transferred out of the personal account still resolve on GitHub's rename redirect, while every repo created directly under the org — `scitex-cards` among them — 404s and is invisible to the poller forever. That is a real defect regardless of the verdict question, and it is one environment variable to fix.

**It is not established as the sole cause of zero verdicts**, because sac's own repo does resolve. Two other designed-to-be-loud conditions remain candidates: the loop disables itself when `gh` is unauthenticated in the daemon's environment (deliberately, rather than emitting `none` forever), and `resolve_owner` returning nothing short-circuits before anything is recorded. **Reading the daemon log to distinguish them is the first executable step of part 4** — the loop is designed to have logged whichever fired.

The cards lane is equally inert and for a plainer reason: **neither `~/.scitex/cards/dashboard.json` (its repo list) nor `~/.scitex/todo/ci-state.json` (its dedupe cache) exists**, on either home. It has never completed a sweep.

---

## 5. Part 3 — push → card

The producer exists. `/home/ywatanabe/proj/scitex-cards/.githooks/` ships `post-commit` and `pre-push` (both executable), which call `scitex-todo hook push --payload -` through `_lib.sh`. The consumer exists with three equivalent surfaces — `POST /hooks/push`, the `hook push` CLI verb, and in-process `dispatch_event` — all converging on one dispatcher, and the built-in handler is idempotent. `scripts/install-todo-git-hooks.sh` is an idempotent installer that points `core.hooksPath` at that directory and refuses to clobber an unrelated existing value.

Linking is deliberately **soft**: a card is annotated only when a card id appears in the branch name (`<type>/<card-id>-<rest>`) or a `Card: <id>` commit trailer. An ad-hoc branch produces no event and no error. This is the right default — it means turning the hooks on cannot break anyone's workflow.

**Measured: it is installed in zero repos, and one repo is worse than uninstalled.**

* `scitex-agent-container`: `core.hooksPath` **unset**; `.git/hooks/` in the main checkout contains only `*.sample`. Its own tracked `.githooks/{pre-commit,pre-push}` never execute — and that `pre-commit` file documents this exact failure about its predecessors: *"Three mechanisms, zero enforcement… the conventions were ADVERTISED as pre-commit-enforced and enforced by NOTHING."* The fix is written, committed, and not installed.
* `scitex-cards`: `core.hooksPath = /work/.git/hooks` — a **container-internal path that does not exist on the host**. Five real hooks sit in `.git/hooks/` and none of them can fire. This is strictly worse than unset, because it looks configured.

Part 3's remaining work is therefore: repair the stale `hooksPath`, run the installer, and decide *where* the hooks run (the host checkout, the agent containers, or both). It is not a build.

---

## 6. Where the hooks live, and whether they deploy

Any design whose delivery depends on hooks must account for this, or it ships inert.

**There are three tiers, not two:**

| Tier | Location | Form | Freshness |
|---|---|---|---|
| 1 — SSOT | `/home/ywatanabe/.dotfiles/src/.claude/to_claude/hooks/` (92 scripts) | real files | authoritative |
| 2 — agent baseline | `~/.scitex/agent-container/agents/_shared/to_home/.claude/hooks/` (45 scripts) | real files, an **independent copy** of tier 1 | **hand-maintained; nothing syncs tier 1 → tier 2** |
| 3 — live container | `/home/agent/.claude/hooks/` | **a mix**: materialized real copies from tier 2, plus absolute symlinks into tier 1 | depends on which |

The rule that decides everything: **the agent layer wins on a name collision.** Tier 2 is materialized first as a real file and the host-merge never overwrites it. So a hook that exists in tier 2 is a frozen copy that **shadows tier 1 forever**; a hook that exists only in tier 1 arrives as a symlink and is always live.

**Consequence, tabulated:**

| A merged hook fix reaches… | tier-1-only hook (symlink) | tier-2 hook (real copy) |
|---|---|---|
| a **running** agent | immediately, zero action | **impossible without a restart** |
| a **newly started** agent | live | only if `_shared/to_home/` was updated **by hand** first |
| a **rebuilt image** | irrelevant — the image carries no hooks | irrelevant |

Materialization happens at **agent start**, in-process on the host (`claude_session.py` → `deploy_to_home` → `apply_host_merge`), not at image build and not via a bind mount of `.claude`.

**Measured drift: 13 hooks exist in both tiers with differing checksums, and dotfiles is newer in all 13 — unanimous, no exceptions.** Worst case is three months (`enforce_fd.sh`: tier 2 dated 2026-05-05, tier 1 dated 2026-08-07). The `enforce_ripgrep.sh` enforcing rules on this session is the stale 2026-07-18 copy, not the 2026-08-11 dotfiles version. And the job that would close the *upstream* axis is currently failing (`dotfiles-sync.service`, exit 10, refusing to touch locally-modified tracked files). There is **no job at all** on the tier 1 → tier 2 axis.

**Design implication.** Two rules, and they are cheap:

1. **Put nothing this design needs into tier 2.** A CI-feedback hook belongs in tier 1 only, where it deploys as a symlink and is live for running agents immediately.
2. **The tier 1 → tier 2 drift must become an alarm.** `assert_no_host_merge_drift()` already exists and is described as "an out-of-band periodic check" — nothing schedules it. Scheduling it is a small, separable piece of work that makes every future hook fix trustworthy.

---

## 7. Dependency order

The prior is right: **the rail must work before anything writes to it.** The code agrees, with one refinement — the rail that must work is Rail A, and its defect is narrower than "the rail is broken".

**Step 0 — make deafness loud. (Blocking; nothing else is worth doing first.)**
9 of 15 agents have no inbox subscriber and nothing reports it as a fault. Make `inbox_reachable == "unreachable"` on a *running* agent an alarm. Until this holds, every downstream step delivers into a void and reports success.

**Step 1 — find out why zero verdicts were ever delivered, and fix the org default.** Read the `sac listen` log for `github_ci_poll_loop` / `deliver_verdict`; one of the designed-to-be-loud conditions has fired. Independently, correct the `ywatanabe1989` → `scitex-ai` default in `_ci_owner.tracked_repos()` (§4.3) — that defect is confirmed and hides every org-native repo whether or not it is the verdict cause. Mostly diagnosis; it may make step 3 cheaper.

**Step 2 — turn part 3 on.** Repair `scitex-cards`' `core.hooksPath = /work/.git/hooks`, run the installer where it belongs, verify one commit annotates one card. Independent of steps 0–1 (a card write is useful even before delivery works), cheap, and it is the operator's 「必ずフックでカードに登録」 requirement satisfied by config rather than code.

**Step 3 — move part 4's ingress to the runner step.** Add the `if: always()` verdict POST to the PR-gate workflow, keep the pollers as reconcilers. Depends on step 0 (else it delivers into the void) and is informed by step 1.

**Step 4 — schedule the hook-drift check.** Makes every subsequent hook change deployable. Separable, and should not block steps 0–3.

**Step 5 — part 2, and the answer is "not this way, not yet".** The measurement is in: queue is 2.7x run. **Do not split the suite.** Do the three supply-side items of §3.3 instead — widen the runner pool, drop `--cov` from one leg, keep reducing jobs per PR. Revisit the split only when queue < run, and then only under §3.4's rules, whose first commit is the shape assertion.

**Explicitly not a dependency: PR #806 and the Rail B migration.** Routing over Rail A decouples this work from that migration entirely. Waiting for #806's successor would block all five steps behind a PR that is itself blocked on CI acquiring a PostgreSQL service.

---

## 8. Deferred — explicitly not done

1. **Rail B's retirement.** Seven surfaces still open the sidecar after #806; `_use_sqlite()` is still the live gate for the read path. Owned by scitex-cards, blocked on CI PostgreSQL, and deliberately not on this ADR's critical path.
2. **The `:55432` vs `:5442` in-container disagreement.** Measured here and in ADR-0022 §Deferred. Not re-litigated; it is a launch-environment defect, and it will corrupt any card-based rail until fixed.
3. **`SCITEX_TODO_AGENT_ID` arriving as the literal string `${SCITEX_TODO_AGENT_ID}`** in the cards MCP server process. A template-expansion defect that silently disables channel pushes. Carded separately, not here.
4. **Cross-host delivery.** Everything above is single-host. A verdict for a PR pushed by an agent on another host does not cross until ADR-0022's sync layer exists.
5. **New tables.** This design deliberately adds none. If one is later added for CI results, ADR-0022 §5.1 binds: five sync columns from creation, classified `log`/append-only with `origin_node` in the primary key.

---

## 9. What could not be verified

* **Whether a public route to `sac listen` exists.** `cloudflared` runs on the host with a remotely-managed ingress map that cannot be enumerated locally. The design does not depend on the answer — the runner-step ingress works either way.
* **Why exactly the verdict poller has never fired.** Two candidate causes are named in §4.3; distinguishing them requires the daemon log and is step 1.
* **`.git/hooks/` contents in the sac main checkout.** Read as 14 `*.sample` files from this vantage and as empty from a worktree vantage (in a worktree `.git` is a file, not a directory). Both readings agree on the load-bearing point: `core.hooksPath` is unset and no non-sample hook exists, so nothing fires.
* **The doctor's `channel_drain` (10 records) and `delivery_confirmed` (0 records)** disagree about this agent's inbox. They read different stores under the split described in §2; the disagreement is a symptom of that split rather than an independent fault, but it was not traced to a single line.

---

## 10. References

* ADR-0022 — States → PostgreSQL, Configuration → files under Git (the sibling; store topology, sync-column contract)
* scitex-cards ADR-0012 — Work receipts: transport success is not work success
* scitex-cards ADR-0016 — Many ways to reach the store, exactly one store (the principle the inbox sidecar violates)
* scitex-cards PR #806 — "the rail owns its table" (merged 2026-08-11T23:59Z; acceptance criterion not met, by its own admission)
* sac PR #982 — PR gate bracketed at 3.11 + 3.13
* Operator ruling 2026-06-15 (dev msg `96afacc7`) — decoupled pollers: todo records, sac delivers

<!-- EOF -->
