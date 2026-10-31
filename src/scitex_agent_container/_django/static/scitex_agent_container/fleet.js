/* Fleet-table refresh.
 *
 * BOUNDED AND IDEMPOTENT BY DESIGN (same contract as timeline.js):
 *
 *  - exactly ONE poll is in flight at a time. A slow control plane (measured
 *    5-60s on this fleet) would otherwise stack requests until the browser
 *    starves. The next tick is scheduled only after the previous response
 *    settles, so `interval` is a floor between polls, not a rate.
 *  - refresh stops when the tab is hidden and resumes when it is shown, so a
 *    backgrounded tab does not poll a shared control plane forever.
 *  - refresh stops after a transport failure and surfaces a Retry, rather than
 *    hammering a listener that is already unhappy.
 *  - a poll never reloads while the operator is typing: an in-progress message
 *    or action form defers the reload to the next tick.
 *
 * WHY THIS EXISTS. The fleet shell renders from the server's last-known
 * snapshot so first paint is instant; a one-shot script owns the cold
 * `loading` shell (fetch + reload). But the `cached`/`ok`/`empty` shells had
 * NO poller, so "Last known Ns ago ... it refreshes automatically" was a
 * promise nothing kept: the banner aged forever until the operator reloaded
 * by hand. This poller keeps exactly that promise — nothing else.
 *
 * It never invents a row: a poll only WARMS the server snapshot via
 * GET /api/fleet and reloads so the server re-renders; the API is the only
 * source and a failure renders a Retry, never stale-as-fresh.
 */
(function () {
  "use strict";

  var root = document.querySelector('.agents-app[data-page="fleet"]');
  if (!root) return;
  // The cold shell is owned by the inline one-shot (fetch + reload once).
  var state = root.getAttribute("data-fleet-state");
  if (state === "loading" || state === "unavailable") return;

  var apiUrl = root.getAttribute("data-fleet-api");
  if (!apiUrl) return;
  var ttl = parseFloat(root.getAttribute("data-cache-ttl") || "45");
  if (!(ttl > 0)) ttl = 45;
  var baseAge = parseFloat(root.getAttribute("data-observed-age") || "0");
  if (!(baseAge >= 0)) baseAge = 0;
  var bootTime = Date.now();

  var inFlight = false;
  var stopped = false;
  var timer = null;
  var banner = root.querySelector('[data-role="fleet-banner"]');
  var ageNodes = root.querySelectorAll('[data-role="fleet-age"]');
  var shortAgeNodes = root.querySelectorAll('[data-role="fleet-age-short"]');

  function snapshotAge() {
    return baseAge + (Date.now() - bootTime) / 1000;
  }

  function tickAge() {
    var seconds = Math.max(0, Math.floor(snapshotAge()));
    var i;
    for (i = 0; i < ageNodes.length; i++) {
      ageNodes[i].textContent = "Last known " + seconds + "s ago";
    }
    for (i = 0; i < shortAgeNodes.length; i++) {
      shortAgeNodes[i].textContent = seconds + "s ago";
    }
  }

  function operatorIsTyping() {
    var el = document.activeElement;
    return !!el && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName);
  }

  function offerRetry() {
    var host = banner || root.querySelector(".agents-scope-note");
    if (!host || host.querySelector('[data-role="fleet-retry"]')) return;
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-retry";
    btn.setAttribute("data-action", "retry");
    btn.setAttribute("data-role", "fleet-retry");
    btn.textContent = "Retry";
    btn.addEventListener("click", function () {
      window.location.reload();
    });
    host.appendChild(btn);
  }

  function schedule(ms) {
    if (stopped || document.hidden) return;
    clearTimeout(timer);
    timer = setTimeout(poll, ms);
  }

  function poll() {
    if (inFlight || stopped) return;   // ONE in flight, always
    inFlight = true;
    fetch(apiUrl, { method: "GET", credentials: "same-origin" })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        inFlight = false;
        if (!data || data.ok !== true) throw new Error("refresh failed");
        // An empty shell only needs a reload once there is something to show.
        if (state === "empty" && (!data.agents || !data.agents.length)) {
          schedule(ttl * 1000);
          return;
        }
        if (operatorIsTyping()) { schedule(30 * 1000); return; }
        window.location.reload();
      })
      .catch(function () {
        inFlight = false;
        // Stop rather than hammer a listener that is already failing.
        stopped = true;
        offerRetry();
      });
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      clearTimeout(timer);
    } else if (!stopped) {
      poll();
    }
  });

  // The rendered age is frozen at paint; keep it honest every 5s.
  tickAge();
  setInterval(function () { if (!document.hidden) tickAge(); }, 5000);

  // `cached` is already stale at paint: warm + reload soon. `ok`/`empty`
  // re-check once the snapshot outlives its TTL.
  var delay = state === "cached" ? 3000 : Math.max(1000, (ttl - snapshotAge()) * 1000);
  schedule(delay);
})();
