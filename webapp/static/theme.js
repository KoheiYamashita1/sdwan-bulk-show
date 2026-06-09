// Theme toggle (light / dark / system) + a tiny CSRF header helper.
//
// The early-applied theme lives in an inline <head> script (avoids a flash of
// the wrong theme); this file only wires the topbar toggle button and exposes
// helpers used across pages. Vanilla JS, no deps.
(function () {
  "use strict";

  var STORAGE_KEY = "theme"; // "light" | "dark" | "system"
  var ORDER = ["system", "light", "dark"];
  var ICONS = { system: "🖥", light: "☀", dark: "🌙" };
  var LABELS = { system: "System", light: "Light", dark: "Dark" };

  function stored() {
    try {
      var v = window.localStorage.getItem(STORAGE_KEY);
      return v === "light" || v === "dark" || v === "system" ? v : "system";
    } catch (e) {
      return "system";
    }
  }

  function apply(mode) {
    var root = document.documentElement;
    if (mode === "system") {
      root.removeAttribute("data-theme");
    } else {
      root.setAttribute("data-theme", mode);
    }
  }

  function persist(mode) {
    try {
      window.localStorage.setItem(STORAGE_KEY, mode);
    } catch (e) {
      /* private mode: just skip persistence */
    }
  }

  function syncButton(btn, mode) {
    if (!btn) {
      return;
    }
    var icon = btn.querySelector(".theme-toggle__icon");
    var label = btn.querySelector(".theme-toggle__label");
    if (icon) {
      icon.textContent = ICONS[mode] || ICONS.system;
    }
    if (label) {
      label.textContent = LABELS[mode] || LABELS.system;
    }
    btn.setAttribute("aria-label", "Theme: " + (LABELS[mode] || "System"));
  }

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("theme-toggle");
    var mode = stored();
    apply(mode);
    syncButton(btn, mode);
    if (btn) {
      btn.addEventListener("click", function () {
        var next = ORDER[(ORDER.indexOf(stored()) + 1) % ORDER.length];
        apply(next);
        persist(next);
        syncButton(btn, next);
      });
    }
  });

  // CSRF helper: same-origin loopback fetches already satisfy the server
  // guard, but if WEBAPP_TOKEN is configured the server renders a
  // <meta name="webapp-token"> we forward as X-Webapp-Token.
  window.csrfHeaders = function (extra) {
    var headers = {};
    var key;
    if (extra) {
      for (key in extra) {
        if (Object.prototype.hasOwnProperty.call(extra, key)) {
          headers[key] = extra[key];
        }
      }
    }
    var meta = document.querySelector('meta[name="webapp-token"]');
    var token = meta && meta.getAttribute("content");
    if (token) {
      headers["X-Webapp-Token"] = token;
    }
    return headers;
  };
})();
