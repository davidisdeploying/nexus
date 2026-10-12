(() => {
  "use strict";
  const root = document.querySelector("#models.models-usage-panel");
  if (!root) return;
  const $ = (s) => root.querySelector(s);
  const $$ = (s) => [...root.querySelectorAll(s)];
  const colors = {claude:"#dc9b67",codex:"#58d6cf",gemini:"#7fa9ed"};
  let range = "30d", provider = "all", controller;
  const setStatus = (status) => {
    root.dataset.status = status;
    document.dispatchEvent(new CustomEvent("model-usage-status", {detail:{status}}));
  };
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const compact = (v) => new Intl.NumberFormat("en",{notation:"compact",maximumFractionDigits:1}).format(v||0);
  const ago = (epoch) => {
    if(!epoch) return "never";
    const s=Math.max(0,Date.now()/1000-epoch), units=[[86400,"d"],[3600,"h"],[60,"m"]];
    for(const [n,u] of units) if(s>=n) return `${Math.floor(s/n)}${u} ago`;
    return `${Math.floor(s)}s ago`;
  };
  const stamp = (epoch) => epoch ? new Date(epoch*1000).toLocaleString([],{
    month:"short",day:"numeric",hour:"numeric",minute:"2-digit"}) : "unknown";
  const metric = (label,value) => `<div class="metric"><span>${esc(label)}</span><b>${esc(value)}</b></div>`;
  function currentCard(item){
    const windows=["five_hour","weekly"].map(name=>{
      const w=item.windows[name], label=name==="five_hour"?"5 hour":"weekly";
      if(!w) return `<div class="window"><span>${label}</span><span class="unavailable">unavailable</span><span></span></div>`;
      return `<div class="window"><span>${label}</span><div class="bar"><i style="width:${w.used_percent}%;--pc:${colors[item.provider]}"></i></div><b>${Math.round(w.used_percent)}%</b></div>`;
    }).join("");
    return `<article class="current" style="--pc:${colors[item.provider]}"><header><span class="provider-label"><i class="swatch"></i>${esc(item.provider)}</span><time>${ago(item.captured_at)}</time></header>${item.ok?windows:`<p class="unavailable">${esc(item.error_class||"unavailable")}</p>`}<p class="unavailable">${esc(item.source||"no source")}</p></article>`;
  }
  function chart(series,windowName,label){
    const rows=series.filter(r=>r.window===windowName), w=960,h=178,p={l:34,r:12,t:12,b:22};
    if(!rows.length) return `<section class="chart-block"><h2 class="chart-title">${label}<span>no recorded values</span></h2><p class="empty">No telemetry in this range.</p></section>`;
    const min=Math.min(...rows.map(r=>r.captured_at)),max=Math.max(...rows.map(r=>r.captured_at),min+1);
    const x=t=>p.l+(t-min)/(max-min)*(w-p.l-p.r), y=v=>p.t+(100-v)/100*(h-p.t-p.b);
    const lines=Object.keys(colors).filter(k=>provider==="all"||provider===k).map(name=>{
      const pts=rows.filter(r=>r.provider===name).map(r=>`${x(r.captured_at).toFixed(1)},${y(r.used_percent).toFixed(1)}`).join(" ");
      return pts ? `<polyline class="plot-line" stroke="${colors[name]}" points="${pts}"/>` : "";
    }).join("");
    const grid=[0,25,50,75,100].map(v=>`<line class="grid-line" x1="${p.l}" y1="${y(v)}" x2="${w-p.r}" y2="${y(v)}"/><text class="axis-label" x="2" y="${y(v)+3}">${v}%</text>`).join("");
    const legend=Object.keys(colors).filter(k=>provider==="all"||provider===k).map(k=>`<span><i class="swatch" style="--pc:${colors[k]}"></i>${k}</span>`).join("");
    return `<section class="chart-block"><h2 class="chart-title">${label}<span>percent used · ${stamp(min)}–${stamp(max)}</span></h2><svg class="chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="${label} usage percentage history">${grid}${lines}</svg><div class="legend">${legend}</div></section>`;
  }
  function render(data){
    const s=data.summary||{}, first=s.first_sample?stamp(s.first_sample):"none";
    $("#usageBody").innerHTML=`<div class="metric-grid">${metric("Samples",compact(s.samples))}${metric("Providers",s.providers||0)}${metric("Healthy",s.healthy_percent==null?"N/A":`${s.healthy_percent}%`)}${metric("Fallbacks",s.fallbacks||0)}${metric("Events",s.events||0)}${metric("Early reanchors",s.early_reanchors||0)}${metric("History since",first)}${metric("Database",compact(data.database_bytes)+"B")}</div><div class="current-grid">${(data.latest||[]).filter(i=>provider==="all"||i.provider===provider).map(currentCard).join("")}</div>${chart(data.series||[],"weekly","weekly window")}${chart(data.series||[],"five_hour","five-hour window")}`;
    $("#eventCount").textContent=`(${s.events||0})`;
    $("#usageEvents").innerHTML=(data.events||[]).map(e=>`<div class="usage-event ${esc(e.severity)}"><time>${stamp(e.captured_at)}</time><span class="event-provider">${esc(e.provider)}${e.window?" · "+esc(e.window):""}</span><span class="event-kind">${esc(e.event_type.replaceAll("_"," "))}</span></div>`).join("")||'<p class="empty">No events in this range.</p>';
    setStatus(`updated ${ago(Math.floor(Date.parse(data.generated_at)/1000))}`);
    $("#usageDatabase").textContent=`${compact(data.database_bytes)}B local SQLite`;
    $("#usageMessage").textContent="";
  }
  async function load(){
    if(controller) controller.abort(); controller=new AbortController();
    setStatus("loading history");
    try{
      const r=await fetch(`/api/model-usage/history?range=${range}&provider=${provider}`,{cache:"no-store",signal:controller.signal});
      const data=await r.json(); if(!r.ok) throw new Error(data.error||"history unavailable"); render(data);
    }catch(e){if(e.name!=="AbortError"){setStatus("history unavailable");$("#usageMessage").textContent=e.message;}}
  }
  $$(".providers button").forEach(b=>b.addEventListener("click",()=>{$$(".providers button").forEach(x=>x.classList.remove("active"));b.classList.add("active");provider=b.dataset.provider;load();}));
  $$(".usage-ranges button").forEach(b=>b.addEventListener("click",()=>{$$(".usage-ranges button").forEach(x=>x.classList.remove("active"));b.classList.add("active");range=b.dataset.usageRange;load();}));
  load();
})();
