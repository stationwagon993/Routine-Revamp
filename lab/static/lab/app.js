// Sample log: clicking a row opens the sample.
document.querySelectorAll("tr[data-href]").forEach(function (row) {
  row.addEventListener("click", function (event) {
    if (event.target.closest("a, button, input, select")) return;
    window.location = row.dataset.href;
  });
});

// Sample log: the grid fills the screen, and each column can be widened or narrowed by
// dragging the right edge of its header. Widths are remembered in this browser only.
(function () {
  var table = document.querySelector("table.log");
  if (!table) return;
  var wrap = table.parentElement;
  var headers = Array.prototype.slice.call(table.querySelectorAll("tr.cols th[data-col]"));
  if (!headers.length) return;
  var KEY = "routine-revamp.log-column-widths";
  var MIN = 24;

  var colgroup = document.createElement("colgroup");
  var cols = headers.map(function () { return colgroup.appendChild(document.createElement("col")); });
  table.insertBefore(colgroup, table.firstChild);

  function load() {
    try { return JSON.parse(localStorage.getItem(KEY)) || null; } catch (e) { return null; }
  }
  function save(widths) {
    try { localStorage.setItem(KEY, JSON.stringify(widths)); } catch (e) { /* not remembered */ }
  }
  function forget() {
    try { localStorage.removeItem(KEY); } catch (e) { /* nothing saved */ }
  }

  function apply(widths) {
    var total = 0;
    widths.forEach(function (w, i) { cols[i].style.width = w + "px"; total += w; });
    table.style.width = total + "px";
    table.classList.add("sized");
  }

  // Natural widths (what the browser picks for the content), stretched to fill the screen.
  function fit() {
    table.classList.remove("sized");
    table.style.width = "";
    cols.forEach(function (col) { col.style.width = ""; });
    var natural = headers.map(function (th) { return th.getBoundingClientRect().width; });
    var sum = natural.reduce(function (a, b) { return a + b; }, 0);
    var available = wrap.clientWidth;
    var scale = sum < available ? available / sum : 1;
    var widths = natural.map(function (w) { return Math.floor(w * scale); });
    widths[widths.length - 1] += Math.max(0, Math.floor(available - widths.reduce(function (a, b) { return a + b; }, 0)));
    apply(widths);
    return widths;
  }

  function current() {
    return cols.map(function (col) { return parseFloat(col.style.width); });
  }

  function start() {
    var saved = load();
    if (saved) {
      var widths = fit();
      headers.forEach(function (th, i) {
        var w = saved[th.dataset.col];
        if (w) widths[i] = w;
      });
      apply(widths);
    } else {
      fit();
    }
  }

  headers.forEach(function (th, i) {
    var handle = document.createElement("div");
    handle.className = "col-resizer";
    handle.title = "Drag to resize";
    th.appendChild(handle);
    handle.addEventListener("mousedown", function (event) {
      event.preventDefault();
      var startX = event.clientX;
      var widths = current();
      var startWidth = widths[i];
      handle.classList.add("dragging");
      document.body.classList.add("resizing-columns");
      function move(e) {
        widths[i] = Math.max(MIN, startWidth + e.clientX - startX);
        apply(widths);
      }
      function up() {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        handle.classList.remove("dragging");
        document.body.classList.remove("resizing-columns");
        var byName = {};
        headers.forEach(function (h, j) { byName[h.dataset.col] = widths[j]; });
        save(byName);
      }
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });
  });

  var fitButton = document.getElementById("fit-columns");
  if (fitButton) fitButton.addEventListener("click", function () { forget(); fit(); });

  var timer = null;
  window.addEventListener("resize", function () {
    clearTimeout(timer);
    timer = setTimeout(function () { if (!load()) fit(); }, 150);
  });

  start();
})();

// Sample log: close the "Testing needed" menu when clicking elsewhere.
document.addEventListener("click", function (event) {
  document.querySelectorAll("details.filter-menu[open]").forEach(function (menu) {
    if (!menu.contains(event.target)) menu.open = false;
  });
});

