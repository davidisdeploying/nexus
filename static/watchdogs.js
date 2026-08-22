/* Watchdogs surface (PANEL-4) — read-only, no polling, no mutation.
 *
 * Bounded-DOM contract: only the 4 host summary rows are server-rendered.
 * Expanding a host fetches GET /api/watchdogs?host=<host> and mounts just
 * that host's rows into its (previously empty) .wd-detail container;
 * collapsing clears that container's innerHTML — unmounted, not just
 * display:none. No timer/interval/WebSocket is created anywhere here.
 */
(function () {
  "use strict";

  // Bounded ceiling for a host-detail fetch. Click-triggered, same-origin,
  // no retry -- 8s is generous slack over normal response time while still
  // giving the user a bounded failure instead of a fetch that can hang
  // indefinitely (no other frontend constant here fits a one-shot fetch
  // timeout: STATUS_MS/POLL_MS in dashboard.js are polling cadences, not
  // per-request ceilings).
  var FETCH_TIMEOUT_MS = 8000;

  // Per-host-section fetch state: controller/timer for the in-flight
  // request, a generation counter to invalidate stale callbacks after a
  // collapse (or a fresh expand), and a busy flag so a click while loading
  // never issues a second fetch for the same host.
  var hostState = new WeakMap();

  function stateFor(hostSection) {
    var state = hostState.get(hostSection);
    if (!state) {
      state = { controller: null, timer: null, generation: 0, busy: false };
      hostState.set(hostSection, state);
    }
    return state;
  }

  var KIND_GLYPH = { guard: "🛡️", watch: "👁️" }; // shield / eye
  var STATUS_META = {
    active: { word: "Active", glyph: "✓", accent: "developed" },
    dormant: { word: "Dormant", glyph: "·", accent: "unexposed" },
    stale_evidence: { word: "Stale evidence", glyph: "!", accent: "safelight" },
    orphaned: { word: "Orphaned", glyph: "⚠", accent: "overexposed" },
    retired: { word: "Retired (by design)", glyph: "⦸", accent: "unexposed retired" },
  };

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function fieldRow(label, value) {
    var row = el("div", "wd-field");
    row.appendChild(el("span", "wd-field-label", label));
    row.appendChild(el("span", "wd-field-value", value));
    return row;
  }

  function buildRow(row) {
    var meta = STATUS_META[row.status] || { word: row.status, glyph: "?", accent: "unexposed" };
    var wrap = el("details", "wd-row");

    var summary = el("summary", "wd-row-summary");
    summary.appendChild(el("span", "wd-kind-glyph", KIND_GLYPH[row.kind] || ""));
    summary.appendChild(el("span", "wd-row-label", row.label));

    var badge = el("span", "wd-badge " + meta.accent);
    badge.setAttribute("aria-label", "status: " + meta.word.toLowerCase());
    badge.appendChild(el("span", "wd-badge-glyph", meta.glyph));
    badge.appendChild(el("span", "wd-badge-word", meta.word));
    summary.appendChild(badge);

    summary.appendChild(el("span", "wd-row-target", row.protected_target));
    wrap.appendChild(summary);

    var detail = el("div", "wd-row-detail");
    [
      ["id", row.id], ["kind", row.kind], ["owner", row.owner], ["host", row.host],
      ["source", row.source], ["protected target", row.protected_target],
      ["cadence / timeout", row.cadence_timeout],
      ["last check evidence", row.last_check_evidence],
      ["last action evidence", row.last_action_evidence],
      ["status", row.status_detail],
      ["source of truth", row.source_of_truth],
      ["evidence as of", row.evidence_as_of],
    ].forEach(function (pair) { detail.appendChild(fieldRow(pair[0], pair[1])); });
    wrap.appendChild(detail);

    return wrap;
  }

  function mountHost(hostSection) {
    var host = hostSection.getAttribute("data-host");
    var toggle = hostSection.querySelector(".wd-host-toggle");
    var detailEl = hostSection.querySelector(".wd-detail");
    var state = stateFor(hostSection);

    if (state.busy) return; // already loading this host -- never duplicate the fetch

    state.generation += 1;
    var generation = state.generation;
    state.busy = true;
    // Flip expanded immediately (not just on success) so a click while
    // loading is read as collapse by the listener below, not a second
    // expand -- that's what lets collapse-while-loading abort cleanly.
    toggle.setAttribute("aria-expanded", "true");

    var controller = (typeof AbortController !== "undefined") ? new AbortController() : null;
    state.controller = controller;
    state.timer = setTimeout(function () {
      if (controller) controller.abort();
    }, FETCH_TIMEOUT_MS);

    fetch(
      "/api/watchdogs?host=" + encodeURIComponent(host),
      controller ? { signal: controller.signal } : undefined
    )
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (state.generation !== generation) return; // collapsed/reopened since -- drop stale response
        detailEl.innerHTML = "";
        (data.rows || []).forEach(function (row) {
          detailEl.appendChild(buildRow(row));
        });
        detailEl.hidden = false;
      })
      .catch(function () {
        if (state.generation !== generation) return; // collapse-triggered abort -- no error shown
        detailEl.innerHTML = "";
        detailEl.appendChild(el("p", "wd-error", "Could not load watchdog data."));
        detailEl.hidden = false;
      })
      .then(function () {
        if (state.generation !== generation) return; // superseded already cleaned up by unmountHost
        state.busy = false;
        if (state.timer) { clearTimeout(state.timer); state.timer = null; }
        state.controller = null;
      });
  }

  function unmountHost(hostSection) {
    var toggle = hostSection.querySelector(".wd-host-toggle");
    var detailEl = hostSection.querySelector(".wd-detail");
    var state = stateFor(hostSection);

    state.generation += 1; // invalidate any in-flight fetch/timeout for this host
    state.busy = false;
    if (state.controller) { try { state.controller.abort(); } catch (_) {} }
    if (state.timer) { clearTimeout(state.timer); state.timer = null; }
    state.controller = null;

    detailEl.hidden = true;
    detailEl.innerHTML = ""; // unmount, not just hide
    toggle.setAttribute("aria-expanded", "false");
  }

  document.querySelectorAll(".wd-host-toggle").forEach(function (toggle) {
    toggle.addEventListener("click", function () {
      var hostSection = toggle.closest(".wd-host");
      var expanded = toggle.getAttribute("aria-expanded") === "true";
      if (expanded) {
        unmountHost(hostSection);
      } else {
        mountHost(hostSection);
      }
    });
  });
})();
