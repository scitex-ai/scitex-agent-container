---
description: |
  [TOPIC] Dashboard native admission receipts
  [DETAILS] Same-host listener routing, signed GET status, bounded polling, and safe manual inspection without automatic resends.
tags: [scitex-agent-container-gui-admission-receipts, dashboard, messaging]
---

## Dashboard message status

The same-host GUI composer sends through the authenticated listener. Cross-host
rows use Cards; selecting one clears and disables the prior POST target while
preserving unsent text. An accepted exchange is not model task completion.
The GUI retains its signed receipt and polls GET only, up to 30 attempts over
60 seconds. Canonical 202/102 remain pending; final native 200 means **Received
by agent** (turn admission/transcript visibility). Switching agents stops the
poll. An unconfirmed receipt needs manual agent inspection before another send;
never automatically resend an uncertain submission. Dispatch IDs correlate
requests and are not durable cross-worker deduplication. See
[talking-to-agents.md](../../_sphinx_html/talking-to-agents.html#fleet-dashboard-admission-receipts).

