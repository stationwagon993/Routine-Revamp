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
