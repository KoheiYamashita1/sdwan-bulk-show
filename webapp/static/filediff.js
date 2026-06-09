// Diff experience — the hero feature.
//
// Two public entry points on window.FileDiff:
//   * init(opts)        — the "pick two files and diff" widget (index inline
//                         results, run detail, single-run compare).
//   * createPanel(opts) — a reusable diff panel (sticky header + stats +
//                         toolbar + body) the cross-run compare page drives
//                         directly with diff-across JSON.
//
// Security notes:
//   * Diffs are computed SERVER-SIDE. The client ONLY ever assigns
//     textContent (never innerHTML), so device output / diff text cannot
//     inject markup. Intra-line segments are rendered as nested <span>s with
//     textContent per segment.
//   * Per-file "Open" posts only the filename to POST /runs/<ts>/open; the
//     server resolves it against logs/<ts>/ with the usual path-safety.
(function () {
  "use strict";

  // ---- prefs (persisted toggle state) ----------------------------------
  var PREF = {
    mode: "filediff.mode", // "sxs" | "unified"
    wrap: "filediff.wrap", // "1" | "0"
    collapse: "filediff.collapse", // "1" | "0"
  };

  function getPref(key, fallback) {
    try {
      var v = window.localStorage.getItem(key);
      return v == null ? fallback : v;
    } catch (e) {
      return fallback;
    }
  }

  function setPref(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (e) {
      /* ignore private-mode failures */
    }
  }

  function el(tag, cls) {
    var node = document.createElement(tag);
    if (cls) {
      node.className = cls;
    }
    return node;
  }

  function headers(extra) {
    return typeof window.csrfHeaders === "function"
      ? window.csrfHeaders(extra)
      : extra || {};
  }

  // ---- side-by-side rendering ------------------------------------------

  function renderSegments(codeEl, segments, kind) {
    // kind: "del" | "add". Render nested spans, highlighting changed runs.
    codeEl.textContent = "";
    segments.forEach(function (seg) {
      if (seg.change) {
        var span = el("span", "sxs__seg--" + kind);
        span.textContent = seg.text;
        codeEl.appendChild(span);
      } else {
        codeEl.appendChild(document.createTextNode(seg.text));
      }
    });
  }

  function makeCell(row, side) {
    var num = el("td", "sxs__ln");
    var lineNo = side === "left" ? row.ln : row.rn;
    num.textContent = lineNo == null ? "" : String(lineNo);

    var code = el("td", "sxs__code sxs__code--" + side);
    var text = side === "left" ? row.left : row.right;
    var tag = row.tag;
    if (text == null) {
      code.classList.add("sxs__code--empty");
      return [num, code];
    }
    if (side === "left" && (tag === "delete" || tag === "replace")) {
      code.classList.add("sxs__code--del");
    } else if (side === "right" && (tag === "insert" || tag === "replace")) {
      code.classList.add("sxs__code--add");
    }
    // Intra-line word/char highlighting on replace rows (C4).
    var segs = side === "left" ? row.left_segments : row.right_segments;
    if (tag === "replace" && Array.isArray(segs)) {
      renderSegments(code, segs, side === "left" ? "del" : "add");
    } else {
      code.textContent = text;
    }
    return [num, code];
  }

  function appendRow(tbody, row) {
    var tr = el("tr", "sxs__row sxs__row--" + row.tag);
    var left = makeCell(row, "left");
    var right = makeCell(row, "right");
    tr.appendChild(left[0]);
    tr.appendChild(left[1]);
    tr.appendChild(right[0]);
    tr.appendChild(right[1]);
    tbody.appendChild(tr);
  }

  // Render rows into a side-by-side table; when `collapse` is on, runs of
  // equal rows become a single "… N unchanged …" fold that expands on click.
  function renderSideBySide(bodyEl, rows, collapse) {
    bodyEl.textContent = "";
    var table = el("table", "sxs");
    var tbody = el("tbody");

    if (!collapse) {
      rows.forEach(function (r) {
        appendRow(tbody, r);
      });
      table.appendChild(tbody);
      bodyEl.appendChild(table);
      return;
    }

    var i = 0;
    while (i < rows.length) {
      if (rows[i].tag === "equal") {
        var run = [];
        while (i < rows.length && rows[i].tag === "equal") {
          run.push(rows[i]);
          i += 1;
        }
        if (run.length > 3) {
          (function (groupRows) {
            var fold = el("tr", "sxs__row sxs__row--fold");
            var td = el("td");
            td.colSpan = 4;
            td.textContent = "… " + groupRows.length + " unchanged lines …";
            fold.appendChild(td);
            fold.addEventListener("click", function () {
              var anchor = fold;
              groupRows.forEach(function (r) {
                var tr = el("tr", "sxs__row sxs__row--" + r.tag);
                var left = makeCell(r, "left");
                var right = makeCell(r, "right");
                tr.appendChild(left[0]);
                tr.appendChild(left[1]);
                tr.appendChild(right[0]);
                tr.appendChild(right[1]);
                anchor.parentNode.insertBefore(tr, anchor.nextSibling);
                anchor = tr;
              });
              fold.parentNode.removeChild(fold);
            });
            tbody.appendChild(fold);
          })(run);
        } else {
          run.forEach(function (r) {
            appendRow(tbody, r);
          });
        }
      } else {
        appendRow(tbody, rows[i]);
        i += 1;
      }
    }

    table.appendChild(tbody);
    bodyEl.appendChild(table);
  }

  // ---- unified rendering -----------------------------------------------

  function classForLine(line) {
    if (line.indexOf("@@") === 0) {
      return "diffline--hunk";
    }
    if (line.indexOf("+++") === 0 || line.indexOf("---") === 0) {
      return "diffline--hunk";
    }
    if (line.charAt(0) === "+") {
      return "diffline--add";
    }
    if (line.charAt(0) === "-") {
      return "diffline--del";
    }
    return "diffline--ctx";
  }

  function renderUnified(bodyEl, lines) {
    bodyEl.textContent = "";
    var pre = el("pre", "diff-unified");
    lines.forEach(function (line) {
      var span = el("span", "diffline " + classForLine(line));
      span.textContent = line;
      pre.appendChild(span);
    });
    bodyEl.appendChild(pre);
  }

  // ---- reusable diff panel ---------------------------------------------

  function createPanel() {
    var prefs = {
      mode: getPref(PREF.mode, "sxs"),
      wrap: getPref(PREF.wrap, "1") === "1",
      collapse: getPref(PREF.collapse, "0") === "1",
      full: false,
    };
    var data = null;

    var panel = el("section", "filediff__panel");
    panel.hidden = true;

    var header = el("div", "filediff__header");
    var title = el("div", "filediff__title");
    var stats = el("span", "filediff__stats");
    title.appendChild(stats);
    var titleText = el("span");
    title.appendChild(titleText);
    header.appendChild(title);

    var tools = el("div", "filediff__tools");
    function toggleBtn(label, pressed) {
      var b = el("button", "diff-toggle");
      b.type = "button";
      b.textContent = label;
      b.setAttribute("aria-pressed", pressed ? "true" : "false");
      return b;
    }
    var modeBtn = toggleBtn("Side-by-side", prefs.mode === "sxs");
    var collapseBtn = toggleBtn("Collapse equal", prefs.collapse);
    var wrapBtn = toggleBtn("Wrap", prefs.wrap);
    var fullBtn = toggleBtn("Fullscreen", false);
    tools.appendChild(modeBtn);
    tools.appendChild(collapseBtn);
    tools.appendChild(wrapBtn);
    tools.appendChild(fullBtn);
    header.appendChild(tools);

    var body = el("div", "filediff__body");
    panel.appendChild(header);
    panel.appendChild(body);

    function renderStats() {
      stats.textContent = "";
      var s = (data && data.stats) || {};
      var add = el("span", "filediff__stat--add");
      add.textContent = "+" + (s.added || 0);
      var del = el("span", "filediff__stat--del");
      del.textContent = "\u2212" + (s.removed || 0);
      var chg = el("span", "filediff__stat--chg");
      chg.textContent = "~" + (s.changed || 0);
      stats.appendChild(add);
      stats.appendChild(del);
      stats.appendChild(chg);
    }

    function renderBody() {
      body.classList.toggle("filediff__body--nowrap", !prefs.wrap);
      if (!data) {
        return;
      }
      if (data.identical) {
        body.textContent = "";
        var note = el("p", "filediff__identical");
        note.textContent = "Files are identical.";
        body.appendChild(note);
        return;
      }
      if (prefs.mode === "unified") {
        renderUnified(body, data.diff || []);
      } else {
        renderSideBySide(body, data.rows || [], prefs.collapse);
      }
    }

    modeBtn.addEventListener("click", function () {
      prefs.mode = prefs.mode === "sxs" ? "unified" : "sxs";
      setPref(PREF.mode, prefs.mode);
      modeBtn.textContent = prefs.mode === "sxs" ? "Side-by-side" : "Unified";
      modeBtn.setAttribute("aria-pressed", prefs.mode === "sxs" ? "true" : "false");
      collapseBtn.disabled = prefs.mode === "unified";
      renderBody();
    });
    modeBtn.textContent = prefs.mode === "sxs" ? "Side-by-side" : "Unified";
    collapseBtn.disabled = prefs.mode === "unified";

    collapseBtn.addEventListener("click", function () {
      prefs.collapse = !prefs.collapse;
      setPref(PREF.collapse, prefs.collapse ? "1" : "0");
      collapseBtn.setAttribute("aria-pressed", prefs.collapse ? "true" : "false");
      renderBody();
    });

    wrapBtn.addEventListener("click", function () {
      prefs.wrap = !prefs.wrap;
      setPref(PREF.wrap, prefs.wrap ? "1" : "0");
      wrapBtn.setAttribute("aria-pressed", prefs.wrap ? "true" : "false");
      renderBody();
    });

    function setFull(on) {
      prefs.full = on;
      panel.classList.toggle("filediff__panel--full", on);
      fullBtn.setAttribute("aria-pressed", on ? "true" : "false");
    }

    function onKey(e) {
      if (e.key === "Escape" && prefs.full) {
        setFull(false);
      }
    }

    fullBtn.addEventListener("click", function () {
      setFull(!prefs.full);
    });
    document.addEventListener("keydown", onKey);

    return {
      el: panel,
      update: function (newData) {
        data = newData;
        panel.hidden = false;
        titleText.textContent = " " + (data.a || "") + " \u2194 " + (data.b || "");
        renderStats();
        renderBody();
        return panel;
      },
      showSkeleton: function () {
        panel.hidden = false;
        titleText.textContent = " loading…";
        stats.textContent = "";
        body.textContent = "";
        var sk = el("div", "skeleton");
        for (var i = 0; i < 4; i += 1) {
          sk.appendChild(el("div", "skeleton__line"));
        }
        body.appendChild(sk);
      },
    };
  }

  // ---- pick-two-and-diff widget ----------------------------------------

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
        headers: headers({
          "Content-Type": "application/json",
          "X-Requested-With": "XMLHttpRequest",
        }),
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

      var openBtn = el("button", "button button--ghost button--sm filelist__open");
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

    var panel = createPanel();
    container.appendChild(panel.el);

    // Auto-select when exactly two files exist: pre-check both, enable Diff.
    if (checkboxes.length === 2) {
      checkboxes[0].checked = true;
      checkboxes[1].checked = true;
      onChange();
    }

    diffBtn.addEventListener("click", function () {
      var sel = selected();
      if (sel.length !== 2) {
        return;
      }
      var a = sel[0].value;
      var b = sel[1].value;
      setHint("Diffing…", false);
      panel.showSkeleton();
      fetch(
        "/api/runs/" + encodeURIComponent(timestamp) + "/diff?a=" +
          encodeURIComponent(a) + "&b=" + encodeURIComponent(b),
        { headers: headers({ Accept: "application/json" }), cache: "no-store" }
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
          panel.update(res.data);
          var trunc = [];
          if (res.data.a_truncated) {
            trunc.push(res.data.a);
          }
          if (res.data.b_truncated) {
            trunc.push(res.data.b);
          }
          setHint(trunc.length ? "truncated: " + trunc.join(", ") : "", false);
        })
        .catch(function () {
          setHint("Could not reach the server.", true);
        });
    });
  }

  window.FileDiff = { init: init, createPanel: createPanel };
})();
