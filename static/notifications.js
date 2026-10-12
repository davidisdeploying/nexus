(function () {
  "use strict";

  function setBellCount(n) {
    var badges = document.querySelectorAll("[data-bell-count]");
    if (!badges.length) return;
    n = Number(n) || 0;
    badges.forEach(function (badge) {
      badge.textContent = n;
      badge.style.display = n > 0 ? "" : "none";
    });
  }

  function setAppBadgeCount(n) {
    n = Number(n) || 0;
    if (n > 0 && navigator.setAppBadge) {
      return Promise.resolve(navigator.setAppBadge(n)).catch(function () {});
    }
    if (n === 0 && navigator.clearAppBadge) {
      return Promise.resolve(navigator.clearAppBadge()).catch(function () {});
    }
    return Promise.resolve();
  }

  function dismissDeliveredNotifications() {
    if (!("serviceWorker" in navigator)) return Promise.resolve();
    return navigator.serviceWorker.getRegistration()
      .then(function (registration) {
        if (!registration || !registration.getNotifications) return;
        return registration.getNotifications().then(function (notifications) {
          notifications.forEach(function (notification) { notification.close(); });
        });
      })
      .catch(function () {});
  }

  function paintAllRead() {
    setBellCount(0);
    document.querySelectorAll(".notification-item.is-unread").forEach(function (item) {
      item.classList.remove("is-unread");
    });
    document.querySelectorAll(".notification-title.unread").forEach(function (title) {
      title.classList.remove("unread");
    });
    document.querySelectorAll(".notification-group-unread").forEach(function (badge) {
      badge.remove();
    });
    var status = document.getElementById("notificationStatus");
    if (status) status.textContent = status.textContent.replace(/^\d+\s+unread/, "0 unread");
    var markAll = document.getElementById("markAllRead");
    if (markAll) markAll.remove();
  }

  function reconcileUnreadCount() {
    return fetch("/api/notify/unread-count")
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (!data || !data.ok) return;
        var unread = Number(data.unread) || 0;
        setBellCount(unread);
        return setAppBadgeCount(unread);
      })
      .catch(function () {});
  }

  function clearAllNotifications() {
    return fetch("/api/notify/mark-all-read", {method: "POST"})
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (!data || !data.ok) throw new Error("clear rejected");
        paintAllRead();
        return Promise.all([setAppBadgeCount(0), dismissDeliveredNotifications()]);
      });
  }

  function initNotificationGroupPicker() {
    var picker = document.querySelector(".notification-group-index[role='tablist']");
    if (!picker || picker.dataset.wired) return;
    picker.dataset.wired = "1";
    var buttons = Array.from(picker.querySelectorAll("[data-notification-group]"));
    var panels = Array.from(document.querySelectorAll("[data-notification-panel]"));
    if (!buttons.length || !panels.length) return;

    function activate(groupKey, updateUrl, moveFocus, preserveViewport) {
      var selected = buttons.find(function (button) {
        return button.dataset.notificationGroup === groupKey;
      });
      if (!selected) return;
      var scrollLeft = window.scrollX;
      var scrollTop = window.scrollY;
      buttons.forEach(function (button) {
        var active = button === selected;
        button.classList.toggle("is-active", active);
        button.setAttribute("aria-selected", active ? "true" : "false");
        button.tabIndex = active ? 0 : -1;
      });
      panels.forEach(function (panel) {
        panel.hidden = panel.dataset.notificationPanel !== groupKey;
      });
      var lensTitle = document.getElementById("notificationLensTitle");
      if (lensTitle) lensTitle.textContent = selected.dataset.notificationGroupLabel || groupKey;
      if (moveFocus) selected.focus({preventScroll: true});
      if (updateUrl) {
        var url = new URL(window.location.href);
        url.searchParams.set("group", groupKey);
        window.history.replaceState({notificationGroup: groupKey}, "", url);
      }
      if (preserveViewport) {
        window.requestAnimationFrame(function () {
          window.scrollTo({left: scrollLeft, top: scrollTop, behavior: "instant"});
        });
      }
    }

    picker.addEventListener("click", function (event) {
      var button = event.target.closest("[data-notification-group]");
      if (!button || !picker.contains(button)) return;
      event.preventDefault();
      activate(button.dataset.notificationGroup, true, false, true);
    });
    picker.addEventListener("keydown", function (event) {
      var current = event.target.closest("[data-notification-group]");
      if (!current) return;
      var index = buttons.indexOf(current);
      var next = index;
      if (event.key === "ArrowRight" || event.key === "ArrowDown") next = (index + 1) % buttons.length;
      else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = (index - 1 + buttons.length) % buttons.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = buttons.length - 1;
      else return;
      event.preventDefault();
      activate(buttons[next].dataset.notificationGroup, true, true, true);
    });
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("#markAllRead, #clearNotifications");
    if (!button) return;
    button.disabled = true;
    clearAllNotifications()
      .then(function () {
        var clearButton = document.getElementById("clearNotifications");
        if (!clearButton) return;
        clearButton.classList.add("is-cleared");
        clearButton.title = "Notifications cleared";
        window.setTimeout(function () {
          clearButton.classList.remove("is-cleared");
          clearButton.title = "Clear badge and delivered notifications";
        }, 1600);
      })
      .catch(function () {})
      .finally(function () { button.disabled = false; });
  });

  var standalone = window.matchMedia("(display-mode: standalone)").matches
    || window.navigator.standalone === true;

  function b64ToUint8(base64) {
    var padding = "=".repeat((4 - base64.length % 4) % 4);
    var safe = (base64 + padding).replace(/-/g, "+").replace(/_/g, "/");
    var raw = window.atob(safe);
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  function getPushManager() {
    if (window.pushManager) return Promise.resolve(window.pushManager);
    if (!("serviceWorker" in navigator)) return Promise.reject(new Error("no serviceWorker"));
    return navigator.serviceWorker.register("/sw.js")
      .then(function () { return navigator.serviceWorker.ready; })
      .then(function (registration) { return registration.pushManager; });
  }

  function vapidKey() {
    return fetch("/api/push/vapid-public-key")
      .then(function (response) {
        if (!response.ok) throw new Error("vapid key unavailable");
        return response.json();
      })
      .then(function (data) { return b64ToUint8(data.key); });
  }

  function upsertSubscription(subscription, deviceLabel) {
    return fetch("/api/push/subscribe", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        subscription: subscription.toJSON(),
        device_label: deviceLabel || "unknown"
      })
    });
  }

  function sendTest() {
    return fetch("/api/push/test", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: "{}"
    }).then(function (response) { return response.json(); });
  }

  function checkSubHealth(subscription) {
    return fetch("/api/push/sub-health", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({endpoint: subscription.endpoint})
    }).then(function (response) { return response.json(); })
      .then(function (data) { return !!(data && data.ok && data.active); })
      .catch(function () { return false; });
  }

  function setBellHealthDot(on) {
    document.querySelectorAll("[data-bell-health-dot]").forEach(function (dot) {
      dot.hidden = !on;
    });
  }

  function evaluateBellHealthDot() {
    if (!standalone) { setBellHealthDot(false); return; }
    var permission = ("Notification" in window) ? Notification.permission : "unsupported";
    if (permission !== "granted") { setBellHealthDot(true); return; }
    getPushManager().then(function (manager) { return manager.getSubscription(); })
      .then(function (subscription) {
        if (!subscription) { setBellHealthDot(true); return; }
        return checkSubHealth(subscription)
          .then(function (healthy) { setBellHealthDot(!healthy); });
      })
      .catch(function () { setBellHealthDot(true); });
  }

  function autoResubscribe(setStatus) {
    return getPushManager().then(function (manager) {
      return manager.getSubscription().then(function (subscription) {
        if (subscription) {
          setStatus("notifications enabled");
          return upsertSubscription(
            subscription,
            window.localStorage.getItem("nexus-device-label") || "unknown"
          );
        }
        setStatus("permission granted — re-arming…");
        return vapidKey()
          .then(function (key) {
            return manager.subscribe({userVisibleOnly: true, applicationServerKey: key});
          })
          .then(function (newSubscription) {
            return upsertSubscription(
              newSubscription,
              window.localStorage.getItem("nexus-device-label") || "unknown"
            );
          })
          .then(function () { setStatus("notifications enabled"); });
      });
    }).catch(function () { setStatus("push unavailable on this device"); });
  }

  function initNotifyPreferences() {
    var status = document.getElementById("push-status");
    if (!status || status.dataset.wired) return;
    status.dataset.wired = "1";
    var enable = document.getElementById("push-enable-btn");
    var picker = document.getElementById("push-device-picker");
    var result = document.getElementById("push-result");
    function setStatus(text) { status.textContent = text; }
    function setResult(text) { if (result) result.textContent = text; }

    if (!standalone) {
      setStatus("Push works in the installed app — add Nexus to your Home Screen");
      return;
    }

    function subscribeFlow(deviceLabel) {
      var manager;
      return getPushManager()
        .then(function (value) { manager = value; return vapidKey(); })
        .then(function (key) {
          return manager.subscribe({userVisibleOnly: true, applicationServerKey: key});
        })
        .then(function (subscription) { return upsertSubscription(subscription, deviceLabel); })
        .then(sendTest)
        .then(function (response) {
          setResult(response.ok ? "test push sent — check this device" : "test push failed to send");
          setStatus("notifications enabled");
          if (enable) enable.hidden = true;
          if (picker) picker.hidden = true;
          evaluateBellHealthDot();
        })
        .catch(function (error) {
          setResult("enable failed: " + (error && error.message ? error.message : "unknown error"));
        });
    }

    var permission = ("Notification" in window) ? Notification.permission : "unsupported";
    if (permission === "granted") {
      autoResubscribe(setStatus).then(evaluateBellHealthDot);
    } else if (permission === "denied") {
      setStatus("notifications blocked — re-enable in device settings");
    } else if (permission === "unsupported") {
      setStatus("push not supported on this device");
    } else {
      setStatus("tap to enable push notifications");
      if (enable) enable.hidden = false;
    }

    if (enable) {
      enable.addEventListener("click", function () {
        Notification.requestPermission().then(function (value) {
          if (value !== "granted") { setStatus("permission not granted"); return; }
          if (picker) picker.hidden = false;
          setStatus("pick a device to finish enabling");
        });
      });
    }
    if (picker) {
      picker.querySelectorAll(".push-device").forEach(function (button) {
        button.addEventListener("click", function () {
          var label = button.getAttribute("data-device");
          window.localStorage.setItem("nexus-device-label", label);
          setStatus("enabling…");
          subscribeFlow(label);
        });
      });
    }
  }

  initNotificationGroupPicker();
  initNotifyPreferences();
  evaluateBellHealthDot();
  reconcileUnreadCount();
})();
