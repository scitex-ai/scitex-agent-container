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
    /* Operator 2026-10-22: the fleet center pane owns an ALWAYS-VISIBLE
       composer (form[data-composer="fleet-dm"]). When it exists the
       fragment's composer stays in the Control tab and the fleet composer
       is retargeted to the selected agent instead of being wiped. */
    const fleetComposer = chatHostEl
      ? chatHostEl.querySelector('form[data-composer="fleet-dm"]')
      : null;
    if (chatHostEl) {
      if (fleetComposer && thread) {
        /* Fleet composer owns the pane: swap in the fresh thread ahead of
           it, drop the cold placeholder, retarget the composer. The
           fragment's own composer stays in the Control tab. */
        const old = chatHostEl.querySelector("[data-role='chat-thread']");
        if (old) old.remove();
        const empty = chatHostEl.querySelector(".agents-chat-empty");
        if (empty) empty.remove();
        chatHostEl.insertBefore(document.adoptNode(thread), fleetComposer);
        retargetFleetComposer(agentNameFromUrl(url), url);
      } else if (!fleetComposer) {
        chatHostEl.innerHTML = "";
        /* Operator 2026-10-26: no name header in the DM pane — the roster
           selection already names the agent (showing it 3x was noise). */
        if (thread) chatHostEl.appendChild(document.adoptNode(thread));
        if (composer) chatHostEl.appendChild(document.adoptNode(composer));
        if (!thread && !composer) chatHostEl.innerHTML = html;
      }
    }
    /* Everything else -> shared Details column. The message thread lives
       in the center DM pane (moved out of the Control tab above), so the
       adopted thread/composer nodes are skipped here — they nest inside
       the fragment root, so check descendants too. */
    if (detailHost) {
      const frag = document.createDocumentFragment();
      for (const child of Array.from(doc.body ? doc.body.childNodes : [])) {
        if (child.nodeType !== 1) continue;
        if (child.matches && (child.matches("[data-role='chat-thread']") || child.matches("form.chat-composer") || child.matches(".detail-name"))) continue;
        frag.appendChild(document.adoptNode(child));
      }
      if (frag.childNodes.length) {
        detailHost.innerHTML = "";
        detailHost.appendChild(frag);
        initDetailTabs(detailHost);
      } else {
        detailHost.innerHTML = html;
        initDetailTabs(detailHost);
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
  retargetFleetComposer(row.getAttribute("data-agent-name"), href);
  loadIntoPanel(href + (href.includes("?") ? "&" : "?") + "partial=1");
}

/* Fleet DM composer (operator 2026-10-22): the center pane owns one
 * ALWAYS-VISIBLE composer. It starts disabled with hint text; on row
 * selection (click, auto-select, or row-menu inspect) it is retargeted to
 * the selected agent's message endpoint — the same POST shape as the
 * detail message-form. The GUI never constructs host/port: the action is
 * the row's own inspect href + "message", i.e. the leaf route. */
function agentNameFromUrl(url) {
  try {
    const path = String(url || "").split("?", 1)[0].split("#", 1)[0].replace(/\/+$/, "");
    const tail = path.split("/").pop() || "";
    return decodeURIComponent(tail);
  } catch {
    return "";
  }
}

function rowIsCrossHost(name) {
  if (!name || typeof CSS === "undefined" || !CSS.escape) return false;
  const row = document.querySelector(
    'tr.agent-row[data-agent-name="' + CSS.escape(name) + '"]'
  );
  return !!(row && row.classList.contains("cross"));
}

function retargetFleetComposer(name, href) {
  const host = chatHost();
  if (!host || !name) return;
  const form = host.querySelector('form[data-composer="fleet-dm"]');
  if (!form) return;
  if (rowIsCrossHost(name)) {
    /* Operator 2026-11-01: cross-host messaging is not supported in this
       console — leave the composer parked (disabled) and point at
       scitex-cards instead of enabling a send that cannot complete. */
    const area = form.querySelector("textarea[name='message']");
    if (area) {
      area.setAttribute(
        "placeholder",
        "Cross-host messaging isn't supported — reach " + name + " through scitex-cards."
      );
    }
    return;
  }
  const path = String(href || "").split("?", 1)[0].split("#", 1)[0].replace(/\/+$/, "");
  if (path) form.setAttribute("action", path + "/message");
  const area = form.querySelector("textarea[name='message']");
  const label = form.querySelector("label[for]");
  if (area) {
    area.id = "msg-" + name;
    area.removeAttribute("disabled");
    area.setAttribute("placeholder", "Message " + name + "… (Enter or Ctrl+Enter to send, Shift+Enter for a new line)");
    if (label) label.setAttribute("for", area.id);
  }
  for (const btn of Array.from(form.querySelectorAll("button[disabled]"))) {
    btn.removeAttribute("disabled");
  }
}

/* Detail tabs (operator 2026-10-22): one tab active at a time; the server
 * renders every panel visible so no-JS readers get the full content and
 * this only hides. Roving tabindex + arrow keys; idempotent per holder. */
function initDetailTabs(root) {
  const scope = root || document;
  if (!scope.querySelectorAll) return;
  const holders = Array.from(scope.querySelectorAll("[data-detail-tabs]"));
  if (scope !== document && scope.matches && scope.matches("[data-detail-tabs]")) {
    holders.push(scope);
  }
  for (const holder of holders) {
    if (holder.getAttribute("data-tabs-ready") === "true") continue;
    holder.setAttribute("data-tabs-ready", "true");
    const tabs = Array.from(holder.querySelectorAll('[role="tab"]'));
    const panels = Array.from(holder.querySelectorAll('[role="tabpanel"]'));
    if (!tabs.length || !panels.length) continue;
    const byControl = {};
    for (const p of panels) {
      if (p.id) byControl[p.id] = p;
    }
    const activate = (tab, focus) => {
      for (const t of tabs) {
        const on = t === tab;
        t.setAttribute("aria-selected", on ? "true" : "false");
        t.classList.toggle("detail-tab--active", on);
        t.tabIndex = on ? 0 : -1;
      }
      const show = byControl[tab.getAttribute("aria-controls") || ""];
      for (const p of panels) {
        if (p === show) p.removeAttribute("hidden");
        else p.setAttribute("hidden", "");
      }
      if (focus) tab.focus();
    };
    tabs.forEach((t, i) => {
      t.addEventListener("click", () => activate(t, false));
      t.addEventListener("keydown", (e) => {
        if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
        e.preventDefault();
        const next = (i + (e.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length;
        activate(tabs[next], true);
      });
    });
    const current = tabs.find((t) => t.getAttribute("aria-selected") === "true") || tabs[0];
    activate(current, false);
  }
}

/* Cold-shell warm poll (operator 2026-10-26: extracted from the fleet
 * template — no inline scripts in HTML, per the scripts-separate rule).
 * BOUNDED (operator 2026-10-22): while fleet_state == loading, poll
 * /api/fleet's JSON state up to COLD_POLL_MAX times. A warm snapshot
 * (ok:true) reloads ONCE to render inventory; a still-failing plane settles
 * the loading shell IN PLACE to unavailable + Retry — no full-page reload
 * loops (at most one reload per cold shell). A hard reload (Ctrl+Shift+R)
 * re-enters here with no cached snapshot and no JS state, and still ends in
 * inventory or unavailable + Retry within the same bound.
 *
 * FETCH FUSE (operator 2026-10-31, card sac-gui-cold-fetch-timeout-20261026):
 * every poll attempt carries an AbortController fuse at COLD_FETCH_TIMEOUT_MS,
 * tied to the 60s server ceiling (_remote.DEFAULT_TIMEOUT_SECONDS = 60.0,
 * measured 60.06s end-to-end). A hung first fetch previously never settled —
 * the retry counter only advances on settled responses, so the page sat on
 * "Reading the fleet…" forever. The fuse turns a hung fetch into a normal
 * settled failure: the existing .catch retries within the bound, then
 * settleUnavailable() ends the page in unavailable + Retry. */
var COLD_POLL_MAX = 8;
var COLD_POLL_MS = 1500;
var COLD_FETCH_TIMEOUT_MS = 65000;
var COLD_MAX_RELOADS = 1;
var COLD_RELOAD_KEY = "sac-cold-reloads-v1";

function coldReloads() {
  try {
    return parseInt(window.sessionStorage.getItem(COLD_RELOAD_KEY) || "0", 10) || 0;
  } catch {
    return 0;
  }
}

function noteColdReload() {
  try {
    window.sessionStorage.setItem(COLD_RELOAD_KEY, String(coldReloads() + 1));
  } catch {
    /* storage unavailable: the reload cap degrades to best-effort */
  }
}

/* Settle the loading shell in place: the same state + Retry copy as the
 * server-rendered unavailable banner, so the page never spins forever. */
function settleUnavailable(app) {
  if (!app || app.getAttribute("data-fleet-state") !== "loading") return;
  if (app.querySelector("[data-role='fleet-settled-unavailable']")) return;
  app.setAttribute("data-fleet-state", "unavailable");
  const banner = document.createElement("div");
  banner.className = "agents-bannor err";
  banner.setAttribute("role", "alert");
  banner.setAttribute("data-role", "fleet-settled-unavailable");
  const icon = document.createElement("i");
  icon.className = "fas fa-triangle-exclamation";
  banner.appendChild(icon);
  const span = document.createElement("span");
  span.textContent = "The fleet is not available right now.";
  banner.appendChild(span);
  const note = document.createElement("small");
  note.textContent = "Your agents are unaffected — this is the console failing to reach its data source, not your fleet going away.";
  banner.appendChild(note);
  const retryWrap = document.createElement("small");
  retryWrap.className = "diag";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn btn-retry";
  btn.setAttribute("data-action", "retry");
  const btnIcon = document.createElement("i");
  btnIcon.className = "fas fa-rotate";
  btn.appendChild(btnIcon);
  btn.appendChild(document.createTextNode(" Retry"));
  btn.addEventListener("click", function () { window.location.reload(); });
  retryWrap.appendChild(btn);
  banner.appendChild(retryWrap);
  const shell = app.querySelector(".agents-loading");
  if (shell) shell.replaceWith(banner);
  else app.appendChild(banner);
}

function warmColdShell() {
  const app = document.querySelector(".agents-app");
  if (!app || app.getAttribute("data-fleet-state") !== "loading") return;
  const base = app.getAttribute("data-fleet-api") || "/apps/agent-container/api/fleet";
  let polls = 0;
  const poll = () => {
    polls += 1;
    /* Fetch fuse: a hung attempt must settle so the retry counter advances.
     * Abort lands in the .catch below (settled failure): retry within the
     * bound, then settleUnavailable. The fuse is cleared on every settle so
     * a late abort can never fire into the next attempt. */
    const controller = new AbortController();
    const fuse = setTimeout(() => controller.abort(), COLD_FETCH_TIMEOUT_MS);
    fetch(base, { method: "GET", credentials: "same-origin", headers: { Accept: "application/json" }, signal: controller.signal })
      .then((response) => response.json().catch(() => ({ ok: false })))
      .then((data) => {
        clearTimeout(fuse);
        if (data && data.ok === true) {
          /* Snapshot is warm: a single reload renders inventory. */
          if (coldReloads() < COLD_MAX_RELOADS) {
            noteColdReload();
            window.location.reload();
            return;
          }
        }
        if (polls < COLD_POLL_MAX) setTimeout(poll, COLD_POLL_MS);
        else settleUnavailable(app);
      })
      .catch(() => {
        /* Console unreachable (or the fetch fuse above fired on a hung
         * attempt): retry within the bound, then settle. */
        clearTimeout(fuse);
        if (polls < COLD_POLL_MAX) setTimeout(poll, COLD_POLL_MS);
        else settleUnavailable(app);
      });
  };
  poll();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initFleet);
} else {
  initFleet();
}

function initFleet() {
  warmColdShell();
  initDetailTabs(document);
  const table = document.querySelector("[data-fleet-table]");
  if (table) {
    table.addEventListener("click", onClick);
    table.addEventListener("contextmenu", onRowMenu);
    /* Operator 2026-10-23: never show a dead DM pane — auto-open the
       first online agent so the thread + Details populate on load.
       Pinned favorites win over state bands. Retry bounded (rows can
       paint after module eval on cache-warm reloads): 3 attempts, 500ms
       apart, then give up silently. */
    applyPins(table);
    let autoTries = 0;
    const autoSelect = () => {
      if (table.querySelector("tr.agent-row[data-stx-selected]")) return;
      const first = table.querySelector("tr.agent-row[data-pinned]") || table.querySelector("tr.agent-row.good") || table.querySelector("tr.agent-row");
      if (first) first.click();
      else if (autoTries < 2) {
        autoTries += 1;
        setTimeout(autoSelect, 500);
      }
    };
    autoSelect();
  }
  document.addEventListener("click", closeRowMenu);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeRowMenu();
  });
  /* Buzz filter (operator 2026-10-23): free-substring match across the
     row's name + carried facts (state text + data-agent-* attributes).
     Vanilla, no bundler; fails open (no match hiding when JS is off). */
  const filterMount = document.querySelector('[data-stx-filter-field][data-filter-id="agents-filter"]');
  filterMount?.addEventListener("input", (event) => {
    if (event.target instanceof HTMLInputElement) applyFilter(event.target.value, table);
  });

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

/* DM composer: Enter-to-send + emoji stamp (operator 2026-10-24,
 * Telegram-learned slice A). Delegated on document because the composer
 * arrives via fetch-injection into the DM pane. Enter or Ctrl/Command+Enter
 * submits through requestSubmit so dispatch_id + validation still run;
 * Shift+Enter keeps the newline. The emoji button toggles a small
 * curated popover that inserts at the cursor. Vanilla, no bundler. */
const DM_EMOJIS = ["👍", "👎", "🙏", "✅", "❌", "🚀", "👀", "💡", "⚠️", "🎉", "❤️", "🤔"];

function dmComposerOf(node) {
  return node instanceof Element ? node.closest('form[data-composer]') : null;
}

function dmTextarea(form) {
  return form ? form.querySelector("textarea[name='message']") : null;
}

function closeEmojiPopover() {
  const open = document.querySelector("[data-role='dm-emoji-popover']");
  if (open) open.remove();
}

function toggleEmojiPopover(button) {
  const form = dmComposerOf(button);
  const area = dmTextarea(form);
  if (!form || !area) return;
  const existing = document.querySelector("[data-role='dm-emoji-popover']");
  if (existing && existing.getAttribute("data-for") === area.id) {
    existing.remove();
    return;
  }
  closeEmojiPopover();
  const pop = document.createElement("div");
  pop.setAttribute("data-role", "dm-emoji-popover");
  pop.setAttribute("data-for", area.id || "");
  pop.setAttribute("role", "menu");
  pop.className = "dm-emoji-popover";
  for (const emoji of DM_EMOJIS) {
    const b = document.createElement("button");
    b.type = "button";
    b.setAttribute("role", "menuitem");
    b.className = "dm-emoji-pick";
    b.textContent = emoji;
    b.addEventListener("click", () => {
      const start = area.selectionStart == null ? area.value.length : area.selectionStart;
      const end = area.selectionEnd == null ? start : area.selectionEnd;
      area.value = area.value.slice(0, start) + emoji + area.value.slice(end);
      const pos = start + emoji.length;
      try {
        area.setSelectionRange(pos, pos);
      } catch {
        /* non-text inputs never reach here; ignore */
      }
      area.focus();
      closeEmojiPopover();
    });
    pop.appendChild(b);
  }
  form.appendChild(pop);
}

document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.shiftKey || e.altKey || e.isComposing) return;
  const form = e.target instanceof Element ? e.target.closest('form[data-composer] textarea[name="message"]') : null;
  if (!form) return;
  const composer = dmComposerOf(form);
  if (!composer) return;
  e.preventDefault();
  if (typeof composer.requestSubmit === "function") composer.requestSubmit();
  else composer.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
});

