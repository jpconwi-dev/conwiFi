(function () {
  "use strict";
  var csrf = document.querySelector('meta[name="csrf-token"]').content;
  var $ = function (id) { return document.getElementById(id); };
  var state = null, syncedAt = 0;

  function post(url, body) {
    return fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(body || {})
    }).then(function (r) { return r.json().then(function (j) { j._status = r.status; return j; }); });
  }
  function banner(msg, ok) {
    var b = $("banner");
    if (!msg) { b.hidden = true; return; }
    b.textContent = msg; b.className = "banner" + (ok ? " ok" : ""); b.hidden = false;
  }
  function fmt(s) {
    s = Math.max(0, Math.floor(s));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    return [h, m, x].map(function (n) { return String(n).padStart(2, "0"); }).join(":");
  }
  function human(s) {
    var h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
    return h ? h + "h " + m + "m" : m + " min";
  }
  function render() {
    if (!state) return;
    var s = state.session, c = state.claim;
    $("dash").hidden = !s;
    $("coin-idle").hidden = !!c;
    $("coin-active").hidden = !c;
    $("btn-start").disabled = state.maintenance || !state.identified || state.slot_busy;
    $("inet").textContent = state.internet === "online" ? "online" : state.internet === "offline" ? "offline" : "unknown";
    if (state.maintenance) banner("The service is in maintenance mode. Please try again later.");
    else if (!state.identified) banner("We could not identify your device. Reconnect to the WiFi and refresh.");
    else if (state.slot_busy && !c) banner("The coin slot is being used by another customer. Please wait a moment.");
    else if (state.internet === "offline" && s) banner("The venue's internet is down. Your time is still counting.");
    else if (banner._sticky !== true) banner("");
    if (s) {
      $("purchased").textContent = human(s.purchased_seconds);
      $("expires").textContent = new Date(s.expires_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      $("conn").textContent = s.connected ? "Connected" : "Connecting\u2026";
      var d = s.down_kbps, u = s.up_kbps;
      $("speed").textContent = (d || u) ? (d ? Math.round(d / 1000 * 10) / 10 + " Mbps down" : "unlimited down") + " / " + (u ? Math.round(u / 1000 * 10) / 10 + " Mbps up" : "unlimited up") : "No limit";
    }
    var sim = $("sim-panel");
    if (state.simulation) {
      sim.hidden = false;
      var box = $("sim-buttons");
      if (!box.children.length) {
        state.coin_values.forEach(function (v) {
          var b = document.createElement("button");
          b.textContent = state.currency + v; b.className = "ghost";
          b.addEventListener("click", function () {
            post("/api/sim/coin", { value: v }).then(function (j) {
              banner(j.status === "credited" ? "Coin accepted: +" + human(j.seconds) : "Coin result: " + (j.status || j.error), j.status === "credited");
              refresh();
            });
          });
          box.appendChild(b);
        });
      }
    }
  }
  function tick() {
    if (!state) return;
    var el = (Date.now() - syncedAt) / 1000;
    if (state.session) $("remaining").textContent = fmt(state.session.remaining_seconds - el);
    if (state.claim) $("claim-left").textContent = Math.max(0, Math.floor(state.claim.seconds_left - el));
  }
  function refresh() {
    return fetch("/api/status", { credentials: "same-origin" }).then(function (r) { return r.json(); })
      .then(function (j) { state = j; syncedAt = Date.now(); render(); tick(); })
      .catch(function () { banner("Cannot reach the portal. Check your WiFi connection."); });
  }
  $("btn-start").addEventListener("click", function () {
    post("/api/coin/start").then(function (j) { if (j.error) banner(j.error); else banner("Coin slot open. Insert your coin.", true); refresh(); });
  });
  $("btn-cancel").addEventListener("click", function () { post("/api/coin/cancel").then(function () { banner(""); refresh(); }); });
  $("btn-disconnect").addEventListener("click", function () {
    if (confirm("End your session now? Any remaining time will be lost.")) post("/api/disconnect").then(refresh);
  });
  $("btn-check").addEventListener("click", function () { refresh(); });
  refresh(); setInterval(refresh, 3000); setInterval(tick, 1000);
})();
