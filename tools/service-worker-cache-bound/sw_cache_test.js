#!/usr/bin/env node
/**
 * Service-worker cache-bound harness — FLEET-WORKER2-BUILD-20260721-panel-service-worker-cache-bound
 *
 * Loads the shipped static/sw.js VERBATIM (no rewrite) into a small hand-rolled
 * fake ServiceWorkerGlobalScope (self/caches/fetch/Response) and dispatches
 * real install/activate/fetch listener invocations against it, so this
 * exercises the exact shipped worker logic, not a reimplementation.
 *
 * Self-contained: no npm dependency, only Node's built-in `vm` module plus
 * the fakes below. Scope is exactly what static/sw.js touches: self.addEventListener,
 * caches.open/keys/delete/match, cache.put/match, fetch(request), `new Response(...)`,
 * `new URL(...)`. Push/notificationclick are registered but never dispatched here —
 * out of scope for the cache-bound behavior this harness proves.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SW_PATH = path.join(__dirname, '../../static/sw.js');
const ORIGIN = 'https://nexus.example';

let failures = 0;
function check(label, cond, detail) {
  if (cond) {
    console.log(`  ok - ${label}`);
  } else {
    failures++;
    console.log(`  FAIL - ${label}${detail ? ' :: ' + detail : ''}`);
  }
}

// ---------------------------------------------------------------------- //
// Fakes — just enough of Cache/CacheStorage/Response to exercise sw.js.
// ---------------------------------------------------------------------- //

class FakeResponse {
  constructor(body, init = {}) {
    this.body = body;
    this.status = init.status === undefined ? 200 : init.status;
    this.type = init.type || 'basic';
    this.redirected = !!init.redirected;
    this.headers = init.headers || {};
    this.ok = this.status >= 200 && this.status < 300;
  }
  clone() {
    return new FakeResponse(this.body, {
      status: this.status, type: this.type, redirected: this.redirected, headers: this.headers,
    });
  }
}

function keyOf(requestOrString) {
  return typeof requestOrString === 'string' ? requestOrString : requestOrString.url;
}

class FakeCache {
  constructor() { this.store = new Map(); }
  async put(request, response) { this.store.set(keyOf(request), response); }
  async match(request) { return this.store.get(keyOf(request)); }
  async keys() { return [...this.store.keys()].map((url) => ({ url })); }
}

class FakeCacheStorage {
  constructor() { this.caches = new Map(); }
  async open(name) {
    if (!this.caches.has(name)) this.caches.set(name, new FakeCache());
    return this.caches.get(name);
  }
  async keys() { return [...this.caches.keys()]; }
  async delete(name) { return this.caches.delete(name); }
  async match(request) {
    const key = keyOf(request);
    for (const cache of this.caches.values()) {
      if (cache.store.has(key)) return cache.store.get(key);
    }
    return undefined;
  }
}

class FakeExtendableEvent {
  waitUntil(promise) { this._promise = Promise.resolve(promise); }
}

class FakeFetchEvent {
  constructor(request) { this.request = request; this._responded = false; this._promise = null; }
  respondWith(promise) { this._responded = true; this._promise = Promise.resolve(promise); }
}

function fakeRequest(url, opts = {}) {
  return { url, method: opts.method || 'GET', mode: opts.mode || 'no-cors' };
}

// ---------------------------------------------------------------------- //
// Environment builder — one fresh isolated worker instance per test case.
// ---------------------------------------------------------------------- //

function buildEnv() {
  const listeners = {};
  const store = new FakeCacheStorage();
  let fetchImpl = async (request) => new FakeResponse(`body:${request.url}`, { status: 200, type: 'basic' });
  const fetchCalls = [];

  const fakeSelf = {
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    skipWaiting() { fakeSelf._skippedWaiting = true; },
    clients: { claim() { fakeSelf._claimed = true; } },
    location: { origin: ORIGIN },
    registration: { showNotification: async () => {} },
  };

  async function fetchFn(request) {
    fetchCalls.push(request.url);
    return fetchImpl(request);
  }

  const context = vm.createContext({
    self: fakeSelf,
    caches: store,
    fetch: fetchFn,
    Response: FakeResponse,
    URL,
    console,
    clients: { openWindow: async () => {} },
    navigator: {},
  });

  const src = fs.readFileSync(SW_PATH, 'utf8');
  vm.runInContext(src, context);

  async function dispatch(type, event) {
    for (const fn of listeners[type] || []) fn(event);
    if (event._promise) await event._promise;
    return event;
  }

  return {
    store, fakeSelf, fetchCalls,
    setFetch(fn) { fetchImpl = fn; },
    async install() { return dispatch('install', new FakeExtendableEvent()); },
    async activate() { return dispatch('activate', new FakeExtendableEvent()); },
    async fetchEvent(url, opts) {
      const ev = new FakeFetchEvent(fakeRequest(url, opts));
      await dispatch('fetch', ev);
      const response = ev._promise ? await ev._promise : null;
      return { event: ev, response };
    },
    staticCacheName() {
      // The shipped script exposes no export; the harness reads it back the
      // only way it can — by opening the cache the install handler just seeded.
      return store.keys();
    },
  };
}

async function run() {
  // ------------------------------------------------------------------ //
  // 1. Install/activate lifecycle.
  // ------------------------------------------------------------------ //
  console.log('case: install seeds the offline shell and skips waiting');
  let currentGeneration;
  {
    const env = buildEnv();
    await env.install();
    check('skipWaiting was called', env.fakeSelf._skippedWaiting === true);
    const names = await env.store.keys();
    check('exactly one cache created by install', names.length === 1, `got ${JSON.stringify(names)}`);
    currentGeneration = names[0];
    check('cache name carries the nexus-static-runtime prefix', currentGeneration.startsWith('nexus-static-runtime'));
    const cache = await env.store.open(currentGeneration);
    const offline = await cache.match('/__offline');
    check('offline shell entry present', !!offline);
    check('offline shell is HTML', typeof offline.body === 'string' && offline.body.includes('Nexus is offline'));
  }

  console.log('case: activate deletes prior owned-prefix generations, preserves unrelated caches, claims clients');
  {
    const env = buildEnv();
    await env.install();
    // Seed pre-existing caches simulating a prior deploy's leftovers plus an
    // unrelated cache this worker must never touch.
    await env.store.open('nexus-static-runtime');       // bare pre-generation name (the old, un-versioned cache)
    await env.store.open('nexus-static-runtime-v1');     // a prior explicit generation
    await env.store.open('unrelated-app-cache');         // not owned by this worker at all
    await env.activate();
    const names = new Set(await env.store.keys());
    check('clients.claim was called', env.fakeSelf._claimed === true);
    check('current generation survives', names.has(currentGeneration));
    check('bare pre-generation cache deleted', !names.has('nexus-static-runtime'));
    check('prior explicit generation deleted', !names.has('nexus-static-runtime-v1'));
    check('unrelated cache left untouched', names.has('unrelated-app-cache'));
  }

  // ------------------------------------------------------------------ //
  // 2. Cache-key normalization: successive ?v= hashes overwrite one entry.
  // ------------------------------------------------------------------ //
  console.log('case: two versioned URLs for the same static path leave exactly one normalized entry with the latest body');
  {
    const env = buildEnv();
    await env.install();
    await env.activate();
    const assetUrl = `${ORIGIN}/static/app.js`;

    const r1 = await env.fetchEvent(`${assetUrl}?v=aaa111`);
    check('first fetch was intercepted (respondWith called)', r1.event._responded === true);
    check('first fetch returned the network body', r1.response.body === `body:${assetUrl}?v=aaa111`);

    const cache = await env.store.open(currentGeneration);
    let keys = (await cache.keys()).map((k) => k.url);
    check('exactly one entry for the asset path after the first fetch',
      keys.filter((k) => k === `${ORIGIN}/static/app.js`).length === 1, `got ${JSON.stringify(keys)}`);

    const r2 = await env.fetchEvent(`${assetUrl}?v=bbb222`);
    check('second fetch (different hash, same path) was intercepted', r2.event._responded === true);

    keys = (await cache.keys()).map((k) => k.url);
    const matching = keys.filter((k) => k === `${ORIGIN}/static/app.js`);
    check('still exactly one entry for the asset path after the second, different-hash fetch',
      matching.length === 1, `got ${JSON.stringify(keys)}`);
    const stored = await cache.match(`${ORIGIN}/static/app.js`);
    check('the single entry holds the LATEST (v=bbb222) response, not the first',
      stored.body === `${assetUrl}?v=bbb222`.replace(assetUrl, 'body:' + assetUrl), `got ${stored.body}`);
  }

  console.log('case: distinct asset paths remain distinct entries');
  {
    const env = buildEnv();
    await env.install();
    await env.activate();
    await env.fetchEvent(`${ORIGIN}/static/app.js?v=1`);
    await env.fetchEvent(`${ORIGIN}/static/styles.css?v=2`);
    const cache = await env.store.open(currentGeneration);
    const keys = (await cache.keys()).map((k) => k.url).sort();
    const assetKeys = keys.filter((k) => k !== '/__offline');
    check('two distinct entries, one per path (plus the unrelated /__offline shell)',
      assetKeys.length === 2 &&
      assetKeys.includes(`${ORIGIN}/static/app.js`) &&
      assetKeys.includes(`${ORIGIN}/static/styles.css`),
      `got ${JSON.stringify(keys)}`);
  }

  // ------------------------------------------------------------------ //
  // 3. Offline fallback reads the normalized key, serves the newest entry.
  // ------------------------------------------------------------------ //
  console.log('case: offline fallback for a static asset ignores the query string and serves the last-cached response');
  {
    const env = buildEnv();
    await env.install();
    await env.activate();
    const path = `${ORIGIN}/static/app.js`;
    await env.fetchEvent(`${path}?v=xxx`); // populate the cache online
    env.setFetch(async () => { throw new Error('network down'); });
    const { response } = await env.fetchEvent(`${path}?v=yyy`); // different hash, offline now
    check('offline fetch for the same path (different query) returns the previously cached body',
      response.body === `body:${path}?v=xxx`, `got ${response && response.body}`);
  }

  console.log('case: offline fetch for a never-cached static asset returns a bounded 503, not a crash');
  {
    const env = buildEnv();
    await env.install();
    await env.activate();
    env.setFetch(async () => { throw new Error('network down'); });
    const { response } = await env.fetchEvent(`${ORIGIN}/static/never-seen.js?v=1`);
    check('503 offline response returned', response.status === 503);
  }

  console.log('case: offline navigation serves the cached offline shell');
  {
    const env = buildEnv();
    await env.install();
    await env.activate();
    env.setFetch(async () => { throw new Error('network down'); });
    const { response } = await env.fetchEvent(`${ORIGIN}/`, { mode: 'navigate' });
    check('offline shell served for navigation', response.body.includes('Nexus is offline'));
  }

  // ------------------------------------------------------------------ //
  // 4. Never-cache cases: API, navigation, cross-origin, non-GET, non-OK,
  //    redirected, and non-basic (opaque/cors) responses.
  // ------------------------------------------------------------------ //
  console.log('case: /api/* is passthrough — never intercepted, never cached');
  {
    const env = buildEnv();
    await env.install();
    await env.activate();
    const { event } = await env.fetchEvent(`${ORIGIN}/api/status`);
    check('not intercepted', event._responded === false);
    check('fetch() was never called for it (true passthrough)', !env.fetchCalls.includes(`${ORIGIN}/api/status`));
  }

  console.log('case: /ws is passthrough — never intercepted, never cached');
  {
    const env = buildEnv();
    await env.install();
    await env.activate();
    const { event } = await env.fetchEvent(`${ORIGIN}/ws`);
    check('not intercepted', event._responded === false);
  }

  console.log('case: non-GET (mutation) requests are passthrough regardless of path');
  {
    const env = buildEnv();
    await env.install();
    await env.activate();
    const { event } = await env.fetchEvent(`${ORIGIN}/static/app.js?v=1`, { method: 'POST' });
    check('not intercepted', event._responded === false);
    check('fetch() was never called for it', env.fetchCalls.length === 0);
  }

  console.log('case: cross-origin GET requests are passthrough, never cached');
  {
    const env = buildEnv();
    await env.install();
    await env.activate();
    const { event } = await env.fetchEvent('https://cdn.other-example.net/static/app.js?v=1');
    check('not intercepted', event._responded === false);
  }

  console.log('case: successful navigation responses are never placed in the runtime cache');
  {
    const env = buildEnv();
    await env.install();
    await env.activate();
    const before = (await (await env.store.open(currentGeneration)).keys()).length;
    const { response } = await env.fetchEvent(`${ORIGIN}/`, { mode: 'navigate' });
    check('navigation fetch succeeded online', response.body === `body:${ORIGIN}/`);
    const after = (await (await env.store.open(currentGeneration)).keys()).length;
    check('cache entry count unchanged by a navigation', after === before, `before=${before} after=${after}`);
  }

  console.log('case: non-OK (404) static responses are not cached');
  {
    const env = buildEnv();
    await env.install();
    await env.activate();
    env.setFetch(async (request) => new FakeResponse('not found', { status: 404, type: 'basic' }));
    await env.fetchEvent(`${ORIGIN}/static/missing.js?v=1`);
    const cache = await env.store.open(currentGeneration);
    const stored = await cache.match(`${ORIGIN}/static/missing.js`);
    check('404 response was not cached', stored === undefined);
  }

  console.log('case: redirected static responses are not cached');
  {
    const env = buildEnv();
    await env.install();
    await env.activate();
    env.setFetch(async (request) => new FakeResponse('redirected body', { status: 200, type: 'basic', redirected: true }));
    await env.fetchEvent(`${ORIGIN}/static/redirected.js?v=1`);
    const cache = await env.store.open(currentGeneration);
    const stored = await cache.match(`${ORIGIN}/static/redirected.js`);
    check('redirected response was not cached', stored === undefined);
  }

  console.log('case: opaque/cors-type static responses are not cached');
  {
    const env = buildEnv();
    await env.install();
    await env.activate();
    env.setFetch(async (request) => new FakeResponse('opaque body', { status: 200, type: 'opaque' }));
    await env.fetchEvent(`${ORIGIN}/static/opaque.js?v=1`);
    const cache = await env.store.open(currentGeneration);
    const stored = await cache.match(`${ORIGIN}/static/opaque.js`);
    check('opaque-type response was not cached', stored === undefined);
  }

  console.log('');
  if (failures > 0) {
    console.log(`${failures} check(s) FAILED`);
    process.exit(1);
  }
  console.log('all checks passed');
}

run();
