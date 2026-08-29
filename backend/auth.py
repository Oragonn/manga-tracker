# backend/auth.py
import ipaddress
import os
from datetime import timedelta

from flask import jsonify, redirect, render_template, request, session, url_for
from flask.sessions import SecureCookieSessionInterface
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf import CSRFProtect
from werkzeug.security import check_password_hash

from .auth_logger import log_failed_login, log_lockout
from .visitor_logger import log_visitor_ip

# Real private LAN ranges only — deliberately excludes loopback (127.0.0.1),
# since a Cloudflare Tunnel connects to this app over loopback too. Bypassing
# loopback would let every tunnel-forwarded (internet) request skip the login
# gate, which defeats the whole point.
DEFAULT_LAN_RANGES = "192.168.0.0/16,10.0.0.0/8,172.16.0.0/12"

EXEMPT_ENDPOINTS = {"login", "static"}


class _LanAwareSessionInterface(SecureCookieSessionInterface):
    """Flask itself never terminates TLS (Cloudflare Tunnel does that at the
    edge), so a request that's actually going over the LAN is always plain
    HTTP from the browser's point of view. A Secure-flagged cookie is
    silently dropped by strict browsers (Chrome/Brave) on plain HTTP unless
    the host is literally 'localhost'/'127.0.0.1' — which breaks CSRF (and
    login) for anyone opening the app via its real LAN IP. Only mark the
    cookie Secure for non-LAN (tunnel) requests, where the browser's address
    bar genuinely shows https://.
    """

    def get_cookie_secure(self, app):
        return not is_lan_request()


def _get_lan_networks():
    raw = os.environ.get("LAN_CIDR_RANGES", DEFAULT_LAN_RANGES)
    networks = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            pass
    return networks


def _get_visitor_ip():
    """Best-effort real client IP, for logging only. Flask's own socket-level
    request.remote_addr is always 127.0.0.1 for tunnel traffic (cloudflared
    connects to Flask over loopback), so the real visitor IP has to come from
    the header Cloudflare's edge sets. This is only used for the visitor log
    line, never for the LAN-bypass/auth decision, which must keep trusting
    only the real socket address."""
    cf_ip = request.headers.get("Cf-Connecting-Ip")
    if cf_ip:
        return cf_ip.strip()
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr


def is_lan_request():
    addr = request.remote_addr
    if not addr:
        return False
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in network for network in _get_lan_networks())


def init_auth(app):
    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key:
        raise RuntimeError(
            "SECRET_KEY environment variable is not set. Copy .env.example to "
            ".env and fill it in before running the app."
        )
    app.secret_key = secret_key

    password_hash = os.environ.get("AUTH_PASSWORD_HASH")
    if not password_hash:
        raise RuntimeError(
            "AUTH_PASSWORD_HASH environment variable is not set. Copy "
            ".env.example to .env and fill it in before running the app."
        )

    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
    app.session_interface = _LanAwareSessionInterface()

    CSRFProtect(app)
    limiter = Limiter(get_remote_address, app=app, default_limits=[])

    def _on_login_breach(*_args, **_kwargs):
        log_lockout(request.remote_addr)

    @app.route("/login", methods=["GET", "POST"])
    @limiter.limit("5 per 15 minutes", methods=["POST"], on_breach=_on_login_breach)
    def login():
        error = None
        if request.method == "POST":
            submitted = request.form.get("password", "")
            if check_password_hash(password_hash, submitted):
                session.permanent = True
                session["authenticated"] = True
                next_path = request.args.get("next")
                return redirect(next_path or url_for("dashboard"))
            log_failed_login(request.remote_addr)
            error = "Invalid password"
        return render_template("login.html", error=error)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.before_request
    def _check_auth():
        on_lan = is_lan_request()
        if not on_lan:
            log_visitor_ip(_get_visitor_ip())
        if request.endpoint in EXEMPT_ENDPOINTS:
            return None
        if on_lan:
            return None
        if session.get("authenticated"):
            return None
        if request.path.startswith("/api/"):
            return jsonify({"error": "Unauthorized"}), 401
        return redirect(url_for("login", next=request.path))

    return limiter
