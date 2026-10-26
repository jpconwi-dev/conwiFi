(function () {
  "use strict";
  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  });
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-confirm-text]");
    if (t && !window.confirm(t.getAttribute("data-confirm-text"))) e.preventDefault();
  });
  var ar = document.querySelector("[data-autorefresh]");
  if (ar) {
    var secs = parseInt(ar.getAttribute("data-autorefresh"), 10) || 30;
    setTimeout(function () { if (!document.querySelector("input:focus,textarea:focus")) location.reload(); }, secs * 1000);
  }
})();
