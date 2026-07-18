"""SEAM ONLY — the 3-day PR expiry is scitex-dev's primitive, NOT sac's.

THIS MODULE CONTAINS NO STALENESS LOGIC, AND MUST NOT GROW ANY.
===============================================================
There is deliberately no ``is_stale()``, no threshold constant and no close
path here. If you came to add one, read this file first — the absence is the
design, not an omission.

WHY
---
The operator ruled the 3-day rule is FLEET-WIDE, not per-repo:
「3日ルールは全てのレポジトリで共通です」. He separately corrected scitex-cards
for reporting it as a local action —「わたしのれぽじもりにはんえいしましたじゃ
なくて、みんなで共有ルールですよね」— and his standing architecture rule is that
**scitex-dev holds primitives and leaves consume them**.

So a sac-local implementation would not be a head start, it would be a FORK of
a shared primitive: two repos hand-rolling "what does stale mean" is precisely
the failure mode being objected to. sac shipping its own definition would make
the fleet's answer depend on which tool you asked — the same class of problem
as the five differently-versioned sac installs measured on this host
(0.21.24 / 0.21.22 / 0.21.21 / 0.21.11 / none).

An earlier draft of this branch DID implement it locally (threshold compare on
``updatedAt``, budget-limited close, policy comment). That draft was removed
rather than kept as a placeholder: "we'll swap it for the real one later" is
how the install sprawl happened, and a placeholder that works is the hardest
kind to remove. The design is preserved in this branch's PR body and on the
hand-off card, where scitex-dev can own it.

STATUS
------
As measured on 2026-07-18 against scitex-dev 0.31.1
(``/home/ywatanabe/proj/scitex-dev``), **the primitive does not exist yet**.
``scitex_dev`` has PR-adjacent CLI (``ecosystem prune-merged``, ``cron
ci-watch``, ``branch-protection``) but nothing that adjudicates PR shelf life.
So sac implements NOTHING for expiry today — no verb, no JobSpec, no timer. A
scheduled job that calls a primitive which does not exist is not a mechanism,
it is a red timer.

WHAT SAC WILL OWN ONCE THE PRIMITIVE LANDS
------------------------------------------
Only glue. sac already supplies both halves it is responsible for:

* :func:`.._gh.fetch_open_prs` — the TRI-STATE read of the open-PR list. This
  is sac's and stays sac's: it is the mechanism that refuses to render an
  unreadable backlog as an empty one, and any consumer of dev's primitive
  needs it (a stale-check fed by a silently-empty list closes nothing and
  reports success).
* :class:`.._gh.PullRequest` — the per-PR facts (``created_at``,
  ``updated_at``, author, draft, CI) dev's predicate would take as input.

The consumption shape sac expects, stated so dev can design against a real
caller rather than a guess::

    verdict = scitex_dev.<ns>.pr_shelf_life(pr_facts)   # name is dev's to pick
    if verdict.stale:  ...

with THREE requirements sac will hold the primitive to, all three from
scitex-cards and all three better than sac's removed draft:

1. **Dry-run by DEFAULT**, explicit ``--apply`` to close anything.
2. **The intent-registry write must SUCCEED before any close proceeds.** A
   close whose record failed is an unrecoverable deletion with no trace of why.
   The 2026-07-18 hand pass wrote intent to a manifest card BEFORE closing 31
   PRs — but never made the close conditional on that write succeeding. The
   ordering was luck, not design, and this requirement closes that hole.
3. **Scheduled materialisation** — never anything that depends on someone
   remembering. (That is the entire premise of this branch.)

4. And sac's own non-negotiable, inherited from :mod:`._gh`: the verdict must
   be TRI-STATE. "I could not read the PR list" must not be expressible as
   "nothing is stale".

TRACKING
--------
Hand-off card: ``sac-pr-expiry-consume-dev-primitive`` (blocked on scitex-dev).
Do not implement expiry in sac until that card is unblocked.
"""

from __future__ import annotations

__all__ = ["HANDOFF_CARD_ID", "PRIMITIVE_OWNER"]

#: Who owns the 3-day rule. Not sac.
PRIMITIVE_OWNER = "scitex-dev"

#: The card tracking sac's consumption of dev's primitive.
HANDOFF_CARD_ID = "sac-pr-expiry-consume-dev-primitive"
