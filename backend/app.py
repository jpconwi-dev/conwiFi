"""JP-PisoWiFi Flask application: customer portal + admin dashboard.

All access decisions are made server-side. The web process only writes to SQLite; the monitor
service turns database state into firewall rules."""
import csv
import io
import ipaddress
import json
import re
import secrets
import time
from datetime import timedelta
from functools import wraps
from pathlib import Path

from flask import (Flask, Response, abort, flash, g, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)

from . import (auth, backup_service, client_service, clock, network_service as ns, payment_service,
               reporting_service as rep, session_service)
from .config import ROOT, VERSION, load_config, paths, secret_key
from .database import (SOUND_EVENTS, all_settings, connect, get_setting, init_db, log_event, set_setting, tx)

HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
SERVICES = ["jp-pisowifi", "jp-pisowifi-monitor", "jp-pisowifi-coin", "dnsmasq"]


IMAGE_LIMIT = 1024 * 1024
SOUND_LIMIT = 700 * 1024
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp",
        ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".wav": "audio/wav", ".ogg": "audio/ogg"}


def sniff_image(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def sniff_audio(data: bytes) -> str | None:
    if data[:3] == b"ID3" or (len(data) > 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "mp3"
    if data[:4] == b"OggS":
        return "ogg"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[4:8] == b"ftyp":
        return "m4a"
    return None


def _rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _on_color(h: str) -> str:
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in _rgb(h))
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    # pick whichever of black/white gives the higher WCAG contrast ratio
    return "#111111" if (lum + 0.05) / 0.05 >= 1.05 / (lum + 0.05) else "#ffffff"


def create_app(cfg: dict | None = None) -> Flask:
    cfg = cfg or load_config()
    init_db(cfg).close()
    P = paths(cfg)
    test_mode = bool(cfg["coin"].get("test_mode")) and not cfg["coin"].get("hardware_verified")
    sim_ui = cfg["simulation_mode"] or test_mode  # simulated-coin buttons available
    app = Flask(__name__, template_folder=str(ROOT / "frontend" / "templates"),
                static_folder=str(ROOT / "frontend" / "static"))
    app.config.update(
        SECRET_KEY=secret_key(cfg),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(cfg["security"]["cookie_secure"]),
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=int(cfg["security"]["session_minutes"])),
        MAX_CONTENT_LENGTH=3 * 1024 * 1024,
        JP_CFG=cfg,
    )

    # ------------------------------------------------------------ helpers
    def db():
        if "db" not in g:
            g.db = connect(P["db"])
        return g.db

    @app.teardown_appcontext
    def _close(_exc):
        c = g.pop("db", None)
        if c is not None:
            c.close()

    def net():
        return ns.effective_network(db(), cfg)

    def client_identity():
        if cfg["simulation_mode"]:
            if request.args.get("sim", "").isdigit():
                session["sim"] = int(request.args["sim"])
            mac, ip = client_service.sim_identity(session.get("sim", 1))
            return mac, ip
        ip = request.remote_addr
        return client_service.mac_for_ip(ip), ip

    def current_client():
        mac, ip = client_identity()
        if not mac:
            return None
        return client_service.upsert_client(db(), mac, ip)

    def admin_allowed() -> bool:
        ip = request.remote_addr or ""
        if cfg["simulation_mode"] or ip in ("127.0.0.1", "::1"):
            return True
        n = net()
        if not client_service.in_subnet(ip, n["lan_ip"], int(n["lan_prefix"])):
            return True  # management side (WAN / upstream LAN)
        mac = client_service.mac_for_ip(ip)
        try:
            return mac in json.loads(get_setting(db(), "admin_lan_macs", "[]"))
        except ValueError:
            return False

    def current_admin():
        aid = session.get("admin_id")
        if not aid:
            return None
        return db().execute("SELECT * FROM admins WHERE id=?", (aid,)).fetchone()

    def admin_required(f):
        @wraps(f)
        def wrapper(*a, **kw):
            adm = current_admin()
            if adm is None:
                return redirect(url_for("admin_login"))
            if adm["must_change_password"] and request.endpoint not in ("admin_password", "admin_logout"):
                return redirect(url_for("admin_password"))
            g.admin = adm
            session.permanent = True
            return f(*a, **kw)
        return wrapper

    def audit(action, detail=""):
        auth.audit(db(), getattr(g, "admin", None) or current_admin(), action, detail, request.remote_addr or "")

    def int_field(name, lo, hi, default=None):
        try:
            v = int(request.form.get(name, ""))
        except ValueError:
            return default
        return v if lo <= v <= hi else default

    # ------------------------------------------------------------ request hooks
    @app.before_request
    def guards():
        # captive-portal redirect for foreign Host headers (connectivity probes, typed websites)
        host = (request.host or "").split(":")[0]
        portal_hosts = {net()["lan_ip"], "localhost", "127.0.0.1", "jp-pisowifi.lan"}
        is_ip = False
        try:
            ipaddress.ip_address(host)
            is_ip = True
        except ValueError:
            pass
        if (host not in portal_hosts and not is_ip and not request.path.startswith(("/admin", "/api", "/static"))
                and not cfg["simulation_mode"]):
            return redirect(f"http://{net()['lan_ip']}/", code=302)
        if request.path.startswith("/admin") and not admin_allowed():
            abort(404)
        if "csrf" not in session:
            session["csrf"] = auth.new_csrf_token()
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
            if not auth.csrf_ok(session.get("csrf"), supplied):
                abort(400, "Invalid or missing CSRF token. Reload the page and try again.")

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; media-src 'self'; "
                                                   "style-src 'self'; script-src 'self'; frame-ancestors 'none'")
        if request.path.startswith(("/admin", "/api")):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.context_processor
    def inject():
        s = all_settings(db())
        return {"S": s, "csrf_token": session.get("csrf", ""), "version": VERSION,
                "sim_mode": cfg["simulation_mode"], "test_mode": test_mode, "admin": current_admin()}

    @app.errorhandler(400)
    def e400(e):
        return render_template("error.html", code=400, message=getattr(e, "description", "Bad request")), 400

    @app.errorhandler(404)
    def e404(e):
        return render_template("error.html", code=404, message="Page not found."), 404

    @app.errorhandler(413)
    def e413(e):
        return render_template("error.html", code=413, message="Upload too large (images max 1 MB, sounds max 700 KB)."), 413

    # ------------------------------------------------------------ customer portal
    def sound_urls(S):
        out = {}
        if S["sounds_enabled"] == "1":
            for ev in SOUND_EVENTS:
                f = S.get("snd_" + ev, "")
                if f:
                    out[ev] = url_for("uploads", name=f)
        return out

    def status_payload():
        conn = db()
        client = current_client()
        S = all_settings(conn)
        try:
            vol = max(0, min(100, int(S["sound_volume"]))) / 100
            low = max(5, int(S["low_time_seconds"]))
        except ValueError:
            vol, low = 0.8, 60
        data = {
            "business_name": S["business_name"], "currency": S["currency"],
            "maintenance": S["maintenance_mode"] == "1",
            "internet": S["internet_status"], "identified": client is not None,
            "simulation": sim_ui,
            "rates": [{"amount": r["amount"], "minutes": r["minutes"]} for r in
                      conn.execute("SELECT amount, minutes FROM rates WHERE enabled=1 ORDER BY amount")],
            "claim": None, "session": None, "slot_busy": False,
            "pause_enabled": S["pause_enabled"] == "1",
            "sounds": sound_urls(S), "sound_volume": vol, "low_time_seconds": low,
            "coin_values": sorted(set(payment_service.pulse_map(conn).values())) if sim_ui else [],
        }
        if client:
            claim = payment_service.current_claim(conn, client["id"])
            if claim:
                data["claim"] = {"seconds_left": max(0, claim["expires_at"] - clock.now())}
            s = session_service.current_session(conn, client["id"])
            if s:
                paused = session_service.is_paused(s)
                data["session"] = {
                    "remaining_seconds": session_service.remaining_seconds(s),
                    "expires_at": None if paused else s["expires_at"],
                    "purchased_seconds": s["purchased_seconds"],
                    "down_kbps": s["down_kbps"], "up_kbps": s["up_kbps"],
                    "paused": paused,
                    "pause_left": max(0, session_service.max_pause_seconds(conn) - (clock.now() - s["paused_at"])) if paused else None,
                    "connected": (not paused) and s["authorized_expiry"] == s["expires_at"],
                }
            other = conn.execute("SELECT 1 FROM coin_claims WHERE status='open' AND client_id!=? AND expires_at>=?",
                                 (client["id"], clock.now())).fetchone()
            data["slot_busy"] = bool(other)
        return data

    @app.get("/")
    def portal():
        conn = db()
        imgs = conn.execute("SELECT * FROM media WHERE enabled=1 ORDER BY sort_order, id").fetchall()
        st = status_payload()
        return render_template("portal.html", st=st, rates=st["rates"],
                               portal_imgs=[i for i in imgs if i["show_on"] in ("both", "portal")],
                               dash_imgs=[i for i in imgs if i["show_on"] in ("both", "dashboard")])

    @app.get("/api/status")
    def api_status():
        return jsonify(status_payload())

    @app.post("/api/coin/start")
    def api_coin_start():
        conn = db()
        client = current_client()
        if not client:
            return jsonify(error="We could not identify your device. Reconnect to the WiFi and try again."), 400
        if client["blocked"]:
            return jsonify(error="This device is blocked. Please contact the operator."), 403
        if get_setting(conn, "maintenance_mode", "0") == "1":
            return jsonify(error="The service is in maintenance mode."), 503
        if not cfg["simulation_mode"] and not test_mode:
            chb = int(get_setting(conn, "coin_daemon_heartbeat", 0))
            if clock.now() - chb > 30:
                return jsonify(error="The coin acceptor is offline. Please contact the operator."), 503
        s = session_service.current_session(conn, client["id"])
        max_s = int(get_setting(conn, "max_session_minutes", 720)) * 60
        if s and session_service.remaining_seconds(s) >= max_s:
            return jsonify(error="You already have the maximum amount of time."), 400
        claim, err = payment_service.open_claim(conn, client["id"], int(cfg["coin"]["claim_seconds"]))
        if err == "busy":
            return jsonify(error="The coin slot is being used by another customer. Try again in a moment."), 409
        return jsonify(ok=True, seconds_left=max(0, claim["expires_at"] - clock.now()))

    @app.post("/api/coin/cancel")
    def api_coin_cancel():
        client = current_client()
        if client:
            payment_service.close_claim(db(), client["id"])
        return jsonify(ok=True)

    @app.post("/api/pause")
    def api_pause():
        client = current_client()
        if not client:
            return jsonify(error="Device not identified"), 400
        with tx(db()):
            ok, err = session_service.pause(db(), client["id"])
        if not ok:
            return jsonify(error=err), 400
        log_event(db(), "info", "portal", f"client {client['mac']} paused")
        return jsonify(ok=True)

    @app.post("/api/resume")
    def api_resume():
        client = current_client()
        if not client:
            return jsonify(error="Device not identified"), 400
        with tx(db()):
            ok, err = session_service.resume(db(), client["id"])
        if not ok:
            return jsonify(error=err), 400
        log_event(db(), "info", "portal", f"client {client['mac']} resumed")
        return jsonify(ok=True)

    @app.post("/api/disconnect")
    def api_disconnect():
        client = current_client()
        if not client:
            return jsonify(error="Device not identified"), 400
        s = session_service.current_session(db(), client["id"])
        if s:
            with tx(db()):
                session_service.end_session(db(), s["id"], "ended")
            log_event(db(), "info", "portal", f"client {client['mac']} disconnected voluntarily")
        return jsonify(ok=True)

    @app.post("/api/sim/coin")
    def api_sim_coin():
        if not sim_ui:
            abort(404)
        value = request.get_json(silent=True, force=True) or {}
        try:
            value = int(value.get("value"))
        except (TypeError, ValueError):
            return jsonify(error="value required"), 400
        if value not in set(payment_service.pulse_map(db()).values()):
            return jsonify(error="Not an accepted coin value"), 400
        res = payment_service.record_coin(db(), secrets.token_hex(8), value, value, "simulation",
                                          int(cfg["coin"]["claim_seconds"]))
        return jsonify(res)

    @app.get("/health")
    def health():
        ok = True
        try:
            ok = db().execute("SELECT 1").fetchone()[0] == 1
        except Exception:  # noqa: BLE001
            ok = False
        return jsonify(status="ok" if ok else "error", version=VERSION), (200 if ok else 503)

    @app.get("/branding.css")
    def branding_css():
        S = all_settings(db())
        vals = {k: (S[k] if HEX_RE.match(S[k]) else d) for k, d in
                (("primary_color", "#c4861a"), ("background_color", "#12233f"), ("text_color", "#f4f1e8"))}
        fr, fg_, fb = _rgb(vals["text_color"])
        ar, ag, ab = _rgb(vals["primary_color"])
        css = (":root{--accent:%s;--bg:%s;--fg:%s;--on-accent:%s;"
               "--line:rgba(%d,%d,%d,.22);--card:rgba(%d,%d,%d,.07);--muted:rgba(%d,%d,%d,.72);"
               "--accent-soft:rgba(%d,%d,%d,.18)}" % (
                   vals["primary_color"], vals["background_color"], vals["text_color"], _on_color(vals["primary_color"]),
                   fr, fg_, fb, fr, fg_, fb, fr, fg_, fb, ar, ag, ab))
        return Response(css, mimetype="text/css", headers={"Cache-Control": "no-cache"})

    @app.get("/uploads/<path:name>")
    def uploads(name):
        safe = Path(name).name
        ext = Path(safe).suffix.lower()
        if ext not in MIME:
            abort(404)
        return send_from_directory(P["uploads"], safe, mimetype=MIME[ext], max_age=86400)

    # ------------------------------------------------------------ admin: auth
    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            user = request.form.get("username", "").strip()[:64]
            key = f"{request.remote_addr}|{user.lower()}"
            sec = cfg["security"]
            if auth.too_many_attempts(db(), key, sec["login_max_failures"], sec["login_window_minutes"]):
                flash("Too many failed attempts. Try again later.", "error")
                return render_template("admin_login.html"), 429
            adm = auth.authenticate(db(), user, request.form.get("password", ""))
            if adm is None:
                auth.record_failure(db(), key)
                log_event(db(), "warning", "admin", f"failed login for '{user}' from {request.remote_addr}")
                flash("Invalid username or password.", "error")
                return render_template("admin_login.html"), 401
            auth.clear_failures(db(), key)
            session.clear()
            session["csrf"] = auth.new_csrf_token()
            session["admin_id"] = adm["id"]
            session.permanent = True
            g.admin = adm
            audit("login")
            return redirect(url_for("admin_dashboard"))
        return render_template("admin_login.html")

    @app.post("/admin/logout")
    def admin_logout():
        audit("logout")
        session.clear()
        return redirect(url_for("admin_login"))

    @app.route("/admin/password", methods=["GET", "POST"])
    @admin_required
    def admin_password():
        if request.method == "POST":
            if not auth.verify_password(g.admin["password_hash"], request.form.get("current", "")):
                flash("Current password is incorrect.", "error")
            elif request.form.get("new") != request.form.get("confirm"):
                flash("New passwords do not match.", "error")
            elif (msg := auth.validate_new_password(request.form.get("new", ""))):
                flash(msg, "error")
            else:
                auth.set_password(db(), g.admin["id"], request.form["new"])
                audit("password_changed")
                flash("Password updated.", "ok")
                return redirect(url_for("admin_dashboard"))
        return render_template("admin_password.html")

    @app.get("/admin/")
    def admin_root():
        return redirect(url_for("admin_dashboard"))

    # ------------------------------------------------------------ admin: pages
    @app.get("/admin/dashboard")
    @admin_required
    def admin_dashboard():
        conn = db()
        n = net()
        ifs = {i["name"]: i for i in ns.detect_interfaces()}
        return render_template(
            "admin_dashboard.html",
            today=rep.sales_today(conn), total=rep.sales_total(conn), unassigned=rep.unassigned_total(conn),
            active=len(session_service.active_sessions(conn)), paused=len(session_service.paused_sessions(conn)),
            clients=rep.connected_clients(conn),
            payments=rep.recent_payments(conn, 8), stats=rep.system_stats(), comp=rep.component_status(conn),
            net=n, wan=ifs.get(n.get("wan_iface")), lan=ifs.get(n.get("lan_iface")))

    @app.route("/admin/rates", methods=["GET", "POST"])
    @admin_required
    def admin_rates():
        conn = db()
        if request.method == "POST":
            act = request.form.get("action")
            if act in ("add", "update"):
                amount, minutes = int_field("amount", 1, 100000), int_field("minutes", 1, 100000)
                if not amount or not minutes:
                    flash("Amount and minutes must be positive whole numbers.", "error")
                elif act == "add":
                    with tx(conn):
                        conn.execute("INSERT INTO rates(amount,minutes) VALUES(?,?)", (amount, minutes))
                    audit("rate_add", f"{amount} = {minutes} min")
                else:
                    rid = int_field("id", 1, 10**9)
                    with tx(conn):
                        conn.execute("UPDATE rates SET amount=?, minutes=?, enabled=? WHERE id=?",
                                     (amount, minutes, 1 if request.form.get("enabled") else 0, rid))
                    audit("rate_update", f"id={rid} {amount} = {minutes} min")
            elif act == "delete":
                with tx(conn):
                    conn.execute("DELETE FROM rates WHERE id=?", (int_field("id", 1, 10**9),))
                audit("rate_delete", request.form.get("id", ""))
            elif act == "pulses":
                pm = {}
                for line in request.form.get("pulse_map", "").splitlines():
                    if "=" in line:
                        a, b = line.split("=", 1)
                        if a.strip().isdigit() and b.strip().isdigit() and 0 < int(a) <= 1000 and 0 < int(b) <= 100000:
                            pm[a.strip()] = int(b)
                if not pm:
                    flash("Enter at least one mapping like 5=5.", "error")
                else:
                    set_setting(conn, "pulse_map", json.dumps(pm))
                    audit("pulse_map", json.dumps(pm))
                    flash("Pulse mapping saved.", "ok")
            return redirect(url_for("admin_rates"))
        rates = conn.execute("SELECT * FROM rates ORDER BY amount").fetchall()
        pm = payment_service.pulse_map(conn)
        return render_template("rates.html", rates=rates,
                               pulse_text="\n".join(f"{k}={v}" for k, v in sorted(pm.items())))

    @app.route("/admin/sessions", methods=["GET", "POST"])
    @admin_required
    def admin_sessions():
        conn = db()
        if request.method == "POST":
            act, cid = request.form.get("action"), int_field("id", 1, 10**9)
            if act == "disconnect":
                with tx(conn):
                    session_service.end_session(conn, cid, "ended")
                audit("session_disconnect", f"session {cid}")
            elif act in ("block", "unblock"):
                with tx(conn):
                    conn.execute("UPDATE clients SET blocked=? WHERE id=?", (1 if act == "block" else 0, cid))
                audit("client_" + act, f"client {cid}")
            return redirect(url_for("admin_sessions"))
        return render_template("sessions.html", active=session_service.active_sessions(conn),
                               paused=session_service.paused_sessions(conn),
                               clients=conn.execute("SELECT * FROM clients ORDER BY last_seen DESC LIMIT 100").fetchall(),
                               now=clock.now())

    @app.get("/admin/sales")
    @admin_required
    def admin_sales():
        conn = db()
        return render_template("sales.html", days=rep.sales_by_day(conn, 30), total=rep.sales_total(conn),
                               today=rep.sales_today(conn), unassigned=rep.unassigned_total(conn),
                               payments=rep.recent_payments(conn, 100),
                               events=conn.execute("SELECT * FROM coin_events ORDER BY id DESC LIMIT 30").fetchall())

    @app.get("/admin/sales.csv")
    @admin_required
    def admin_sales_csv():
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["payment_id", "time", "amount", "pulses", "credited_seconds", "status", "source", "client_mac"])
        for p in rep.recent_payments(db(), 100000):
            w.writerow([p["payment_id"], time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p["created_at"])),
                        p["amount"], p["pulses"], p["credited_seconds"], p["status"], p["source"], p["mac"] or ""])
        audit("sales_export")
        return Response(out.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": "attachment; filename=jp-pisowifi-sales.csv"})

    @app.route("/admin/branding", methods=["GET", "POST"])
    @admin_required
    def admin_branding():
        conn = db()
        if request.method == "POST":
            f = request.form
            name = f.get("business_name", "").strip()[:60] or "JP-PisoWiFi"
            wifi = f.get("wifi_name", "").strip()
            if not wifi or len(wifi.encode("utf-8")) > 32 or any(ord(ch) < 32 for ch in wifi):
                flash("WiFi name must be 1-32 bytes with no control characters.", "error")
                return redirect(url_for("admin_branding"))
            for key in ("primary_color", "background_color", "text_color"):
                if not HEX_RE.match(f.get(key, "")):
                    flash("Colors must look like #aabbcc.", "error")
                    return redirect(url_for("admin_branding"))
            with tx(conn):
                set_setting(conn, "business_name", name)
                set_setting(conn, "wifi_name", wifi)
                set_setting(conn, "tagline", f.get("tagline", "").strip()[:140])
                set_setting(conn, "currency", f.get("currency", "\u20b1").strip()[:4] or "\u20b1")
                set_setting(conn, "instructions", f.get("instructions", "").strip()[:600])
                set_setting(conn, "support_contact", f.get("support_contact", "").strip()[:140])
                for key in ("primary_color", "background_color", "text_color"):
                    set_setting(conn, key, f[key].lower())
            logo = request.files.get("logo")
            if logo and logo.filename:
                data = logo.read(512 * 1024 + 1)
                ext = ("png" if data.startswith(b"\x89PNG\r\n\x1a\n") else "jpg" if data.startswith(b"\xff\xd8\xff")
                       else "webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else None)
                if ext is None or len(data) > 512 * 1024:
                    flash("Logo must be a PNG, JPG or WEBP image under 512 KB.", "error")
                else:
                    old = get_setting(conn, "logo_file", "")
                    fname = f"logo-{secrets.token_hex(4)}.{ext}"
                    (P["uploads"] / fname).write_bytes(data)
                    set_setting(conn, "logo_file", fname)
                    if old:
                        (P["uploads"] / Path(old).name).unlink(missing_ok=True)
            audit("branding_update")
            flash("Branding saved.", "ok")
            return redirect(url_for("admin_branding"))
        return render_template("branding.html")

    @app.route("/admin/network", methods=["GET", "POST"])
    @admin_required
    def admin_network():
        conn = db()
        if request.method == "POST":
            act = request.form.get("action")
            if act == "confirm":
                ns.confirm_network_change(conn)
                audit("network_confirm")
                flash("Network change confirmed.", "ok")
            elif act == "lan":
                f = request.form
                changes = {"lan_ip": f.get("lan_ip", "").strip(), "lan_prefix": int_field("lan_prefix", 16, 29, 0),
                           "dhcp_start": f.get("dhcp_start", "").strip(), "dhcp_end": f.get("dhcp_end", "").strip(),
                           "dhcp_lease": f.get("dhcp_lease", "1h").strip() if re.match(r"^\d+[mhd]$", f.get("dhcp_lease", "")) else "1h",
                           "dns_servers": [d.strip() for d in f.get("dns", "").split(",") if d.strip()]}
                errs = ns.stage_network_change(conn, cfg, changes, clock.now())
                if errs:
                    for e in errs:
                        flash(e, "error")
                else:
                    audit("network_stage", json.dumps(changes))
                    flash(f"Change staged. Reconnect using the new address and press Confirm within "
                          f"{cfg['network']['confirm_seconds']} seconds or it will be rolled back.", "ok")
            elif act == "speed":
                with tx(conn):
                    set_setting(conn, "default_down_kbps", int_field("down", 0, 1000000, 0))
                    set_setting(conn, "default_up_kbps", int_field("up", 0, 1000000, 0))
                audit("speed_defaults")
                flash("Default speed limits saved (applies to new sessions; 0 = unlimited).", "ok")
            return redirect(url_for("admin_network"))
        pending = get_setting(conn, "network_pending", "")
        deadline = json.loads(pending)["deadline"] if pending else None
        return render_template("network_settings.html", net=net(), ifaces=ns.detect_interfaces(),
                               pending_left=(deadline - clock.now()) if deadline else None,
                               fw=ns.firewall_status() if not cfg["simulation_mode"] else None)

    @app.route("/admin/system", methods=["GET", "POST"])
    @admin_required
    def admin_system():
        conn = db()
        if request.method == "POST":
            act = request.form.get("action")
            if act == "maintenance":
                v = "1" if request.form.get("enabled") else "0"
                set_setting(conn, "maintenance_mode", v)
                audit("maintenance", v)
            elif act == "pause_settings":
                set_setting(conn, "pause_enabled", "1" if request.form.get("pause_enabled") else "0")
                set_setting(conn, "max_pause_minutes", int_field("max_pause", 1, 1440, 60))
                audit("pause_settings")
                flash("Pause settings saved.", "ok")
            elif act == "limits":
                set_setting(conn, "max_session_minutes", int_field("max_minutes", 1, 10080, 720))
                audit("max_session")
            elif act == "backup":
                try:
                    f = backup_service.create_backup(cfg, "manual")
                    audit("backup_create", f.name)
                    flash(f"Backup created: {f.name}", "ok")
                except Exception as e:  # noqa: BLE001
                    flash(f"Backup failed: {e}", "error")
            elif act == "admin_macs":
                macs = [m for m in (client_service.normalize_mac(x) for x in
                                    re.split(r"[\s,]+", request.form.get("macs", ""))) if m]
                set_setting(conn, "admin_lan_macs", json.dumps(macs))
                audit("admin_lan_macs", json.dumps(macs))
            elif act == "update":
                if cfg["simulation_mode"]:
                    flash("Updates are disabled in simulation mode.", "error")
                else:
                    P["update_request"].write_text(str(clock.now()))
                    audit("update_requested")
                    flash("Update requested. The result appears below in a minute or two.", "ok")
            return redirect(url_for("admin_system"))
        svc = {}
        if not cfg["simulation_mode"]:
            import subprocess
            for s in SERVICES:
                r = subprocess.run(["systemctl", "is-active", s], capture_output=True, text=True)
                svc[s] = r.stdout.strip() or "unknown"
        return render_template(
            "system_settings.html", services=svc, max_minutes=int(get_setting(conn, "max_session_minutes", 720)),
            max_pause=int(get_setting(conn, "max_pause_minutes", 60)),
            events=conn.execute("SELECT * FROM system_events ORDER BY id DESC LIMIT 40").fetchall(),
            audits=conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 25").fetchall(),
            backups=[b.name for b in backup_service.list_backups(cfg)][:15],
            admin_macs="\n".join(json.loads(get_setting(conn, "admin_lan_macs", "[]"))),
            last_update=get_setting(conn, "last_update_result", ""), stats=rep.system_stats(),
            comp=rep.component_status(conn))

    def _link_ok(link: str) -> bool:
        return link == "" or (len(link) <= 200 and re.match(r"^https?://[^\s<>\"']+$", link) is not None)

    def _drop_file(name):
        if name:
            (P["uploads"] / Path(name).name).unlink(missing_ok=True)

    @app.route("/admin/media", methods=["GET", "POST"])
    @admin_required
    def admin_media():
        conn = db()
        if request.method == "POST":
            f, act = request.form, request.form.get("action")
            if act == "add_image":
                up = request.files.get("image")
                data = up.read(IMAGE_LIMIT + 1) if up and up.filename else b""
                ext = sniff_image(data) if data else None
                link = f.get("link", "").strip()
                if ext is None or len(data) > IMAGE_LIMIT:
                    flash("Choose a PNG, JPG or WEBP image under 1 MB.", "error")
                elif not _link_ok(link):
                    flash("The link must start with http:// or https://", "error")
                else:
                    show = f.get("show_on") if f.get("show_on") in ("both", "portal", "dashboard") else "both"
                    fname = f"img-{secrets.token_hex(6)}.{ext}"
                    (P["uploads"] / fname).write_bytes(data)
                    nxt = conn.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM media").fetchone()[0]
                    with tx(conn):
                        conn.execute("INSERT INTO media(file,title,link,show_on,sort_order,created_at) VALUES(?,?,?,?,?,?)",
                                     (fname, f.get("title", "").strip()[:80], link, show, nxt, clock.now()))
                    audit("image_add", fname)
                    flash("Picture added.", "ok")
            elif act == "update_image":
                link = f.get("link", "").strip()
                if not _link_ok(link):
                    flash("The link must start with http:// or https://", "error")
                else:
                    show = f.get("show_on") if f.get("show_on") in ("both", "portal", "dashboard") else "both"
                    with tx(conn):
                        conn.execute("UPDATE media SET title=?, link=?, show_on=?, enabled=?, sort_order=? WHERE id=?",
                                     (f.get("title", "").strip()[:80], link, show, 1 if f.get("enabled") else 0,
                                      int_field("sort_order", 0, 9999, 0), int_field("id", 1, 10**9)))
                    audit("image_update", f.get("id", ""))
            elif act == "delete_image":
                row = conn.execute("SELECT file FROM media WHERE id=?", (int_field("id", 1, 10**9),)).fetchone()
                if row:
                    with tx(conn):
                        conn.execute("DELETE FROM media WHERE id=?", (int_field("id", 1, 10**9),))
                    _drop_file(row["file"])
                    audit("image_delete", row["file"])
            elif act == "dash_text":
                with tx(conn):
                    set_setting(conn, "dashboard_title", f.get("dashboard_title", "").strip()[:60] or "You are online")
                    set_setting(conn, "dashboard_message", f.get("dashboard_message", "").strip()[:300])
                audit("dashboard_text")
                flash("Dashboard text saved.", "ok")
            elif act == "sound_upload":
                ev = f.get("event")
                up = request.files.get("sound")
                data = up.read(SOUND_LIMIT + 1) if up and up.filename else b""
                ext = sniff_audio(data) if data else None
                if ev not in SOUND_EVENTS:
                    flash("Unknown sound event.", "error")
                elif ext is None or len(data) > SOUND_LIMIT:
                    flash("Choose an MP3, M4A, WAV or OGG sound under 700 KB. MP3 or M4A works on every phone.", "error")
                else:
                    old = get_setting(conn, "snd_" + ev, "")
                    fname = f"snd-{ev}-{secrets.token_hex(4)}.{ext}"
                    (P["uploads"] / fname).write_bytes(data)
                    set_setting(conn, "snd_" + ev, fname)
                    _drop_file(old)
                    audit("sound_upload", f"{ev}={fname}")
                    flash(f"Sound saved for: {SOUND_EVENTS[ev]}.", "ok")
            elif act == "sound_remove":
                ev = f.get("event")
                if ev in SOUND_EVENTS:
                    _drop_file(get_setting(conn, "snd_" + ev, ""))
                    set_setting(conn, "snd_" + ev, "")
                    audit("sound_remove", ev)
            elif act == "sound_settings":
                with tx(conn):
                    set_setting(conn, "sounds_enabled", "1" if f.get("sounds_enabled") else "0")
                    set_setting(conn, "sound_volume", int_field("volume", 0, 100, 80))
                    set_setting(conn, "low_time_seconds", int_field("low_time", 5, 600, 60))
                audit("sound_settings")
                flash("Sound settings saved.", "ok")
            return redirect(url_for("admin_media"))
        return render_template(
            "media.html", images=conn.execute("SELECT * FROM media ORDER BY sort_order, id").fetchall(),
            sounds=[(k, v, get_setting(conn, "snd_" + k, "")) for k, v in SOUND_EVENTS.items()])

    @app.get("/admin/backup/<name>")
    @admin_required
    def admin_backup_download(name):
        safe = Path(name).name
        if not re.match(r"^jp-pisowifi-[\w-]+\.db$", safe) or not (P["backups"] / safe).exists():
            abort(404)
        audit("backup_download", safe)
        return send_from_directory(P["backups"], safe, as_attachment=True)

    app.jinja_env.filters["dur"] = lambda s: ("%dh %02dm" % (s // 3600, (s % 3600) // 60)) if s >= 3600 else ("%dm %02ds" % (s // 60, s % 60))
    def _mins(m):
        h, mm = divmod(int(m), 60)
        return f"{h} hr {mm} min" if h and mm else (f"{h} hr" if h else f"{mm} min")

    app.jinja_env.filters["mins"] = _mins
    app.jinja_env.filters["dt"] = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "-"
    return app
