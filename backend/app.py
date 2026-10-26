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
from .database import all_settings, connect, get_setting, init_db, log_event, set_setting, tx

HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
SERVICES = ["jp-pisowifi", "jp-pisowifi-monitor", "jp-pisowifi-coin", "dnsmasq"]


def create_app(cfg: dict | None = None) -> Flask:
    cfg = cfg or load_config()
    init_db(cfg).close()
    P = paths(cfg)
    app = Flask(__name__, template_folder=str(ROOT / "frontend" / "templates"),
                static_folder=str(ROOT / "frontend" / "static"))
    app.config.update(
        SECRET_KEY=secret_key(cfg),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=bool(cfg["security"]["cookie_secure"]),
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=int(cfg["security"]["session_minutes"])),
        MAX_CONTENT_LENGTH=1024 * 1024,
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
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; "
                                                   "style-src 'self'; script-src 'self'; frame-ancestors 'none'")
        if request.path.startswith(("/admin", "/api")):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.context_processor
    def inject():
        s = all_settings(db())
        return {"S": s, "csrf_token": session.get("csrf", ""), "version": VERSION,
                "sim_mode": cfg["simulation_mode"], "admin": current_admin()}

    @app.errorhandler(400)
    def e400(e):
        return render_template("error.html", code=400, message=getattr(e, "description", "Bad request")), 400

    @app.errorhandler(404)
    def e404(e):
        return render_template("error.html", code=404, message="Page not found."), 404

    @app.errorhandler(413)
    def e413(e):
        return render_template("error.html", code=413, message="Upload too large (max 1 MB)."), 413

    # ------------------------------------------------------------ customer portal
    def status_payload():
        conn = db()
        client = current_client()
        S = all_settings(conn)
        data = {
            "business_name": S["business_name"], "currency": S["currency"],
            "maintenance": S["maintenance_mode"] == "1",
            "internet": S["internet_status"], "identified": client is not None,
            "simulation": cfg["simulation_mode"],
            "rates": [{"amount": r["amount"], "minutes": r["minutes"]} for r in
                      conn.execute("SELECT amount, minutes FROM rates WHERE enabled=1 ORDER BY amount")],
            "claim": None, "session": None,
            "coin_values": sorted(set(payment_service.pulse_map(conn).values())),
        }
        if client:
            claim = payment_service.current_claim(conn, client["id"])
            if claim:
                data["claim"] = {"seconds_left": max(0, claim["expires_at"] - clock.now())}
            s = session_service.active_session(conn, client["id"])
            if s:
                data["session"] = {
                    "remaining_seconds": session_service.remaining_seconds(s),
                    "expires_at": s["expires_at"], "purchased_seconds": s["purchased_seconds"],
                    "down_kbps": s["down_kbps"], "up_kbps": s["up_kbps"],
                    "connected": s["authorized_expiry"] == s["expires_at"],
                }
            other = conn.execute("SELECT 1 FROM coin_claims WHERE status='open' AND client_id!=? AND expires_at>=?",
                                 (client["id"], clock.now())).fetchone()
            data["slot_busy"] = bool(other)
        return data

    @app.get("/")
    def portal():
        return render_template("portal.html", st=status_payload(), rates=status_payload()["rates"])

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
        if not cfg["simulation_mode"]:
            chb = int(get_setting(conn, "coin_daemon_heartbeat", 0))
            if clock.now() - chb > 30:
                return jsonify(error="The coin acceptor is offline. Please contact the operator."), 503
        s = session_service.active_session(conn, client["id"])
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

    @app.post("/api/disconnect")
    def api_disconnect():
        client = current_client()
        if not client:
            return jsonify(error="Device not identified"), 400
        s = session_service.active_session(db(), client["id"])
        if s:
            with tx(db()):
                session_service.end_session(db(), s["id"], "ended")
            log_event(db(), "info", "portal", f"client {client['mac']} disconnected voluntarily")
        return jsonify(ok=True)

    @app.post("/api/sim/coin")
    def api_sim_coin():
        if not cfg["simulation_mode"]:
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
        css = (":root{--accent:%s;--bg:%s;--fg:%s}" % (S["primary_color"], S["background_color"], S["text_color"]))
        return Response(css, mimetype="text/css", headers={"Cache-Control": "no-cache"})

    @app.get("/uploads/<path:name>")
    def uploads(name):
        return send_from_directory(P["uploads"], name)

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
            active=len(session_service.active_sessions(conn)), clients=rep.connected_clients(conn),
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
            events=conn.execute("SELECT * FROM system_events ORDER BY id DESC LIMIT 40").fetchall(),
            audits=conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT 25").fetchall(),
            backups=[b.name for b in backup_service.list_backups(cfg)][:15],
            admin_macs="\n".join(json.loads(get_setting(conn, "admin_lan_macs", "[]"))),
            last_update=get_setting(conn, "last_update_result", ""), stats=rep.system_stats(),
            comp=rep.component_status(conn))

    @app.get("/admin/backup/<name>")
    @admin_required
    def admin_backup_download(name):
        safe = Path(name).name
        if not re.match(r"^jp-pisowifi-[\w-]+\.db$", safe) or not (P["backups"] / safe).exists():
            abort(404)
        audit("backup_download", safe)
        return send_from_directory(P["backups"], safe, as_attachment=True)

    app.jinja_env.filters["dur"] = lambda s: ("%dh %02dm" % (s // 3600, (s % 3600) // 60)) if s >= 3600 else ("%dm %02ds" % (s // 60, s % 60))
    app.jinja_env.filters["dt"] = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "-"
    return app
