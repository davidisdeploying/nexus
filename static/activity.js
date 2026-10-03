(() => {
  const params = new URLSearchParams(window.location.search);
  const requestedRange = params.get("range");
  const requestedTab = params.get("tab") || document.body.dataset.initialTab;
  let range = ["all", "30d", "7d"].includes(requestedRange)
    ? requestedRange
    : "all";
  let activeTab = ["models", "workers", "jobs"].includes(requestedTab)
    ? requestedTab
    : "overview";
  let cacheStatus = "loading cache";
  const $ = (selector) => document.querySelector(selector);
  const esc = (value) => String(value ?? "—").replace(
    /[&<>"']/g,
    (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    })[character],
  );
  const number = new Intl.NumberFormat("en-US");
  const compact = new Intl.NumberFormat("en-US", {
    notation: "compact",
    maximumFractionDigits: 1,
  });
  const metricLabels = {
    commits: "Commits",
    successful_pushes: "Successful pushes",
    repositories_touched: "Repositories",
    active_days: "Active days",
    current_streak: "Current streak",
    longest_streak: "Longest streak",
    peak_commit_hour: "Peak hour",
    top_repository: "Top repository",
  };
  const metricOrder = Object.keys(metricLabels);

  // === BEGIN Activity Central display-time formatter ===
  // The API continues to expose the aggregate bucket as HH:00 UTC. Convert
  // only the card label, using today's Central offset.
  const ACTIVITY_CENTRAL_HOUR_FORMAT = new Intl.DateTimeFormat("en-US", {
    timeZone:"America/Chicago", hour:"numeric", hour12:true,
  });
  function formatPeakHourCentral(value, referenceDate = new Date()){
    const match = /^([01]\d|2[0-3]):00 UTC$/.exec(String(value || ""));
    if(!match) return value ?? "—";
    const instant = new Date(Date.UTC(
      referenceDate.getUTCFullYear(),
      referenceDate.getUTCMonth(),
      referenceDate.getUTCDate(),
      Number(match[1]),
    ));
    const parts = {};
    ACTIVITY_CENTRAL_HOUR_FORMAT.formatToParts(instant).forEach(part => {
      if(part.type !== "literal") parts[part.type] = part.value;
    });
    return `${parts.hour}:00 ${parts.dayPeriod}`;
  }
  // === END Activity Central display-time formatter ===

  const metric = (key, value) => `
    <article class="metric">
      <span>${esc(metricLabels[key] || key.replaceAll("_", " "))}</span>
      <b title="${esc(value)}">${esc(value ?? "—")}</b>
    </article>`;

  const isoDay = (date) => date.toISOString().slice(0, 10);
  const addDays = (date, amount) => {
    const next = new Date(date);
    next.setUTCDate(next.getUTCDate() + amount);
    return next;
  };

  function contributionMap(daily) {
    const byDay = new Map(daily.map((entry) => [entry.date, entry.commits || 0]));
    const latest = daily.length
      ? new Date(`${daily[daily.length - 1].date}T00:00:00Z`)
      : new Date();
    const firstKnown = daily.length ? daily[0].date : null;
    const start = addDays(latest, -370);
    const max = Math.max(1, ...daily.map((entry) => entry.commits || 0));
    const cells = [];
    for (let index = 0; index < 371; index += 1) {
      const date = isoDay(addDays(start, index));
      const known = firstKnown && date >= firstKnown;
      const commits = byDay.get(date) || 0;
      const level = commits
        ? Math.min(4, Math.max(1, Math.ceil((commits / max) * 4)))
        : 0;
      cells.push(`
        <span class="heat ${known ? `level-${level}` : "unknown"}"
          title="${esc(date)}: ${known ? `${commits} commits` : "outside collected range"}"></span>`);
    }
    return cells.join("");
  }

  function recentEvents(events) {
    return events.slice(0, 40).map((event) => event.event === "push"
      ? `<div class="event"><b>push</b> ${esc(event.repository)} · ${esc(event.host)} · ${esc(event.status)} · ${esc(event.finished_at)}</div>`
      : `<div class="event"><b>${esc(event.short_hash)}</b> ${esc(event.repository)} · ${esc(event.host)} · ${esc(event.timestamp)}<br>${esc(event.subject)}</div>`
    ).join("") || '<p class="empty-event">Nothing in this range.</p>';
  }

  function modelChart(daily, providerMax) {
    if (!daily.length) return '<p class="empty-event">No model activity in this range.</p>';
    const first = daily[0].date.slice(5);
    const middle = daily[Math.floor(daily.length / 2)].date.slice(5);
    const last = daily[daily.length - 1].date.slice(5);
    return `
      <div class="model-plot" aria-label="Daily stacked provider activity">
        <div class="y-axis" aria-hidden="true">
          <span>${compact.format(providerMax)}</span>
          <span>${compact.format(providerMax * .75)}</span>
          <span>${compact.format(providerMax * .5)}</span>
          <span>${compact.format(providerMax * .25)}</span>
          <span>0</span>
        </div>
        <div class="bars-wrap">
          <div class="model-bars">
            ${daily.map((entry) => `
              <span class="stack" title="${esc(entry.date)} · Claude ${entry.Claude} · OpenAI ${entry.OpenAI} · Google ${entry.Google}">
                <i class="Claude" style="height:${entry.Claude / providerMax * 100}%"></i>
                <i class="OpenAI" style="height:${entry.OpenAI / providerMax * 100}%"></i>
                <i class="Google" style="height:${entry.Google / providerMax * 100}%"></i>
              </span>`).join("")}
          </div>
          <div class="x-axis" aria-hidden="true"><span>${esc(first)}</span><span>${esc(middle)}</span><span>${esc(last)}</span></div>
        </div>
      </div>`;
  }

  function providerLegend(providers) {
    return ["Claude", "OpenAI", "Google"].map((name) => {
      const value = providers[name] || {};
      if (!value.comparable) {
        return `
          <div class="provider-row unavailable">
            <span class="provider-name"><i class="swatch ${name}"></i>${name}</span>
            <span class="provider-detail">historical assistant-turn metric unavailable</span>
            <span class="provider-share">N/A</span>
          </div>`;
      }
      return `
        <div class="provider-row">
          <span class="provider-name"><i class="swatch ${name}"></i>${name}</span>
          <span class="provider-detail">${compact.format(value.assistant_turns || 0)} turns · ${number.format(value.sessions || 0)} sessions · ${value.active_days || 0} days</span>
          <span class="provider-share">${value.share}%</span>
        </div>`;
    }).join("");
  }

  function render(data) {
    const daily = data.daily || [];
    const providerMax = Math.max(
      1,
      ...daily.map((entry) => entry.Claude + entry.OpenAI + entry.Google),
    );
    cacheStatus = data.stale
      ? "stale cache"
      : `cached ${data.generated_at || "—"}`;
    if (activeTab === "overview") $("#activityStatus").textContent = cacheStatus;
    const warnings = [];
    if (data.host_errors && Object.keys(data.host_errors).length) {
      warnings.push(`partial host errors: ${Object.keys(data.host_errors).join(", ")}`);
    }
    if (data.assistant_turns_truncated) {
      warnings.push("model history reached its collection bound");
    }
    $("#activityMessage").textContent = warnings.join(" · ");
    $("#modelActivityMessage").textContent = warnings.join(" · ");

    const summary = data.summary || {};
    $("#overview").innerHTML = `
      <div class="metric-grid">
        ${metricOrder.map((key) => metric(
          key,
          key === "peak_commit_hour"
            ? formatPeakHourCentral(summary[key])
            : summary[key],
        )).join("")}
      </div>
      <div class="contribution-wrap">
        <div class="contribution-map" aria-label="371-day contribution map">
          ${contributionMap(daily)}
        </div>
      </div>
      <p class="activity-summary">
        <b>${number.format(summary.commits || 0)} commits</b> across
        <b>${number.format(summary.repositories_touched || 0)} repositories</b>
        on ${number.format(summary.active_days || 0)} active days.
      </p>`;

    const providers = data.providers || {};
    $("#modelActivityBody").innerHTML = `
      ${modelChart(daily, providerMax)}
      <div class="provider-legend">${providerLegend(providers)}</div>
      <p class="model-note">
        Comparable unit: redacted assistant turns, not tokens. Google is N/A—not zero—until
        Gemini evidence includes assistant-role labels.
      </p>`;

    $("#recentActivity").innerHTML = recentEvents(data.recent_events || []);
  }

  async function load() {
    try {
      const response = await fetch(`/api/activity?range=${range}`, { cache: "no-store" });
      const data = await response.json();
      if (!response.ok) throw Error(data.error || "cache unavailable");
      render(data);
    } catch (error) {
      cacheStatus = "cache unavailable";
      if (activeTab === "overview") $("#activityStatus").textContent = cacheStatus;
      $("#activityMessage").textContent = error.message;
      $("#modelActivityMessage").textContent = error.message;
    }
  }

  const syncActivityRanges = () => {
    document.querySelectorAll(".activity-controls [data-range]").forEach((button) => {
      button.setAttribute("aria-pressed", button.dataset.range === range);
    });
    document.querySelectorAll("[data-model-range]").forEach((button) => {
      button.setAttribute("aria-pressed", button.dataset.modelRange === range);
    });
  };
  document.querySelectorAll(".activity-controls [data-range]").forEach((button) => {
    button.setAttribute("aria-pressed", button.dataset.range === range);
    button.onclick = () => {
      range = button.dataset.range;
      syncActivityRanges();
      load();
    };
  });
  document.querySelectorAll("[data-model-range]").forEach((button) => {
    button.onclick = () => {
      range = button.dataset.modelRange;
      syncActivityRanges();
      load();
    };
  });
  syncActivityRanges();
  function applyTab(updateUrl = false) {
    document.querySelectorAll("[data-tab]").forEach((button) => {
      const selected = button.dataset.tab === activeTab;
      button.setAttribute("aria-selected", selected);
      button.classList.toggle("is-active", selected);
      button.tabIndex = selected ? 0 : -1;
    });
    ["overview", "models", "workers", "jobs"].forEach((name) => {
      $("#" + name).hidden = activeTab !== name;
    });
    const isCommits = activeTab === "overview";
    $(".activity-controls").hidden = !isCommits;
    $("#activityMessage").hidden = !isCommits;
    $("#recentActivityDrawer").hidden = !isCommits;
    $(".activity-meta").hidden = activeTab === "models";
    $("#activityLensTitle").textContent = activeTab === "overview" ? "commits" : activeTab;
    $("#activityMetaText").textContent = activeTab === "workers"
      ? "relay run transcripts · bounded operational evidence"
      : activeTab === "jobs"
        ? "live and recent jobs · heartbeat-backed fleet evidence"
        : "fleet commits, pushes, and comparable assistant turns";
    const workerCount = Number($("#workers").dataset.runCount || 0);
    const jobCount = Number($("#jobs").dataset.jobCount || 0);
    $("#activityStatus").textContent = activeTab === "models"
      ? ($("#models").dataset.status || "loading history")
      : activeTab === "workers"
        ? `${number.format(workerCount)} worker run${workerCount === 1 ? "" : "s"}`
      : activeTab === "jobs"
        ? `${number.format(jobCount)} job${jobCount === 1 ? "" : "s"}`
        : cacheStatus;
    if (updateUrl) {
      const url = new URL(window.location.href);
      if (activeTab === "overview") url.searchParams.delete("tab");
      else url.searchParams.set("tab", activeTab);
      window.history.replaceState({}, "", url);
    }
  }

  document.querySelectorAll("[data-tab]").forEach((button) => {
    button.onclick = () => {
      activeTab = button.dataset.tab;
      applyTab(true);
    };
  });
  document.addEventListener("model-usage-status", (event) => {
    if (activeTab === "models") $("#activityStatus").textContent = event.detail.status;
  });
  applyTab();
  load();
})();
