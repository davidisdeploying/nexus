/* Nexus — minimal, gate-safe service worker.
 *
 * SCOPE OF CACHING (deliberately narrow, for Cloudflare-Access safety):
 *   - Caches ONLY same-origin STATIC assets: /static/* and /manifest.webmanifest,
 *     network-first with a cached offline fallback. Cache-busted query strings
 *     (?v=<hash>) are stripped before the cache key is formed, so each asset
 *     path holds exactly one entry that gets overwritten on every deploy
 *     instead of accumulating a new permanent entry per hash forever.
 *   - NAVIGATIONS ( / , /hero-path, … ) are network-first with an OFFLINE
 *     SHELL fallback only when the network is unreachable. Online responses —
 *     including any Cloudflare Access redirect/403 — are passed straight
 *     through and NEVER cached, so authed HTML is never served stale and an
 *     Access redirect is never persisted.
 *   - Everything else (/api/*, /events, /ws, POSTs, cross-origin) is passthrough:
 *     the SW does not call respondWith, so the browser fetches normally over the
 *     live network with the Access cookie. /api/status is therefore always fresh.
 *
 * A stale SW cannot wedge the app: it caches no HTML or data, skips waiting, and
 * claims clients immediately, so the newest SW takes over on the next load.
 */
'use strict';

// Runtime cache generation: bump CACHE_GENERATION (not the whole name) when the
// cache-key format changes, so activate's cleanup below evicts every prior
// generation by prefix match, one owned prefix, nothing unrelated.
const CACHE_PREFIX = 'nexus-static-runtime';
const CACHE_GENERATION = 'v21'; // v21: node-matrix app icon replacing the Fleet eye
const STATIC_CACHE = `${CACHE_PREFIX}-${CACHE_GENERATION}`;
const OFFLINE_URL = '/__offline';

const OFFLINE_HTML =
  '<!doctype html><html lang="en"><head><meta charset="utf-8">' +
  '<meta name="viewport" content="width=device-width,initial-scale=1">' +
  '<title>Nexus — offline</title>' +
  '<style>html,body{margin:0;height:100%;background:#080d0f;color:#dcece9;' +
  'font-family:system-ui,-apple-system,"SF Pro Text","Segoe UI",sans-serif;display:flex;' +
  'align-items:center;justify-content:center;text-align:center}' +
  '.b{max-width:22rem;padding:1.5rem}.e{color:#57e0d8;font-size:11px;' +
  'letter-spacing:.22em;text-transform:uppercase}h1{font-size:1.1rem;' +
  'font-weight:600;margin:.6rem 0}p{color:#7f9b98;font-size:.85rem;line-height:1.5}' +
  '</style></head><body><div class="b"><div class="e">Fleet rune · no signal</div>' +
  '<h1>Nexus is offline</h1><p>The Nexus needs the network to scan the ' +
  'fleet. Reconnect and it will develop the next rune.</p></div></body></html>';

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) =>
      cache.put(OFFLINE_URL, new Response(OFFLINE_HTML, {
        headers: { 'Content-Type': 'text/html; charset=utf-8' },
      }))
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k.startsWith(CACHE_PREFIX) && k !== STATIC_CACHE)
        .map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

function isStaticAsset(url) {
  return url.pathname.startsWith('/static/') || url.pathname === '/manifest.webmanifest';
}

// Cache-busted asset URLs (?v=<hash>) must collapse to one entry per path, or
// every deploy leaves another permanent cache entry behind. The query string
// only ever encodes "which build"; the pathname is the entry's real identity.
function cacheKeyFor(url) {
  return url.origin + url.pathname;
}

async function networkFirstStatic(request, url) {
  try {
    const resp = await fetch(request);
    // Only cache genuine, direct, same-origin 200s; never persist a redirect,
    // an error/Access page, or an opaque/cross-origin response.
    if (resp && resp.status === 200 && resp.type === 'basic' && !resp.redirected) {
      const cache = await caches.open(STATIC_CACHE);
      await cache.put(cacheKeyFor(url), resp.clone());
    }
    return resp;
  } catch (e) {
    const cached = await caches.match(cacheKeyFor(url));
    return cached || new Response('offline', { status: 503 });
  }
}

async function networkThenOffline(request) {
  try {
    // Network-only: whatever the origin returns (incl. an Access redirect/403)
    // is passed straight through, never cached.
    return await fetch(request);
  } catch (e) {
    const shell = await caches.match(OFFLINE_URL);
    return shell || new Response('offline', { status: 503 });
  }
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;               // passthrough: writes/POSTs
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;     // passthrough: cross-origin

  if (request.mode === 'navigate') {
    event.respondWith(networkThenOffline(request));    // HTML: fresh or offline shell
    return;
  }
  if (isStaticAsset(url)) {
    event.respondWith(networkFirstStatic(request, url)); // fresh online; cached offline
    return;
  }
  // Everything else (/api/*, /events backfill, etc.) — do not intercept.
});

/* ---- Push (fallback path — DWP-capable devices display declaratively and
 * never run this handler; see panel-notifications-design.md C.2). Single
 * canonical payload shape consumed here and by the DWP display path. */
self.addEventListener('push', (event) => {
  if (!event.data) return;
  let msg;
  try {
    msg = event.data.json();
  } catch (e) {
    return;
  }
  const n = msg.notification || {};
  const title = n.title || 'Nexus';
  const options = {
    body: n.body || '',
    tag: n.tag || undefined,
    data: { navigate: n.navigate || '/' },
    silent: !!n.silent,
  };
  event.waitUntil((async () => {
    if (typeof n.app_badge === 'number' && 'setAppBadge' in navigator) {
      try { await navigator.setAppBadge(n.app_badge); } catch (e) { /* best-effort */ }
    }
    await self.registration.showNotification(title, options);
  })());
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const navigate = (event.notification.data && event.notification.data.navigate) || '/';
  const sep = navigate.indexOf('?') === -1 ? '?' : '&';
  event.waitUntil(clients.openWindow(navigate + sep + 'nf=1'));
});
