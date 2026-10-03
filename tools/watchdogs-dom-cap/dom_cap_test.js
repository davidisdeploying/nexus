#!/usr/bin/env node
/**
 * DOM mount/unmount cap harness for static/watchdogs.js (PANEL-4, PANEL-8).
 *
 * Loads the shipped script VERBATIM against a small hand-rolled fake DOM
 * (just enough surface for this script: createElement/appendChild/
 * classList/attrs/closest/querySelectorAll/hidden) plus a stubbed fetch(),
 * and asserts the bounded-DOM contract: a host's detail container starts
 * empty, expanding it mounts exactly the fetched row count, collapsing it
 * unmounts back to zero children (not just hidden) -- matching PANEL-4's
 * DoD ("initial DOM contains only 3 host summary rows; on expand, mount
 * only that host's rows; on collapse, unmount").
 *
 * PANEL-8 adds: the host-detail fetch carries an AbortController signal,
 * a bounded 8s timeout, single-in-flight-per-host, and stale/collapse-safe
 * response handling. Those scenarios use a *controllable* fake fetch (the
 * caller decides when/if it resolves) plus a real global AbortController
 * (Node's own -- not faked) and a manual fake-timer patch so an "8 second"
 * timeout can be asserted and fired without a real 8s wait.
 */
"use strict";

const fs = require("fs");
const path = require("path");

const SCRIPT_PATH = path.join(__dirname, "../../static/watchdogs.js");
const SRC = fs.readFileSync(SCRIPT_PATH, "utf8");

let failures = 0;
function check(label, cond, detail) {
  if (cond) {
    console.log(`  ok - ${label}`);
  } else {
    failures++;
    console.log(`  FAIL - ${label}${detail ? " :: " + detail : ""}`);
  }
}

class FakeElement {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.attrs = new Map();
    this.childNodes = [];
    this.parentNode = null;
    this._listeners = {};
    this._hidden = false;
  }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
  get className() { return this.attrs.get("class") || ""; }
  set className(v) { this.attrs.set("class", v); }
  get classList() {
    const self = this;
    return {
      contains(c) { return self.className.split(/\s+/).filter(Boolean).includes(c); },
    };
  }
  get hidden() { return this._hidden; }
  set hidden(v) { this._hidden = !!v; }
  get children() { return this.childNodes; }
  set textContent(v) { this._text = v; this.childNodes = []; }
  get textContent() { return this._text || ""; }
  set innerHTML(v) {
    if (v !== "") throw new Error("fake DOM only supports innerHTML = '' (clear)");
    this.childNodes.forEach((c) => (c.parentNode = null));
    this.childNodes = [];
  }
  appendChild(node) { node.parentNode = this; this.childNodes.push(node); return node; }
  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  dispatch(type) { (this._listeners[type] || []).forEach((fn) => fn.call(this)); }
  closest(sel) {
    const cls = sel.replace(/^\./, "");
    let n = this;
    while (n) {
      if (n.classList && n.classList.contains(cls)) return n;
      n = n.parentNode;
    }
    return null;
  }
  querySelector(sel) {
    const cls = sel.replace(/^\./, "");
    const stack = [...this.childNodes];
    while (stack.length) {
      const n = stack.shift();
      if (n.classList && n.classList.contains(cls)) return n;
      stack.push(...(n.childNodes || []));
    }
    return null;
  }
}

function buildFixtureDom(hosts) {
  const body = new FakeElement("body");
  const toggles = [];
  const details = [];
  for (const h of hosts) {
    const section = new FakeElement("section");
    section.className = "wd-host";
    section.setAttribute("data-host", h.host);
    body.appendChild(section);

    const toggle = new FakeElement("button");
    toggle.className = "wd-host-toggle";
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("data-host", h.host);
    section.appendChild(toggle);
    toggles.push(toggle);

    const detail = new FakeElement("div");
    detail.className = "wd-detail";
    detail.hidden = true;
    section.appendChild(detail);
    details.push(detail);
  }
  return { body, toggles, details };
}

