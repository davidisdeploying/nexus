"""
Tests for the SeatBoard /ws reconnect-backoff pass
(FLEET-WORKER2-BUILD-20260721-panel-ws-reconnect-backoff).

Before this pass, the dashboard's live-seat WebSocket transport in
static/dashboard.js reconnected on a flat 2.5s loop from onclose, with no cap,
no reset, and no protection against duplicate timers/sockets across
onerror/onclose/visibilitychange transitions. This file has two layers of
proof, mirroring test_dashboard_poll_controller.py:

1. Static assertions on the shipped source, confirming the flat-delay
   `setTimeout(connectWS, 2500)` calls are gone and the bounded-backoff
   constants/markers exist.
2. A dynamic Node harness that extracts the real "live seat WebSocket
   transport" IIFE verbatim (regex-bounded by its own start/end marker
   comments) and runs it under a hand-rolled fake WebSocket/document/window/
   timer/fetch environment — exercising the actual shipped reconnect logic
   (delay sequence + cap, reset-on-open, single-flight timer, hidden
   cancellation, visible immediate reconnect, stale-socket-callback
   ignoring), not a reimplementation of it. No new dependency — node ships
   in tools/_runtime/node per this repo's own bundled runtime.
"""
from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
NODE_BIN = REPO_ROOT / "tools" / "_runtime" / "node" / "bin" / "node"
DASHBOARD_JS = REPO_ROOT / "static" / "dashboard.js"

BEGIN_MARKER = "// === BEGIN live seat WebSocket transport (reconnect backoff) ==="
END_MARKER = "// === END live seat WebSocket transport (reconnect backoff) ==="


def _extract_transport_src(js_src: str) -> str:
    start = js_src.index(BEGIN_MARKER)
    end = js_src.index(END_MARKER, start) + len(END_MARKER)
    return js_src[start:end]


class StaticBackoffTests(unittest.TestCase):
    """Confirms the old flat-delay reconnect is actually gone, not just that new code exists."""

    def setUp(self):
        self.src = DASHBOARD_JS.read_text(encoding="utf-8")

    def test_no_flat_reconnect_delay_calls(self):
        self.assertNotIn("setTimeout(connectWS, 2500)", self.src)

    def test_transport_markers_present_exactly_once(self):
        self.assertEqual(self.src.count(BEGIN_MARKER), 1)
        self.assertEqual(self.src.count(END_MARKER), 1)

    def test_backoff_constants_present(self):
        transport = _extract_transport_src(self.src)
        self.assertIn("INITIAL_DELAY_MS = 2500", transport)
        self.assertIn("MAX_DELAY_MS = 60000", transport)
        self.assertIn("BACKOFF_FACTOR = 2", transport)

    def test_hero_path_ws_untouched(self):
        # This pass must not touch Worker Activity's own WebSocket transport.
        hero_src = (REPO_ROOT / "templates" / "hero_path_session.html").read_text(encoding="utf-8")
        self.assertIn("try { ws = new WebSocket(url); } catch(e){ return; }", hero_src)


