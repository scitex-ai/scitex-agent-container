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
  return document.querySelector("[data-role='fleet-detail-body']") || document.querySelector("[data-role='fleet-detail']");
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
  let href = link ? link.getAttribute("href") : null;
  if (!href || href === "#") {
    const name = row.getAttribute("data-agent-name");
    if (!name) return;
    const api = document.querySelector(".agents-app")?.getAttribute("data-fleet-api") || "/apps/agent-container/api/fleet";
    href = api.replace(/\/api\/fleet\/?$/, "") + "/" + encodeURIComponent(name) + "/";
  }
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
  if (table) {
    table.addEventListener("click", onClick);
    table.addEventListener("contextmenu", onRowMenu);
  }
  document.addEventListener("click", closeRowMenu);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeRowMenu();
  });
  /* Buzz filter (operator 2026-10-23): free-substring match across the
     row's name + carried facts (state text + data-agent-* attributes).
     Vanilla, no bundler; fails open (no match hiding when JS is off). */
  const box = document.querySelector("[data-role='agents-filter']");
  if (box) {
    box.addEventListener("input", () => applyFilter(box.value, table));
    /* Ctrl/⌘+K focuses the filter (global-header palette idiom). */
    document.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "k") {
        e.preventDefault();
        box.focus();
        box.select();
      }
    });
    box.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        box.value = "";
        applyFilter("", table);
        box.blur();
      }
    });
  }
}

/* Right-click menu on each fleet row (operator 2026-10-23): inspect in
   Details + lifecycle/message shortcuts. Reuses the row's own links and
   POST forms, so gating matches the visible buttons exactly. Vanilla. */
function closeRowMenu() {
  const open = document.querySelector("[data-role='fleet-row-menu']");
  if (open) open.remove();
}

function onRowMenu(event) {
  const row = event.target instanceof Element ? event.target.closest("tr.agent-row") : null;
  if (!row) return;
  event.preventDefault();
  closeRowMenu();
  const name = row.getAttribute("data-agent-name") || "";
  const inspectHref = (row.querySelector("a[href]") || {}).getAttribute
    ? row.querySelector("a[href]").getAttribute("href")
    : "#";
  const menu = document.createElement("div");
  menu.setAttribute("data-role", "fleet-row-menu");
  menu.setAttribute("role", "menu");
  menu.className = "fleet-row-menu";
  const items = [
    { label: "Inspect in Details", act: () => loadIntoPanel(inspectHref + (inspectHref.includes("?") ? "&" : "?") + "partial=1") },
    { label: "Open detail page", act: () => { window.location.href = inspectHref; } },
  ];
  for (const action of ["start", "restart", "stop"]) {
    const form = row.querySelector('form[action$="/' + action + '"]');
    if (form) {
      items.push({
        label: action[0].toUpperCase() + action.slice(1),
        act: () => form.submit(),
      });
    }
  }
  /* Registry verbs reuse the row's own forget/delete forms, so the POST
     routes, gates, and confirm() dialogs match the former buttons.
     requestSubmit (not submit) so the inline confirm() still fires. */
  for (const verb of ["forget", "delete"]) {
    const form = row.querySelector('form[action$="/' + verb + '"]');
    if (form) {
      items.push({
        label: verb[0].toUpperCase() + verb.slice(1),
        act: () => {
          const btn = form.querySelector("button[type='submit']");
          if (btn && typeof form.requestSubmit === "function") form.requestSubmit(btn);
          else form.submit();
        },
      });
    }
  }
  const msgForm = row.querySelector(".qa-msg");
  if (msgForm) {
    items.push({
      label: "Focus message input",
      act: () => {
        const input = msgForm.querySelector("input[type='text']");
        if (input) input.focus();
      },
    });
  }
  for (const item of items) {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "menuitem");
    b.textContent = (item.label + " — " + name).slice(0, 80);
    b.addEventListener("click", () => {
      closeRowMenu();
      item.act();
    });
    menu.appendChild(b);
  }
  menu.style.left = Math.min(event.clientX, window.innerWidth - 240) + "px";
  menu.style.top = Math.min(event.clientY, window.innerHeight - items.length * 36 - 16) + "px";
  document.body.appendChild(menu);
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
