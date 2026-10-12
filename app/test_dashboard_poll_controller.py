"""
Tests for the T5 dashboard browser-poll consolidation
(FLEET-WORKER2-BUILD-20260721-panel-browser-poll-consolidation).

Before this pass, static/dashboard.js ran four independent browser timers:
refresh() every 15s (/api/status), SeatBoard's own render()-tick every 15s
(pure duplicate DOM churn on the same cadence), pollUnread() every 15s, and
the timeline's load() every 60s — none paused while the tab/PWA was hidden.
This file has two layers of proof:

1. Static assertions on the shipped source, confirming the old independent
   timers are actually gone and the new consolidated ones exist (a dynamic
   test alone can't prove an OLD bug was removed, only that a NEW behavior
   exists).
2. A dynamic Node harness that extracts the real "central dashboard polling
   controller" IIFE verbatim (regex-bounded by its own start/end marker
   comments) and runs it under a hand-rolled fake document/window/timer/
   AbortController environment — exercising the actual shipped scheduling
   logic (in-flight guard, 60s subcadence, visibility start/stop, no timer
   multiplication across hide/show cycles), not a reimplementation of it.
   No new dependency — node ships in tools/_runtime/node per this repo's own
   bundled runtime (same approach as test_routes_jobs_sort.py).
"""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
NODE_BIN = REPO_ROOT / "tools" / "_runtime" / "node" / "bin" / "node"
DASHBOARD_JS = REPO_ROOT / "static" / "dashboard.js"

BEGIN_MARKER = "// === BEGIN central dashboard polling controller ==="
END_MARKER = "// === END central dashboard polling controller ==="


def _extract_controller_src(js_src: str) -> str:
    start = js_src.index(BEGIN_MARKER)
    end = js_src.index(END_MARKER, start) + len(END_MARKER)
    return js_src[start:end]


class StaticConsolidationTests(unittest.TestCase):
    """Confirms the old independent timers are gone, not just that new ones exist."""

    def setUp(self):
        self.src = DASHBOARD_JS.read_text(encoding="utf-8")

    def test_no_independent_status_refresh_interval(self):
        self.assertNotIn("setInterval(refresh, 15000)", self.src)

    def test_no_independent_seatboard_render_interval(self):
        self.assertNotIn("setInterval(render, 15000)", self.src)

    def test_no_independent_unread_poll_interval(self):
        self.assertNotIn("setInterval(pollUnread, 15000)", self.src)

    def test_no_independent_timeline_load_interval(self):
        self.assertNotIn("setInterval(load, 60000)", self.src)

    def test_pollunread_no_longer_has_its_own_visibilitychange_listener(self):
        # It used to re-poll unread-count on its own visibilitychange hook;
        # that's now the central controller's job.
        unread_block = self.src[self.src.index("function pollUnread") : self.src.index("window.__NEXUS_UNREAD__") + 40]
        self.assertNotIn('addEventListener("visibilitychange"', unread_block)

    def test_exactly_three_setinterval_call_sites_remain(self):
        # The bottom-sheet's own open-route poll (untouched, lifecycle-bound
        # to the sheet being open) + the controller's status-poll + clock.
        self.assertEqual(self.src.count("setInterval("), 3)

    def test_controller_markers_present_exactly_once(self):
        self.assertEqual(self.src.count(BEGIN_MARKER), 1)
        self.assertEqual(self.src.count(END_MARKER), 1)

    def test_shared_endpoints_exposed_for_the_controller(self):
        self.assertIn("window.__NEXUS_UNREAD__ = {poll: pollUnread};", self.src)
        self.assertIn("window.__NEXUS_TIMELINE__ = {load: load};", self.src)

    def test_refresh_and_subendpoints_accept_an_abort_signal(self):
        self.assertIn("async function refresh(signal){", self.src)
        self.assertIn('fetch("/api/status", {cache:"no-store", signal});', self.src)
        self.assertIn("function pollUnread(signal){", self.src)
        self.assertIn("function load(signal){", self.src)

    def test_page_hidden_class_toggled_by_controller(self):
        self.assertIn('documentElement.classList.add("page-hidden")', self.src)
        self.assertIn('documentElement.classList.remove("page-hidden")', self.src)


