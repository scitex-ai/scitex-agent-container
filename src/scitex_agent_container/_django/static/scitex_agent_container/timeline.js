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
  var nativeSessions = Object.create(null);
  var initialSnapshots = list.querySelectorAll("[data-native-agent]");
  for (var n = 0; n < initialSnapshots.length; n++) {
    var initial = initialSnapshots[n];
    var initialAt = initial.getAttribute("data-native-session-at");
    nativeSessions[initial.getAttribute("data-native-agent")] = {
      session: initial.getAttribute("data-native-session") || "",
      at: initialAt === null ? null : Number(initialAt),
      uncertain: false,
    };
  }

  function keys() {
    var seen = Object.create(null);
    var nodes = list.querySelectorAll("[data-key]");
    for (var i = 0; i < nodes.length; i++) seen[nodes[i].getAttribute("data-key")] = nodes[i];
    return seen;
  }

  function eventKey(e) {
    // Only server-published dated event identity qualifies for state refresh.
    // Neither a read time nor an undated value establishes an event order.
    return !e.snapshot && typeof e.at === "number" && Number.isFinite(e.at) && e.at > 0 &&
      typeof e.event_key === "string" ? e.event_key : "";
  }

  function entryNode(e) {
    var li = document.createElement("li");
    var dated = typeof e.at === "number" && Number.isFinite(e.at) && e.at > 0;
    li.className = "tl-entry tl-" + e.state +
      (e.state === "observed" && dated && !e.snapshot && !e.static ? " is-new" : "");
    li.setAttribute("data-key", e.key);
    if (eventKey(e)) {
      li.setAttribute("data-event-key", e.event_key);
      li.setAttribute("data-observed-at", e.at);
      if (typeof e.age_seconds === "number" && Number.isFinite(e.age_seconds) && e.age_seconds >= 0) {
        li.setAttribute("data-age-seconds", e.age_seconds);
      }
    }
    if (e.snapshot_key) {
      li.setAttribute("data-snapshot-key", e.snapshot_key);
      li.setAttribute("data-native-agent", e.agent);
      li.setAttribute("data-native-session", e.native_session || "");
      if (typeof e.native_session_at === "number") li.setAttribute("data-native-session-at", e.native_session_at);
      if (typeof e.at === "number") li.setAttribute("data-observed-at", e.at);
    }
    var display = e.display_value === undefined ? e.value : e.display_value;
    var value = display === null || display === undefined ? "—" : String(display);
    li.innerHTML =
      '<span class="tl-dot ' + e.state + '" aria-hidden="true"></span>' +
      '<span class="tl-when"></span>' +
      '<span class="tl-agent"></span>' +
      '<span class="tl-kind"></span>' +
      '<span class="tl-value"></span>' +
      '<span class="tl-state tl-state-' + e.state + '"></span>';
    li.querySelector(".tl-when").textContent = dated ? e.relative || "Age unknown" : "Age unknown";
    li.querySelector(".tl-agent").textContent = e.agent;
    li.querySelector(".tl-kind").textContent = e.kind_label || e.kind;
    li.querySelector(".tl-value").textContent = value;
    for (var j = 0; j < 2; j++) {
      var detail = j === 0 ? e.reason : e.note;
      if (!detail) continue;
      var explanation = document.createElement("small");
      explanation.className = "tl-reason dim";
      explanation.textContent = detail;
      li.querySelector(".tl-value").appendChild(explanation);
    }
    li.querySelector(".tl-state").textContent = e.state_label || e.state;
    if (e.source) {
      var provenance = document.createElement("span");
      provenance.className = "tl-source dim";
      provenance.textContent = e.source;
      li.appendChild(provenance);
    }
    return li;
  }

  function refreshEntry(prior, e) {
    var priorAge = prior.getAttribute("data-age-seconds");
    if (priorAge !== null && typeof e.age_seconds === "number" &&
        Number.isFinite(e.age_seconds) && e.age_seconds < Number(priorAge)) return;
    var fresh = entryNode(e);
    prior.setAttribute("data-key", e.key);
    if (eventKey(e)) prior.setAttribute("data-event-key", e.event_key);
    if (fresh.hasAttribute("data-age-seconds")) prior.setAttribute("data-age-seconds", fresh.getAttribute("data-age-seconds"));
    // Keep the same node and its animation lifetime. A stale transition
    // removes the initial animation; a repeated observed event never pulses.
    ["observed", "stale", "unknown", "unreachable"].forEach(function (state) { prior.classList.remove("tl-" + state); });
    prior.classList.add("tl-" + e.state);
    if (e.state !== "observed") prior.classList.remove("is-new");
    prior.querySelector(".tl-when").textContent = fresh.querySelector(".tl-when").textContent;
    prior.querySelector(".tl-dot").className = fresh.querySelector(".tl-dot").className;
    prior.querySelector(".tl-state").className = fresh.querySelector(".tl-state").className;
    prior.querySelector(".tl-state").textContent = fresh.querySelector(".tl-state").textContent;
    var value = prior.querySelector(".tl-value");
    var freshValue = fresh.querySelector(".tl-value");
    value.textContent = "";
    while (freshValue.firstChild) value.appendChild(freshValue.firstChild);
    var oldSource = prior.querySelector(".tl-source");
    var freshSource = fresh.querySelector(".tl-source");
    if (freshSource && oldSource) oldSource.textContent = freshSource.textContent;
    else if (freshSource) prior.appendChild(freshSource);
    else if (oldSource) oldSource.remove();
  }

  function merge(entries) {
    var known = keys();
    var events = Object.create(null);
    var eventNodes = list.querySelectorAll("[data-event-key]");
    for (var d = 0; d < eventNodes.length; d++) events[eventNodes[d].getAttribute("data-event-key")] = eventNodes[d];
    var groups = Object.create(null);
    var incomingSnapshots = Object.create(null);
    for (var g = 0; g < entries.length; g++) {
      if (!entries[g].snapshot) continue;
      (groups[entries[g].agent] || (groups[entries[g].agent] = [])).push(entries[g]);
      incomingSnapshots[entries[g].snapshot_key] = true;
    }
    var decisions = Object.create(null);
    for (var agent in groups) {
      var first = groups[agent][0];
      var session = first.native_session || "";
      var at = typeof first.native_session_at === "number" && Number.isFinite(first.native_session_at) ? first.native_session_at : null;
      var previous = nativeSessions[agent];
      var same = !previous || session === previous.session;
      var consistent = groups[agent].every(function (e) { return (e.native_session || "") === session; });
      var decision = "accept";
      if (!consistent) decision = "unknown";
      else if (previous && previous.at !== null && at !== null && at < previous.at) decision = "reject";
      else if (previous && ((!same && (at === null || previous.at === null || at === previous.at)) ||
               (previous.uncertain && (previous.at === null || at === null || at <= previous.at)) ||
               (previous.at !== null && at === null))) decision = "unknown";
      decisions[agent] = decision;
      if (decision === "accept") nativeSessions[agent] = {session: session, at: at, uncertain: false};
      else if (decision === "unknown") {
        if (!previous) nativeSessions[agent] = {session: "", at: null, uncertain: true};
        else previous.uncertain = true;
      }
    }
    var snapshots = Object.create(null);
    var snapshotNodes = list.querySelectorAll("[data-snapshot-key]");
    for (var s = 0; s < snapshotNodes.length; s++) {
      var old = snapshotNodes[s];
      var oldAgent = old.getAttribute("data-native-agent");
      if (!groups[oldAgent] || (decisions[oldAgent] !== "reject" && !incomingSnapshots[old.getAttribute("data-snapshot-key")])) old.remove();
      else snapshots[old.getAttribute("data-snapshot-key")] = old;
    }
    var added = 0;
    for (var i = entries.length - 1; i >= 0; i--) {
      var e = entries[i];
      // Native counters/session/status are current snapshots, not a history
      // of inferred per-tool events. Replace even a same-key snapshot so its
      // published age/reason can refresh, with no new-row animation.
      if (e.snapshot && e.snapshot_key) {
        if (decisions[e.agent] === "reject") continue;
        var prior = snapshots[e.snapshot_key];
        if (prior && (prior.getAttribute("data-native-session") || "") === (e.native_session || "")) {
          var priorAt = prior.getAttribute("data-observed-at");
          if (priorAt !== null && typeof e.at === "number" && e.at < Number(priorAt)) continue;
          if (priorAt !== null && typeof e.at !== "number") {
            e = Object.assign({}, e, {value: null, display_value: "Unknown", state: "unknown", state_label: "unknown",
              at: null, relative: "Age unknown", note: "An undated native value cannot supersede a dated observation."});
          }
        }
        if (decisions[e.agent] === "unknown") {
          e = Object.assign({}, e, {value: null, display_value: "Unknown", state: "unknown", state_label: "unknown",
            at: null, relative: "Age unknown", native_session: "", native_session_at: null,
            note: (e.note ? e.note + " " : "") + "Native session order is unknown from the published timestamps."});
        }
        if (snapshots[e.snapshot_key]) snapshots[e.snapshot_key].remove();
        var node = entryNode(e);
        list.insertBefore(node, list.firstChild);
        snapshots[e.snapshot_key] = node;
        added++;
        continue;
      }
      var priorEvent = eventKey(e) ? events[e.event_key] : known[e.key];
      if (priorEvent) {
        refreshEntry(priorEvent, e);
        known[e.key] = priorEvent;
        continue;
      }
      var eventNode = entryNode(e);
      known[e.key] = eventNode;
      if (eventKey(e)) events[e.event_key] = eventNode;
      list.insertBefore(eventNode, list.firstChild);
      added++;
    }
    return added;
  }

  function tally() {
    // Count accepted displayed rows, including retained dated history.
    // A rejected frame or uncertain session cannot relabel their aggregate.
    var nodes = list.querySelectorAll(".tl-entry");
    var counts = { total: nodes.length, observed: 0, stale: 0, unreachable: 0 };
    for (var i = 0; i < nodes.length; i++) {
      ["observed", "stale", "unreachable"].forEach(function (state) {
        if (nodes[i].classList.contains("tl-" + state)) counts[state]++;
      });
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
        tally();
        setPollerState('<i class="fas fa-circle"></i> refreshed ' +
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