function loadScript(fakeDocument, fetchImpl) {
  const sandboxGlobals = { document: fakeDocument, fetch: fetchImpl, console };
  const fn = new Function(...Object.keys(sandboxGlobals), SRC);
  fn(...Object.values(sandboxGlobals));
}

const realSetTimeout = setTimeout;
function nextTick() {
  return new Promise((r) => realSetTimeout(r, 0));
}
async function flush(times) {
  for (let i = 0; i < (times || 3); i++) await nextTick();
}

function makeAbortError() {
  const e = new Error("The operation was aborted.");
  e.name = "AbortError";
  return e;
}

// A fetch stub the test fully controls: each call is recorded (url + opts)
// and returns a promise the test resolves/rejects on demand. If opts.signal
// is present, an abort on that real AbortSignal rejects the promise with an
// AbortError, mirroring real fetch()'s documented abort contract.
function makeControllableFetch() {
  const calls = [];
  const fetchImpl = (url, opts) => {
    const call = { url, opts, settled: false };
    call.promise = new Promise((resolve, reject) => {
      call.resolve = (v) => { if (!call.settled) { call.settled = true; resolve(v); } };
      call.reject = (e) => { if (!call.settled) { call.settled = true; reject(e); } };
    });
    if (opts && opts.signal) {
      if (opts.signal.aborted) call.reject(makeAbortError());
      else opts.signal.addEventListener("abort", () => call.reject(makeAbortError()));
    }
    calls.push(call);
    return call.promise;
  };
  return { fetchImpl, calls };
}

function jsonResponse(rows) {
  return { json: () => Promise.resolve({ rows, summary: {} }) };
}

// ---- fake timer patch: lets the 8s timeout be asserted/fired without a
// real wait. Only setTimeout/clearTimeout are patched (globally, since the
// script's `new Function` body resolves free variables against the real
// global scope); this test's own async waits use the saved realSetTimeout
// so they are unaffected by the patch.
function installFakeTimers() {
  const realClearTimeout = clearTimeout;
  const timers = [];
  let nextId = 1;
  global.setTimeout = (cb, delay) => {
    const id = nextId++;
    timers.push({ id, delay, cb, cleared: false });
    return id;
  };
  global.clearTimeout = (id) => {
    const t = timers.find((t) => t.id === id);
    if (t) t.cleared = true;
  };
  return {
    timers,
    restore() {
      global.setTimeout = realSetTimeout;
      global.clearTimeout = realClearTimeout;
    },
  };
}

async function scenarioBoundedDomMountUnmount() {
  console.log("-- bounded DOM: mount on expand, unmount on collapse --");
  const hostsMeta = [
    { host: "alpha", count: 10 },
    { host: "charlie", count: 6 },
    { host: "delta", count: 9 },
  ];
  const { body, toggles, details } = buildFixtureDom(hostsMeta);
  const fakeDocument = {
    createElement(tag) { return new FakeElement(tag); },
    querySelectorAll(sel) {
      if (sel === ".wd-host-toggle") return toggles;
      throw new Error("unsupported top-level selector: " + sel);
    },
  };
  const fetchCalls = [];
  const fakeFetch = (url) => {
    fetchCalls.push(url);
    const m = /host=([a-z]+)/.exec(url);
    const host = m ? m[1] : null;
    const hostMeta = hostsMeta.find((h) => h.host === host);
    const rows = Array.from({ length: hostMeta.count }, (_, i) => ({
      id: `${host}-row-${i}`, kind: i % 2 === 0 ? "guard" : "watch",
      owner: "david", host, label: `mechanism ${i}`, source: "src",
      protected_target: "target", cadence_timeout: "60s",
      last_check_evidence: "evidence", last_action_evidence: "evidence",
      status: "active", status_detail: "detail", source_of_truth: "sot",
      evidence_as_of: "2026-07-23T00:00Z",
    }));
    return Promise.resolve(jsonResponse(rows));
  };
  loadScript(fakeDocument, fakeFetch);

  details.forEach((d, i) => {
    check(`host[${i}] starts with zero mounted rows`, d.childNodes.length === 0);
    check(`host[${i}] starts hidden`, d.hidden === true);
  });

  toggles[0].dispatch("click");
  await flush(2);
  check("fetch called with host=alpha", fetchCalls.some((u) => u.includes("host=alpha")));
  check("alpha mounts exactly 10 rows", details[0].childNodes.length === 10,
    `got ${details[0].childNodes.length}`);
  check("alpha detail no longer hidden", details[0].hidden === false);
  check("alpha toggle aria-expanded=true", toggles[0].getAttribute("aria-expanded") === "true");
  check("charlie/delta stay unmounted while only alpha expanded",
    details[1].childNodes.length === 0 && details[2].childNodes.length === 0);

  toggles[0].dispatch("click");
  check("alpha unmounts back to zero children", details[0].childNodes.length === 0);
  check("alpha detail hidden again", details[0].hidden === true);
  check("alpha toggle aria-expanded=false", toggles[0].getAttribute("aria-expanded") === "false");
}

