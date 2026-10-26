(function () {
  "use strict";
  var CIRC = 326.73;
  var csrf = document.querySelector('meta[name="csrf-token"]').content;
  function $(id) { return document.getElementById(id); }
  var el = {
    hero: $("hero"), ring: $("ring-fg"), label: $("hero-label"), time: $("hero-time"), sub: $("hero-sub"),
    start: $("btn-start"), pause: $("btn-pause"), resume: $("btn-resume"), cancel: $("btn-cancel"),
    coinBox: $("coin-active"), claimLeft: $("claim-left"), claimBar: $("claim-bar"),
    dash: $("dash"), status: $("f-status"), ends: $("f-ends"), speedRow: $("f-speed-row"), speed: $("f-speed"),
    end: $("btn-end"), confirmBox: $("confirm-end"), yes: $("confirm-yes"), no: $("confirm-no"),
    banner: $("banner"), dot: $("dot"), inet: $("inet"), sound: $("btn-sound"), pgal: $("portal-gallery")
  };
  var state = null, prev = null, syncedAt = 0, userEnded = false, lowFired = false, bannerTimer = null;

  /* ---------- helpers ---------- */
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function clock(s) {
    s = Math.max(0, Math.floor(s));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    return h > 0 ? h + ":" + pad(m) + ":" + pad(x) : pad(m) + ":" + pad(x);
  }
  function human(s) {
    var m = Math.round(s / 60), h = Math.floor(m / 60);
    m = m % 60;
    return h && m ? h + " hr " + m + " min" : h ? h + " hr" : m + " min";
  }
  function post(url, body) {
    return fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().then(function (j) { j._status = r.status; return j; }, function () { return { error: "Unexpected reply", _status: r.status }; });
    });
  }
  function banner(msg, kind, sticky) {
    clearTimeout(bannerTimer);
    if (!msg) { el.banner.hidden = true; return; }
    el.banner.textContent = msg;
    el.banner.className = "banner" + (kind ? " " + kind : "");
    el.banner.hidden = false;
    if (!sticky) bannerTimer = setTimeout(function () { el.banner.hidden = true; }, 6000);
  }

  /* ---------- sound (Web Audio: works after the first tap on iOS and Android) ---------- */
  var AC = window.AudioContext || window.webkitAudioContext, ctx = null, buffers = {}, muted = false;
  try { muted = localStorage.getItem("jp_mute") === "1"; } catch (e) { /* storage blocked in some captive browsers */ }
  function unlock() {
    if (!AC) return;
    try {
      if (!ctx) ctx = new AC();
      if (ctx.state === "suspended") ctx.resume();
      loadSounds();
    } catch (e) { /* ignore */ }
  }
  ["touchend", "pointerdown", "click", "keydown"].forEach(function (ev) { document.addEventListener(ev, unlock, { passive: true }); });
  function loadSounds() {
    if (!ctx || !state || !state.sounds) return;
    Object.keys(state.sounds).forEach(function (name) {
      var url = state.sounds[name];
      if (buffers[name] && buffers[name].url === url) return;
      buffers[name] = { url: url, buf: null };
      fetch(url, { credentials: "same-origin" }).then(function (r) { return r.arrayBuffer(); }).then(function (data) {
        ctx.decodeAudioData(data, function (b) { if (buffers[name] && buffers[name].url === url) buffers[name].buf = b; }, function () { /* undecodable */ });
      }).catch(function () { /* offline: no sound */ });
    });
  }
  function play(name, delay) {
    if (muted || !ctx || !buffers[name] || !buffers[name].buf) return;
    setTimeout(function () {
      try {
        var src = ctx.createBufferSource(), gain = ctx.createGain();
        src.buffer = buffers[name].buf;
        gain.gain.value = state ? state.sound_volume : 0.8;
        src.connect(gain); gain.connect(ctx.destination); src.start(0);
      } catch (e) { /* ignore */ }
    }, delay || 0);
  }
  function paintSoundButton() {
    var any = state && state.sounds && Object.keys(state.sounds).length > 0;
    el.sound.hidden = !any;
    el.sound.setAttribute("aria-pressed", muted ? "true" : "false");
    el.sound.setAttribute("aria-label", muted ? "Sound off" : "Sound on");
    el.sound.innerHTML = muted ? "&#128263;" : "&#128266;";
  }
  el.sound.addEventListener("click", function () {
    muted = !muted;
    try { localStorage.setItem("jp_mute", muted ? "1" : "0"); } catch (e) { /* ignore */ }
    paintSoundButton();
    if (!muted) play("coin");
  });

  /* ---------- sound events from state changes ---------- */
  function transitions(p, n) {
    if (!p) return; // never play sounds on first load
    var ps = p.session, ns = n.session;
    if (!ps && ns) { play("coin"); play("connected", 450); }
    else if (ps && ns) {
      if (ns.purchased_seconds > ps.purchased_seconds) { play("coin"); if (ps.paused && !ns.paused) play("connected", 450); lowFired = false; }
      else if (!ps.paused && ns.paused) play("paused");
      else if (ps.paused && !ns.paused) play("resumed");
    } else if (ps && !ns && !userEnded) {
      play("expired");
      banner("Your time is up. Insert a coin to continue.", "info", true);
    }
    if (!ns) userEnded = false;
  }

  /* ---------- rendering ---------- */
  function render() {
    if (!state) return;
    var s = state.session, c = state.claim, mode = !s ? "idle" : s.paused ? "paused" : "running";
    el.hero.setAttribute("data-state", mode);
    el.dash.hidden = !s;
    el.pgal.hidden = !!s;
    el.pause.hidden = !(mode === "running" && state.pause_enabled);
    el.resume.hidden = mode !== "paused";
    el.coinBox.hidden = !c;
    el.start.hidden = !!c;
    el.start.textContent = s ? "Add time" : "Insert coin";
    el.start.className = "btn" + (s ? "" : " primary");
    el.start.disabled = state.maintenance || !state.identified || (state.slot_busy && !c);
    if (!s) el.confirmBox.hidden = true;

    el.dot.className = "dot " + (state.internet === "online" ? "on" : state.internet === "offline" ? "off" : "");
    el.inet.textContent = state.internet === "online" ? "Internet is online" : state.internet === "offline" ? "Venue internet is down" : "Checking connection";

    if (state.maintenance) banner("Service is paused for maintenance. Please try again later.", "", true);
    else if (!state.identified) banner("We could not recognise your device. Reconnect to the WiFi and refresh this page.", "", true);
    else if (state.slot_busy && !c) banner("Another customer is using the coin slot. Please wait a moment.", "info", true);
    else if (state.internet === "offline" && s && !s.paused) banner("The venue's internet is down. Your time is still counting.", "info", true);
    else if (!el.banner.hidden && (state.session || !/time is up/.test(el.banner.textContent)) && /maintenance|recognise|coin slot|internet is down|time is up/.test(el.banner.textContent)) banner("");

    if (s) {
      el.label.textContent = s.paused ? "Paused" : s.connected ? "Connected" : "Connecting\u2026";
      el.status.textContent = s.paused ? "Paused" : s.connected ? "Connected" : "Connecting\u2026";
      el.ends.textContent = s.paused ? "Resume to see" : new Date(s.expires_at * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
      var lim = s.down_kbps || s.up_kbps;
      el.speedRow.hidden = !lim;
      if (lim) el.speed.textContent = (s.down_kbps ? Math.round(s.down_kbps / 100) / 10 : "no limit") + " / " + (s.up_kbps ? Math.round(s.up_kbps / 100) / 10 : "no limit") + " Mbps";
    } else {
      el.label.textContent = "Not connected";
      el.time.textContent = "--:--";
      el.sub.textContent = state.maintenance ? "Under maintenance" : "Insert a coin to start";
      el.ring.setAttribute("stroke-dashoffset", CIRC);
    }

    if (state.simulation) buildSimButtons();
    paintSoundButton();
    tick();
  }

  function tick() {
    if (!state) return;
    var elapsed = (Date.now() - syncedAt) / 1000, s = state.session, c = state.claim;
    if (s) {
      var rem = s.paused ? s.remaining_seconds : Math.max(0, s.remaining_seconds - elapsed);
      el.time.textContent = clock(rem);
      var frac = s.purchased_seconds > 0 ? Math.min(1, rem / Math.max(s.purchased_seconds, rem)) : 0;
      el.ring.setAttribute("stroke-dashoffset", (CIRC * (1 - frac)).toFixed(2));
      if (s.paused) {
        var left = Math.max(0, s.pause_left - elapsed);
        el.sub.textContent = "Resume within " + clock(left) + " or the session ends";
      } else {
        el.sub.textContent = rem <= state.low_time_seconds ? "Almost out of time. Add a coin to keep going." : "Time left";
        if (rem <= state.low_time_seconds && !lowFired) { lowFired = true; play("low_time"); }
        if (rem > state.low_time_seconds) lowFired = false;
        if (rem <= 0 && !tick.asked) { tick.asked = true; refresh(); }
        if (rem > 0) tick.asked = false;
      }
    }
    if (c) {
      var left2 = Math.max(0, Math.round(c.seconds_left - elapsed));
      el.claimLeft.textContent = left2;
      el.claimBar.style.width = Math.min(100, (left2 / 30) * 100) + "%";
    }
  }

  var simBuilt = false;
  function buildSimButtons() {
    var box = $("sim-buttons");
    if (!box || simBuilt || !state.coin_values.length) return;
    simBuilt = true;
    state.coin_values.forEach(function (v) {
      var b = document.createElement("button");
      b.className = "btn"; b.textContent = state.currency + v;
      b.addEventListener("click", function () {
        post("/api/sim/coin", { value: v }).then(function (j) {
          banner(j.status === "credited" ? "Coin counted: +" + human(j.seconds) : "Coin result: " + (j.status || j.error), j.status === "credited" ? "ok" : "");
          refresh();
        });
      });
      box.appendChild(b);
    });
  }

  /* ---------- data loop ---------- */
  function refresh() {
    return fetch("/api/status", { credentials: "same-origin", cache: "no-store" }).then(function (r) { return r.json(); }).then(function (j) {
      prev = state; state = j; syncedAt = Date.now();
      transitions(prev, state);
      render();
    }).catch(function () { banner("Cannot reach the portal. Check that you are still on the WiFi.", "", false); });
  }
  function loop() {
    var done = function () { setTimeout(loop, state && state.claim ? 1500 : 3500); };
    refresh().then(done, done);
  }

  /* ---------- actions ---------- */
  function act(url, okMsg) {
    return post(url).then(function (j) {
      if (j.error) banner(j.error, ""); else if (okMsg) banner(okMsg, "ok");
      return refresh();
    });
  }
  el.start.addEventListener("click", function () { unlock(); act("/api/coin/start", "Coin slot is open. Insert your coin."); });
  el.cancel.addEventListener("click", function () { act("/api/coin/cancel"); });
  el.pause.addEventListener("click", function () { act("/api/pause", "Paused. Your time is saved."); });
  el.resume.addEventListener("click", function () { act("/api/resume", "Welcome back. Reconnecting\u2026"); });
  el.end.addEventListener("click", function () { el.confirmBox.hidden = false; el.end.hidden = true; });
  el.no.addEventListener("click", function () { el.confirmBox.hidden = true; el.end.hidden = false; });
  el.yes.addEventListener("click", function () {
    userEnded = true; el.confirmBox.hidden = true; el.end.hidden = false;
    act("/api/disconnect", "Session ended.");
  });
  document.addEventListener("visibilitychange", function () { if (!document.hidden) refresh(); });

  loop();
  setInterval(tick, 1000);
})();