NODE_HARNESS_TEMPLATE = r"""
"use strict";

// ---- fake timer registry: setInterval/clearInterval captured, fired manually ----
let intervals = [];
let nextId = 1;
function fakeSetInterval(fn, ms){ const id = nextId++; intervals.push({id, fn, ms}); return id; }
function fakeClearInterval(id){ intervals = intervals.filter(t => t.id !== id); }
function intervalsWithMs(ms){ return intervals.filter(t => t.ms === ms); }
function fire(ms){ intervalsWithMs(ms).forEach(t => t.fn()); }
function flush(){ return new Promise(function(resolve){ setImmediate(resolve); }); }

// ---- fake document/window ----
let hiddenState = false;
let visListeners = [];
const htmlClasses = new Set();
const documentElement = {
  classList: {
    add(c){ htmlClasses.add(c); },
    remove(c){ htmlClasses.delete(c); },
    contains(c){ return htmlClasses.has(c); },
  },
};
const document = {
  get hidden(){ return hiddenState; },
  documentElement,
  addEventListener(type, fn){ if(type === "visibilitychange") visListeners.push(fn); },
};
function fireVisibilityChange(){ visListeners.slice().forEach(fn => fn()); }

const createdAbortControllers = [];
class FakeAbortController {
  constructor(){ this.signal = {aborted:false}; this.aborted = false; createdAbortControllers.push(this); }
  abort(){ this.aborted = true; this.signal.aborted = true; }
}

// ---- fake collaborators (refresh/tickClock are bare identifiers the
// extracted controller closes over, same as they are in the real file;
// window.__NEXUS_UNREAD__/__NEXUS_TIMELINE__ mirror the real exposure) ----
let refreshCalls = 0, tickClockCalls = 0, unreadCalls = 0, timelineCalls = 0;
let refreshBehavior = "resolve";      // "resolve" | "reject" | "pending"
let unreadBehavior = "resolve";       // "resolve" | "reject"
let pendingRefreshResolvers = [];

async function refresh(signal){
  refreshCalls++;
  if(refreshBehavior === "reject") throw new Error("status endpoint failed");
  if(refreshBehavior === "pending"){
    await new Promise(function(resolve){ pendingRefreshResolvers.push(resolve); });
  }
}
function tickClock(){ tickClockCalls++; }

const window = {
  __NEXUS_UNREAD__: { poll(signal){ unreadCalls++;
    if(unreadBehavior === "reject") return Promise.reject(new Error("unread endpoint failed"));
    return Promise.resolve(); } },
  __NEXUS_TIMELINE__: { load(signal){ timelineCalls++; return Promise.resolve(); } },
};

const setInterval = fakeSetInterval;
const clearInterval = fakeClearInterval;
const AbortController = FakeAbortController;

const unhandled = [];
process.on("unhandledRejection", function(err){ unhandled.push(String(err && err.stack || err)); });

const results = {};
function check(name, cond){ results[name] = !!cond; }

async function main(){
  // Running the extracted controller IIFE now — it reads document/window/
  // refresh/tickClock/setInterval/clearInterval/AbortController from this
  // enclosing scope via a direct eval, exactly as it would read real globals
  // in a browser.
  eval(%(controller_src)s);

  // ---- 1. initial load: one immediate consolidated tick + both timers armed ----
  await flush(); await flush();
  check("initial_status_fetched_once", refreshCalls === 1);
  check("initial_unread_fetched_once", unreadCalls === 1);
  check("initial_history_fetched_once", timelineCalls === 1);
  check("initial_clock_ticked_once", tickClockCalls === 1);
  check("one_status_interval_armed", intervalsWithMs(15000).length === 1);
  check("one_clock_interval_armed", intervalsWithMs(1000).length === 1);
  check("one_visibilitychange_listener", visListeners.length === 1);

  // ---- 2. steady-state 60s cadence: 4 scheduled ticks -> 4 status, 1 unread, 1 history ----
  const r0 = refreshCalls, u0 = unreadCalls, t0 = timelineCalls;
  for(let i=0;i<4;i++){ fire(15000); await flush(); await flush(); }
  check("steady_60s_status_count_is_4", (refreshCalls - r0) === 4);
  check("steady_60s_unread_count_is_1", (unreadCalls - u0) === 1);
  check("steady_60s_history_count_is_1", (timelineCalls - t0) === 1);

  // ---- 3. in-flight guard: a slow tick can't overlap the next scheduled one ----
  refreshBehavior = "pending";
  const rBefore = refreshCalls;
  fire(15000);                 // tick starts, refresh() hangs awaiting pendingRefreshResolvers
  await flush();
  fire(15000);                 // a second scheduled tick arrives mid-flight
  await flush();
  check("overlapping_tick_did_not_double_fetch", refreshCalls === rBefore + 1);
  // resolve the hung fetch, then confirm the guard cleared (a further tick fetches again)
  refreshBehavior = "resolve";
  pendingRefreshResolvers.forEach(function(r){ r(); });
  pendingRefreshResolvers = [];
  await flush(); await flush();
  const rAfterSettle = refreshCalls;
  fire(15000);
  await flush(); await flush();
  check("guard_cleared_after_settle_next_tick_fetches", refreshCalls === rAfterSettle + 1);

  // ---- 4. failure recovery: one endpoint failing doesn't wedge future ticks ----
  refreshBehavior = "reject";
  const rBeforeFail = refreshCalls;
  fire(15000);
  await flush(); await flush();
  refreshBehavior = "resolve";
  fire(15000);
  await flush(); await flush();
  check("failed_tick_recovers_next_tick", refreshCalls === rBeforeFail + 2);

  unreadBehavior = "reject";
  // force this to be the "due" tick by draining to the next multiple of 4
  for(let i=0;i<4;i++){ fire(15000); await flush(); await flush(); }
  unreadBehavior = "resolve";
  const rBeforeUnreadFail = refreshCalls;
  fire(15000);
  await flush(); await flush();
  check("unread_rejection_does_not_stop_status_fetch", refreshCalls === rBeforeUnreadFail + 1);

  // ---- 5. hidden: all polling + clock stop, animation-pause class set, in-flight aborted ----
  refreshBehavior = "pending";
  fire(15000);                 // start an in-flight status fetch
  await flush();
  hiddenState = true;
  fireVisibilityChange();
  check("hidden_clears_status_interval", intervalsWithMs(15000).length === 0);
  check("hidden_clears_clock_interval", intervalsWithMs(1000).length === 0);
  check("hidden_sets_page_hidden_class", htmlClasses.has("page-hidden"));
  const lastAbort = createdAbortControllers[createdAbortControllers.length - 1];
  check("hidden_aborts_inflight_request", !!lastAbort && lastAbort.aborted === true);
  refreshBehavior = "resolve";
  pendingRefreshResolvers.forEach(function(r){ r(); });
  pendingRefreshResolvers = [];
  await flush(); await flush();
  const fetchesWhileHidden = refreshCalls;
  // nothing registered while hidden -> firing has no effect regardless
  fire(15000); fire(1000);
  await flush();
  check("zero_fetches_while_hidden", refreshCalls === fetchesWhileHidden);
  check("zero_clock_ticks_while_hidden_tick_count_unchanged", tickClockCalls === tickClockCalls);

  // ---- 6. resume: exactly one immediate consolidated refresh, cadence restored ----
  const rBeforeResume = refreshCalls, uBeforeResume = unreadCalls, hBeforeResume = timelineCalls;
  hiddenState = false;
  fireVisibilityChange();
  await flush(); await flush();
  check("resume_page_hidden_class_removed", !htmlClasses.has("page-hidden"));
  check("resume_immediate_status_fetch", refreshCalls === rBeforeResume + 1);
  check("resume_immediate_unread_fetch", unreadCalls === uBeforeResume + 1);
  check("resume_immediate_history_fetch", timelineCalls === hBeforeResume + 1);
  check("resume_restarts_status_interval_once", intervalsWithMs(15000).length === 1);
  check("resume_restarts_clock_interval_once", intervalsWithMs(1000).length === 1);

  // ---- 7. repeated hide/show cycles never multiply timers or listeners ----
  for(let i=0;i<5;i++){
    hiddenState = true; fireVisibilityChange();
    hiddenState = false; fireVisibilityChange();
    await flush(); await flush();
  }
  check("no_timer_multiplication_after_repeated_toggles_status", intervalsWithMs(15000).length === 1);
  check("no_timer_multiplication_after_repeated_toggles_clock", intervalsWithMs(1000).length === 1);
  check("no_listener_multiplication_after_repeated_toggles", visListeners.length === 1);

  // ---- 8. no unhandled promise rejections across the whole sequence ----
  check("no_unhandled_rejections", unhandled.length === 0);
  if(unhandled.length) results["_unhandled_detail"] = unhandled;

  console.log(JSON.stringify(results));
}

main().catch(function(e){ console.error("HARNESS CRASHED:", e && e.stack || e); process.exit(1); });
"""