async function scenarioSignalPassedAndTimeoutConstant() {
  console.log("-- fetch carries an AbortSignal, timeout scheduled at 8000ms --");
  const { toggles, details } = buildFixtureDom([{ host: "alpha" }]);
  const fakeDocument = {
    createElement(tag) { return new FakeElement(tag); },
    querySelectorAll: () => toggles,
  };
  const { fetchImpl, calls } = makeControllableFetch();
  const fakeTimers = installFakeTimers();
  try {
    loadScript(fakeDocument, fetchImpl);
    toggles[0].dispatch("click");
    await flush(1);

    check("fetch called exactly once", calls.length === 1, `got ${calls.length}`);
    const opts = calls[0].opts;
    check("fetch call passed a signal", !!(opts && opts.signal));
    check("signal is a real AbortSignal", opts && opts.signal instanceof AbortSignal);
    check("exactly one timer scheduled", fakeTimers.timers.length === 1,
      `got ${fakeTimers.timers.length}`);
    check("timer scheduled at 8000ms", fakeTimers.timers[0] && fakeTimers.timers[0].delay === 8000,
      `got ${fakeTimers.timers[0] && fakeTimers.timers[0].delay}`);

    // resolve so the pending promise doesn't linger across scenarios
    calls[0].resolve(jsonResponse([]));
    await flush(2);
  } finally {
    fakeTimers.restore();
  }
}

async function scenarioTimeoutAbortsAndShowsError() {
  console.log("-- unresolved fetch aborts at the 8s timeout, shows bounded error --");
  const { toggles, details } = buildFixtureDom([{ host: "alpha" }]);
  const fakeDocument = {
    createElement(tag) { return new FakeElement(tag); },
    querySelectorAll: () => toggles,
  };
  const { fetchImpl, calls } = makeControllableFetch();
  const fakeTimers = installFakeTimers();
  try {
    loadScript(fakeDocument, fetchImpl);
    toggles[0].dispatch("click");
    await flush(1);

    check("one timer scheduled before firing", fakeTimers.timers.length === 1);
    // fire the 8s timeout ourselves -- this is the abort() call
    fakeTimers.timers[0].cb();
    await flush(3);

    check("host stays expanded (no collapse happened)",
      toggles[0].getAttribute("aria-expanded") === "true");
    check("detail no longer hidden (bounded error shown)", details[0].hidden === false);
    check("exactly one error node mounted, no data rows", details[0].childNodes.length === 1 &&
      details[0].childNodes[0].classList.contains("wd-error"));

    // controller/timer must be cleared after the abort settles: a fresh
    // collapse+expand should issue a brand-new fetch, not be stuck "busy".
    toggles[0].dispatch("click"); // collapse the error state
    check("collapse clears the error row", details[0].childNodes.length === 0);
    toggles[0].dispatch("click"); // expand again
    await flush(1);
    check("re-expand issues a second, independent fetch", calls.length === 2, `got ${calls.length}`);
    calls[1].resolve(jsonResponse([]));
    await flush(2);
  } finally {
    fakeTimers.restore();
  }
}