NODE_HARNESS_TEMPLATE = r"""
"use strict";

// ---- fake WebSocket ----
const createdSockets = [];
class FakeWebSocket {
  constructor(url){
    this.url = url;
    this.readyState = FakeWebSocket.CONNECTING;
    this.onopen = null; this.onmessage = null; this.onclose = null; this.onerror = null;
    createdSockets.push(this);
  }
  close(){
    // Real browsers close asynchronously and may never fire another event
    // for an already-closed socket; this fake mirrors that by NOT
    // synchronously invoking onclose here. Tests fire onclose explicitly to
    // simulate the server-driven close event.
    this.readyState = FakeWebSocket.CLOSED;
  }
}
FakeWebSocket.CONNECTING = 0;
FakeWebSocket.OPEN = 1;
FakeWebSocket.CLOSING = 2;
FakeWebSocket.CLOSED = 3;
const WebSocket = FakeWebSocket;

// ---- fake setTimeout/clearTimeout: single pending-timer registry ----
let timers = [];
let nextTimerId = 1;
function fakeSetTimeout(fn, delay){ const id = nextTimerId++; timers.push({id, fn, delay}); return id; }
function fakeClearTimeout(id){ timers = timers.filter(t => t.id !== id); }
const setTimeout = fakeSetTimeout;
const clearTimeout = fakeClearTimeout;

function firePendingTimer(){
  if(timers.length !== 1) throw new Error("expected exactly one pending timer, got " + timers.length);
  const t = timers[0];
  timers = [];
  const delay = t.delay;
  t.fn();
  return delay;
}

// ---- fake location ----
const location = { protocol: "https:", host: "example.test" };

// ---- fake fetch (backfill) ----
let fetchCalls = 0;
function fetch(url, opts){
  fetchCalls++;
  return Promise.resolve({ ok: true, json: function(){ return Promise.resolve([]); } });
}

// ---- fake document/window ----
let hiddenState = false;
let visListeners = [];
const document = {
  get visibilityState(){ return hiddenState ? "hidden" : "visible"; },
  addEventListener(type, fn){ if(type === "visibilitychange") visListeners.push(fn); },
};
function fireVisibilityChange(){ visListeners.slice().forEach(fn => fn()); }

let flipCalls = 0;
let lastFlipPayload = null;
const window = {
  SeatBoard: {
    flip(ev){ flipCalls++; lastFlipPayload = ev; },
  },
};

const results = {};
function check(name, cond){ results[name] = !!cond; }

function lastSocket(){ return createdSockets[createdSockets.length - 1]; }

async function main(){
  // Running the extracted transport IIFE now — it reads location/window/
  // document/WebSocket/fetch/setTimeout/clearTimeout from this enclosing
  // scope via a direct eval, exactly as it would read real globals in a
  // browser.
  eval(%(transport_src)s);

  // ---- 1. initial load: one backfill fetch, one socket created ----
  check("initial_backfill_fetched_once", fetchCalls === 1);
  check("initial_one_socket_created", createdSockets.length === 1);
  check("initial_one_visibilitychange_listener", visListeners.length === 1);

  // ---- 2. delay sequence + cap: repeated close-without-open ----
  const delays = [];
  for(let i=0;i<7;i++){
    lastSocket().onclose();
    check("exactly_one_pending_timer_after_close_" + i, timers.length === 1);
    delays.push(firePendingTimer());
  }
  check("delay_sequence_correct", JSON.stringify(delays) ===
    JSON.stringify([2500, 5000, 10000, 20000, 40000, 60000, 60000]));

  // ---- 3. reset after a successful open ----
  lastSocket().onopen();
  lastSocket().onclose();
  const delayAfterOpen = firePendingTimer();
  check("delay_resets_to_initial_after_open", delayAfterOpen === 2500);

  // ---- 4. single-flight: onerror + onclose must not double-schedule ----
  const socketCountBefore4 = createdSockets.length;
  lastSocket().onerror();   // just closes the socket in our impl; no event synthesized
  check("onerror_alone_does_not_schedule_a_timer", timers.length === 0);
  lastSocket().onclose();   // the real close event arrives once
  check("exactly_one_timer_after_error_then_close", timers.length === 1);
  firePendingTimer();
  check("error_then_close_created_exactly_one_new_socket",
    createdSockets.length === socketCountBefore4 + 1);

  // ---- 5. stale socket callback ignored ----
  const staleSocket = lastSocket();
  const staleOnMessage = staleSocket.onmessage;
  // supersede it with a fresh connect before the stale message "arrives"
  fireVisibilityChange(); // not yet toggled hidden; visible->visible is a no-op below in real browsers,
                          // but our listener only special-cases hidden, so call hidden then visible to
                          // force a fresh connect deterministically:
  hiddenState = true; fireVisibilityChange();
  hiddenState = false; fireVisibilityChange();
  const flipCallsBeforeStale = flipCalls;
  staleOnMessage({ data: JSON.stringify({seat: "stale"}) });
  check("stale_socket_callback_ignored", flipCalls === flipCallsBeforeStale);

  // ---- 6. hidden cancels a pending reconnect timer (socket already closed itself) ----
  lastSocket().onclose(); // schedule a pending reconnect; this also nulls out `ws` internally
  check("timer_pending_before_hide", timers.length === 1);
  hiddenState = true;
  fireVisibilityChange();
  check("hidden_cancels_pending_timer", timers.length === 0);
  hiddenState = false;
  fireVisibilityChange(); // resume: connect immediately once, back to a clean live socket

  // ---- 7. hidden closes an actually-live (open, no pending timer) socket safely ----
  const liveSocketBeforeHide = lastSocket();
  check("live_socket_open_before_hide", liveSocketBeforeHide.readyState !== FakeWebSocket.CLOSED);
  check("no_timer_pending_before_hide_of_live_socket", timers.length === 0);
  hiddenState = true;
  fireVisibilityChange();
  check("hidden_closes_live_socket", liveSocketBeforeHide.readyState === FakeWebSocket.CLOSED);
  check("hidden_nulls_socket_handlers", liveSocketBeforeHide.onclose === null && liveSocketBeforeHide.onmessage === null);
  check("hidden_does_not_schedule_a_timer_for_a_deliberate_close", timers.length === 0);

  // ---- 8. visible resume: immediate single connect + backfill, no reconnect storm while hidden ----
  const socketCountBeforeVisible = createdSockets.length;
  const fetchCallsBeforeVisible = fetchCalls;
  hiddenState = false;
  fireVisibilityChange();
  check("visible_creates_exactly_one_new_socket", createdSockets.length === socketCountBeforeVisible + 1);
  check("visible_forces_one_backfill", fetchCalls === fetchCallsBeforeVisible + 1);
  check("no_reconnect_timer_pending_immediately_after_visible_connect", timers.length === 0);

  // backoff after resume starts at the floor again (not the pre-hide accumulated cap)
  lastSocket().onclose();
  check("post_resume_backoff_starts_at_initial_delay", firePendingTimer() === 2500);

  // ---- 9. repeated hide/show cycles: no socket/timer/listener multiplication ----
  const socketCountBeforeToggles = createdSockets.length;
  for(let i=0;i<5;i++){
    hiddenState = true; fireVisibilityChange();
    hiddenState = false; fireVisibilityChange();
  }
  check("repeated_toggles_one_socket_per_visible", createdSockets.length === socketCountBeforeToggles + 5);
  check("repeated_toggles_no_timer_left_pending", timers.length === 0);
  check("repeated_toggles_no_listener_multiplication", visListeners.length === 1);

  // ---- 10. payload still calls SeatBoard.flip exactly once ----
  const flipCallsBeforePayload = flipCalls;
  const payload = {seat: "alice", state: "BUSY"};
  lastSocket().onmessage({ data: JSON.stringify(payload) });
  check("payload_calls_flip_exactly_once", flipCalls === flipCallsBeforePayload + 1);
  check("payload_forwarded_correctly", JSON.stringify(lastFlipPayload) === JSON.stringify(payload));

  console.log(JSON.stringify(results));
}

main().catch(function(e){ console.error("HARNESS CRASHED:", e && e.stack || e); process.exit(1); });
"""


