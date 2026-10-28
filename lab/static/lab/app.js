// Sample log: clicking a row opens the sample.
document.querySelectorAll("tr[data-href]").forEach(function (row) {
  row.addEventListener("click", function (event) {
    if (event.target.closest("a, button, input, select")) return;
    window.location = row.dataset.href;
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
    f["general-refrigerant"] = f.tank || (f.system && !f.plant7);
    var type = f.tank ? "refrigerant" : f.drum ? "oil" : f.system ? value("sample_type") : "";
    f["refrigerant-sample"] = type === "refrigerant";
    f["type-other"] = type === "other";
    f.kind = type === "refrigerant" ? value("phase") : type;
    var blend = form.querySelector("input[data-blend]");
    f["blend-requested"] = !blend || blend.checked;
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
      label.hidden = !ok;
      if (!ok) label.querySelector("input").checked = false;
      any = any || ok;
    });
    form.querySelectorAll(".test-group").forEach(function (group) {
      group.hidden = !group.querySelector("label[data-applies]:not([hidden])");
    });
    var hint = document.getElementById("tests-hint");
    if (hint) hint.hidden = any;
    var blend = form.querySelector("input[data-blend]");
    var blendBox = form.querySelector('[data-when="blend-requested"]');
    if (blendBox) blendBox.hidden = !(blend && blend.checked && !blend.closest("[hidden]"));
  }

  form.addEventListener("change", update);
  form.addEventListener("input", update);
  update();
})();
