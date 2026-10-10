(function () {
  "use strict";

  var chrome = document.querySelector(".nexus-dashboard-chrome");
  if (!chrome || document.body.dataset.dashboard === "true") return;

  function centralClock(date) {
    return new Intl.DateTimeFormat("en-US", {
      timeZone: "America/Chicago",
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
      hour12: true
    }).format(date) + " CT";
  }

  var clock = document.getElementById("clock");
  function tick() {
    if (clock) clock.textContent = centralClock(new Date());
  }
  tick();
  window.setInterval(tick, 1000);

  var refreshControls = Array.from(document.querySelectorAll("[data-refresh-control]"));
  function setRefreshState(isRefreshing) {
    refreshControls.forEach(function (control) {
      control.disabled = isRefreshing;
      control.classList.toggle("is-scanning", isRefreshing);
      control.setAttribute("aria-label", isRefreshing ? "Refreshing fleet status" : "Refresh fleet status");
    });
  }
  refreshControls.forEach(function (control) {
    control.addEventListener("click", async function () {
      if (control.disabled) return;
      setRefreshState(true);
      try {
        await fetch("/api/run/heartbeat", {method: "POST"});
        window.location.reload();
      } catch (_) {
        setRefreshState(false);
      }
    });
  });

  var ticker = document.getElementById("wtTrack");
  if (ticker) {
    var list = ticker.querySelector(".wt-list:not(.wt-dup)");
    var viewport = ticker.closest(".wt-viewport");
    if (list && viewport && list.scrollWidth > viewport.clientWidth) {
      ticker.classList.add("wt-scrolling");
      ticker.style.setProperty("--wt-dur", Math.max(18, list.scrollWidth / 28) + "s");
    }
  }
})();
