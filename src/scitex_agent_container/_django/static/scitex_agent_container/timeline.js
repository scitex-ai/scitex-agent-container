/* Activity-timeline refresh.
 *
 * BOUNDED and IDEMPOTENT BY DESIGN:
 *
 *  - exactly ONE poll is in flight at a time. A slow control plane (measured
 *    5-60s on this fleet) would otherwise stack requests until the browser
 *    starves. The next tick is scheduled only after the previous response
 *    settles, so `refresh_seconds` is a floor between polls, not a rate.
 *  - refresh stops when the tab is hidden and resumes when it is shown, so a
 *    backgrounded tab does not poll a shared control plane forever.
 *  - refresh stops after a transport failure and surfaces a Retry, rather than
 *    hammering a listener that is already unhappy.
 *  - rows already rendered are deduped BY KEY, so a poll that re-sees an
 *    unchanged observation does not duplicate it. This mirrors the server's
 *    dedup key exactly; if the two disagreed, the page would drift from the
 *    API it claims to show.
 *
 * It never invents a row: the API is the only source, and a failure renders
 * the error banner, not stale-as-fresh.
 */
(function () {
  "use strict";

  var root = document.querySelector('.agents-app[data-page="timeline"]');
  if (!root) return;
  var list = root.querySelector('[data-role="timeline"]');
  var url = root.getAttribute("data-timeline-url");
  var seconds = parseInt(root.getAttribute("data-refresh-seconds") || "15", 10);
  if (!list || !url) return;

  var errorBox = root.querySelector('[data-role="timeline-error"]');
  var errorText = root.querySelector('[data-role="timeline-error-text"]');
  var pollerState = root.querySelector('[data-role="poller-state"]');

  var inFlight = false;
  var stopped = false;
  var timer = null;

  function keys() {
    var seen = Object.create(null);
    var nodes = list.querySelectorAll("[data-key]");
    for (var i = 0; i < nodes.length; i++) seen[nodes[i].getAttribute("data-key")] = true;
    return seen;
  }

  function entryNode(e) {
    var li = document.createElement("li");
    li.className = "tl-entry tl-" + e.state + " is-new";
    li.setAttribute("data-key", e.key);
    var value = e.value === null || e.value === undefined ? "—" : String(e.value);
    li.innerHTML =
      '<span class="tl-dot ' + e.state + '" aria-hidden="true"></span>' +
      '<time class="tl-when">' + e.relative + "</time>" +
      '<span class="tl-agent"></span>' +
      '<span class="tl-kind"></span>' +
      '<span class="tl-value"></span>' +
      '<span class="tl-state tl-state-' + e.state + '"></span>';
    li.querySelector(".tl-agent").textContent = e.agent;
    li.querySelector(".tl-kind").textContent = e.kind;
    li.querySelector(".tl-value").textContent = value;
    li.querySelector(".tl-state").textContent = e.state_label || e.state;
    return li;
  }

  function merge(entries) {
    var known = keys();
    var added = 0;
    for (var i = entries.length - 1; i >= 0; i--) {
      var e = entries[i];
      if (known[e.key]) continue;   // already on the page: never duplicate
      known[e.key] = true;
      list.insertBefore(entryNode(e), list.firstChild);
      added++;
    }
    return added;
  }

  function tally(entries) {
    var counts = { observed: 0, stale: 0, unreachable: 0 };
    for (var i = 0; i < entries.length; i++) {
      var st = entries[i].state;
      if (counts[st] !== undefined) counts[st]++;
    }
    for (var k in counts) {
      if (!Object.prototype.hasOwnProperty.call(counts, k)) continue;
      var el = root.querySelector('[data-count="' + k + '"]');
      if (el) el.textContent = counts[k];
    }
  }

  function showError(message) {
    if (!errorBox) return;
    if (errorText) errorText.textContent = message || "";
    errorBox.hidden = false;
  }

  function clearError() {
    if (errorBox) errorBox.hidden = true;
  }

  function setPollerState(text) {
    if (pollerState) pollerState.innerHTML = text;
  }

  function schedule() {
    if (stopped || document.hidden) return;
    clearTimeout(timer);
    timer = setTimeout(poll, seconds * 1000);
  }

  function poll() {
    if (inFlight || stopped) return;   // ONE in flight, always
    inFlight = true;
    var params = new URLSearchParams(window.location.search);
    var qs = params.toString();
    fetch(url + (qs ? "?" + qs : ""), {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    })
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        if (!data.ok) throw new Error(data.error || "refresh failed");
        clearError();
        merge(data.entries || []);
        tally(data.entries || []);
        setPollerState('<i class="fas fa-circle"></i> live · updated ' +
          new Date().toLocaleTimeString());
        inFlight = false;
        schedule();
      })
      .catch(function (err) {
        inFlight = false;
        // Stop rather than hammer a listener that is already failing.
        stopped = true;
        showError(String(err && err.message ? err.message : err));
        // Offer a way back that is a real control, not an auto-retry loop.
        if (errorBox && !errorBox.querySelector("[data-role=timeline-retry]")) {
          var btn = document.createElement("button");
          btn.type = "button";
          btn.className = "btn btn-retry";
          btn.setAttribute("data-action", "retry");
          btn.setAttribute("data-role", "timeline-retry");
          btn.textContent = "Retry";
          btn.addEventListener("click", function () {
            stopped = false;
            clearError();
            setPollerState('<i class="fas fa-circle"></i> resuming');
            poll();
          });
          errorBox.appendChild(btn);
        }
      });
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      clearTimeout(timer);
    } else if (!stopped) {
      poll();
    }
  });

  // First refresh after the interval, not immediately: the server already
  // rendered a complete timeline, so an instant poll would be wasted work.
  setPollerState('<i class="fas fa-circle"></i> auto-refresh every ' + seconds + "s");
  schedule();
})();
