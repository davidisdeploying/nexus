  const ACCENT = {ok:"developed", warn:"safelight", crit:"overexposed", unknown:"unexposed"};
  const esc = s => (s==null?"":String(s)).replace(/[&<>"]/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  const slugify = s => String(s).toLowerCase().replace(/[^a-z0-9]+/g,"-").replace(/^-+|-+$/g,"");

  // === BEGIN Central display-time formatters ===
  // Presentation only: API/storage timestamps remain UTC. America/Chicago
  // supplies the correct local offset across daylight-saving changes.
  const CENTRAL_STAMP_FORMAT = new Intl.DateTimeFormat("en-US", {
    timeZone:"America/Chicago", year:"numeric", month:"2-digit", day:"2-digit",
    hour:"numeric", minute:"2-digit", hour12:true,
  });
  const CENTRAL_CLOCK_FORMAT = new Intl.DateTimeFormat("en-US", {
    timeZone:"America/Chicago", hour:"numeric", minute:"2-digit", second:"2-digit",
    hour12:true,
  });
  const CENTRAL_HOUR_FORMAT = new Intl.DateTimeFormat("en-US", {
    timeZone:"America/Chicago", hour:"numeric", hour12:true,
  });
  function centralFormatParts(formatter, date){
    const parts = {};
    formatter.formatToParts(date).forEach(part => {
      if(part.type !== "literal") parts[part.type] = part.value;
    });
    return parts;
  }
  function formatCentralStamp(date){
    if(!(date instanceof Date) || Number.isNaN(date.getTime())) return "—";
    const p = centralFormatParts(CENTRAL_STAMP_FORMAT, date);
    return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute} ${p.dayPeriod}`;
  }
  function formatCentralClock(date){
    if(!(date instanceof Date) || Number.isNaN(date.getTime())) return "--:--:-- CT";
    const p = centralFormatParts(CENTRAL_CLOCK_FORMAT, date);
    return `${p.hour}:${p.minute}:${p.second} ${p.dayPeriod}`;
  }
  function formatCentralHourBucket(value, referenceDate = new Date()){
    const match = /^([01]\d|2[0-3]):00 UTC$/.exec(String(value || ""));
    if(!match) return value == null ? "—" : value;
    const instant = new Date(Date.UTC(
      referenceDate.getUTCFullYear(),
      referenceDate.getUTCMonth(),
      referenceDate.getUTCDate(),
      Number(match[1]),
    ));
    const p = centralFormatParts(CENTRAL_HOUR_FORMAT, instant);
    return `${p.hour}:00 ${p.dayPeriod}`;
  }
  // === END Central display-time formatters ===

  // Shared keyed DOM reconciler (T4). Creates missing children via buildFn,
  // patches existing ones in place via updateFn (never rebuilding the node),
  // removes children whose key is no longer present, and reorders only when
  // the DOM order actually diverges from items' order. Explicit-DOM style,
  // consistent with the existing insert-based #stream updates — not a
  // virtual-DOM diff. Node identity for keys still present is preserved, so
  // CSS transitions / focus survive a refresh.
  //
  // The key is tracked as an in-memory property (node.__reconcileKey), never
  // a DOM attribute — so it never shows up in outerHTML/innerHTML and can't
  // diverge from the server-rendered (Jinja) markup for the same container.
  // The container's first call typically runs against nodes the SERVER
  // already rendered (first paint), which carry no such property yet; those
  // untracked children are hydrated by pairing them 1:1, in order, against
  // `items` before the keyed diff runs, so the client "takes over" existing
  // markup in place instead of recreating/duplicating it.
  function reconcile(container, items, keyFn, buildFn, updateFn){
    if(!container) return;
    var existing = {};
    var untracked = [];
    var child = container.firstElementChild;
    while(child){
      var next = child.nextElementSibling;
      if(child.__reconcileKey != null) existing[child.__reconcileKey] = child;
      else untracked.push(child);
      child = next;
    }
    for(var h=0; h<untracked.length && h<items.length; h++){
      var hKey = String(keyFn(items[h]));
      if(!(hKey in existing)){
        untracked[h].__reconcileKey = hKey;
        existing[hKey] = untracked[h];
      }
    }
    var prevNode = null;
    for(var i=0;i<items.length;i++){
      var item = items[i], key = String(keyFn(item));
      var node = existing[key];
      if(node){
        delete existing[key];
        updateFn(node, item);
      } else {
        node = buildFn(item);
        node.__reconcileKey = key;
      }
      var expectedNext = prevNode ? prevNode.nextSibling : container.firstChild;
      if(node !== expectedNext) container.insertBefore(node, expectedNext);
      prevNode = node;
    }
    for(var leftoverKey in existing){ container.removeChild(existing[leftoverKey]); }
  }

  function frameInnerHTML(node){
    const a = ACCENT[node.health] || "unexposed";
    const probes = node.probes.map(p => {
      const pa = ACCENT[p.health] || "unexposed";
      const detail = p.detail ? `<span class="detail">${esc(p.detail)}</span>` : "";
      return `<li class="probe"><span class="dot ${pa}"></span>`
           + `<span class="kind">${esc(p.kind)}</span>`
           + `<span class="value">${esc(p.value||"—")}</span>${detail}</li>`;
    }).join("");
    return `<div class="frame-head">`
         + `<span class="node-name">${esc(node.display_name || node.name)}</span>`
         + `<span class="chip ${a}">${esc(node.health)}</span></div>`
         + `<ul class="probes">${probes}</ul>`;
  }
  function frameOuterHTML(node){
    const a = ACCENT[node.health] || "unexposed";
    return `<article class="frame ${a}" id="node-${esc(slugify(node.name))}">${frameInnerHTML(node)}</article>`;
  }
  // buildFn: brand-new node card, frameOuterHTML reused verbatim (no restyle).
  function buildFrame(node){
    var wrap = document.createElement("div");
    wrap.innerHTML = frameOuterHTML(node);
    return wrap.firstElementChild;
  }
  // updateFn: patch the volatile content of an EXISTING node card in place —
  // never recreating the <article id="node-*"> node itself, so the
  // tap-to-expand .expanded class (toggled directly on this node by the
  // delegated click listener further down) survives a refresh, along with
  // any CSS transition on .frame's accent box-shadow. Mirrors updateJobCard's
  // className-then-innerHTML contract, with .expanded carried over explicitly.
  function updateFrame(el, node){
    const a = ACCENT[node.health] || "unexposed";
    el.className = "frame " + a + (el.classList.contains("expanded") ? " expanded" : "");
    el.innerHTML = frameInnerHTML(node);
  }
  // T4: reconciled keyed on the raw node identity (node.name — e.g.
  // worker1/worker3/worker2), NOT the rendered id="node-{slug}" id (see slugify()),
  // so the known Jinja<->JS slug divergence on special-char names is
  // non-load-bearing for the key.
  function renderContact(nodes){
    const ct = document.getElementById("contact");
    if(!ct) return;
    if(!nodes || !nodes.length){
      ct.innerHTML = `<p class="empty">No signal yet.</p>`;
      return;
    }
    reconcile(ct, nodes, node => node.name, buildFrame, updateFrame);
  }

  // Mobile header warning ticker (row 2, right of the bell). Mirrors the
  // server-side Jinja render (warnchips macro) so a live refresh and a fresh
  // page load produce identical chip markup. Fixed overall-state lead +
  // one compact chip per NOT-OK node/job/seat, rendered twice (real +
  // aria-hidden duplicate) for the seamless left-to-right scroll loop;
  // layoutWarnTicker() (below) decides whether that loop is actually needed.
  // Healthy -> a calm static "all systems ok" line, no scroll.
  const WT_SYM = {crit:"✕", warn:"⚠", unknown:"○"};
  function wtchip(cls, sym, label, href){
    return `<a class="wt-chip ${cls}" href="${href}"><span class="wt-sym">${sym}</span>`
         + `<span>${esc(label)}</span></a>`;
  }
  function warnChipsHTML(snap){
    const chips = [];
    (snap.nodes||[]).forEach(n => {
      if(n.health === "ok") return;
      let kind = "";
      for(const p of (n.probes||[])){ if(p.health !== "ok"){ kind = p.kind; break; } }
      const label = n.name + (kind ? " " + String(kind).toLowerCase() : "");
      chips.push(wtchip(n.health, WT_SYM[n.health]||"⚠", label, "#node-"+slugify(n.name)));
    });
    const jobs = (snap.work && snap.work.jobs) || [];
    jobs.forEach(j => {
      if(j.state === "failed" || j.state === "stalled"){
        const cls = j.state === "failed" ? "crit" : "warn";
        chips.push(wtchip(cls, WT_SYM[cls]||"⚠", (j.job||"job")+" "+j.state, "#jobstable"));
      }
    });
    const seats = (snap.seats && snap.seats.seats) || [];
    seats.forEach(s => {
      if(s.state === "died")
        chips.push(wtchip("crit", "✕", (s.label||s.seat)+" died", "#seatstrip"));
    });
    return chips.join("");
  }
  function warnTickerHTML(snap){
    if(!snap){
      return `<span class="wt-lead unexposed"><span class="lamp"></span>`
        + `<span class="wt-lead-word">no signal</span></span>`;
    }
    const overall = snap.overall || "unknown";
    const oa = ACCENT[overall] || "unexposed";
    const lead = `<span class="wt-lead ${oa}"><span class="lamp"></span>`
      + `<span class="wt-lead-word">${esc(overall)}</span></span>`;
    const chipHtml = warnChipsHTML(snap);
    const body = chipHtml
      ? `<div class="wt-viewport"><div class="wt-track" id="wtTrack">`
        + `<div class="wt-list">${chipHtml}</div>`
        + `<div class="wt-list wt-dup" inert aria-hidden="true">${chipHtml}</div>`
        + `</div></div>`
      : `<div class="wt-viewport wt-ok"><span class="wt-ok-msg"><b>all systems ok</b> · fleet nominal</span></div>`;
    return lead + body;
  }

  // Only reveal the scrolling duplicate + animate once the real chip list
  // actually overflows the ticker's visible width — a short list (one or two
  // warnings) just sits static instead of looping "awkwardly" over almost
  // nothing. Speed is constant px/s (not a fixed duration) so a long list
  // never feels rushed and a short one never feels frantic.
  const WT_SPEED_PX_S = 46, WT_MIN_DUR = 10, WT_MAX_DUR = 60;
  function layoutWarnTicker(){
    const track = document.getElementById("wtTrack");
    if(!track) return;
    const vp = track.parentElement;
    const list = track.querySelector(".wt-list:not(.wt-dup)");
    if(!vp || !list) return;
    const listW = list.scrollWidth;
    const overflowing = listW > vp.clientWidth + 1;
    track.classList.toggle("wt-scrolling", overflowing);
    if(overflowing){
      const dur = Math.max(WT_MIN_DUR, Math.min(WT_MAX_DUR, listW / WT_SPEED_PX_S));
      track.style.setProperty("--wt-dur", dur.toFixed(2)+"s");
    }
  }
  var wtResizeTimer = null;
  window.addEventListener("resize", function(){
    clearTimeout(wtResizeTimer);
    wtResizeTimer = setTimeout(layoutWarnTicker, 150);
  });

  // liInner: the <li> contents only, split out from li() so T4's positional
  // patch (below) can reset an EXISTING <li>'s innerHTML in place without
  // recreating the node.
  function liInner(title, sub, subClass){
    return `${title?`<span class="wl-title">${title}</span>`:""}`
         + `${sub?`<span class="${subClass||"wl-sub"}">${sub}</span>`:""}`;
  }

  const STATE_ACCENT = {running:"developed", stalled:"safelight",
    failed:"overexposed", done:"developed", ended:"unexposed", unknown:"unexposed"};
  const nfmt = n => (n==null?"":Number(n).toLocaleString("en-US"));

  // one small sparkline <svg> for the given points string
  const sparkSvg = pts => `<svg class="jobspark" viewBox="0 0 100 24" preserveAspectRatio="none" aria-hidden="true"><polyline points="${esc(pts)}"/></svg>`;
  // a metric/stat tile, optionally carrying the sparkline on its right
  const tile = (val, label, spark) => spark
    ? `<span class="stat withspark"><span style="display:flex;flex-direction:column"><b>${esc(val)}</b><span>${esc(label)}</span></span>${sparkSvg(spark)}</span>`
    : `<span class="stat"><b>${esc(val)}</b><span>${esc(label)}</span></span>`;

  function jobCardHTML(gj){
    const a = STATE_ACCENT[gj.state] || "unexposed";
    let bar = "";
    if(gj.total && gj.done!=null){
      bar = `<div class="pbar"><i style="width:${gj.pct||0}%"></i></div>`
          + `<div class="pbar-legend"><span><b>${nfmt(gj.done)}</b> / ${nfmt(gj.total)}${gj.unit?" "+esc(gj.unit):""}</span>`
          + `<span class="pct">${gj.pct!=null?esc(gj.pct):"—"}%</span></div>`;
    }
    // per-queue strip — mirrors the Jinja gj.queues block (dashboard.html
    // first-paint render) so the refresh path looks identical (same classes).
    let queueStrip = "";
    if(gj.queues && gj.queues.length){
      const rows = gj.queues.map(q => {
        let text = `<b>${nfmt(q.done)}</b>/${nfmt(q.total)} (${q.pct!=null?esc(q.pct):"—"}%)`;
        if(q.rate) text += ` · ~${esc(q.rate)}/min`;
        if(q.eta) text += ` · ETA ~${esc(q.eta)}m`;
        if(q.waiting!=null){
          text += ` · ${nfmt(q.waiting)} queued`;
          if(q.active) text += ` · ${esc(q.active)} active`;
        }
        return `<div class="queue-row" title="${esc(q.name)}">`
            + `<span class="queue-label">${esc(q.name)}</span>`
            + `<div class="queue-bar"><i style="width:${q.pct||0}%"></i></div>`
            + `<span class="queue-text">${text}</span></div>`;
      }).join("");
      queueStrip = `<div class="queue-strip">${rows}</div>`;
    }
    // per-GPU strip - mirrors the Jinja gj.gpus block above so the refresh
    // path looks identical after the auto-refresh (same classes).
    let gpuStrip = "";
    if(gj.gpus && gj.gpus.length){
      const rows = gj.gpus.map(g => {
        const isMl = String(g.role||"").indexOf("ML") !== -1;
        const text = isMl
          ? `${g.util!=null?esc(g.util):"—"}% · ${g.mem_used}/${g.mem_total}MB · ${g.temp}°`
          : `enc ${g.enc!=null?esc(g.enc):"—"}% · dec ${g.dec!=null?esc(g.dec):"—"}% · ${g.mem_used}MB · ${g.temp}°`;
        return `<div class="queue-row" title="${esc(g.name)}">`
            + `<span class="queue-label">${esc(g.role)}</span>`
            + `<div class="queue-bar"><i style="width:${g.util||0}%"></i></div>`
            + `<span class="queue-text">${text}</span></div>`;
      }).join("");
      gpuStrip = `<div class="queue-strip gpu-strip">${rows}</div>`;
    }
    const stats = [];
    if(gj.rate!=null) stats.push(tile(gj.rate, gj.unit||"rate", (gj.spark&&gj.spark_label)?gj.spark:null));
    if(gj.eta) stats.push(tile(gj.eta, "eta"));
    if(gj.uptime) stats.push(tile(gj.uptime, "uptime"));
    // JOB METRICS block — the counter half of the heartbeat, mirrors the Jinja
    // renderer. Numbers render as value tiles (+ sparkline on the tracked
    // metric); string values (last_path) render truncated mono, full in title=.
    // Distinct from the host·cpu and progress.json blocks. Zero new colors.
    let metricsblk = "";
    if(gj.metrics && Object.keys(gj.metrics).length){
      const mLabels = {files_per_min:"files/min", read_mb_per_s:"MB/s", gb_hashed:"GB hashed", last_path:"current"};
      const mst = [];
      for(const [k,v] of Object.entries(gj.metrics)){
        const lbl = mLabels[k] || k.replace(/_/g," ");
        if(typeof v === "number"){
          mst.push(tile(v, lbl, (gj.spark&&gj.spark_key===k)?gj.spark:null));
        } else {
          const sv = String(v);
          const disp = sv.includes("/") ? sv.split("/").pop() : (sv.length>32 ? sv.slice(0,31)+"…" : sv);
          mst.push(`<span class="stat"><b class="mono" title="${esc(sv)}">${esc(disp)}</b><span>${esc(lbl)}</span></span>`);
        }
      }
      metricsblk = `<div class="prog"><div class="prog-head">job metrics</div><div class="jobstats">${mst.join("")}</div></div>`;
    }
    // progress.json enrichment — mirrors the Jinja renderer. Present only when a
    // run's status file matched this card; each block gated on its own field.
    let prog = "";
    if(gj.progress_age_s!=null || gj.thermal || gj.tripwire || gj.remaining!=null || gj.last_path){
      const parts = [];
      if(gj.resumed_from!=null){
        let s = `resumed from <b>${nfmt(gj.resumed_from)}</b>`;
        if(gj.scanned_this_run!=null) s += ` · +${nfmt(gj.scanned_this_run)} this run`;
        if(gj.remaining!=null) s += ` · <b>${nfmt(gj.remaining)}</b> remaining`;
        parts.push(`<div class="prog-sub">${s}</div>`);
      } else if(gj.remaining!=null){
        parts.push(`<div class="prog-sub"><b>${nfmt(gj.remaining)}</b> remaining</div>`);
      }
      const rst = [];
      if(gj.clips_per_min!=null){
        const cum = gj.clips_per_min_cumulative!=null ? ` <span class="cum">(${esc(gj.clips_per_min_cumulative)} cum)</span>` : "";
        rst.push(`<span class="stat"><b>${esc(gj.clips_per_min)}${cum}</b><span>clips/min</span></span>`);
      }
      if(gj.eta_zone) rst.push(`<span class="stat"><b>${esc(gj.eta_zone.central)}</b><span>eta · ${esc(gj.eta_zone.utc)}</span></span>`);
      if(gj.elapsed_hours!=null) rst.push(`<span class="stat"><b>${esc(gj.elapsed_hours)}h</b><span>elapsed</span></span>`);
      if(rst.length) parts.push(`<div class="jobstats">${rst.join("")}</div>`);
      if(gj.thermal){
        const th = gj.thermal;
        const tcl = th.throttle_active ? "overexposed"
          : ((th.thermal_state && th.thermal_state!=="nominal" && th.thermal_state!=="ok") ? "safelight" : "developed");
        let head = "thermal";
        if(th.thermal_state) head += ` <span class="chip ${tcl}">${esc(th.thermal_state)}</span>`;
        if(th.throttle_active) head += ` <span class="chip overexposed">throttling${(th.throttle_reasons && th.throttle_reasons!=="none")?" · "+esc(th.throttle_reasons):""}</span>`;
        parts.push(`<div class="prog-head">${head}</div>`);
        const tst = [];
        if(th.temp_c!=null) tst.push(`<span class="stat"><b>${esc(th.temp_c)}°${th.temp_c_max!=null?` <span class="cum">max ${esc(th.temp_c_max)}°</span>`:""}</b><span>temp c</span></span>`);
        if(th.margin_c!=null) tst.push(`<span class="stat"><b>${esc(th.margin_c)}°</b><span>margin</span></span>`);
        if(th.fan_pct!=null) tst.push(`<span class="stat"><b>${esc(th.fan_pct)}%</b><span>fan</span></span>`);
        if(th.power_w!=null) tst.push(`<span class="stat"><b>${esc(th.power_w)}${th.power_limit_w!=null?` <span class="cum">/ ${esc(th.power_limit_w)}</span>`:""}</b><span>power w</span></span>`);
        if(th.thermal_soft_events!=null) tst.push(`<span class="stat"><b>${esc(th.thermal_soft_events)}</b><span>soft events</span></span>`);
        if(tst.length) parts.push(`<div class="jobstats">${tst.join("")}</div>`);
      }
      const tw = gj.tripwire || {};
      if(gj.passa_errors!=null || gj.tripwire || gj.dead_clips!=null || gj.parse_errors!=null){
        let head = "health &amp; errors";
        if(tw.tripped) head += ` <span class="chip overexposed">tripwire tripped</span>`;
        else if(tw.armed) head += ` <span class="chip developed"${tw.last_probe?` title="${esc(tw.last_probe)}"`:""}>tripwire armed</span>`;
        parts.push(`<div class="prog-head">${head}</div>`);
        const hst = [];
        if(gj.passa_errors!=null) hst.push(`<span class="stat"><b>${esc(gj.passa_errors)}${gj.passb_errors!=null?` / ${esc(gj.passb_errors)}`:""}</b><span>passA / passB err</span></span>`);
        if(gj.parse_errors!=null) hst.push(`<span class="stat"><b>${esc(gj.parse_errors)}</b><span>parse err</span></span>`);
        const iofb = tw.io_fallbacks!=null ? tw.io_fallbacks : (gj.io_fallbacks!=null?gj.io_fallbacks:null);
        const defb = tw.decoder_fallbacks!=null ? tw.decoder_fallbacks : (gj.decoder_fallbacks!=null?gj.decoder_fallbacks:null);
        if(iofb!=null) hst.push(`<span class="stat"><b>${esc(iofb)}${defb!=null?` / ${esc(defb)}`:""}</b><span>io / dec fallbacks</span></span>`);
        if(gj.dead_clips!=null) hst.push(`<span class="stat"><b>${esc(gj.dead_clips)}</b><span>dead clips</span></span>`);
        if(hst.length) parts.push(`<div class="jobstats">${hst.join("")}</div>`);
        if(tw.last_probe_ok!=null) parts.push(`<div class="prog-sub"${tw.last_probe?` title="${esc(tw.last_probe)}"`:""}>last probe ${tw.last_probe_ok?"ok":"FAILED"}</div>`);
      }
      if(gj.last_path){
        const base = String(gj.last_path).split("/").pop();
        parts.push(`<div class="prog-clip" title="${esc(gj.last_path)}">current · <b>${esc(base)}</b></div>`);
      }
      prog = `<div class="prog${gj.progress_stale?" stale":""}">${parts.join("")}</div>`;
    }
    // HOST · CPU block — CPU analog of the GPU thermal block, mirrors the Jinja
    // renderer. Present only when a fresh host-<hostname>.json fed host_* metrics;
    // each tile gated on its own key. Dims (not hides) when host_stale. Zero new colors.
    let hostblk = "";
    if(gj.host_temp_c!=null || gj.host_load1!=null || gj.host_cpu_util_pct!=null || gj.host_mem_used_mb!=null){
      const hNearCrit = gj.host_temp_crit_c!=null && gj.host_temp_c!=null && gj.host_temp_c >= gj.host_temp_crit_c - 5;
      const hWarm = gj.host_temp_high_c!=null && gj.host_temp_c!=null && gj.host_temp_c >= gj.host_temp_high_c;
      const hBusy = gj.host_cpu_util_pct!=null && gj.host_cpu_util_pct >= 95;
      const hLoaded = gj.host_cpu_util_pct!=null && gj.host_cpu_util_pct >= 80;
      const hClass = (hNearCrit||hBusy) ? "overexposed" : ((hWarm||hLoaded) ? "safelight" : "");
      let head = "host · cpu";
      if(hClass) head += ` <span class="chip ${hClass}">${(hNearCrit||hWarm)?"hot":"busy"}</span>`;
      if(gj.host_stale) head += ` <span class="chip unexposed">stale</span>`;
      const hst = [];
      if(gj.host_temp_c!=null){
        let cum = "";
        if(gj.host_temp_high_c!=null) cum = ` <span class="cum">/ ${esc(gj.host_temp_high_c)}°${gj.host_temp_crit_c!=null?` · ${esc(gj.host_temp_crit_c)}° crit`:""}</span>`;
        hst.push(`<span class="stat"><b>${esc(gj.host_temp_c)}°${cum}</b><span>cpu temp</span></span>`);
      }
      if(gj.host_cpu_util_pct!=null) hst.push(`<span class="stat"><b>${esc(gj.host_cpu_util_pct)}%</b><span>cpu util</span></span>`);
      if(gj.host_load1!=null) hst.push(`<span class="stat"><b>${esc(gj.host_load1)}${gj.host_nproc!=null?` <span class="cum">/ ${esc(gj.host_nproc)}</span>`:""}</b><span>load1${gj.host_nproc!=null?" · nproc":""}</span></span>`);
      if(gj.host_mem_used_mb!=null) hst.push(`<span class="stat"><b>${esc(gj.host_mem_used_mb)}${gj.host_mem_avail_mb!=null?` <span class="cum">/ ${esc(gj.host_mem_avail_mb)} free</span>`:""}</b><span>mem mb used</span></span>`);
      if(gj.host_freq_mhz!=null) hst.push(`<span class="stat"><b>${esc(gj.host_freq_mhz)}${gj.host_freq_max_mhz!=null?` <span class="cum">/ ${esc(gj.host_freq_max_mhz)}</span>`:""}</b><span>freq mhz</span></span>`);
      if(gj.host_fan_rpm!=null) hst.push(`<span class="stat"><b>${esc(gj.host_fan_rpm)}</b><span>max fan rpm</span></span>`);
      if(gj.host_fan1_rpm!=null) hst.push(`<span class="stat"><b>${esc(gj.host_fan1_rpm)}</b><span>fan1 rpm</span></span>`);
      if(gj.host_fan2_rpm!=null) hst.push(`<span class="stat"><b>${esc(gj.host_fan2_rpm)}</b><span>cpu fan rpm</span></span>`);
      if(gj.host_fan4_rpm!=null) hst.push(`<span class="stat"><b>${esc(gj.host_fan4_rpm)}</b><span>fan4 rpm</span></span>`);
      if(gj.host_cooling_state!=null) hst.push(`<span class="stat"><b>${esc(String(gj.host_cooling_state).toUpperCase())}</b><span>cooling state</span></span>`);
      if(gj.host_fancontrol_active!=null) hst.push(`<span class="stat"><b>${gj.host_fancontrol_active?"ACTIVE":"DOWN"}</b><span>fancontrol</span></span>`);
      if(gj.host_pwm_full!=null) hst.push(`<span class="stat"><b>${gj.host_pwm_full?"FULL":"VARIABLE"}</b><span>fan posture</span></span>`);
      if(gj.host_thermal_throttle_count!=null) hst.push(`<span class="stat"><b>${esc(gj.host_thermal_throttle_count)}</b><span>thermal throttles</span></span>`);
      if(gj.host_thermal_guard_action) hst.push(`<span class="stat"><b>${esc(gj.host_thermal_guard_action)}</b><span>guard action</span></span>`);
      if(gj.host_power_w!=null) hst.push(`<span class="stat"><b>${esc(gj.host_power_w)}</b><span>power w</span></span>`);
      hostblk = `<div class="prog${gj.host_stale?" stale":""}"><div class="prog-head">${head}</div><div class="jobstats">${hst.join("")}</div></div>`;
    }
    // logical-job aggregate — one job across all its stops/fails/restarts
    let agg = "";
    if(gj.attempts_count){
      const oc = gj.outcomes_summary ? ` · ${esc(gj.outcomes_summary)}` : "";
      if(gj.state === "running"){
        if(gj.attempts_count > 1)
          agg = `<div class="job-agg">attempt ${esc(gj.attempts_count)}`
              + `${gj.prior_attempts?` · ${esc(gj.prior_attempts)} prior`:""}`
              + ` · <b>${esc(gj.active_time)}</b> active so far${oc}</div>`;
      } else {
        const s = gj.attempts_count!=1 ? "s" : "";
        agg = `<div class="job-agg finished">ran <b>${esc(gj.active_time)}</b> active across `
            + `${esc(gj.attempts_count)} attempt${s} · ${esc(gj.wall_span)} wall-clock${oc}</div>`;
      }
    }
    const beat = gj.beat_age ? `<div class="job-beat">last beat <span class="fresh">${esc(gj.beat_age)}</span>`
      + `${gj.phase?` · phase ${esc(gj.phase)}`:""}</div>` : "";
    const detail = gj.detail ? `<div class="job-detail">${esc(gj.detail)}</div>` : "";
    const msg = gj.message ? `<div class="job-last" title="${esc(gj.message)}">${esc(gj.message)}</div>` : "";
    const srcDiv = gj.source ? `<div class="job-src">source · ${esc(gj.source)}</div>` : "";
    // dashboard-only: mark-done lives inside the expanded body now (see
    // templates/_job_row.html's show_markdone) — /jobs never renders it.
    const actions = (gj.state === "running" || gj.state === "stalled")
      ? `<div class="job-actions"><button class="markdone" onclick="markJobDone(${JSON.stringify(gj.job)}, this)">mark done</button></div>` : "";
    return `<div class="jobbody">${actions}${srcDiv}${bar}${queueStrip}${gpuStrip}<div class="jobstats">${stats.join("")}</div>${metricsblk}${prog}${hostblk}${agg}${beat}${detail}${msg}</div>`;
  }
  // jobSummaryHTML: the collapsed <summary> row — job id/host, pct (when a
  // total/done pair exists), state chip, beat age. Mirrors _job_row.html's
  // macro exactly so first paint (Jinja) and a live refresh (this file) never
  // diverge (FLEET-WORKER2-BUILD-20260721-panel-dashboard-compact-jobs).
  function jobSummaryHTML(gj, a){
    const host = gj.host ? `<span class="job-host">${esc(gj.host)}</span>` : "";
    const pct = (gj.total && gj.done!=null) ? `<span class="job-pct">${gj.pct!=null?esc(gj.pct):"—"}%</span>` : "";
    const age = gj.beat_age ? `<span class="job-age">${esc(gj.beat_age)}</span>` : "";
    return `<summary class="job-summary">`
      + `<span class="job-summary-id"><span class="node-name">${esc(gj.job||"job")}</span>${host}</span>`
      + `<span class="job-summary-status">${pct}<span class="chip ${a}">${esc(gj.state)}</span>${age}</span>`
      + `</summary>`;
  }
  function jobCardOuterHTML(gj){
    const a = STATE_ACCENT[gj.state] || "unexposed";
    return `<details class="frame work gpujob job-row ${a}" data-sheet-route="/jobs/${encodeURIComponent(gj.job||"")}">${jobSummaryHTML(gj, a)}${jobCardHTML(gj)}</details>`;
  }
  // buildFn: brand-new job row, jobCardOuterHTML reused verbatim (no restyle).
  function buildJobCard(gj){
    var wrap = document.createElement("div");
    wrap.innerHTML = jobCardOuterHTML(gj);
    return wrap.firstElementChild;
  }
  // updateFn: patch the volatile content of an EXISTING job row in place —
  // never recreating the <details data-sheet-route> node itself, so the
  // T3 deep-link/bottom-sheet route, the .frame accent box-shadow transition,
  // any tap-focus on the row, AND the native disclosure's own open/closed
  // state (an attribute on this same node, untouched by an innerHTML swap of
  // its children) all survive a refresh. jobCardHTML's body has ~12
  // independently-conditional blocks (bar/queues/gpus/metrics/thermal/
  // tripwire/host/agg/beat/detail/msg) that can each appear or vanish between
  // polls, so the inner content is refreshed as a unit rather than
  // field-by-field; there is no CSS width-transition on .pbar > i (only a
  // shimmer animation) to lose by doing so.
  function updateJobCard(node, gj){
    const a = STATE_ACCENT[gj.state] || "unexposed";
    node.className = "frame work gpujob job-row " + a;
    node.innerHTML = jobSummaryHTML(gj, a) + jobCardHTML(gj);
  }

  // Non-job heartbeat classification — mirror of app/config.py's
  // JOB_NONJOB_KINDS and app/routes.py's _is_watch_guard(). /api/status
  // (unlike the server-rendered dashboard route) returns the raw snapshot
  // with watch/guard sidecars still in it, so a live refresh must apply the
  // same classification the first-paint Jinja render already got, or a
  // sidecar like thermal-guard-charlie re-appears in the panel post-refresh
  // (FLEET-WORKER2-BUILD-20260721-panel-dashboard-jobs-refresh-filter).
  // Classification is by explicit kind/type only — missing/unrecognized kind
  // is job-compatible (not filtered); the pre-kind-field legacy-id fallback
  // was retired once every live producer either sets kind/type or was
  // archived out of heartbeats/ (PANEL-2 final compat retirement,
  // FLEET-WORKER2-BUILD-20260723-slate2-final-compat-retirement).
  const JOB_NONJOB_KINDS = {watch:1, guard:1};
  const JOB_KIND_VALUES = {job:1, watch:1, guard:1};
  function isWatchGuardJob(gj){
    const kind = gj.kind || gj.type;
    if(typeof kind === "string" && JOB_KIND_VALUES[kind]) return !!JOB_NONJOB_KINDS[kind];
    return false;
  }

  // render-only cap for the top live jobs panel — mirror of Jinja's
  // work.jobs[:JOBS_PANEL_MAX].
  const JOBS_PANEL_MAX = 10;
  // mirror of app/routes.py _job_sort_key: running/stalled first, then newest→oldest.
  // A finished job's recency prefers ended_at (real completion time) over
  // started, so a job done for days can't outrank one that just finished.
  function sortJobsForPanel(list){
    var ACTIVE = {running:1, stalled:1};
    function parseEpoch(v){
      if(typeof v === 'number') return v;
      if(typeof v === 'string' && v){ var t = Date.parse(v); if(!isNaN(t)) return t/1000; }
      return null;
    }
    function epoch(j, finished){
      var e = finished ? parseEpoch(j.ended_at) : null;
      if(e===null) e = parseEpoch(j.started);
      if(e===null && typeof j.beat_age_s === 'number') e = (Date.now()/1000) - j.beat_age_s;
      return e;
    }
    return list.slice().sort(function(a,b){
      var aa = ACTIVE[a.state]?0:1, ab = ACTIVE[b.state]?0:1;
      if(aa!==ab) return aa-ab;
      var ea = epoch(a, aa===1), eb = epoch(b, ab===1);
      return ((ea===null)?0:-ea) - ((eb===null)?0:-eb);
    });
  }
  // T4: reconciled keyed on gj.job (unique/stable — see app/routes.py's
  // {j["job"]: j for j in jobs} dict-keying — and the same value the
  // data-sheet-route attribute is derived from) rather than full-innerHTML
  // replacement.
  function renderJobs(jt, jobs){
    if(!jt) return;
    const realJobs = (jobs||[]).filter(function(gj){ return !isWatchGuardJob(gj); });
    if(!realJobs.length){
      jt.innerHTML = `<p class="empty">No job heartbeats — drop a <code>heartbeats/&lt;job&gt;.json</code> and it appears here.</p>`;
      return;
    }
    reconcile(jt, sortJobsForPanel(realJobs).slice(0, JOBS_PANEL_MAX), gj => gj.job, buildJobCard, updateJobCard);
  }

  // ---- relay_runs (keyed reconcile — the proof case; token is a true
  // unique id, app/work.py:354) ----
  function relayRunLiInner(r){
    return liInner(`<a class="wl-link" href="/activity/workers/${esc(r.token)}">${esc(r.token)}</a>`,
      `<span class="wl-seat">${esc(r.seat)}</span> `
      + `<span class="state-${esc(r.state)}">${esc(r.state)}</span> · ${esc(r.age)}`);
  }
  function relayRunLiHTML(r){ return `<li>${relayRunLiInner(r)}</li>`; }
  function buildRelayRunLi(r){
    var wrap = document.createElement("div");
    wrap.innerHTML = relayRunLiHTML(r);
    return wrap.firstElementChild;
  }
  function updateRelayRunLi(node, r){ node.innerHTML = relayRunLiInner(r); }
  const WORKER_ACTIVITY_PANEL_MAX = Number(
    window.__NEXUS_BOOTSTRAP__.workerActivityPanelMax
  ) || 5;

  var lastGeneratedAt = null;
  var scanPassTimer = null;
  function triggerScanPass(){
    if(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    var screen = document.querySelector(".screen");
    if(!screen) return;
    screen.classList.remove("scan-pass");
    void screen.offsetWidth;
    screen.classList.add("scan-pass");
    clearTimeout(scanPassTimer);
    scanPassTimer = setTimeout(function(){
      screen.classList.remove("scan-pass");
    }, 2400);
  }

  async function refresh(signal){
    try{
      const r = await fetch("/api/status", {cache:"no-store", signal});
      if(!r.ok) return;
      const snap = await r.json();
      const nextGeneratedAt = snap.generated_at || null;
      const changedSnapshot = Boolean(lastGeneratedAt && nextGeneratedAt && nextGeneratedAt !== lastGeneratedAt);
      const oa = ACCENT[snap.overall] || "unexposed";
      // The registry-backed system-status poller exclusively owns the eye;
      // this snapshot continues to drive the ambient fleet treatment.
      const wrap = document.getElementById("overall-wrap");
      if(wrap) wrap.className = "overall " + oa;
      const amb = document.getElementById("ambientTint");
      if(amb) amb.className = "ambient-tint " + oa;
      if(nextGeneratedAt){
        const d = new Date(nextGeneratedAt);
        document.getElementById("stamp").textContent = formatCentralStamp(d);
      }
      renderContact(snap.nodes);
      renderJobs(document.getElementById("jobstable"), snap.work ? snap.work.jobs : null);
      var relCol = document.querySelector('#relaycol ul.worklist');
      if(relCol && snap.work && snap.work.relay_runs && snap.work.relay_runs.runs){
        reconcile(
          relCol,
          snap.work.relay_runs.runs.slice(0, WORKER_ACTIVITY_PANEL_MAX),
          r => r.token,
          buildRelayRunLi,
          updateRelayRunLi
        );
      }
      // Mobile header warning ticker — rebuilt from the same snapshot so it
      // never diverges from the server first-paint after the first refresh.
      const wt = document.getElementById("warnticker");
      if(wt){ wt.innerHTML = warnTickerHTML(snap); layoutWarnTicker(); }
      // Hand the authoritative seat sweep to the availability strip (it reconciles
      // any WS-driven override against it and re-renders).
      if(window.SeatBoard) window.SeatBoard.applySnap(snap.seats);
      lastGeneratedAt = nextGeneratedAt;
      if(changedSnapshot) triggerScanPass();
    }catch(e){/* keep last good render */}
  }

  // Manual "mark done": close + mute the job server-side, then re-render the
  // jobs panel off the fresh snapshot. A genuine relaunch (new PID) auto-un-mutes.
  async function markJobDone(job, btn){
    if(!confirm(`Mark "${job}" done?\n(a genuine relaunch — new PID — re-opens it automatically)`)) return;
    if(btn){ btn.disabled = true; btn.textContent = "…"; }
    try{
      await fetch("/api/jobs/"+encodeURIComponent(job)+"/done", {method:"POST"});
      await refresh();   // same poller the jobs panel already re-renders through
    }catch(e){ if(btn){ btn.disabled=false; btn.textContent="mark done"; } }
  }

  async function developNow(event){
    const controls = Array.from(document.querySelectorAll("[data-refresh-control]"));
    const btn = event && event.currentTarget ? event.currentTarget : controls[0];
    if(!btn || btn.disabled) return;
    controls.forEach((control) => {
      control.disabled = true;
      control.classList.add("is-scanning");
      control.setAttribute("aria-label", "Refreshing fleet status");
    });
    try{ await fetch("/api/run/heartbeat", {method:"POST"}); await refresh(); }
    finally{
      controls.forEach((control) => {
        control.disabled = false;
        control.classList.remove("is-scanning");
        control.setAttribute("aria-label", "Refresh fleet status");
      });
    }
  }

  // Ticking Central-time clock in the header. Started/stopped by the central polling
  // controller (end of file, T5) rather than free-running here, so it stops
  // writing the DOM every second while the tab/PWA is hidden.
  function tickClock(){
    const c = document.getElementById("clock");
    if(!c) return;
    c.textContent = formatCentralClock(new Date());
  }

  // /api/status is fresh-per-request (seat strip recomputed from local reads
  // each call), so polling at the same ~15s cadence the SeatBoard ticks its ETA
  // makes a BUSY seat's done/total climb every poll and the bar visibly move.
  // refresh() now takes an optional AbortSignal so the central polling
  // controller (end of file) can cancel an in-flight GET on tab-hide; its own
  // 15s driving setInterval moved there too, alongside pollUnread()/history
  // load() and SeatBoard's render() tick (T5 consolidation).
;
// --- Health timeline: 24h fleet/per-node stepped state bands (HEALTH-
// TIMELINE-2, supersedes the T1-1 sparkline strip). /api/history is a
// separate, coarser-cadence source (5-min rows) from /api/status's 15s
// poll, so this owns its own fetch/render loop rather than piggybacking
// refresh() above; /api/health-timeline is the derived 24h projection
// (cadence coverage, incidents, streaks) computed server-side so this file
// never re-implements that math. Rides the central polling controller's 60s
// subcadence (see window.__NEXUS_TIMELINE__ at the bottom) — no independent
// setInterval here.
(function(){
  "use strict";
  var NODE_META = window.__NEXUS_BOOTSTRAP__.nodeMeta;
  var stripEl = document.getElementById("timelineStrip");
  var summaryEl = document.getElementById("timelineSummary");
  if(!stripEl || !NODE_META.length) return;

  var CADENCE_MS = 300 * 1000;    // mirrors settings.heartbeat_interval_seconds
  var GAP_MS = CADENCE_MS * 1.5;  // wider than this = a missed beat, not sampling jitter

  function fmtDurationAgo(seconds){
    if(seconds == null || !isFinite(seconds)) return "—";
    if(seconds < 90) return Math.max(0, Math.round(seconds)) + "s ago";
    var mins = Math.round(seconds/60);
    if(mins < 90) return mins + "m ago";
    return Math.round(mins/60) + "h ago";
  }
  function fmtMs(ms){ return (ms == null) ? "—" : Math.round(ms) + "ms"; }
  function fmtPct(pct){ return (pct == null) ? "—" : pct + "%"; }
  function withCount(n, word){ return n + " " + word + (n === 1 ? "" : "s"); }
  function wordSuffix(n, word){ return word + (n === 1 ? "" : "s"); }

  // ---- Stepped bands: time-proportional segments built from the RAW
  // /api/history rows (never the derived projection) — one segment per
  // sample-to-sample interval, colored by the EARLIER sample so a color is
  // never attributed to a reading that didn't produce it. A gap wider than
  // GAP_MS renders as one cadence-width tick of the known state followed by
  // an explicit gap/unknown segment — never a color interpolated across
  // time nothing was actually sampled. The final segment always extends to
  // "now", so a stale scan visibly trails off into a gap rather than
  // silently reading as continued health.
  function buildSegments(rows, name){
    var segs = [];
    if(!rows.length) return segs;
    var extended = rows.concat([{t: new Date().toISOString()}]);
    for(var i=0;i<extended.length-1;i++){
      var t0 = new Date(rows[i].t).getTime();
      var t1 = new Date(extended[i+1].t).getTime();
      if(!isFinite(t0) || !isFinite(t1) || t1 <= t0) continue;
      var state = (rows[i].nodes && rows[i].nodes[name]) || "unknown";
      if((t1 - t0) > GAP_MS){
        segs.push({start:t0, end:t0+CADENCE_MS, state:state, gap:false});
        segs.push({start:t0+CADENCE_MS, end:t1, state:"unknown", gap:true});
      } else {
        segs.push({start:t0, end:t1, state:state, gap:false});
      }
    }
    return segs;
  }

  function segmentsHTML(segs){
    return segs.map(function(s){
      var dur = Math.max(1, s.end - s.start);
      var acc = ACCENT[s.state] || "unexposed";
      var cls = "ht-seg " + acc + (s.gap ? " ht-seg-gap" : "");
      return '<span class="'+cls+'" style="flex:'+dur+' 0 0"></span>';
    }).join("");
  }

  function nodeDetailHTML(name, np, corrList){
    if(!np) return '<p class="empty">no data in window</p>';
    var counts = np.counts_by_state || {};
    var countsLine = ["ok","warn","crit","unknown"].map(function(k){
      return (counts[k]||0) + " " + k;
    }).join(" · ");
    var inc = np.last_incident, incLine;
    if(!inc){
      incLine = "no incidents in this window";
    } else {
      var durMin = Math.max(0, Math.round(inc.duration_seconds/60));
      var causeTxt = "cause not retained";
      if(inc.cause){
        var c = inc.cause;
        causeTxt = esc([c.kind, c.value].filter(Boolean).join(" · ")) || "cause not retained";
      }
      incLine = (inc.recovered ? "last incident" : "ongoing incident") + " · " + esc(inc.peak_state)
        + " · " + withCount(durMin, "min") + " · " + causeTxt;
    }
    var corr = (corrList||[]).filter(function(c){ return c.nodes.indexOf(name) !== -1; });
    var corrLine = corr.length ? "correlated transition with "
      + esc(corr[corr.length-1].nodes.filter(function(n){ return n!==name; }).join(", "))
      + " · " + esc(corr[corr.length-1].t_central) : "";
    return '<dl class="ht-details-grid">'
      + '<dt>counts (24h)</dt><dd>'+esc(countsLine)+'</dd>'
      + '<dt>incident</dt><dd>'+incLine+'</dd>'
      + (np.last_recovery_at ? '<dt>last recovery</dt><dd>'+esc(formatCentralStamp(new Date(np.last_recovery_at)))+' CT</dd>' : '')
      + (corrLine ? '<dt>note</dt><dd>'+corrLine+'</dd>' : '')
      + '</dl>';
  }

  function rowHTML(meta, rows, proj){
    var latest = rows.length ? (rows[rows.length-1].nodes||{})[meta.name] : null;
    var state = latest || "unknown";
    var acc = ACCENT[state] || "unexposed";
    var np = proj && proj.nodes ? proj.nodes[meta.name] : null;
    var segs = buildSegments(rows, meta.name);
    var healthyTxt = np ? fmtPct(np.healthy_pct) : "—";
    var incTxt = np ? np.incident_count : "—";
    var sinceTxt = np ? fmtDurationAgo((Date.now() - new Date(np.current_state_since).getTime())/1000) : "—";
    var corrList = (proj && proj.correlated_incidents) || [];
    return '<article class="ht-row '+acc+'" data-node="'+esc(meta.name)+'">'
      + '<div class="ht-row-head">'
      + '<span class="ht-name">'+esc(meta.label)+'</span>'
      + '<span class="ht-chip">'+esc(state)+'</span>'
      + '<span class="ht-metric">'+healthyTxt+' healthy</span>'
      + '<span class="ht-metric">'+incTxt+' inc</span>'
      + '<span class="ht-metric">stable '+sinceTxt+'</span>'
      + '</div>'
      + '<div class="ht-band-wrap">'
      + '<div class="ht-band" role="img" aria-label="'+esc(meta.label)+' 24 hour health band, currently '+esc(state)+'">'+segmentsHTML(segs)+'</div>'
      + '<div class="ht-readout" aria-hidden="true"></div>'
      + '</div>'
      + '<div class="ht-ticks" aria-hidden="true"><span>24h</span><span>12h</span><span>now</span></div>'
      + '<details class="ht-details-toggle"><summary>details</summary>'+nodeDetailHTML(meta.name, np, corrList)+'</details>'
      + '</article>';
  }

  function renderSummary(proj, haveRows){
    if(!proj || !proj.received_samples){
      summaryEl.innerHTML = haveRows
        ? '<p class="empty">summary unavailable — history recording continues.</p>'
        : '<p class="empty">no history yet — not enough samples to summarize.</p>';
      return;
    }
    var incidentTotal = 0;
    if(proj.nodes){
      Object.keys(proj.nodes).forEach(function(n){ incidentTotal += (proj.nodes[n].incident_count||0); });
    }
    var dm = proj.duration_ms || {};
    summaryEl.innerHTML = '<div class="ht-summary-row">'
      + '<span class="ht-stat"><b>'+proj.nodes_healthy_now+'/'+proj.nodes_total+'</b><span>healthy now</span></span>'
      + '<span class="ht-stat"><b>'+fmtPct(proj.overall_healthy_pct)+'</b><span>24h healthy</span></span>'
      + '<span class="ht-stat"><b>'+incidentTotal+'</b><span>'+esc(wordSuffix(incidentTotal,"incident"))+'</span></span>'
      + '<span class="ht-stat"><b>'+fmtPct(proj.cadence_coverage_pct)+'</b><span>cadence coverage</span></span>'
      + '<span class="ht-stat" title="median '+fmtMs(dm.median)+' · p95 '+fmtMs(dm.p95)+' · max '+fmtMs(dm.max)+'"><b>'+fmtMs(dm.current)+'</b><span>last sweep</span></span>'
      + '<span class="ht-stat"><b>'+fmtDurationAgo(proj.scan_age_seconds)+'</b><span>'+esc(proj.scan_generated_at_central||"—")+(proj.scan_is_fresh ? '' : ' · stale')+'</span></span>'
      + '</div>';
  }

  var lastRows = [], lastProjection = null;

  function render(rows, proj){
    lastRows = rows != null ? rows : lastRows;
    lastProjection = proj != null ? proj : lastProjection;
    renderSummary(lastProjection, lastRows.length > 0);
    if(!lastRows.length){
      stripEl.innerHTML = '<p class="empty">no history yet — the fleet hasn’t scanned enough to draw a timeline.</p>';
      return;
    }
    stripEl.innerHTML = NODE_META.map(function(m){ return rowHTML(m, lastRows, lastProjection); }).join("");
  }

  function load(signal){
    var historyP = fetch("/api/history?limit=288", {cache:"no-store", signal})
      .then(function(r){ return r.ok ? r.json() : null; })
      .catch(function(){ return null; });
    var projP = fetch("/api/health-timeline?hours=24", {cache:"no-store", signal})
      .then(function(r){ return r.ok ? r.json() : null; })
      .catch(function(){ return null; });
    return Promise.all([historyP, projP]).then(function(results){
      render(results[0], results[1]);
    });
  }

  // ---- Scrub/tap readout — Central time, retained cause when available.
  // Passive + rAF-throttled listeners only (no preventDefault anywhere), so
  // page scroll is never blocked. A TOUCH tap keeps the readout open until
  // another tap, an outside interaction, or the next render — unlike a
  // mouse drag, it is deliberately NOT dismissed on pointerup/pointerleave.
  function nearestRow(rows, ratio){
    if(!rows.length) return null;
    var idx = Math.round(ratio * (rows.length - 1));
    return rows[Math.max(0, Math.min(rows.length - 1, idx))];
  }
  var scrubActive = null;
  function hideScrub(){
    if(scrubActive){ scrubActive.classList.remove("show"); scrubActive = null; }
  }
  function paintScrub(ev){
    var band = ev.target.closest && ev.target.closest(".ht-band");
    if(!band || !lastRows.length) return;
    var row = band.closest(".ht-row");
    var name = row && row.getAttribute("data-node");
    var ro = row && row.querySelector(".ht-readout");
    if(!name || !ro) return;
    var rect = band.getBoundingClientRect();
    if(!rect.width) return;
    var localX = Math.min(rect.width, Math.max(0, ev.clientX - rect.left));
    var pt = nearestRow(lastRows, localX / rect.width);
    if(!pt) return;
    var health = (pt.nodes && pt.nodes[name]) || "unknown";
    var causeTxt = "";
    if(pt.sample_version && pt.issues && pt.issues[name] && pt.issues[name].length){
      var first = pt.issues[name][0];
      causeTxt = " · " + esc([first.kind, first.value].filter(Boolean).join(" "));
    } else if(health !== "ok"){
      causeTxt = " · cause not retained";
    }
    ro.textContent = health + causeTxt + " · " + formatCentralStamp(new Date(pt.t)) + " CT";
    ro.style.left = (band.offsetLeft + localX) + "px";
    ro.classList.add("show");
    if(scrubActive && scrubActive !== ro) scrubActive.classList.remove("show");
    scrubActive = ro;
  }
  var scrubPending = false, scrubLastEvent = null;
  function queueScrub(ev){
    scrubLastEvent = ev;
    if(scrubPending) return;
    scrubPending = true;
    requestAnimationFrame(function(){ scrubPending = false; paintScrub(scrubLastEvent); });
  }
  function isTouch(ev){ return ev.pointerType === "touch"; }
  stripEl.addEventListener("pointerdown", function(ev){
    if(ev.target.closest && ev.target.closest(".ht-band")) queueScrub(ev);
  }, {passive:true});
  stripEl.addEventListener("pointermove", function(ev){
    if(ev.buttons || (scrubActive && isTouch(ev))) queueScrub(ev);
  }, {passive:true});
  stripEl.addEventListener("pointerup", function(ev){ if(!isTouch(ev)) hideScrub(); }, {passive:true});
  stripEl.addEventListener("pointercancel", hideScrub, {passive:true});
  stripEl.addEventListener("pointerleave", function(ev){ if(!isTouch(ev)) hideScrub(); }, {passive:true});
  // A tap OUTSIDE any band dismisses a touch-held-open readout.
  document.addEventListener("pointerdown", function(ev){
    if(!scrubActive) return;
    if(stripEl.contains(ev.target) && ev.target.closest && ev.target.closest(".ht-band")) return;
    hideScrub();
  }, {passive:true});

  // Driven by the central polling controller (end of file) on a 60s
  // subcadence, not its own setInterval — see T5 consolidation.
  window.__NEXUS_TIMELINE__ = {load: load};
})();
;
// --- per-node availability strip (legacy worker ids remain source aliases) ---
// Renders FREE/BUSY tiles from the authoritative sweep (snap.seats), live-ticks
// the ETA between sweeps, ages finished tiles out at 15 min, and flips a tile
// INSTANTLY off the live-seat WebSocket (SessionStart -> BUSY, Stop/SessionEnd ->
// finished/FREE) — reconciling against the sweep when it next lands. The ad-hoc
// local worker2 session (seat "worker2", no relay- prefix, no token) lights the Worker2
// tile as "in session (local)". Own IIFE; exposes window.SeatBoard for the status
// poller (applySnap) and the WS block (flip/seedBackfill) to drive it.
(function(){
  "use strict";
  var COLORS = {charlie:"amber", delta:"cyan", alpha:"green", localworker:"emerald", worker4:"gold"};
  var LABELS = {charlie:"charlie", delta:"delta", alpha:"alpha", localworker:"Localworker", worker4:"Model Usage"};
  var NODES  = {charlie:"", delta:"", alpha:"", localworker:"", worker4:"cloud quotas"};
  var ORDER  = ["charlie","delta","alpha","localworker"];
  var FRESH_MS = 15*60*1000;
  // inline-progress bar ages out at the same age the server gates on (job_stale_seconds)
  var INLINE_STALE_MS = (window.__NEXUS_BOOTSTRAP__.jobStaleSeconds) * 1000;
  var el = document.getElementById("seatstrip");
  var usageEl = document.getElementById("modelUsageStrip");
  var base = {};       // seat -> tile from the last authoritative sweep
  var override = {};   // seat -> WS-driven tile, bridges the ~5-min sweep gap
  var SEAT_INIT = window.__NEXUS_BOOTSTRAP__.seatInit;

  var esc = function(s){ return (s==null?"":String(s)).replace(/[&<>"]/g,function(c){
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]; }); };
  function mins(sec){ return Math.max(0, Math.round(sec/60)); }
  function displaySub(seat, t){
    if(Object.prototype.hasOwnProperty.call(t, "sub")) return t.sub;
    return NODES[seat] || "";
  }
  function kindOf(tok){ return !tok?null:(/-RECON-/i.test(tok)?"RECON":(/-BUILD-/i.test(tok)?"BUILD":null)); }
  function shortTok(tok){ return tok ? tok.replace(/^FLEET-.*?\d{8}-/,"") : null; }

  // Same seat-family logic the live-seat transport uses; relay runs carry seat "relay-<seat>".
  function famSeat(ev){
    var host=(ev.host||"").toLowerCase(), seat=(ev.seat||"").toLowerCase();
    if(seat.indexOf("localworker")>=0) return "localworker";
    if(host.indexOf("worker2")>=0 || host.indexOf("alpha")>=0 || seat.indexOf("worker2")>=0 || seat.indexOf("alpha")>=0) return "alpha";
    if(host.indexOf("charlie")>=0 || seat.indexOf("worker3")>=0 || seat.indexOf("charlie")>=0) return "charlie";
    return "delta";  // mini/delta/worker1/cc
  }
  function isRelay(ev){ return /^relay-/.test((ev.seat||"").toLowerCase()); }

  // The tile's context line — kept in lockstep with seatboard._primary_line so a
  // live render and a fresh page load read identically.
  function primaryOf(t){
    if(t.usage_card) return t.primary || "quota sources unavailable";
    var k = t.kind || "run", now = Date.now();
    if(t.state==="local"){
      return "in session (local) · "+mins(t.started_ms?(now-t.started_ms)/1000:0)+"m elapsed";
    }
    if(t.state==="busy" || t.state==="running"){
      var el2 = t.started_ms ? (now-t.started_ms)/1000 : 0;
      // Fresh inline progress → real (progress-derived) ETA, counted DOWN from the
      // server's eta_s snapshot; no "est." tag (it's measured, not guessed).
      if(t.inline){
        var inl=t.inline, lbl=inl.label?" · "+inl.label:"", prog=inl.done+"/"+inl.total;
        if(inl.eta_s==null) return "job in progress · "+k+lbl+" · "+prog;
        var irem = inl.eta_s - (now-(inl.eta_at_ms||now))/1000;
        if(irem<=0) return "job in progress · "+k+lbl+" · "+prog+" · finishing up…";
        return "job in progress · "+k+lbl+" · "+prog+" · ETA ~"+Math.max(1,mins(irem))+"m";
      }
      if(t.median_s==null) return "running · "+mins(el2)+"m elapsed";
      var rem = t.median_s - el2;
      if(rem<=0) return "job in progress · "+k+" · finishing up…";
      return "job in progress · "+k+" · ETA ~"+Math.max(1,mins(rem))+"m est.";
    }
    if(t.state==="done" || t.state==="died"){
      var ago = t.ended_ms ? (now-t.ended_ms)/1000 : 0;
      var when = mins(ago)===0 ? "just now" : mins(ago)+" min ago";
      return t.state==="died" ? "died "+when : "finished "+k+" "+when;
    }
    return "no recent runs";
  }
  function badgeOf(t){
    if(t.usage_card) return t.badge || "UNKNOWN";
    if(t.state==="died") return "DIED";
    if(t.state==="busy" || t.state==="running" || t.state==="local") return "BUSY";
    return "FREE";
  }

  // The tile to actually render: override if present, else the sweep — with two
  // time-decays applied so nothing sticks: a finished tile older than 15 min
  // reverts to idle/FREE; a WS override that never saw its Stop ages out too.
  function effective(seat){
    var t = override[seat] || base[seat];
    if(!t) return null;
    t = Object.assign({}, t);
    if(!t.provider_line && base[seat]) t.provider_line = base[seat].provider_line;
    var now = Date.now();
    // drop an inline bar whose last beat has gone stale between sweeps (mirrors
    // the server gate) — a wedged inline loop must not leave a frozen bar up.
    if(t.inline && t.inline.ts_ms && (now-t.inline.ts_ms)>INLINE_STALE_MS){
      t.inline = null;   // t is already a shallow copy (above) — safe to null the ref
    }
    if((t.state==="done"||t.state==="died") && t.ended_ms && (now-t.ended_ms)>FRESH_MS){
      t.state="idle"; t.token=null; t.full_token=null; t.kind=null; t.inline=null;
      t.model_badge=null;
    }
    if(override[seat] && (t.state==="busy"||t.state==="local"||t.state==="running")
       && (now-(t.at||0))>FRESH_MS){
      return base[seat] ? Object.assign({}, base[seat]) : {seat:seat, state:"idle"};
    }
    return t;
  }

  // The inline-progress bar — only on a busy tile carrying a fresh inline record.
  // Mirrors the server Jinja block so a live rebuild and a fresh page load match.
  function barHTML(t){
    if(!t.inline) return "";
    var inl=t.inline;
    var pct = inl.pct!=null ? inl.pct : (inl.total?100*inl.done/inl.total:0);
    pct = Math.max(0, Math.min(100, pct));
    var rate = inl.rate!=null ? (esc(inl.rate)+" "+esc(inl.unit||"")) : "";
    var label = inl.label ? '<span class="sp-label">'+esc(inl.label)+'</span>' : "";
    var right = label + (rate ? (label?" · ":"")+rate : "");
    return '<div class="seat-prog"><div class="sp-bar"><i style="width:'+pct+'%"></i></div>'
      + '<div class="sp-legend"><b>'+esc(inl.done)+'/'+esc(inl.total)+'</b>'
      + '<span class="sp-rate">'+right+'</span></div></div>';
  }
  function usageHTML(t){
    if(!t.usage_card || !t.usage_items) return "";
    var rows = "";
    for(var i=0;i<t.usage_items.length;i++){
      var item=t.usage_items[i], resets="";
      for(var j=0;j<(item.resets||[]).length;j++){
        resets += '<div class="model-usage-reset">'+esc(item.resets[j])+'</div>';
      }
      rows += '<div class="model-usage-item"><div class="model-usage-row">'
        + esc(item.usage||"") + '</div>' + resets + '</div>';
    }
    var routes = "";
    if(t.routing){
      ["strategy","worker"].forEach(function(lane){
        var route=t.routing[lane]||{};
        routes += '<div class="model-routing-row"><span>'+esc(lane)+'</span>'
          + '<b>'+esc(route.ok ? (route.model||route.provider||"available") : "unavailable")+'</b>'
          + (route.ok ? '<em>'+esc(route.state||"")+'</em>' : '')+'</div>';
      });
      routes = '<div class="model-routing-lines">'+routes+'</div>';
    }
    return '<div class="model-usage-lines">'+rows+'</div>'+routes;
  }
  function pctLabel(value){
    if(value==null || !isFinite(Number(value))) return null;
    value = Math.max(0, Math.min(100, Number(value)));
    return (value>0 && value<1 ? "&lt;1" : String(Math.round(value)))+"%";
  }
  function ageLabel(updatedMs){
    if(!updatedMs) return "unavailable";
    var minsAgo=Math.max(0,Math.floor((Date.now()-Number(updatedMs))/60000));
    return minsAgo+"m ago";
  }
  function providerSummaryHTML(provider){
    var name=provider.provider||"unknown";
    var rows="";
    [["5 hour",provider.five_hour_used],["weekly",provider.weekly_used]].forEach(function(row){
      var label=pctLabel(row[1]);
      if(label==null){
        rows += '<div class="model-quota-line"><span>'+esc(row[0])
          +'</span><em>unavailable</em></div>';
      }else{
        var width=Math.max(0,Math.min(100,Number(row[1])));
        rows += '<div class="model-quota-line"><span>'+esc(row[0])+'</span>'
          +'<i><u style="width:'+width+'%"></u></i><b>'+label+'</b></div>';
      }
    });
    return '<article class="model-provider-summary '+esc(name)+'">'
      +'<div class="model-panel-title"><span class="provider-mark"></span>'
      +'<b>'+esc((provider.label||name).toLowerCase())+'</b>'
      +'<time>'+esc(ageLabel(provider.updated_ms))+'</time></div>'
      +rows+'<p>'+esc(provider.source||"source unavailable")+'</p></article>';
  }
  function usageStripHTML(t){
    var head='<div class="model-usage-strip-head"><div>'
      +'<h2 id="modelUsageHeading">model usage</h2><span>cloud quotas</span></div>'
      +'<span class="model-usage-updated">'+esc(t ? primaryOf(t) : "quota sources unavailable")+'</span>'
      +'<a class="model-usage-more" href="/activity?tab=models">more →</a></div>';
    if(!t) return head;
    var routes="";
    var routing=t.routing||{}, worker=routing.worker||{};
    (routing.candidates||[]).forEach(function(route){
      var state=String(route.state||"RED").toLowerCase();
      routes += '<div class="model-route-line'+(route.selected?' selected':'')+'">'
        +'<span>'+esc(route.provider||"provider")+'</span>'
        +'<b>'+esc(route.model||"unavailable")+'</b>'
        +'<em class="state-'+esc(state)+'">'+esc(route.state||"RED")+'</em></div>';
    });
    var grid='<div class="model-usage-grid"><article class="model-route-summary">'
      +'<div class="model-panel-title"><span class="route-mark">◆</span><b>worker routing</b></div>'
      +'<div class="model-route-list">'+routes+'</div><p class="model-route-recommendation">recommended now · '
      +esc(worker.ok ? (worker.model||worker.provider||"available") : "unavailable")
      +'</p></article>';
    (t.provider_usage||[]).forEach(function(provider){
      grid += providerSummaryHTML(provider);
    });
    return head+grid+'</div>';
  }
  function renderUsageStrip(){
    if(!usageEl) return;
    usageEl.innerHTML=usageStripHTML(effective("worker4"));
  }
  function modelBadgeHTML(t){
    var m=t.model_badge;
    if(!m && t.seat==="localworker"){
      m={family:"local", mark:"⬡", label:"GPT-OSS 20B"};
    }
    if(!m) return "";
    if(t.seat!=="localworker" && !(t.state==="busy" || t.state==="running" || t.state==="local")) return "";
    return '<span class="seat-model-badge '+esc(m.family||"unknown")+'">'
      + '<span class="seat-model-logo" aria-hidden="true">'+esc(m.mark||"●")+'</span>'
      + '<span class="seat-model-name">'+esc(m.label||"Model")+'</span></span>';
  }
  function tileHTML(seat){
    var t = effective(seat) || {seat:seat, state:"idle"};
    var stateCls = (t.state==="running") ? "busy" : (t.state||"idle");
    var label = t.label || LABELS[seat] || seat;
    var sub   = displaySub(seat, t);
    var tok   = t.token ? ' · <a class="seat-tok-link" href="/activity/workers/'+esc(t.full_token)+'"><span class="seat-tok">'+esc(t.token)+'</span></a>' : '';
    var modelBadge = modelBadgeHTML(t);
    var usageMore = t.usage_card ? '<a class="seat-usage-more" href="/activity?tab=models">more →</a>' : '';
    return '<article class="seat-tile '+esc(seat)+' '+esc(stateCls)+(modelBadge?' has-model':'')+'" data-seat="'+esc(seat)+'">'
      + '<div class="seat-head"><span class="seat-name">'+esc(label)+'</span>'
      + '<span class="seat-sub">'+esc(sub)+'</span>'+usageMore+'</div>'
      + modelBadge
      + '<div class="seat-primary">'+esc(primaryOf(t))+tok+'</div>'
      + (t.usage_card ? usageHTML(t) : (t.provider_line ? '<div class="seat-provider">'+esc(t.provider_line)+'</div>' : ''))
      + barHTML(t)
      + '<span class="seat-badge">'+esc(badgeOf(t))+'</span></article>';
  }
  // buildFn: brand-new tile, tileHTML reused verbatim (no restyle). Only used
  // the first time a key appears — the ORDER set is fixed, so in practice
  // this only fires once per seat, on the very first render.
  function buildSeatTile(seat){
    var wrap = document.createElement("div");
    wrap.innerHTML = tileHTML(seat);
    return wrap.firstElementChild;
  }
  // updateFn: patch the volatile fields of an EXISTING tile in place — status
  // class, name/sub, primary countdown/ETA text, progress bar, badge — never
  // recreating the <article data-seat> node itself, so CSS transitions and
  // any focus on a tile survive a refresh.
  function updateSeatTile(node, seat){
    var t = effective(seat) || {seat:seat, state:"idle"};
    var stateCls = (t.state==="running") ? "busy" : (t.state||"idle");
    var label = t.label || LABELS[seat] || seat;
    var sub   = displaySub(seat, t);
    var tok   = t.token ? ' · <a class="seat-tok-link" href="/activity/workers/'+esc(t.full_token)+'"><span class="seat-tok">'+esc(t.token)+'</span></a>' : '';
    var modelBadge = modelBadgeHTML(t);
    node.className = "seat-tile "+esc(seat)+" "+esc(stateCls)+(modelBadge?" has-model":"");
    node.dataset.seat = seat;   // defensive invariant — keep identity correct even if a stray key mismatch ever slips through
    var nameEl = node.querySelector(".seat-name");
    if(nameEl) nameEl.textContent = label;
    var subEl = node.querySelector(".seat-sub");
    if(subEl) subEl.textContent = sub;
    var moreEl = node.querySelector(".seat-usage-more");
    if(t.usage_card && !moreEl){
      moreEl = document.createElement("a");
      moreEl.className = "seat-usage-more";
      moreEl.href = "/activity?tab=models";
      moreEl.textContent = "more →";
      node.querySelector(".seat-head").appendChild(moreEl);
    } else if(!t.usage_card && moreEl){
      moreEl.remove();
    }
    var primEl = node.querySelector(".seat-primary");
    if(primEl) primEl.innerHTML = esc(primaryOf(t))+tok;
    var modelEl = node.querySelector(".seat-model-badge");
    if(modelBadge){
      var modelTmp = document.createElement("div");
      modelTmp.innerHTML = modelBadge;
      if(modelEl) modelEl.replaceWith(modelTmp.firstElementChild);
      else node.insertBefore(modelTmp.firstElementChild, primEl);
    } else if(modelEl){
      modelEl.remove();
    }
    var providerEl = node.querySelector(".seat-provider");
    var usageEl = node.querySelector(".model-usage-lines");
    if(t.usage_card){
      var usage = usageHTML(t);
      var usageTmp = document.createElement("div");
      usageTmp.innerHTML = usage;
      if(usageEl) usageEl.replaceWith(usageTmp.firstElementChild);
      else if(providerEl) providerEl.replaceWith(usageTmp.firstElementChild);
      else node.insertBefore(usageTmp.firstElementChild, node.querySelector(".seat-badge"));
    } else {
      if(usageEl){
        var provider = document.createElement("div");
        provider.className = "seat-provider";
        usageEl.replaceWith(provider);
        providerEl = provider;
      }
      if(t.provider_line){
        if(!providerEl){
          providerEl = document.createElement("div");
          providerEl.className = "seat-provider";
          node.insertBefore(providerEl, node.querySelector(".seat-prog, .seat-badge"));
        }
        providerEl.textContent = t.provider_line;
      } else if(providerEl){
        providerEl.remove();
      }
    }
    var bar = barHTML(t);
    var progEl = node.querySelector(".seat-prog");
    var badgeEl = node.querySelector(".seat-badge");
    if(bar){
      var tmp = document.createElement("div");
      tmp.innerHTML = bar;
      if(progEl) progEl.replaceWith(tmp.firstElementChild);
      else node.insertBefore(tmp.firstElementChild, badgeEl);
    } else if(progEl){
      progEl.remove();
    }
    if(badgeEl) badgeEl.textContent = badgeOf(t);
  }
  // FREE<->BUSY (and any other state) flips ride the view-transition system
  // (2026-07-16 motion pass) — but ONLY when a tile's state class is actually
  // about to change; wrapping every 15s no-op refresh in a transition would
  // be pure overhead. Each seat tile carries a STATIC view-transition-name
  // (CSS, keyed by seat identity — see .seat-tile.worker1 etc above), so the
  // transition can name-match old vs new across the reconcile()'s in-place
  // className patch. Feature-detected + reduced-motion-gated like the sheet
  // morph; unsupported/opted-out clients get the exact same instant flip as
  // before this pass.
  function stateClsOf(seat){
    var t = effective(seat) || {seat:seat, state:"idle"};
    return (t.state==="running") ? "busy" : (t.state||"idle");
  }
  function anySeatFlipping(){
    for(var i=0;i<ORDER.length;i++){
      var seat = ORDER[i];
      var tile = el.querySelector('.seat-tile[data-seat="'+seat+'"]');
      if(tile && !tile.classList.contains(stateClsOf(seat))) return true;
    }
    return false;
  }
  // Seed each existing (server-rendered) tile's reconcile key from its own
  // data-seat BEFORE the generic reconcile() runs — that function hydrates
  // untracked first-paint children by pairing them 1:1, in order, against
  // `items`. The server renders seatstrip in snap.seats order while ORDER
  // folds Localworker second for display, so positional pairing can hand a
  // tile the wrong identity on the very first reconcile. Seeding from the
  // markup's own data-seat (already correct at first paint) sidesteps that
  // without touching the shared reconcile() used by other panels.
  function doReconcile(){
    var child = el.firstElementChild;
    while(child){
      if(child.__reconcileKey == null && child.dataset && child.dataset.seat){
        child.__reconcileKey = child.dataset.seat;
      }
      child = child.nextElementSibling;
    }
    reconcile(el, ORDER, function(seat){ return seat; }, buildSeatTile, updateSeatTile);
  }
  function render(){
    renderUsageStrip();
    var canVT = !!document.startViewTransition &&
      !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if(canVT && anySeatFlipping()){
      // .ready rejects if the transition gets skipped (tab hidden, overlapping
      // transition, etc.); .finished rejects if the animation itself fails.
      // Neither failure should surface as an unhandled rejection — doReconcile
      // already ran (or runs) as the update callback regardless of either.
      var vt = document.startViewTransition(doReconcile);
      vt.ready.catch(function(){});
      vt.finished.catch(function(){});
    } else {
      doReconcile();
    }
  }

  function applySnap(seatsObj){
    var arr = (seatsObj && seatsObj.seats) || [];
    base = {};
    for(var i=0;i<arr.length;i++) base[arr[i].seat] = arr[i];
    // Reconcile: drop a WS override once the sweep reflects the same/newer run
    // (sweep shows this seat busy, the same run finished, or the override is stale).
    for(var seat in override){
      var ov=override[seat], b=base[seat];
      if(!ov) continue;
      if((b && (b.state==="busy"||b.state==="running"))
         || (b && ov.full_token && b.full_token===ov.full_token)
         || (Date.now()-(ov.at||0) > FRESH_MS)) override[seat]=null;
    }
    render();
  }

  function setBusy(seat, ev){
    var tok = ev.run_token || null;   // relay hook events currently carry no token
    override[seat] = {seat:seat, color:COLORS[seat], label:LABELS[seat], sub:NODES[seat],
      state:"busy", kind:kindOf(tok), token:shortTok(tok), full_token:tok,
      started_ms:Date.parse(ev.ts)||Date.now(),
      median_s:(base[seat]&&base[seat].median_s)||null, at:Date.now()};
  }
  function setLocal(seat, ev){
    override[seat] = {seat:seat, color:COLORS[seat], label:LABELS[seat], sub:NODES[seat],
      state:"local", local:true, token:null, full_token:null,
      started_ms:Date.parse(ev.ts)||Date.now(), at:Date.now()};
  }

  function flip(ev){
    if(!ev) return;
    var seat = famSeat(ev), et = ev.event_type||"", relay = isRelay(ev);
    if(et==="SessionStart"){
      if(seat==="alpha" && !relay) setLocal(seat, ev);  // ad-hoc local CLI session
      else setBusy(seat, ev);                            // relay run (delta/charlie/alpha)
      render();
    } else if(et==="Stop" || et==="SessionEnd"){
      var ov = override[seat];
      if(ov && ov.local){ override[seat]=null; }         // local session ended -> FREE
      else {
        override[seat] = {seat:seat, color:COLORS[seat], label:LABELS[seat], sub:NODES[seat],
          state:"done", kind:(ov&&ov.kind)||null, token:(ov&&ov.token)||null,
          full_token:(ov&&ov.full_token)||null, ended_ms:Date.parse(ev.ts)||Date.now(),
          at:Date.now()};
      }
      render();
    } else {
      // Any other event is a liveness signal: light a seat whose SessionStart we
      // missed (page opened mid-run), but never re-light a just-finished tile.
      var ov2 = override[seat];
      if(ov2 && ov2.local){ ov2.at=Date.now(); return; }
      if(ov2 && (ov2.state==="done"||ov2.state==="died")) return;
      if(base[seat] && (base[seat].state==="busy"||base[seat].state==="running")) return;
      if(!ov2){
        if(seat==="alpha" && !relay) setLocal(seat, ev); else setBusy(seat, ev);
        render();
      }
    }
  }

  // On load, reconstruct current state from the backfill: per seat, if the LATEST
  // lifecycle event is a SessionStart (no later Stop/SessionEnd), that seat is live.
  function seedBackfill(list){
    var latest = {};
    for(var i=0;i<(list||[]).length;i++){
      var ev=list[i], et=ev.event_type||"";
      if(et!=="SessionStart" && et!=="Stop" && et!=="SessionEnd") continue;
      latest[famSeat(ev)] = ev;   // list is oldest->newest, so last write wins
    }
    for(var seat in latest){ if(latest[seat].event_type==="SessionStart") flip(latest[seat]); }
  }

  applySnap(SEAT_INIT);           // client takes over the server-rendered strip
  // No independent render()-ticking setInterval here (T5 consolidation): the
  // central polling controller's own 15s /api/status tick already calls
  // applySnap(snap.seats), which calls render() at the end — a second timer
  // on the same cadence was pure duplicate DOM churn, not a second data
  // source. render() stays exposed below for that path (and any other
  // caller) to invoke directly.
  window.SeatBoard = {applySnap:applySnap, flip:flip, seedBackfill:seedBackfill, render:render};
})();
;
// --- live seat transport (WebSocket) -----------------------------------------
// The scan-log feed UI is gone; this is the minimal transport carve-out that
// keeps the seat strip (window.SeatBoard) near-real-time: one WS connection
// with reconnect/backoff, a bounded backfill for SeatBoard.seedBackfill, and
// visibility-resume glue. No DOM/rendering of its own.
// === BEGIN live seat WebSocket transport (reconnect backoff) ===
(function(){
  "use strict";
  var INITIAL_DELAY_MS = 2500;
  var MAX_DELAY_MS = 60000;
  var BACKOFF_FACTOR = 2;

  var ws = null;             // current live socket, or null
  var reconnectTimer = null; // pending reconnect timeout id, or null (single-flight)
  var nextDelay = INITIAL_DELAY_MS;
  var generation = 0;        // bumped on every connect/hide so stale sockets' callbacks no-op

  function clearReconnectTimer(){
    if(reconnectTimer !== null){ clearTimeout(reconnectTimer); reconnectTimer = null; }
  }

  function closeCurrentSocket(){
    var sock = ws;
    ws = null;
    if(!sock) return;
    sock.onopen = sock.onmessage = sock.onclose = sock.onerror = null;
    try{ sock.close(); }catch(_){}
  }

  function scheduleReconnect(){
    if(reconnectTimer !== null) return; // already have a pending attempt queued
    var delay = nextDelay;
    nextDelay = Math.min(nextDelay * BACKOFF_FACTOR, MAX_DELAY_MS);
    reconnectTimer = setTimeout(function(){
      reconnectTimer = null;
      connectWS();
    }, delay);
  }

  function connectWS(){
    clearReconnectTimer();
    closeCurrentSocket();
    var myGen = ++generation;
    var proto = location.protocol==="https:" ? "wss" : "ws";
    var sock;
    try{ sock = new WebSocket(proto+"://"+location.host+"/ws"); }
    catch(_){ scheduleReconnect(); return; }
    ws = sock;
    sock.onopen = function(){
      if(myGen !== generation) return;
      nextDelay = INITIAL_DELAY_MS; // stable connection: backoff resets to the floor
    };
    sock.onmessage = function(e){
      if(myGen !== generation) return;
      try{ var ev = JSON.parse(e.data); if(window.SeatBoard) window.SeatBoard.flip(ev); }catch(_){}
    };
    sock.onclose = function(){
      if(myGen !== generation) return;
      ws = null;
      scheduleReconnect();
    };
    sock.onerror = function(){
      if(myGen !== generation) return;
      try{ sock.close(); }catch(_){} // triggers onclose, which alone schedules the reconnect
    };
  }

  function backfill(){
    fetch("/api/events?limit=200", {cache:"no-store"})
      .then(function(r){ if(!r.ok) throw 0; return r.json(); })
      .then(function(list){ if(window.SeatBoard) window.SeatBoard.seedBackfill(list); })
      .catch(function(){/* leave last good state */});
  }

  // Resume correctness (the critical stale-data fix): iOS freezes/kills sockets
  // on a backgrounded PWA and the onclose handler doesn't reliably fire, so a
  // resumed session can sit on a dead socket showing old data forever. Going
  // hidden cancels any pending reconnect timer and closes the live socket
  // (its callbacks are invalidated via generation, so no reconnect gets
  // scheduled from it); becoming visible always reconnects immediately once
  // and forces a backfill, with backoff starting from the floor on failure.
  document.addEventListener("visibilitychange", function(){
    if(document.visibilityState !== "visible"){
      clearReconnectTimer();
      generation++;
      closeCurrentSocket();
      return;
    }
    nextDelay = INITIAL_DELAY_MS;
    connectWS();
    backfill();
  });

  backfill();
  connectWS();
})();
// === END live seat WebSocket transport (reconnect backoff) ===
;
// --- PWA + mobile chrome: SW registration, nav toggle, tap-to-expand --------
(function(){
  "use strict";
  // Register the gate-safe service worker (static-assets-only cache + network
  // passthrough for navigations and /api/*). Failures (offline / gate) are silent.
  if("serviceWorker" in navigator){
    window.addEventListener("load", function(){
      navigator.serviceWorker.register("/sw.js").catch(function(){});
    });
  }
  // Tap-to-expand condensed cards (phone / no-hover). Delegated on document so it
  // survives every JS re-render of #contact; class-based so both the server
  // render and the client refresh behave identically. Taps on links / controls
  // pass through untouched.
  // Touch T3: .gpujob cards are EXCLUDED here — they have a real detail route
  // (/jobs/{id}) now, so their tap opens the bottom sheet instead (see the
  // sheet-controller script below). #contact node cards have no detail route
  // (there is no "/nodes/{id}" page), so they keep the original in-place
  // expand/collapse behavior unchanged.
  var mq = window.matchMedia("(max-width:640px)");
  document.addEventListener("click", function(e){
    if(!mq.matches) return;
    if(e.target.closest("a,button,input,select,textarea,label")) return;
    var head = e.target.closest(".frame-head");
    if(!head) return;
    var frame = head.parentElement;
    if(!frame || !frame.classList.contains("frame")) return;
    if(!frame.closest("#contact")) return;
    frame.classList.toggle("expanded");
  });
})();
;
// --- Touch T3: bottom sheet over the 6 deep-link detail routes ------------
// Phone only — every listener below bails on the same mq that gates the CSS,
// so desktop/iPad never call fetch()/pushState() here (byte-identical old
// behavior). Sheet content is ALWAYS the shared ?partial=1 fragment the
// standalone detail pages already render — no second render path (recon
// §3/§7). Close is unconditionally history.back(): the ✕, the backdrop tap,
// and drag-past-threshold all just call it, and the single popstate handler
// below is the one place that actually hides the sheet — so the hardware/
// on-screen Back button and iOS edge-swipe-back "just work" for free
// (design doc H.3/H.4), same as a normal navigation.
(function(){
  "use strict";
  var mq = window.matchMedia("(max-width:640px)");
  var backdrop = document.getElementById("sheetBackdrop");
  var sheet = document.getElementById("sheet");
  var handle = document.getElementById("sheetHandle");
  var closeBtn = document.getElementById("sheetClose");
  var body = document.getElementById("sheetBody");
  if(!backdrop || !sheet || !body) return;

  // ---- A11y: background inert + focus in/out of the dialog -----------------
  // .nexus is the single root that holds every piece of interactive dashboard
  // content (seatstrip/contact/jobstable/worktable/warnticker, header
  // bell) and is a sibling of #sheet/#sheetBackdrop (both direct children
  // of <body> — see the template comment above the sheet markup), so marking
  // it inert while the sheet is open removes the whole background from both
  // keyboard and AT without ever touching the dialog or its own backdrop
  // (inert on the backdrop would swallow the tap-to-close click). bgInertSet
  // guards restore so we only clear inert we ourselves set — any inert state
  // already present on .nexus before open is left exactly as we found it.
  var nexus = document.querySelector(".nexus");
  var sheetOpenerEl = null; // exact control focus returns to on close
  var bgInertSet = false;

  function applyBackgroundInert(){
    if(!nexus || nexus.hasAttribute("inert")) return;
    nexus.setAttribute("inert", "");
    bgInertSet = true;
  }
  function removeBackgroundInert(){
    if(nexus && bgInertSet) nexus.removeAttribute("inert");
    bgInertSet = false;
  }
  function focusIntoSheet(){
    if(closeBtn){ try{ closeBtn.focus({preventScroll:true}); }catch(e){} }
  }
  function restoreOpenerFocus(){
    var el = sheetOpenerEl;
    sheetOpenerEl = null;
    if(el && el.isConnected){ try{ el.focus({preventScroll:true}); }catch(e){} }
  }

  var DETAIL_RE = /^\/(run|jobs|queues|alerts|approve)(\/|$)/;
  var POLL_MS = 15000; // matches the dashboard's own refresh() cadence
  var pollTimer = null;
  var currentRoute = null;

  function stopPoll(){ if(pollTimer){ clearInterval(pollTimer); pollTimer = null; } }

  function loadInto(route){
    var sep = route.indexOf("?") === -1 ? "?" : "&";
    return fetch(route + sep + "partial=1", {cache:"no-store"})
      .then(function(r){ return r.text(); })
      .then(function(html){
        body.innerHTML = html;
      })
      .catch(function(){
        body.innerHTML = '<div class="d-empty">could not load this — check the connection and try again.</div>';
      });
  }

  // openSheet(route, push, sourceEl): push=true on a fresh card tap (adds a
  // history entry); push=false when popstate is just re-syncing content for
  // a route that's already the current location (e.g. forward/back between
  // two different open sheets — see the popstate handler). sourceEl is the
  // tapped card, present ONLY on a real push=true tap.
  //
  // Shared-element morph (2026-07-16 motion pass): on a real tap, the card
  // grows into the sheet via document.startViewTransition — feature-detected
  // AND reduced-motion-gated, so an unsupported/opted-out client gets the
  // exact instant-open behavior from before this pass. Originally open-only
  // (see the pass-1 report): closeSheet() morphs back too as of motion pass
  // 2, but ONLY when the source card is still connected — see closeSheet()
  // below for the isConnected guard that makes that safe across popstate,
  // programmatic-open paths and a list refresh that dropped
  // the card in between.
  // lastSourceEl: the card a REAL tap opened the sheet from (motion pass 2's
  // close-morph needs this at close time, which can be moments — or a full
  // popstate/back-forward hop — after the open). Reset on every openSheet
  // call, including programmatic opens (sourceEl undefined
  // there), so those correctly fall back to a plain close below.
  var lastSourceEl = null;

  // Cold/direct load: the sheet already arrived open (server-rendered detail
  // route — see the cold_sheet history-synth script above this one), so
  // openSheet() is never called for it. Apply the same background-inert and
  // focus-into-dialog treatment once here instead of duplicating that path.
  if(sheet.classList.contains("open")){
    currentRoute = location.pathname;
    applyBackgroundInert();
    focusIntoSheet();
  }

  function openSheet(route, push, sourceEl){
    // Only capture a new focus-return target on a genuine fresh open — a
    // popstate reroute between two already-open detail sheets (forward/back
    // without closing, see the comment above) must keep the ORIGINAL opener,
    // not the close button that a same-session route swap just focused.
    if(!sheet.classList.contains("open")){
      sheetOpenerEl = sourceEl || document.activeElement || null;
    }
    lastSourceEl = sourceEl || null;
    function apply(){
      currentRoute = route;
      body.innerHTML = "";
      loadInto(route);
      backdrop.classList.add("open");
      sheet.classList.add("open");
      sheet.setAttribute("aria-hidden", "false");
      document.body.style.overflow = "hidden";
      if(push) history.pushState(null, "", route);
      applyBackgroundInert();
      focusIntoSheet();
      stopPoll();
      // No per-run WS channel exists (recon §5/§8.7) — re-poll the same
      // partial on the dashboard's own timer while the sheet is open.
      pollTimer = setInterval(function(){ loadInto(route); }, POLL_MS);
    }
    var doMorph = !!(push && sourceEl && document.startViewTransition &&
      !window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    if(!doMorph){ apply(); return; }
    // Old-state snapshot happens synchronously at the startViewTransition()
    // call, so the source card must carry the shared name BEFORE this call —
    // renaming it onto #sheet happens inside the callback, ahead of the DOM
    // mutations that produce the "new" snapshot.
    sourceEl.style.viewTransitionName = "sheet-morph";
    var vt = document.startViewTransition(function(){
      sourceEl.style.viewTransitionName = "";
      sheet.style.viewTransitionName = "sheet-morph";
      apply();
    });
    vt.finished.catch(function(){}).then(function(){
      sheet.style.viewTransitionName = "";
    });
  }

  // Close morph (motion pass 2) — the reverse of openSheet's morph, but only
  // when the source card is still a real, attached element: isConnected
  // covers a list refresh that dropped the card between open and close, and
  // lastSourceEl being null covers popstate/back-forward and
  // programmatic-open paths (no originating card at all), and any close that
  // didn't come from a real card tap. All of those just fall through to the
  // exact plain close this file always had.
  function closeSheet(){
    function finish(){
      backdrop.classList.remove("open");
      sheet.classList.remove("open");
      sheet.setAttribute("aria-hidden", "true");
      document.body.style.overflow = "";
      stopPoll();
      currentRoute = null;
      removeBackgroundInert();
      restoreOpenerFocus();
    }
    var src = lastSourceEl;
    var doMorph = !!(src && src.isConnected && document.startViewTransition &&
      !window.matchMedia("(prefers-reduced-motion: reduce)").matches);
    lastSourceEl = null;
    if(!doMorph){ finish(); return; }
    sheet.style.viewTransitionName = "sheet-morph";
    var vt = document.startViewTransition(function(){
      sheet.style.viewTransitionName = "";
      src.style.viewTransitionName = "sheet-morph";
      finish();
    });
    vt.finished.catch(function(){}).then(function(){
      src.style.viewTransitionName = "";
    });
  }

  // Card tap -> open. Only elements carrying data-sheet-route are routable
  // (today: .gpujob/.job-row rows -> /jobs/{id}; #contact node cards have no
  // detail route and keep plain tap-to-expand, handled above). Taps on real
  // links/buttons/controls inside a card (e.g. "mark done") pass through.
  // Touch T3.6 (FLEET-WORKER2-BUILD-20260721-panel-dashboard-compact-jobs):
  // job rows are now a native <details>/<summary> disclosure (same markup as
  // /jobs), so a summary tap ALSO toggles the row open/closed in place via the
  // browser's own default action — independent of, and not suppressed by,
  // this delegate. That's intentional: the sheet still opens (unchanged
  // behavior, this preserves the /queues/{name} links and full attempt
  // history detail_jobs.html carries that the inline disclosure doesn't), and
  // the toggle underneath is harmless since the sheet covers it.
  var CARD_CONTROL_SEL = "a,button,input,select,textarea,label";
  document.addEventListener("click", function(e){
    if(!mq.matches) return;
    if(e.target.closest(CARD_CONTROL_SEL)) return;
    var card = e.target.closest("[data-sheet-route]");
    if(!card) return;
    openSheet(card.getAttribute("data-sheet-route"), true, card);
  });

  // Keyboard activation, mirroring the tap delegate above at the same mobile
  // width. #contact node cards carry role="button" tabindex="0" so Tab
  // reaches them with no built-in key handling of their own — Enter/Space are
  // wired up here for those. Job rows are a native <details>/<summary> (no
  // role/tabindex needed — summary is focusable and Enter/Space-activatable
  // by the browser itself already); this delegate still matches them via
  // data-sheet-route and opens the sheet alongside the native toggle, same
  // coexistence as the click delegate above. Space is prevented on keydown
  // (stop the page from scrolling under a role=button, same as a native
  // <button>; a no-op extra preventDefault on a summary, which already
  // suppresses its own space-scroll) and both keys are guarded by e.repeat so
  // holding the key down doesn't reopen the sheet on every auto-repeat tick.
  // Nested real controls (a/button/input/select/textarea/label) keep their
  // own native key handling and must not also trigger the card here.
  document.addEventListener("keydown", function(e){
    if(!mq.matches) return;
    if(e.key !== "Enter" && e.key !== " " && e.key !== "Spacebar") return;
    if(e.target.closest(CARD_CONTROL_SEL)) return;
    var card = e.target.closest("[data-sheet-route]");
    if(!card) return;
    if(e.key !== "Enter") e.preventDefault();
    if(e.repeat) return;
    openSheet(card.getAttribute("data-sheet-route"), true, card);
  });

  if(closeBtn) closeBtn.addEventListener("click", function(){ history.back(); });
  backdrop.addEventListener("click", function(){ history.back(); });

  // Escape closes the same way Close/backdrop do — through history.back(),
  // so popstate's closeSheet() stays the single cleanup path for every route.
  document.addEventListener("keydown", function(e){
    if(e.key !== "Escape" && e.key !== "Esc") return;
    if(!sheet.classList.contains("open")) return;
    history.back();
  });

  window.addEventListener("popstate", function(){
    if(DETAIL_RE.test(location.pathname)){
      if(location.pathname !== currentRoute) openSheet(location.pathname, false);
    } else {
      closeSheet();
    }
  });

  // Drag-to-dismiss: touchstart/move/end on the drag handle ONLY. translateY
  // tracks the finger with a plain CSS transition (no library); past a
  // distance/velocity threshold on touchend, close via the same
  // history.back() path everything else uses; otherwise snap back.
  var dragging = false, startY = 0, startT = 0, dy = 0;
  var DISMISS_DIST = 120, DISMISS_VELOCITY = 0.5; // px, px/ms
  if(handle){
    handle.addEventListener("touchstart", function(e){
      if(!e.touches || !e.touches.length) return;
      dragging = true; dy = 0;
      startY = e.touches[0].clientY;
      startT = e.timeStamp;
      sheet.classList.add("dragging");
    }, {passive:true});
    handle.addEventListener("touchmove", function(e){
      if(!dragging || !e.touches || !e.touches.length) return;
      dy = Math.max(0, e.touches[0].clientY - startY);
      sheet.style.transform = "translateY(" + dy + "px)";
    }, {passive:true});
    handle.addEventListener("touchend", function(e){
      if(!dragging) return;
      dragging = false;
      sheet.classList.remove("dragging");
      sheet.style.transform = "";
      var dt = Math.max(1, e.timeStamp - startT);
      if(dy > DISMISS_DIST || (dy / dt) > DISMISS_VELOCITY) history.back();
    });
    handle.addEventListener("touchcancel", function(){
      dragging = false;
      sheet.classList.remove("dragging");
      sheet.style.transform = "";
    });
  }

  var bellCounts = document.querySelectorAll("[data-bell-count]");
  function setBellCount(n){
    if(!bellCounts.length) return;
    n = n || 0;
    bellCounts.forEach(function(bellCount){
      bellCount.textContent = n;
      bellCount.style.display = n > 0 ? "" : "none";
    });
  }
  // Dual-render parity: both the first-paint value (server-rendered `unread`
  // in the template) and this poll come from the same source, notify_store.
  // count_unread() — never a separately-derived count.
  function pollUnread(signal){
    return fetch("/api/notify/unread-count", {cache:"no-store", signal})
      .then(function(r){ return r.json(); })
      .then(function(d){ if(d && d.ok) setBellCount(d.unread); })
      .catch(function(){});
  }
  // Driven by the central polling controller (end of file) on the same 60s
  // subcadence as the history load, not its own setInterval/visibilitychange
  // listener — see T5 consolidation.
  window.__NEXUS_UNREAD__ = {poll: pollUnread};

})();
;
// --- Fleet Activity dashboard projections ----------------------------------
// One /api/activity?range=all request feeds both bounded modules. The central
// polling controller below owns cadence/visibility/abort behavior; this block
// only renders the last successful aggregate.
(function(){
  "use strict";
  var overview = document.getElementById("fleetOverviewModule");
  var models = document.getElementById("modelActivityModule");
  if(!overview || !models) return;

  var number = new Intl.NumberFormat("en-US");
  var compact = new Intl.NumberFormat("en-US", {notation:"compact", maximumFractionDigits:1});
  var labels = {
    commits:"commits", successful_pushes:"pushes", repositories_touched:"repositories",
    active_days:"active days", current_streak:"current streak", longest_streak:"longest streak",
    peak_commit_hour:"peak hour", top_repository:"top repository"
  };
  var order = Object.keys(labels);
  function esc(value){
    return String(value == null ? "—" : value).replace(/[&<>"']/g,function(c){
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
    });
  }
  function isoDay(date){ return date.toISOString().slice(0,10); }
  function addDays(date,amount){
    var next = new Date(date); next.setUTCDate(next.getUTCDate()+amount); return next;
  }
  function heatmap(daily){
    var byDay = new Map(daily.map(function(entry){ return [entry.date,entry.commits||0]; }));
    var latest = daily.length ? new Date(daily[daily.length-1].date+"T00:00:00Z") : new Date();
    var firstKnown = daily.length ? daily[0].date : null;
    var start = addDays(latest,-370);
    var max = Math.max.apply(null,[1].concat(daily.map(function(entry){ return entry.commits||0; })));
    var html = "";
    for(var i=0;i<371;i++){
      var date = isoDay(addDays(start,i));
      var known = firstKnown && date>=firstKnown;
      var commits = byDay.get(date)||0;
      var level = commits ? Math.min(4,Math.max(1,Math.ceil(commits/max*4))) : 0;
      html += '<span class="dam-cell '+(known?"level-"+level:"unknown")+'" title="'+
        esc(date)+": "+(known?commits+" commits":"outside collected range")+'"></span>';
    }
    return html;
  }
  function renderOverview(data){
    var summary = data.summary||{}, daily = data.daily||[];
    overview.innerHTML =
      '<div class="dam-metrics">'+order.map(function(key){
        var value=key==="peak_commit_hour"
          ? formatCentralHourBucket(summary[key])
          : summary[key];
        return '<div class="dam-metric"><span>'+labels[key]+'</span><b title="'+
          esc(value)+'">'+esc(value)+'</b></div>';
      }).join("")+'</div>'+
      '<div class="dam-heat" aria-label="371-day contribution map">'+heatmap(daily)+'</div>'+
      '<p class="dam-summary"><b>'+number.format(summary.commits||0)+' commits</b> · '+
        number.format(summary.repositories_touched||0)+' repositories · '+
        number.format(summary.active_days||0)+' active days</p>';
  }
  function providerRow(name,value){
    var cls=name.toLowerCase();
    if(!value || !value.comparable){
      return '<div class="dam-provider unavailable"><span class="dam-provider-name">'+
        '<i class="dam-swatch '+cls+'"></i>'+name+'</span>'+
        '<span class="dam-provider-detail">historical metric unavailable</span>'+
        '<span class="dam-provider-share">N/A</span></div>';
    }
    return '<div class="dam-provider"><span class="dam-provider-name">'+
      '<i class="dam-swatch '+cls+'"></i>'+name+'</span>'+
      '<span class="dam-provider-detail">'+compact.format(value.assistant_turns||0)+
      ' turns · '+number.format(value.sessions||0)+' sessions</span>'+
      '<span class="dam-provider-share">'+value.share+'%</span></div>';
  }
  function renderModels(data){
    var daily=data.daily||[], providers=data.providers||{};
    var max=Math.max.apply(null,[1].concat(daily.map(function(entry){
      return (entry.Claude||0)+(entry.OpenAI||0)+(entry.Google||0);
    })));
    models.innerHTML =
      '<div class="dam-chart" aria-label="Daily stacked provider activity">'+
      daily.map(function(entry){
        return '<span class="dam-stack" title="'+esc(entry.date)+' · Claude '+
          (entry.Claude||0)+' · OpenAI '+(entry.OpenAI||0)+' · Google '+(entry.Google||0)+'">'+
          '<i class="claude" style="height:'+((entry.Claude||0)/max*100)+'%"></i>'+
          '<i class="openai" style="height:'+((entry.OpenAI||0)/max*100)+'%"></i>'+
          '<i class="google" style="height:'+((entry.Google||0)/max*100)+'%"></i></span>';
      }).join("")+'</div>'+
      '<div class="dam-legend">'+["Claude","OpenAI","Google"].map(function(name){
        return providerRow(name,providers[name]);
      }).join("")+'</div>';
  }
  async function load(signal){
    try{
      var response=await fetch("/api/activity?range=all",{cache:"no-store",signal:signal});
      var data=await response.json();
      if(!response.ok) throw new Error(data.error||"activity cache unavailable");
      renderOverview(data); renderModels(data);
    }catch(error){
      if(error && error.name==="AbortError") return;
      var message='<p class="dam-error">'+esc(error&&error.message||"activity unavailable")+'</p>';
      overview.innerHTML=message; models.innerHTML=message;
    }
  }
  window.__NEXUS_ACTIVITY__={load:load};
})();

// --- T5: central dashboard polling controller (visibility-aware) ------------
// Before this pass, four independent browser timers fired on this page:
// refresh() every 15s (/api/status, shared with SeatBoard via applySnap —
// see refresh() above, unchanged), SeatBoard's OWN render()-tick every 15s
// (pure duplicate DOM churn — applySnap already calls render() at the same
// cadence), pollUnread() every 15s (/api/notify/unread-count), and the
// timeline's load() every 60s (/api/history) — none of them paused while the
// tab/PWA was hidden, and iOS/macOS Safari wakes the whole page for each one.
// This IIFE is the single scheduler left: it drives refresh() on the 15s
// cadence and, on the same ticks, rides unread-count + history + Fleet Activity on a 60s
// subcadence (every 4th tick) — no independent setInterval remains in
// refresh()/SeatBoard/pollUnread()/the timeline loader (see the comments at
// each of those call sites above). An in-flight guard means a slow tick can
// never overlap the next one, and going hidden stops all polling and the 1s
// clock outright (plus aborts whatever GET was in flight); coming back
// visible does exactly one immediate consolidated refresh, then resumes
// cadence — start*/stop* below are no-ops when already in the requested
// state, so repeated hide/show cycles never multiply timers or listeners.
// === BEGIN central dashboard polling controller ===
(function(){
  "use strict";
  var STATUS_MS = 15000;
  var SUBCADENCE_TICKS = 4;      // 4 * 15s = 60s for unread-count + history
  var pollTimer = null, clockTimer = null;
  var tick = 0;                  // tick===0 means "subcadence due this tick"
  var busy = false;              // in-flight guard: a slow tick can't overlap the next
  var abortCtl = null;

  async function runTick(){
    if(busy) return;             // previous tick still in flight — skip, don't queue
    busy = true;
    var due = (tick === 0);
    abortCtl = (typeof AbortController !== "undefined") ? new AbortController() : null;
    var signal = abortCtl ? abortCtl.signal : undefined;
    try{
      await refresh(signal);     // one /api/status fetch, shared by the main render + SeatBoard.applySnap
      if(due){
        var jobs = [];
        var unread = window.__NEXUS_UNREAD__, timeline = window.__NEXUS_TIMELINE__,
            activity = window.__NEXUS_ACTIVITY__;
        if(unread) jobs.push(unread.poll(signal));
        if(timeline) jobs.push(timeline.load(signal));
        if(activity) jobs.push(activity.load(signal));
        // unread.poll()/timeline.load()/activity.load() (like refresh() itself) each catch
        // their own fetch errors and keep the last good render, so one
        // endpoint failing here never rejects Promise.all or stalls the
        // other's result — and never stops the next scheduled tick either.
        if(jobs.length) await Promise.all(jobs);
      }
    } finally {
      busy = false;
      abortCtl = null;
      tick = due ? 1 : (tick + 1) % SUBCADENCE_TICKS;
    }
  }

  function startPolling(){
    if(pollTimer) return;        // already running — never multiply timers
    pollTimer = setInterval(function(){ runTick().catch(function(){}); }, STATUS_MS);
  }
  function stopPolling(){
    if(pollTimer){ clearInterval(pollTimer); pollTimer = null; }
    // Aborting an in-flight GET is safe here: refresh()/poll()/load() all
    // already treat a rejected fetch (network error or AbortError alike) as
    // "keep the last good render", never as corrupt/partial state.
    if(abortCtl){ try{ abortCtl.abort(); }catch(_){} }
  }
  function startClock(){
    if(clockTimer) return;
    tickClock();
    clockTimer = setInterval(tickClock, 1000);
  }
  function stopClock(){
    if(clockTimer){ clearInterval(clockTimer); clockTimer = null; }
  }

  function onHidden(){
    stopPolling();
    stopClock();
    // Pauses decorative CSS animations while hidden (dashboard.css); CSS
    // transitions and the prefers-reduced-motion collapse are untouched.
    document.documentElement.classList.add("page-hidden");
  }
  function onVisible(){
    document.documentElement.classList.remove("page-hidden");
    startClock();
    tick = 0;                    // next runTick is the "immediate consolidated refresh"
    runTick().catch(function(){});
    startPolling();
  }

  document.addEventListener("visibilitychange", function(){
    if(document.hidden) onHidden(); else onVisible();
  });

  if(document.hidden) onHidden(); else onVisible();
})();
// === END central dashboard polling controller ===
;