class DynamicBackoffTests(unittest.TestCase):
    """Runs the actual extracted transport IIFE under a fake browser environment."""

    @classmethod
    def setUpClass(cls):
        if not NODE_BIN.exists():
            raise unittest.SkipTest("bundled node runtime not present at tools/_runtime/node")
        js_src = DASHBOARD_JS.read_text(encoding="utf-8")
        transport_src = _extract_transport_src(js_src)
        script = NODE_HARNESS_TEMPLATE % {"transport_src": json.dumps(transport_src)}
        proc = subprocess.run(
            [str(NODE_BIN), "-e", script], capture_output=True, text=True, timeout=20
        )
        if proc.returncode != 0:
            raise AssertionError(
                f"node harness failed (exit {proc.returncode}):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
            )
        cls.results = json.loads(proc.stdout.strip().splitlines()[-1])

    def _assert_true(self, key):
        self.assertIn(key, self.results, f"harness never recorded check {key!r}: {self.results}")
        self.assertTrue(self.results[key], f"{key} failed: {self.results}")

    def test_initial_backfill_fetched_once(self):
        self._assert_true("initial_backfill_fetched_once")

    def test_initial_one_socket_created(self):
        self._assert_true("initial_one_socket_created")

    def test_initial_one_visibilitychange_listener(self):
        self._assert_true("initial_one_visibilitychange_listener")

    def test_delay_sequence_correct(self):
        self._assert_true("delay_sequence_correct")

    def test_delay_resets_to_initial_after_open(self):
        self._assert_true("delay_resets_to_initial_after_open")

    def test_onerror_alone_does_not_schedule_a_timer(self):
        self._assert_true("onerror_alone_does_not_schedule_a_timer")

    def test_exactly_one_timer_after_error_then_close(self):
        self._assert_true("exactly_one_timer_after_error_then_close")

    def test_error_then_close_created_exactly_one_new_socket(self):
        self._assert_true("error_then_close_created_exactly_one_new_socket")

    def test_stale_socket_callback_ignored(self):
        self._assert_true("stale_socket_callback_ignored")

    def test_timer_pending_before_hide(self):
        self._assert_true("timer_pending_before_hide")

    def test_hidden_cancels_pending_timer(self):
        self._assert_true("hidden_cancels_pending_timer")

    def test_live_socket_open_before_hide(self):
        self._assert_true("live_socket_open_before_hide")

    def test_no_timer_pending_before_hide_of_live_socket(self):
        self._assert_true("no_timer_pending_before_hide_of_live_socket")

    def test_hidden_closes_live_socket(self):
        self._assert_true("hidden_closes_live_socket")

    def test_hidden_nulls_socket_handlers(self):
        self._assert_true("hidden_nulls_socket_handlers")

    def test_hidden_does_not_schedule_a_timer_for_a_deliberate_close(self):
        self._assert_true("hidden_does_not_schedule_a_timer_for_a_deliberate_close")

    def test_visible_creates_exactly_one_new_socket(self):
        self._assert_true("visible_creates_exactly_one_new_socket")

    def test_visible_forces_one_backfill(self):
        self._assert_true("visible_forces_one_backfill")

    def test_no_reconnect_timer_pending_immediately_after_visible_connect(self):
        self._assert_true("no_reconnect_timer_pending_immediately_after_visible_connect")

    def test_post_resume_backoff_starts_at_initial_delay(self):
        self._assert_true("post_resume_backoff_starts_at_initial_delay")

    def test_repeated_toggles_one_socket_per_visible(self):
        self._assert_true("repeated_toggles_one_socket_per_visible")

    def test_repeated_toggles_no_timer_left_pending(self):
        self._assert_true("repeated_toggles_no_timer_left_pending")

    def test_repeated_toggles_no_listener_multiplication(self):
        self._assert_true("repeated_toggles_no_listener_multiplication")

    def test_payload_calls_flip_exactly_once(self):
        self._assert_true("payload_calls_flip_exactly_once")

    def test_payload_forwarded_correctly(self):
        self._assert_true("payload_forwarded_correctly")

    def test_no_exactly_one_pending_timer_checks_failed(self):
        for key in self.results:
            if key.startswith("exactly_one_pending_timer_after_close_"):
                self.assertTrue(self.results[key], f"{key} failed: {self.results}")


if __name__ == "__main__":
    unittest.main()
