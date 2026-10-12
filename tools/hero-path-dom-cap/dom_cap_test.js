#!/usr/bin/env node
/**
 * DOM-cap harness — FLEET-WORKER2-BUILD-20260721-panel-hero-path-dom-cap
 *
 * Loads the inline <script> from templates/hero_path_session.html VERBATIM
 * (regex-extracted, only the Jinja placeholders substituted with literal test
 * values — no other edits) and runs it against a small bounded fake DOM, so
 * this exercises the exact shipped client logic, not a reimplementation.
 *
 * Self-contained: no npm dependency. Uses only Node's built-in `vm` module
 * plus a hand-rolled fake DOM (FakeElement/FakeDocument/FakeTextNode below)
 * that implements exactly the DOM surface the shipped script and this test
 * touch — a real HTML parser subset, id/class/child/descendant/:scope
 * selector matching, classList, style, textContent, insertAdjacentHTML,
 * and offsetHeight/scrollHeight layout stubs. It is not a general DOM/CSS
 * engine; extending the shipped script's DOM usage may require extending
 * the selector engine or element API here too.
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const TEMPLATE_PATH = path.join(__dirname, "../../templates/hero_path_session.html");
const ROW_HEIGHT = 24; // arbitrary fixed per-row px height for the layout stub

let failures = 0;
function check(label, cond, detail) {
  if (cond) {
    console.log(`  ok - ${label}`);
  } else {
    failures++;
    console.log(`  FAIL - ${label}${detail ? " :: " + detail : ""}`);
  }
}

function extractScript(html) {
  const m = html.match(/<script>\s*\(function\(\)\{[\s\S]*?\}\)\(\);\s*<\/script>/);
  if (!m) throw new Error("could not locate the inline hero-path IIFE <script> block");
  let src = m[0].replace(/^<script>/, "").replace(/<\/script>$/, "");
  // Substitute the exact Jinja expressions this template emits (verified
  // against the current file — this is a literal-value swap, not a rewrite
  // of any logic).
  src = src.replace("{{ 'true' if is_running else 'false' }}", "true");
  src = src.replace("{{ token|tojson }}", JSON.stringify("TEST-TOKEN"));
  src = src.replace("{{ size }}", "0");
  if (src.includes("{{")) {
    throw new Error("unsubstituted Jinja expression left in extracted script: " + src);
  }
  return src;
}

function rowHtml(idx) {
  return `<div class="hp-row hp-text hp-assistant" data-idx="${idx}"><div class="hp-node"></div><div class="hp-body">row ${idx}</div></div>`;
}

function buildPage(initialRowCount) {
  const rows = Array.from({ length: initialRowCount }, (_, i) => rowHtml(i)).join("");
  return `<!DOCTYPE html><html><body>
<div class="hp-meta-bar"><span class="hp-live-dot"></span><span id="hp-live-word">live</span></div>
<div class="hp-scroll" id="hp-scroll">${rows}</div>
<button class="hp-jump" id="hp-jump">jump</button>
</body></html>`;
}

// ---------------------------------------------------------------------- //
// Minimal fake DOM — just enough of the API surface the shipped hero-path
// script and this harness touch. See file header for scope/limits.
// ---------------------------------------------------------------------- //

class FakeTextNode {
  constructor(text) {
    this.nodeType = 3;
    this.textContent = text;
    this.parentNode = null;
  }
}

class FakeElement {
  constructor(tagName) {
    this.nodeType = 1;
    this.tagName = tagName.toUpperCase();
    this.attrs = new Map();
    this.childNodes = [];
    this.parentNode = null;
    this.style = {};
    this._listeners = {};
  }

  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }

  get id() { return this.attrs.get("id") || ""; }
  set id(v) { this.attrs.set("id", v); }

  get className() { return this.attrs.get("class") || ""; }
  set className(v) { this.attrs.set("class", v); }

  get classList() {
    const self = this;
    return {
      add(c) {
        const set = new Set(self.className.split(/\s+/).filter(Boolean));
        set.add(c);
        self.className = [...set].join(" ");
      },
      remove(c) {
        const set = new Set(self.className.split(/\s+/).filter(Boolean));
        set.delete(c);
        self.className = [...set].join(" ");
      },
      contains(c) { return self.className.split(/\s+/).filter(Boolean).includes(c); },
    };
  }

  get children() { return this.childNodes.filter((n) => n.nodeType === 1); }
  get firstChild() { return this.childNodes.length ? this.childNodes[0] : null; }

  get textContent() {
    let s = "";
    for (const c of this.childNodes) s += c.textContent;
    return s;
  }
  set textContent(v) {
    this.childNodes = [];
    if (v !== "") {
      const t = new FakeTextNode(v);
      t.parentNode = this;
      this.childNodes.push(t);
    }
  }

  appendChild(node) {
    node.parentNode = this;
    this.childNodes.push(node);
    return node;
  }
  insertBefore(node, ref) {
    node.parentNode = this;
    if (ref == null) {
      this.childNodes.push(node);
    } else {
      const idx = this.childNodes.indexOf(ref);
      this.childNodes.splice(idx < 0 ? this.childNodes.length : idx, 0, node);
    }
    return node;
  }
  removeChild(node) {
    const idx = this.childNodes.indexOf(node);
    if (idx >= 0) this.childNodes.splice(idx, 1);
    node.parentNode = null;
    return node;
  }

  insertAdjacentHTML(position, html) {
    if (position !== "beforeend") throw new Error(`fake DOM only supports beforeend, got ${position}`);
    for (const node of parseFragment(html)) this.appendChild(node);
  }

  addEventListener(type, fn) {
    (this._listeners[type] = this._listeners[type] || []).push(fn);
  }

  querySelectorAll(sel) { return evalSelector(this, sel); }
  querySelector(sel) { return evalSelector(this, sel)[0] || null; }
}

Object.defineProperty(FakeElement.prototype, "offsetHeight", {
  get() { return ROW_HEIGHT; },
  configurable: true,
});

class FakeDocument {
  constructor(bodyEl) { this.body = bodyEl; }
  createElement(tag) { return new FakeElement(tag); }
  getElementById(id) {
    for (const el of collectDescendants(this.body)) if (el.id === id) return el;
    return this.body.id === id ? this.body : null;
  }
  querySelectorAll(sel) { return evalSelector(this.body, sel); }
  querySelector(sel) { return evalSelector(this.body, sel)[0] || null; }
}

function collectDescendants(el) {
  const out = [];
  for (const c of el.childNodes) {
    if (c.nodeType === 1) { out.push(c); out.push(...collectDescendants(c)); }
  }
  return out;
}

function matchesSimple(el, token) {
  if (token[0] === "#") return el.id === token.slice(1);
  if (token[0] === ".") return el.classList.contains(token.slice(1));
  throw new Error(`unsupported selector token: ${token}`);
}

// Selector engine covering exactly what the shipped script and this test
// use: "#id", ".class", descendant (space) and child (">") combinators, and
// a leading ":scope" anchoring the query to the calling element itself.
function evalSelector(scopeEl, selector) {
  const tokens = selector.trim().split(/\s+/);
  let currentSet;
  let i = 0;
  if (tokens[0] === ":scope") { currentSet = [scopeEl]; i = 1; }
  else { currentSet = [scopeEl]; }
  let combinator = "descendant";
  for (; i < tokens.length; i++) {
    const tok = tokens[i];
    if (tok === ">") { combinator = "child"; continue; }
    const matched = [];
    for (const el of currentSet) {
      const pool = combinator === "child" ? el.children : collectDescendants(el);
      matched.push(...pool.filter((c) => matchesSimple(c, tok)));
    }
    currentSet = [...new Set(matched)];
    combinator = "descendant";
  }
  return currentSet;
}

// ---- tiny HTML-subset parser (tags/attrs/text, no comments/void-tag magic
// beyond what these fixtures need) ----------------------------------------
function tokenizeHtml(html) {
  const tokens = [];
  let i = 0;
  const n = html.length;
  while (i < n) {
    if (html.slice(i, i + 9).toUpperCase() === "<!DOCTYPE") {
      i = html.indexOf(">", i) + 1;
      continue;
    }
    if (html[i] === "<") {
      const end = html.indexOf(">", i);
      if (html[i + 1] === "/") {
        tokens.push({ type: "close", name: html.slice(i + 2, end).trim().toLowerCase() });
      } else {
        let content = html.slice(i + 1, end);
        let selfClose = false;
        if (content.endsWith("/")) { selfClose = true; content = content.slice(0, -1); }
        const m = content.match(/^([a-zA-Z0-9]+)([\s\S]*)$/);
        const name = m[1].toLowerCase();
        const attrs = {};
        const attrRe = /([a-zA-Z0-9_-]+)(?:=("([^"]*)"|'([^']*)'))?/g;
        let am;
        while ((am = attrRe.exec(m[2]))) {
          attrs[am[1]] = am[3] !== undefined ? am[3] : am[4] !== undefined ? am[4] : "";
        }
        tokens.push({ type: "open", name, attrs, selfClose });
      }
      i = end + 1;
    } else {
      const next = html.indexOf("<", i);
      const text = next === -1 ? html.slice(i) : html.slice(i, next);
      tokens.push({ type: "text", text });
      i = next === -1 ? n : next;
    }
  }
  return tokens;
}

function parseFragment(html) {
  const tokens = tokenizeHtml(html);
  const root = { childNodes: [], nodeType: 11 };
  const stack = [root];
  for (const t of tokens) {
    const top = stack[stack.length - 1];
    if (t.type === "open") {
      const el = new FakeElement(t.name);
      for (const [k, v] of Object.entries(t.attrs)) el.setAttribute(k, v);
      el.parentNode = top;
      top.childNodes.push(el);
      if (!t.selfClose) stack.push(el);
    } else if (t.type === "close") {
      for (let s = stack.length - 1; s > 0; s--) {
        if (stack[s].tagName && stack[s].tagName.toLowerCase() === t.name) { stack.length = s; break; }
      }
    } else if (t.type === "text" && t.text.length) {
      const tn = new FakeTextNode(t.text);
      tn.parentNode = top;
      top.childNodes.push(tn);
    }
  }
  return root.childNodes;
}

function buildDocument(initialRowCount) {
  const html = buildPage(initialRowCount);
  const topLevel = parseFragment(html);
  const htmlEl = topLevel.find((n) => n.nodeType === 1 && n.tagName === "HTML");
  const bodyEl = htmlEl.children.find((c) => c.tagName === "BODY");
  return new FakeDocument(bodyEl);
}

function makeEnv(initialRowCount) {
  const document = buildDocument(initialRowCount);

  function liveRowCount() {
    return document.querySelectorAll("#hp-scroll > .hp-row").length;
  }
  Object.defineProperty(document.body, "scrollHeight", {
    get() { return liveRowCount() * ROW_HEIGHT + 200; },
    configurable: true,
  });

  let _scrollY = 0;
  const window = {
    document,
    innerHeight: 800,
    addEventListener() {},
  };
  Object.defineProperty(window, "scrollY", { get() { return _scrollY; }, configurable: true });
  window.scrollTo = function (a, b) {
    if (typeof a === "object" && a !== null) _scrollY = a.top;
    else _scrollY = b;
  };

  window.__sockets = [];
  class FakeWebSocket {
    constructor(url) { this.url = url; this.onmessage = null; this.onerror = null; window.__sockets.push(this); }
    close() { this.closed = true; }
  }
  window.WebSocket = FakeWebSocket;

  const location = { protocol: "http:", host: "localhost" };

  const context = vm.createContext({ document, window, location, WebSocket: FakeWebSocket, console });
  return { context, document, window };
}

function run() {
  const html = fs.readFileSync(TEMPLATE_PATH, "utf8");
  const script = extractScript(html);

  console.log("case: static check — evicted nodes are not retained anywhere");
  {
    // trimToCap() must remove and drop each row (no array/map/closure
    // collecting them for later reattachment/render).
    const noCollection = !/\[\s*\]|new Array|new Map|new Set\(/.test(script);
    check("no array/map/set constructed in the script (nothing to retain evicted rows in)", noCollection);
  }

  // ------------------------------------------------------------------ //
  // 1. Initial SSR render bounded to 500, newest retained, in order.
  // ------------------------------------------------------------------ //
  console.log("case: initial SSR render (700 rows, simulating an old large session)");
  {
    const { context, document } = makeEnv(700);
    vm.runInContext(script, context);
    const rows = document.querySelectorAll("#hp-scroll > .hp-row");
    check("exactly 500 rows remain", rows.length === 500, `got ${rows.length}`);
    const idxs = rows.map((r) => Number(r.getAttribute("data-idx")));
    check("oldest evicted / newest retained (200..699)",
      idxs[0] === 200 && idxs[idxs.length - 1] === 699,
      `first=${idxs[0]} last=${idxs[idxs.length - 1]}`);
    let ordered = true;
    for (let i = 1; i < idxs.length; i++) if (idxs[i] !== idxs[i - 1] + 1) ordered = false;
    check("retained rows in correct ascending order", ordered);
    const notices = document.querySelectorAll(".hp-omitted-notice");
    check("exactly one notice node", notices.length === 1, `got ${notices.length}`);
    check("notice reports 200 omitted", /200/.test(notices[0].textContent), notices[0].textContent);
    check("notice is visible", notices[0].style.display !== "none");
  }

  // ------------------------------------------------------------------ //
  // 2. Under-cap initial render: no eviction, no notice shown.
  // ------------------------------------------------------------------ //
  console.log("case: initial SSR render (220 rows, default cap — under 500)");
  {
    const { context, document } = makeEnv(220);
    vm.runInContext(script, context);
    const rows = document.querySelectorAll("#hp-scroll > .hp-row");
    check("all 220 rows retained", rows.length === 220, `got ${rows.length}`);
    const notices = document.querySelectorAll(".hp-omitted-notice");
    check("no visible notice when under cap",
      notices.length === 0 || notices[0].style.display === "none");
  }

  // ------------------------------------------------------------------ //
  // 3. 501 sequential WS appends onto a fresh (0-row) session -> exactly
  //    500 nodes, oldest evicted, newest retained, single notice, near-
  //    bottom auto-follow keeps scrollY pinned to the (fake) bottom.
  // ------------------------------------------------------------------ //
  console.log("case: 501 sequential live WS appends, reader near bottom (auto-follow)");
  {
    const { context, document, window } = makeEnv(0);
    vm.runInContext(script, context);
    const ws = window.__sockets[0];
    check("WebSocket was constructed", !!ws);
    for (let i = 0; i < 501; i++) {
      ws.onmessage({ data: JSON.stringify({ type: "events", html: rowHtml(i) }) });
    }
    const rows = document.querySelectorAll("#hp-scroll > .hp-row");
    check("exactly 500 rows after 501 appends", rows.length === 500, `got ${rows.length}`);
    const idxs = rows.map((r) => Number(r.getAttribute("data-idx")));
    check("oldest (0) evicted, newest (500) retained", idxs[0] === 1 && idxs[idxs.length - 1] === 500,
      `first=${idxs[0]} last=${idxs[idxs.length - 1]}`);
    const notices = document.querySelectorAll(".hp-omitted-notice");
    check("still exactly one notice node after many evictions", notices.length === 1, `got ${notices.length}`);
    check("notice reports 1 omitted", /\b1\b/.test(notices[0].textContent), notices[0].textContent);
    const expectedBottom = document.body.scrollHeight;
    check("auto-follow kept scrollY pinned near bottom",
      window.scrollY === expectedBottom, `scrollY=${window.scrollY} expected=${expectedBottom}`);
  }

  // ------------------------------------------------------------------ //
  // 4. Scrolled-up reader: appends must not force them to bottom, and
  //    evictions must not produce a large jump (compensated scroll).
  // ------------------------------------------------------------------ //
  console.log("case: reader scrolled up — no forced jump, eviction-compensated scroll");
  {
    const { context, document, window } = makeEnv(500); // already at cap
    vm.runInContext(script, context);
    const ws = window.__sockets[0];
    // Scroll well away from the bottom before any append.
    window.scrollTo(0, 1000);
    const scrollBefore = window.scrollY;
    const heightBefore = document.body.scrollHeight;
    check("reader is not near bottom", (window.innerHeight + scrollBefore) < (heightBefore - 120));

    ws.onmessage({ data: JSON.stringify({ type: "events", html: rowHtml(9000) }) });

    check("did not force-scroll to bottom",
      window.scrollY !== document.body.scrollHeight);
    // One row (24px) evicted from the top -> compensate scrollY by -24.
    check("scroll position compensated by evicted height (no jump)",
      window.scrollY === Math.max(0, scrollBefore - ROW_HEIGHT),
      `scrollY=${window.scrollY} expected=${Math.max(0, scrollBefore - ROW_HEIGHT)}`);
    const rows = document.querySelectorAll("#hp-scroll > .hp-row");
    check("still exactly 500 rows", rows.length === 500, `got ${rows.length}`);
  }

  // ------------------------------------------------------------------ //
  // 5. Non-transcript chrome (meta bar / live word) untouched by trimming.
  // ------------------------------------------------------------------ //
  console.log("case: header/status chrome preserved across eviction");
  {
    const { context, document } = makeEnv(700);
    vm.runInContext(script, context);
    check("hp-live-word still present", !!document.getElementById("hp-live-word"));
    check("hp-meta-bar still present", !!document.querySelector(".hp-meta-bar"));
    check("hp-jump button still present", !!document.getElementById("hp-jump"));
  }

  console.log("");
  if (failures > 0) {
    console.log(`${failures} check(s) FAILED`);
    process.exit(1);
  }
  console.log("all checks passed");
}

run();
