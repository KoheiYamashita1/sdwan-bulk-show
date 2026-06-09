// Shared "pick two files and diff" widget used by the index inline results,
// the run detail page, and the standalone compare page.
//
// Security notes:
//  * The diff is computed SERVER-SIDE (GET /api/runs/<ts>/diff). The client
//    only ever assigns textContent (never innerHTML), so device output / diff
//    text cannot inject markup.
//  * Per-file "Open" posts only the filename to POST /runs/<ts>/open; the
//    server resolves it against logs/<ts>/ with the usual path-safety.
(function () {
  "use strict";

  function el(tag, cls) {
    var node = document.createElement(tag);
    if (cls) {
      node.className = cls;
    }
    return node;
  }

  // Render unified-diff lines into a <pre>, colouring by leading character.
  // Each line becomes its own <span> so CSS classes can tint it; textContent
  // keeps it inert.
  function renderDiff(bodyEl, lines) {
    bodyEl.textContent = "";
    lines.forEach(function (line) {
      var row = el("span", "diffline");
      var head = line.charAt(0);
      if (line.indexOf("@@") === 0) {
        row.classList.add("diffline--hunk");
      } else if (head === "+") {
        row.classList.add("diffline--add");
      } else if (head === "-") {
        row.classList.add("diffline--del");
      } else {
        row.classList.add("diffline--ctx");
      }
      row.textContent = line + "\n";
      bodyEl.appendChild(row);
    });
  }

  function init(opts) {
    var container = opts && opts.container;
    var timestamp = opts && opts.timestamp;
    var files = (opts && opts.files) || [];
    if (!container || !timestamp) {
      return;
    }

    container.textContent = "";

    if (!files.length) {
      var empty = el("p", "empty");
      empty.textContent = "No output_* files captured for this run.";
      container.appendChild(empty);
      return;
    }

    var hint = el("span", "toolbar__hint filediff__hint");
    hint.setAttribute("role", "status");
    hint.setAttribute("aria-live", "polite");

    function setHint(text, isError) {
      hint.textContent = text || "";
      hint.classList.toggle("toolbar__hint--error", Boolean(isError));
    }

    var list = el("ul", "filelist");
    var checkboxes = [];

    function selected() {
      return checkboxes.filter(function (cb) {
        return cb.checked;
      });
    }

    function onChange() {
      var sel = selected();
      var atMax = sel.length >= 2;
      // Disabling the remaining checkboxes once two are picked is the cleaner
      // UX than silently auto-unchecking the oldest selection.
      checkboxes.forEach(function (cb) {
        cb.disabled = atMax && !cb.checked;
      });
      diffBtn.disabled = sel.length !== 2;
    }

    function openFile(name, btn) {
      setHint("Opening " + name + "…", false);
      if (btn) {
        btn.disabled = true;
      }
      fetch("/runs/" + encodeURIComponent(timestamp) + "/open", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Requested-With": "XMLHttpRequest",
        },
        body: JSON.stringify({ name: name }),
      })
        .then(function (resp) {
          return resp.json().then(function (body) {
            return { ok: resp.ok, body: body };
          });
        })
        .then(function (res) {
          if (res.ok && res.body && res.body.ok) {
            setHint("Opened " + name + ".", false);
          } else {
            setHint((res.body && res.body.error) || "Could not open file.", true);
          }
        })
        .catch(function () {
          setHint("Could not reach the server.", true);
        })
        .then(function () {
          if (btn) {
            btn.disabled = false;
          }
        });
    }

    files.forEach(function (name) {
      var row = el("li", "filelist__row");

      var label = el("label", "filelist__label");
      var cb = el("input");
      cb.type = "checkbox";
      cb.className = "filelist__check";
      cb.value = name;
      cb.addEventListener("change", onChange);
      var nameSpan = el("span", "filelist__name");
      nameSpan.textContent = name;
      label.appendChild(cb);
      label.appendChild(nameSpan);
      row.appendChild(label);

      var openBtn = el("button", "button button--ghost filelist__open");
      openBtn.type = "button";
      openBtn.textContent = "Open";
      openBtn.addEventListener("click", function () {
        openFile(name, openBtn);
      });
      row.appendChild(openBtn);

      list.appendChild(row);
      checkboxes.push(cb);
    });
    container.appendChild(list);

    var actions = el("div", "filediff__actions");
    var diffBtn = el("button", "button filediff__diff");
    diffBtn.type = "button";
    diffBtn.textContent = "Diff";
    diffBtn.disabled = true;
    actions.appendChild(diffBtn);
    actions.appendChild(hint);
    container.appendChild(actions);

    var panel = el("section", "filediff__panel");
    panel.hidden = true;
    var header = el("div", "filediff__header");
    var body = el("pre", "filediff__body");
    panel.appendChild(header);
    panel.appendChild(body);
    container.appendChild(panel);

    diffBtn.addEventListener("click", function () {
      var sel = selected();
      if (sel.length !== 2) {
        return;
      }
      var a = sel[0].value;
      var b = sel[1].value;
      setHint("Diffing…", false);
      fetch(
        "/api/runs/" + encodeURIComponent(timestamp) + "/diff?a=" +
          encodeURIComponent(a) + "&b=" + encodeURIComponent(b),
        { headers: { Accept: "application/json" }, cache: "no-store" }
      )
        .then(function (resp) {
          return resp.json().then(function (data) {
            return { ok: resp.ok, data: data };
          });
        })
        .then(function (res) {
          if (!res.ok || !res.data) {
            setHint((res.data && res.data.error) || "Diff failed.", true);
            return;
          }
          var data = res.data;
          panel.hidden = false;
          header.textContent = data.a + " \u2194 " + data.b;
          if (data.identical) {
            body.textContent = "";
            renderDiff(body, []);
            var note = el("span", "diffline diffline--ctx");
            note.textContent = "Files are identical.";
            body.appendChild(note);
          } else {
            renderDiff(body, data.diff || []);
          }
          var trunc = [];
          if (data.a_truncated) {
            trunc.push(data.a);
          }
          if (data.b_truncated) {
            trunc.push(data.b);
          }
          setHint(trunc.length ? "truncated: " + trunc.join(", ") : "", false);
        })
        .catch(function () {
          setHint("Could not reach the server.", true);
        });
    });
  }

  window.FileDiff = { init: init };
})();
