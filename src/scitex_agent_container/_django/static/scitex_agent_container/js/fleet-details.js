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

function chatHost() {
  return document.querySelector("[data-role='fleet-chat-body']") || document.querySelector("[data-role='fleet-chat']");
}

async function loadIntoPanels(url) {
  const detailHost = panel();
  const chatHostEl = chatHost();
  if (chatHostEl) chatHostEl.setAttribute("aria-busy", "true");
  if (detailHost) detailHost.setAttribute("aria-busy", "true");
  try {
    const response = await fetch(url, { headers: { Accept: "text/html" } });
    if (!response.ok) return;
    const html = await response.text();
    const doc = new DOMParser().parseFromString(html, "text/html");
    /* Chat thread -> center DM pane (operator 2026-10-23 three-column). */
    const thread = doc.querySelector("[data-role='chat-thread']");
    const composer = doc.querySelector("form.chat-composer");
    if (chatHostEl) {
      chatHostEl.innerHTML = "";
      /* Operator 2026-10-26: no name header in the DM pane — the roster
         selection already names the agent (showing it 3x was noise). */
      if (thread) chatHostEl.appendChild(document.adoptNode(thread));
      if (composer) chatHostEl.appendChild(document.adoptNode(composer));
      if (!thread && !composer) chatHostEl.innerHTML = html;
    }
    /* Everything else -> shared Details column. The message thread lives
       in the center DM pane; its emptied <details> shell stays out
       (it nests inside the fragment root, so check descendants too). */
    if (detailHost) {
      const skip = doc.querySelector("details#message-thread");
      if (skip) skip.remove();
      const frag = document.createDocumentFragment();
      for (const child of Array.from(doc.body ? doc.body.childNodes : [])) {
        if (child.nodeType !== 1) continue;
        if (child.matches && (child.matches("[data-role='chat-thread']") || child.matches("form.chat-composer") || child.matches(".detail-name"))) continue;
        frag.appendChild(document.adoptNode(child));
      }
      if (frag.childNodes.length) {
        detailHost.innerHTML = "";
        detailHost.appendChild(frag);
      } else {
        detailHost.innerHTML = html;
      }
    }
    showDetails();
  } catch {
    /* fail-soft: keep placeholders; the detail page stays reachable */
  } finally {
    if (chatHostEl) chatHostEl.removeAttribute("aria-busy");
    if (detailHost) detailHost.removeAttribute("aria-busy");
  }
}

async function loadIntoPanel(url) {
  return loadIntoPanels(url);
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

/* Cold-shell warm poll (operator 2026-10-26: extracted from the fleet
 * template — no inline scripts in HTML, per the scripts-separate rule).
 * READ-ONLY single shot while fleet_state == loading: warms the server's
 * last-known snapshot via /api/fleet, then one reload shows the true state
 * (inventory, or unavailable + Retry). Never loops. */
function warmColdShell() {
  const app = document.querySelector(".agents-app");
  if (!app || app.getAttribute("data-fleet-state") !== "loading") return;
  const base = app.getAttribute("data-fleet-api") || "/apps/agent-container/api/fleet";
  fetch(base, { method: "GET", credentials: "same-origin" })
    .then(function () { window.location.reload(); })
    .catch(function () { /* console unreachable: stay in loading, do not loop */ });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initFleet);
} else {
  initFleet();
}

function initFleet() {
  warmColdShell();
  const table = document.querySelector("[data-fleet-table]");
  if (table) {
    table.addEventListener("click", onClick);
    table.addEventListener("contextmenu", onRowMenu);
    /* Operator 2026-10-23: never show a dead DM pane — auto-open the
       first online agent so the thread + Details populate on load.
       Pinned favorites win over state bands. */
    applyPins(table);
    if (!table.querySelector("tr.agent-row[data-stx-selected]")) {
      const first = table.querySelector("tr.agent-row[data-pinned]") || table.querySelector("tr.agent-row.good") || table.querySelector("tr.agent-row");
      if (first) first.click();
    }
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

/* Pinned agents (operator 2026-10-26): favorites float above the state
   bands. Client-side (localStorage per browser) — no server state. */
const PIN_KEY = "sac-fleet-pinned-v1";
function getPinned() {
  try {
    const raw = window.localStorage.getItem(PIN_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(arr) ? arr.filter((x) => typeof x === "string") : []);
  } catch {
    return new Set();
  }
}
function setPinned(set) {
  try {
    window.localStorage.setItem(PIN_KEY, JSON.stringify(Array.from(set)));
  } catch {
    /* storage unavailable: pins last for the page view */
  }
}
function applyPins(table) {
  const pinned = getPinned();
  const rows = table ? Array.from(table.querySelectorAll("tr.agent-row")) : [];
  for (const row of rows) {
    const name = row.getAttribute("data-agent-name") || "";
    if (pinned.has(name)) row.setAttribute("data-pinned", "true");
    else row.removeAttribute("data-pinned");
  }
  /* Pinned rows float above the state bands; bands keep server order. */
  const pinnedRows = rows.filter((r) => r.hasAttribute("data-pinned"));
  const tbody = table ? table.querySelector("tbody") : null;
  if (tbody && pinnedRows.length) {
    for (const r of pinnedRows) tbody.insertBefore(r, tbody.firstChild);
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
  /* Pin / unpin favorite (operator 2026-10-26): pinned agents float above
     the state bands on every load. */
  {
    const name = row.getAttribute("data-agent-name") || "";
    const pinned = name && getPinned().has(name);
    items.push({
      label: pinned ? "Unpin agent" : "Pin agent to top",
      act: () => {
        const set = getPinned();
        if (pinned) set.delete(name);
        else if (name) set.add(name);
        setPinned(set);
        const table = document.querySelector("[data-fleet-table]");
        if (table) applyPins(table);
      },
    });
  }
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
    b.textContent = item.label;
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
