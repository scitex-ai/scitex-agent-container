# Hermes credential and restart preflight

SAC checks Hermes successor credentials before stopping a running agent. The
same packaged check serves declared-pool starts, `sac agents check`, and the
restart path used by reconciliation and health recovery.

The check reads the declared credential slots from the normal provider resolver
and the canonical SAC secret pool. Unresolved slots and empty declared pools
are omitted. Unknown engines, malformed provider declarations, incompatible
harnesses, and invalid failover policies still refuse the launch.

Each usable provider must complete a small native inference probe for its
selected model and reject an invalid credential control. A public models list
alone does not prove inference access. An unavailable secondary route does not
block a healthy primary. Only providers with a currently proven usable key enter
the active fallback list; a future preflight can readmit recovered providers.
Known rejected keys and quota-reset timestamps remain in the private pool.
Rotated tokens cannot inherit another token's rejection or cooldown.

Health observations are cached in memory for 90 seconds and shared by concurrent
agents in one CLI or listen process. Expired cache entries are removed. Separate
processes probe independently. Diagnostics contain provider, model, slot names,
and status metadata; they omit credentials and upstream response details.

The verified route is handed to the start leg once, avoiding another probe after
shutdown. A spec change invalidates that handoff. Production restart also checks
workspace ownership and policy without provisioning before stopping. Failed
credential or workspace preflight leaves the process and profile intact.

Restart continues the existing conversation unless the operator selects a fresh
session, and reuses the configured image and environment. Active Hermes turns
retain the existing native drain guard. A goal judge using `provider: main`
follows the verified main pool; a Muse wire alias can change from
`muse-spark-...` to `meta/muse-spark-...`, while an unrelated model is refused
when a same-model judge was explicitly authored.