// Result entry: recalculate on the server as the user types (nothing is saved).
document.querySelectorAll("form[data-calc-url]").forEach(function (form) {
  var output = form.querySelector(".calc-output");
  var timer = null;

  function render(data) {
    output.innerHTML = "";
    if (!data.ok) {
      var err = document.createElement("span");
      err.className = "error";
      err.textContent = data.error;
      output.appendChild(err);
      return;
    }
    data.results.forEach(function (r) {
      var line = document.createElement("div");
      if (r.label) line.appendChild(document.createTextNode(r.label + ": "));
      var value = document.createElement("span");
      value.className = "value";
      value.textContent = r.value + (r.unit ? " " + r.unit : "");
      line.appendChild(value);
      output.appendChild(line);
    });
  }

  function recalc() {
    var filled = Array.prototype.every.call(
      form.querySelectorAll("input[data-required]"),
      function (input) { return input.value.trim() !== ""; }
    );
    if (!filled) { output.innerHTML = ""; return; }
    fetch(form.dataset.calcUrl, { method: "POST", body: new FormData(form) })
      .then(function (response) { return response.json(); })
      .then(render)
      .catch(function () { output.textContent = "Could not calculate."; });
  }

  form.addEventListener("input", function () {
    clearTimeout(timer);
    timer = setTimeout(recalc, 250);
  });
  recalc();
});

// Sample submission form: show only the questions and tests that apply,
// the same way the lab's Microsoft Form branches. The server checks the same rules.
(function () {
  var form = document.getElementById("intake-form");
  if (!form) return;
  var OTHER = "__other__";
  var singleComponent = (form.dataset.singleComponent || "").split(" ").filter(Boolean);

  // Same comparison as the server: R-1233zd(E) = r1233zde.
  function nameKey(name) { return name.toLowerCase().replace(/[^a-z0-9]/g, ""); }

  function value(name) {
    var els = form.querySelectorAll('[name="' + name + '"]');
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (el.type === "radio") { if (el.checked) return el.value; }
      else return el.value;
    }
    return "";
  }

  function flags() {
    var origin = value("origin");
    var f = {};
    f.tank = origin === "New Refrigerant Tank";
    f.drum = origin === "New Lubricant Drum";
    f.plant7 = origin === "Plant 7";
    f.lab11 = origin === "Lab 11";
    f.system = origin !== "" && !f.tank && !f.drum;
    f.compressor = f.lab11 && value("lab11_area") === "Compressor Group";
    f.unit = f.system && !f.compressor;
    f["general-refrigerant"] = f.tank || (f.system && !f.plant7);
    var type = f.tank ? "refrigerant" : f.drum ? "oil" : f.system ? value("sample_type") : "";
    f["refrigerant-sample"] = type === "refrigerant";
    f["type-other"] = type === "other";
    f.kind = type === "refrigerant" ? value("phase") : type;
    var refrigerantField = f.plant7 ? "plant7_refrigerant" : "refrigerant";
    var refrigerant = value(refrigerantField);
    if (refrigerant === OTHER) refrigerant = value(refrigerantField + "_other");
    f.singleComponent = singleComponent.indexOf(nameKey(refrigerant)) !== -1;
    return f;
  }

  function update() {
    var f = flags();
    form.querySelectorAll("[data-when]").forEach(function (el) {
      el.hidden = !el.dataset.when.split(" ").some(function (k) { return f[k]; });
    });
    form.querySelectorAll("[data-other-for]").forEach(function (el) {
      var select = form.querySelector('[name="' + el.dataset.otherFor + '"]');
      var shown = select && select.value === OTHER && !select.closest("[hidden]");
      el.hidden = !shown;
    });
    var any = false;
    form.querySelectorAll("label[data-applies]").forEach(function (label) {
      var applies = label.dataset.applies.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
      var ok = !!f.kind && (f.kind === "other" || applies.length === 0 || applies.indexOf(f.kind) !== -1);
      // No Blend Analysis for single-component refrigerants.
      if (f.singleComponent && label.querySelector("input[data-blend]")) ok = false;
      label.hidden = !ok;
      if (!ok) label.querySelector("input").checked = false;
      any = any || ok;
    });
    form.querySelectorAll(".test-group").forEach(function (group) {
      group.hidden = !group.querySelector("label[data-applies]:not([hidden])");
    });
    var hint = document.getElementById("tests-hint");
    if (hint) hint.hidden = any;
  }

  form.addEventListener("change", update);
  form.addEventListener("input", update);
  update();
})();