/* Fleet composer send-in-place (operator 2026-10-26): the fleet composer
 * POSTs via fetch so Enter-to-send never navigates away from the fleet
 * page (the native POST redirects to the agent detail page). Success
 * clears the input and refreshes the DM thread through the existing
 * fragment path. A refusal stays inline with the draft and dispatch id;
 * retrying as a native POST would navigate away and may submit twice. */
function composerError(form, message, pending = false) {
  let notice = form.querySelector("[data-composer-error]");
  if (!notice) {
    notice = document.createElement("div");
    notice.setAttribute("data-composer-error", "");
    notice.setAttribute("role", "alert");
    notice.className = "agents-bannor err";
    form.appendChild(notice);
  }
  notice.className = pending ? "agents-bannor warn" : "agents-bannor err";
  notice.textContent = message;
  notice.hidden = !message;
}

function renewComposerDispatch(form) {
  const field = form.querySelector("input[name='dispatch_id']");
  if (field) {
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    field.value = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  }
}

document.addEventListener("submit", (e) => {
  const form = e.target instanceof Element ? e.target.closest('form[data-composer]') : null;
  if (!form) return;
  if (!form.closest('.agents-app[data-page="fleet"]')) return;
  // Even a missing/invalid target must stay on the dashboard with its draft.
  e.preventDefault();
  const action = String(form.getAttribute("action") || "");
  if (!/\/message\/?$/.test(action)) {
    composerError(form, "Select an agent before sending a message.");
    return;
  }
  if (form.dataset.sending === "true") return;
  const area = form.querySelector("textarea[name='message']");
  const sentText = area ? area.value : "";
  const payload = JSON.stringify([action, sentText]);
  if (form.dataset.uncertainPayload === payload) {
    composerError(form, "Delivery is unconfirmed. Check the agent before starting a new draft; this message will not be sent again.");
    return;
  }
  if (form.dataset.dispatchPayload && form.dataset.dispatchPayload !== payload) {
    renewComposerDispatch(form);
  }
  form.dataset.dispatchPayload = payload;
  form.dataset.sending = "true";
  composerError(form, "");
  const button = form.querySelector("button[type='submit']");
  if (button) button.disabled = true;
  fetch(action, {
    method: "POST",
    body: new FormData(form),
    credentials: "same-origin",
    headers: { Accept: "application/json" },
  })
    .then((r) => r.json()
      .catch(() => ({ ok: false, message: "The server did not confirm delivery." }))
      .then((result) => ({ ...result, ok: r.ok && result.ok === true })))
    .then((result) => {
      if (result && result.ok === true) {
        renewComposerDispatch(form);
        form.dataset.dispatchPayload = "";
        if (form.getAttribute("action") === action) {
          if (area) { if (area.value === sentText) area.value = ""; area.focus(); }
          loadIntoPanel(action.replace(/\/message\/?$/, "") + "?partial=1");
          if (result.state === "accepted") composerError(form, result.message, true);
        }
        return;
      }
      if (result.state === "unknown") form.dataset.uncertainPayload = payload;
      composerError(form, String((result && (result.message || result.error)) || "Message delivery was not confirmed."));
    })
    .catch(() => {
      form.dataset.uncertainPayload = payload;
      composerError(form, "Message delivery was not confirmed. Your draft is kept; check the agent before starting a new draft.");
    })
    .finally(() => {
      form.dataset.sending = "false";
      if (button) button.disabled = false;
    });
});

document.addEventListener("click", (e) => {
  const button = e.target instanceof Element ? e.target.closest("[data-emoji-button]") : null;
  if (button) {
    toggleEmojiPopover(button);
    return;
  }
  if (e.target instanceof Element && !e.target.closest("[data-role='dm-emoji-popover']")) {
    closeEmojiPopover();
  }
});
