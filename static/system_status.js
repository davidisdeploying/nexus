(() => {
  const VALID = new Set(["ok", "warn", "critical"]);
  const EYE_ACCENT = {
    ok: "developed",
    warn: "safelight",
    critical: "overexposed",
  };
  let lastOverall = null;

  function applyStatus(payload) {
    const overall = VALID.has(payload?.overall) ? payload.overall : "warn";
    document.querySelectorAll("[data-system-status]").forEach((node) => {
      node.dataset.systemState = overall;
      node.setAttribute("aria-label", `System status: ${overall}`);
      const text = node.querySelector("[data-system-status-label]");
      if (text) text.textContent = "Status";
    });

    const eye = document.getElementById("overall-eye");
    if (eye) {
      eye.classList.remove("developed", "safelight", "overexposed", "unexposed");
      eye.classList.add(EYE_ACCENT[overall]);
    }

    const generated = document.querySelector("[data-status-generated]");
    if (generated && payload?.generated_at) {
      generated.dateTime = payload.generated_at;
      generated.textContent = payload.generated_at;
    }

    const pageSummary = document.getElementById("systemStatusPageSummary");
    if (pageSummary && Number.isFinite(payload?.module_count)) {
      pageSummary.textContent = `${payload.module_count} modules · ${payload.issue_count || 0} flagged checks`;
    }

    if (lastOverall && lastOverall !== overall && document.querySelector(".system-status-page")) {
      document.documentElement.dataset.systemStatusChanged = "true";
    }
    lastOverall = overall;
  }

  async function refresh() {
    try {
      const response = await fetch("/api/system-status", {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error(`status ${response.status}`);
      applyStatus(await response.json());
    } catch (_error) {
      // A failed registry fetch is incomplete evidence, never a silent OK.
      applyStatus({ overall: "warn" });
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    const initial = document.querySelector("[data-system-status]");
    if (initial) {
      lastOverall = VALID.has(initial.dataset.systemState)
        ? initial.dataset.systemState
        : null;
    }
    refresh();
    window.setInterval(() => {
      if (document.visibilityState === "visible") refresh();
    }, 30_000);
  });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh();
  });
})();
