// Shared "open in Finder / Terminal" wiring + a lightweight toast layer.
//
// Extracted from the per-page inline scripts (index, run detail, single-run
// compare) so the open-folder behaviour lives in one place. Vanilla JS, no
// deps; same-origin POSTs satisfy the server CSRF guard.
(function () {
  "use strict";

  // ---- toast layer ------------------------------------------------------
  function toast(message, kind, timeoutMs) {
    var stack = document.getElementById("toast-stack");
    if (!stack || !message) {
      return;
    }
    var node = document.createElement("div");
    node.className = "toast" + (kind ? " toast--" + kind : "");
    node.setAttribute("role", kind === "error" ? "alert" : "status");
    node.textContent = message;
    stack.appendChild(node);
    var ttl = typeof timeoutMs === "number" ? timeoutMs : 3500;
    window.setTimeout(function () {
      if (node.parentNode) {
        node.parentNode.removeChild(node);
      }
    }, ttl);
  }
  window.toast = toast;

  function headers(extra) {
    return typeof window.csrfHeaders === "function"
      ? window.csrfHeaders(extra)
      : extra || {};
  }

  function setStatus(statusEl, text, isError) {
    if (!statusEl) {
      return;
    }
    statusEl.textContent = text || "";
    statusEl.classList.toggle("toolbar__hint--error", Boolean(isError));
  }

  // POST a folder/file open and reflect the outcome in statusEl + a toast.
  function open(timestamp, payload, statusEl) {
    if (!timestamp) {
      return Promise.resolve();
    }
    var what = payload && payload.target ? payload.target : "item";
    setStatus(statusEl, "Opening " + what + "…", false);
    return fetch("/runs/" + encodeURIComponent(timestamp) + "/open", {
      method: "POST",
      headers: headers({
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
      }),
      body: JSON.stringify(payload || {}),
    })
      .then(function (resp) {
        return resp.json().then(function (body) {
          return { ok: resp.ok, body: body };
        });
      })
      .then(function (res) {
        if (res.ok && res.body && res.body.ok) {
          setStatus(statusEl, "Opened in " + what + ".", false);
          toast("Opened in " + what + ".", "ok");
        } else {
          var msg = (res.body && res.body.error) || "Could not open " + what + ".";
          setStatus(statusEl, msg, true);
          toast(msg, "error");
        }
      })
      .catch(function () {
        setStatus(statusEl, "Could not reach the server.", true);
        toast("Could not reach the server.", "error");
      });
  }

  // Wire the standard Reveal-in-Finder / Open-in-Terminal buttons.
  // opts: { finder, terminal, statusEl, getTimestamp }
  function bindFolderButtons(opts) {
    opts = opts || {};
    var getTs =
      typeof opts.getTimestamp === "function"
        ? opts.getTimestamp
        : function () {
            return opts.timestamp || null;
          };
    if (opts.finder) {
      opts.finder.addEventListener("click", function () {
        open(getTs(), { target: "finder" }, opts.statusEl);
      });
    }
    if (opts.terminal) {
      opts.terminal.addEventListener("click", function () {
        open(getTs(), { target: "terminal" }, opts.statusEl);
      });
    }
  }

  window.OpenActions = { open: open, bindFolderButtons: bindFolderButtons };
})();