async function scenarioCollapseAbortsAndDropsStaleResponse() {
  console.log("-- collapse aborts in-flight fetch immediately; late resolve never mounts rows --");
  const { toggles, details } = buildFixtureDom([{ host: "alpha" }]);
  const fakeDocument = {
    createElement(tag) { return new FakeElement(tag); },
    querySelectorAll: () => toggles,
  };
  const { fetchImpl, calls } = makeControllableFetch();

  loadScript(fakeDocument, fetchImpl);

  toggles[0].dispatch("click"); // expand -- fetch left unresolved
  await flush(1);
  check("host optimistically marked expanded while loading",
    toggles[0].getAttribute("aria-expanded") === "true");
  check("detail still hidden while loading (nothing mounted yet)", details[0].hidden === true);

  toggles[0].dispatch("click"); // collapse while still loading
  await flush(1);
  check("collapse-while-loading unmounts immediately", details[0].childNodes.length === 0);
  check("collapse-while-loading hides detail", details[0].hidden === true);
  check("collapse-while-loading sets aria-expanded=false",
    toggles[0].getAttribute("aria-expanded") === "false");
  check("collapse aborted the real signal", calls[0].opts.signal.aborted === true);

  // the original request now resolves late (stale) -- must never mount rows
  calls[0].resolve(jsonResponse([{
    id: "alpha-row-0", kind: "guard", owner: "david", host: "alpha",
    label: "late mechanism", source: "src", protected_target: "target",
    cadence_timeout: "60s", last_check_evidence: "e", last_action_evidence: "e",
    status: "active", status_detail: "d", source_of_truth: "sot",
    evidence_as_of: "2026-07-23T00:00Z",
  }]));
  await flush(3);
  check("stale resolve after collapse mounts nothing", details[0].childNodes.length === 0);
  check("stale resolve after collapse keeps detail hidden", details[0].hidden === true);
  check("stale resolve after collapse does not re-expand", toggles[0].getAttribute("aria-expanded") === "false");
}

async function scenarioNoRetryOrPoll() {
  console.log("-- no retry/poll: exactly one fetch per expand, nothing after settling --");
  const { toggles, details } = buildFixtureDom([{ host: "alpha" }]);
  const fakeDocument = {
    createElement(tag) { return new FakeElement(tag); },
    querySelectorAll: () => toggles,
  };
  const { fetchImpl, calls } = makeControllableFetch();
  loadScript(fakeDocument, fetchImpl);

  toggles[0].dispatch("click");
  await flush(1);
  calls[0].reject(new Error("network error"));
  await flush(3);

  check("error path mounts the bounded error, not a retry", details[0].childNodes.length === 1 &&
    details[0].childNodes[0].classList.contains("wd-error"));
  check("still exactly one fetch call after error settles (no retry fired)", calls.length === 1,
    `got ${calls.length}`);

  // wait a bit longer (several more ticks) to prove nothing schedules a
  // follow-up call on its own
  await flush(5);
  check("no follow-up fetch after additional idle ticks", calls.length === 1, `got ${calls.length}`);
}

async function main() {
  await scenarioBoundedDomMountUnmount();
  await scenarioSignalPassedAndTimeoutConstant();
  await scenarioTimeoutAbortsAndShowsError();
  await scenarioCollapseAbortsAndDropsStaleResponse();
  await scenarioNoRetryOrPoll();

  if (failures > 0) {
    console.log(`\n${failures} FAILURE(S)`);
    process.exit(1);
  }
  console.log("\nall checks passed");
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
