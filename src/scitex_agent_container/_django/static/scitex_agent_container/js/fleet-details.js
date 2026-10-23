/* Agents fleet: dynamic details pane (operator 2026-10-23 — agent details
 * live in the shared Details column, filled when a row is selected).
 *
 * Row click (not on links, buttons, forms or inputs) fetches the agent's
 * detail fragment (`?partial=1`, same view, `_detail_content.html` only)
 * and injects it into the SDK panes "Details" pane, then shows that pane
 * (matters on phones, where panes collapse to tabs). Fail-soft: a fetch
 * failure leaves the placeholder — every row keeps its inspect link, so
 * the detail page stays reachable with no JS at all. Vanilla, no bundler.
 */
function panel() {
  return document.querySelector("[data-role='fleet-detail']");
}

function showDetails() {
  try {
    const api = window.stxPanes;
    if (api && typeof api.show === "function") api.show("detail", "agents");
  } catch {
    /* panes module absent: the injected fragment is still readable inline */
  }
}

async function loadIntoPanel(url) {
  const host = panel();
  if (!host) return;
  host.setAttribute("aria-busy", "true");
  try {
    const response = await fetch(url, { headers: { Accept: "text/html" } });
    if (!response.ok) return;
    host.innerHTML = await response.text();
    showDetails();
  } catch {
    /* fail-soft: keep the placeholder; the inspect link still works */
  } finally {
    host.removeAttribute("aria-busy");
  }
}

function onClick(event) {
  const row = event.target instanceof Element ? event.target.closest("tr.agent-row") : null;
  if (!row) return;
  if (event.target.closest("a, button, form, input, select, textarea")) return;
  const link = row.querySelector("a[href]");
  if (!link) return;
  const href = link.getAttribute("href");
  if (!href || href === "#") return;
  event.preventDefault();
  for (const other of document.querySelectorAll("tr.agent-row[data-stx-selected]")) {
    other.removeAttribute("data-stx-selected");
  }
  row.setAttribute("data-stx-selected", "true");
  loadIntoPanel(href + (href.includes("?") ? "&" : "?") + "partial=1");
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initFleet);
} else {
  initFleet();
}

function initFleet() {
  const table = document.querySelector("[data-fleet-table]");
  if (table) table.addEventListener("click", onClick);
  /* Buzz filter (operator 2026-10-23): free-substring match across the
     row's name + carried facts (state text + data-agent-* attributes).
     Vanilla, no bundler; fails open (no match hiding when JS is off). */
  const box = document.querySelector("[data-role='agents-filter']");
  if (box) box.addEventListener("input", () => applyFilter(box.value, table));
}

function applyFilter(query, table) {
  const q = (query || "").trim().toLowerCase();
  const rows = table
    ? table.querySelectorAll("tr.agent-row")
    : document.querySelectorAll("tr.agent-row");
  for (const row of rows) {
    if (!q) {
      row.removeAttribute("data-fleet-hidden");
      continue;
    }
    const hay = [
      row.getAttribute("data-agent-name"),
      row.getAttribute("data-agent-runtime"),
      row.getAttribute("data-agent-billing"),
      row.getAttribute("data-agent-auth"),
      row.getAttribute("data-agent-harness"),
      row.getAttribute("data-agent-engine"),
      row.getAttribute("data-agent-model"),
      row.getAttribute("data-agent-host"),
      row.getAttribute("data-agent-port"),
      row.textContent,
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    if (hay.includes(q)) row.removeAttribute("data-fleet-hidden");
    else row.setAttribute("data-fleet-hidden", "true");
  }
}
