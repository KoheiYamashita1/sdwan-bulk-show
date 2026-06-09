// Shared progress rendering: the Connect → Upload → Execute → Download → Done
// stepper plus the percent bar / counters / log tail. Used by the index inline
// run flow and the standalone run_progress page so both stay in sync.
//
// textContent only (masked log lines can never inject markup).
(function () {
  "use strict";

  var STEPS = ["Connect", "Upload", "Execute", "Download", "Done"];
  var TERMINAL = {
    success: true,
    failed: true,
    timeout: true,
    error: true,
    cancelled: true,
  };

  // Map a phase string (and percent fallback) to the active step index 0..4.
  function activeStep(phase, status, percent) {
    if (status === "success") {
      return 4;
    }
    var p = String(phase || "").toLowerCase();
    if (p.indexOf("done") >= 0) {
      return 4;
    }
    if (p.indexOf("download") >= 0 || p.indexOf("wrapping") >= 0) {
      return 3;
    }
    if (p.indexOf("running") >= 0 || p.indexOf("final") >= 0 || p.indexOf("vshell") >= 0) {
      return 2;
    }
    if (p.indexOf("upload") >= 0) {
      return 1;
    }
    if (p.indexOf("connect") >= 0 || p.indexOf("start") >= 0) {
      return 0;
    }
    // Fallback by percent.
    if (percent >= 95) {
      return 3;
    }
    if (percent >= 25) {
      return 2;
    }
    if (percent >= 15) {
      return 1;
    }
    return 0;
  }

  // Build the 5 step <li>s into an <ol class="stepper"> (idempotent).
  function ensureSteps(ol) {
    if (ol.childElementCount === STEPS.length) {
      return;
    }
    ol.textContent = "";
    STEPS.forEach(function (name) {
      var li = document.createElement("li");
      li.className = "stepper__step";
      var label = document.createElement("span");
      label.className = "stepper__label";
      label.textContent = name;
      li.appendChild(label);
      ol.appendChild(li);
    });
  }

  function renderStepper(ol, phase, status, percent) {
    if (!ol) {
      return;
    }
    ensureSteps(ol);
    var terminal = TERMINAL[status] || false;
    ol.setAttribute("data-terminal", terminal && status !== "success" ? status : "");
    var active = activeStep(phase, status, percent);
    var steps = ol.children;
    for (var i = 0; i < steps.length; i += 1) {
      steps[i].classList.remove("stepper__step--done", "stepper__step--active");
      if (i < active) {
        steps[i].classList.add("stepper__step--done");
      } else if (i === active) {
        // On a clean success the final step reads "done" rather than "active".
        if (status === "success") {
          steps[i].classList.add("stepper__step--done");
        } else {
          steps[i].classList.add("stepper__step--active");
        }
      }
    }
  }

  // Fold one progress snapshot into a map of elements. `els` may include:
  // bar, percent, phase, status, wrap, counters, hosts, message, log, stepper.
  function applyProgress(els, data) {
    els = els || {};
    var pct = typeof data.percent === "number" ? data.percent : 0;
    if (els.bar) {
      els.bar.value = pct;
    }
    if (els.percent) {
      els.percent.textContent = pct + "%";
    }
    if (els.phase) {
      els.phase.textContent = data.phase || "";
    }
    if (els.status) {
      els.status.textContent = data.status || "";
      els.status.className = "status status--" + (data.status || "unknown");
    }
    if (els.wrap) {
      els.wrap.setAttribute("data-status", data.status || "running");
    }
    if (els.counters) {
      els.counters.textContent =
        "commands " + (data.commands_done || 0) + " / " + (data.commands_total || 0);
    }
    if (els.hosts) {
      els.hosts.textContent = "hosts " + (data.hosts_total || 0);
    }
    if (els.message) {
      els.message.textContent = data.message || "";
    }
    if (els.log && Array.isArray(data.log_tail)) {
      // Autoscroll only when the user is already pinned to the bottom.
      var pinned =
        els.log.scrollTop + els.log.clientHeight >= els.log.scrollHeight - 4;
      els.log.textContent = data.log_tail.join("\n");
      if (pinned) {
        els.log.scrollTop = els.log.scrollHeight;
      }
    }
    if (els.stepper) {
      renderStepper(els.stepper, data.phase, data.status, pct);
    }
  }

  window.ProgressUI = {
    STEPS: STEPS,
    renderStepper: renderStepper,
    applyProgress: applyProgress,
    isTerminal: function (status) {
      return Boolean(TERMINAL[status]);
    },
  };
})();