class DynamicPollControllerTests(unittest.TestCase):
    """Runs the actual extracted controller IIFE under a fake browser environment."""

    @classmethod
    def setUpClass(cls):
        if not NODE_BIN.exists():
            raise unittest.SkipTest("bundled node runtime not present at tools/_runtime/node")
        js_src = DASHBOARD_JS.read_text(encoding="utf-8")
        controller_src = _extract_controller_src(js_src)
        script = NODE_HARNESS_TEMPLATE % {"controller_src": json.dumps(controller_src)}
        proc = subprocess.run(
            [str(NODE_BIN), "-e", script], capture_output=True, text=True, timeout=20
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"node harness failed (exit {proc.returncode}):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
            )
        # The harness prints exactly one JSON line of results.
        cls.results = json.loads(proc.stdout.strip().splitlines()[-1])

    def _assert_true(self, key):
        self.assertIn(key, self.results, f"harness never recorded check {key!r}: {self.results}")
        self.assertTrue(self.results[key], f"{key} failed: {self.results}")

    def test_initial_status_fetched_once(self):
        self._assert_true("initial_status_fetched_once")

    def test_initial_unread_fetched_once(self):
        self._assert_true("initial_unread_fetched_once")

    def test_initial_history_fetched_once(self):
        self._assert_true("initial_history_fetched_once")

    def test_initial_clock_ticked_once(self):
        self._assert_true("initial_clock_ticked_once")

    def test_one_status_interval_armed(self):
        self._assert_true("one_status_interval_armed")

    def test_one_clock_interval_armed(self):
        self._assert_true("one_clock_interval_armed")

    def test_one_visibilitychange_listener(self):
        self._assert_true("one_visibilitychange_listener")

    def test_steady_60s_status_count_is_4(self):
        self._assert_true("steady_60s_status_count_is_4")

    def test_steady_60s_unread_count_is_1(self):
        self._assert_true("steady_60s_unread_count_is_1")

    def test_steady_60s_history_count_is_1(self):
        self._assert_true("steady_60s_history_count_is_1")

    def test_overlapping_tick_did_not_double_fetch(self):
        self._assert_true("overlapping_tick_did_not_double_fetch")

    def test_guard_cleared_after_settle_next_tick_fetches(self):
        self._assert_true("guard_cleared_after_settle_next_tick_fetches")

    def test_failed_tick_recovers_next_tick(self):
        self._assert_true("failed_tick_recovers_next_tick")

    def test_unread_rejection_does_not_stop_status_fetch(self):
        self._assert_true("unread_rejection_does_not_stop_status_fetch")

    def test_hidden_clears_status_interval(self):
        self._assert_true("hidden_clears_status_interval")

    def test_hidden_clears_clock_interval(self):
        self._assert_true("hidden_clears_clock_interval")

    def test_hidden_sets_page_hidden_class(self):
        self._assert_true("hidden_sets_page_hidden_class")

    def test_hidden_aborts_inflight_request(self):
        self._assert_true("hidden_aborts_inflight_request")

    def test_zero_fetches_while_hidden(self):
        self._assert_true("zero_fetches_while_hidden")

    def test_resume_page_hidden_class_removed(self):
        self._assert_true("resume_page_hidden_class_removed")

    def test_resume_immediate_status_fetch(self):
        self._assert_true("resume_immediate_status_fetch")

    def test_resume_immediate_unread_fetch(self):
        self._assert_true("resume_immediate_unread_fetch")

    def test_resume_immediate_history_fetch(self):
        self._assert_true("resume_immediate_history_fetch")

    def test_resume_restarts_status_interval_once(self):
        self._assert_true("resume_restarts_status_interval_once")

    def test_resume_restarts_clock_interval_once(self):
        self._assert_true("resume_restarts_clock_interval_once")

    def test_no_timer_multiplication_after_repeated_toggles_status(self):
        self._assert_true("no_timer_multiplication_after_repeated_toggles_status")

    def test_no_timer_multiplication_after_repeated_toggles_clock(self):
        self._assert_true("no_timer_multiplication_after_repeated_toggles_clock")

    def test_no_listener_multiplication_after_repeated_toggles(self):
        self._assert_true("no_listener_multiplication_after_repeated_toggles")

    def test_no_unhandled_rejections(self):
        self._assert_true("no_unhandled_rejections")


if __name__ == "__main__":
    unittest.main()
