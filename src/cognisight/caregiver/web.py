"""Isolated caregiver-only HTTPS application and reusable monitoring blueprint."""

from datetime import timedelta
from io import BytesIO
import hashlib
import hmac
import json
from pathlib import Path
import time

from cognisight.indoor import (
    IndoorMapStore,
    MapStoreError,
    MapValidationError,
    parse_map_document,
    plan_route,
    RoutePlanningError,
)
from .projection import collect_snapshot
from .security import allowed_request, canonical_hosts


def create_dashboard_blueprint(provider, map_store=None):
    from flask import (
        Blueprint,
        jsonify,
        redirect,
        render_template,
        request,
        send_file,
        session,
    )

    blueprint = Blueprint(
        "caregiver",
        __name__,
        template_folder="templates",
        static_folder="static",
        static_url_path="/caregiver-static",
    )

    @blueprint.get("/dashboard")
    def dashboard():
        if not session.get("authenticated"):
            return redirect("/login")
        return render_template("caregiver_dashboard.html")

    @blueprint.get("/dashboard/indoor-map")
    def indoor_map():
        if not session.get("authenticated"):
            return redirect("/login")
        if map_store is None:
            return jsonify({"error": "map_not_configured"}), 503
        return render_template("caregiver_map.html")

    @blueprint.get("/dashboard/state")
    def state():
        if not session.get("authenticated"):
            return jsonify({"error": "authentication_required"}), 401
        try:
            snapshot = collect_snapshot(provider())
        except Exception:
            # Native/DB exceptions may contain paths or personal information.
            return jsonify(collect_snapshot({})), 503
        return jsonify(snapshot)

    @blueprint.get("/dashboard/indoor-map/data")
    def indoor_map_data():
        if not session.get("authenticated"):
            return jsonify({"error": "authentication_required"}), 401
        if map_store is None:
            return jsonify({"error": "map_not_configured"}), 503
        try:
            return jsonify({"map": map_store.load()})
        except MapStoreError:
            return jsonify({"error": "map_unavailable"}), 503

    @blueprint.put("/dashboard/indoor-map/data")
    def save_indoor_map():
        if not session.get("authenticated"):
            return jsonify({"error": "authentication_required"}), 401
        if map_store is None:
            return jsonify({"error": "map_not_configured"}), 503
        if not request.is_json:
            return jsonify({"error": "json_required"}), 415
        try:
            document = parse_map_document(request.get_data(cache=False))
            saved = map_store.save(document)
        except MapValidationError as error:
            return jsonify({"error": "invalid_map", "message": str(error)}), 400
        except MapStoreError:
            return jsonify({"error": "map_save_failed"}), 503
        return jsonify({"saved": True, "map": saved})

    @blueprint.get("/dashboard/indoor-map/export")
    def export_indoor_map():
        if not session.get("authenticated"):
            return jsonify({"error": "authentication_required"}), 401
        if map_store is None:
            return jsonify({"error": "map_not_configured"}), 503
        try:
            document = map_store.load()
        except MapStoreError:
            return jsonify({"error": "map_unavailable"}), 503
        if document is None:
            return jsonify({"error": "map_not_created"}), 404
        payload = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        return send_file(
            BytesIO(payload.encode("utf-8")),
            mimetype="application/json",
            as_attachment=True,
            download_name="cognisight-home-map.json",
            max_age=0,
        )

    @blueprint.post("/dashboard/indoor-map/route")
    def preview_indoor_route():
        if not session.get("authenticated"):
            return jsonify({"error": "authentication_required"}), 401
        if map_store is None:
            return jsonify({"error": "map_not_configured"}), 503
        if not request.is_json:
            return jsonify({"error": "json_required"}), 415
        try:
            payload = parse_map_document(request.get_data(cache=False))
        except MapValidationError:
            return jsonify({"error": "invalid_route_request"}), 400
        if (
            type(payload) is not dict
            or set(payload) != {"start_room_id", "destination_room_id"}
            or any(
                not isinstance(payload.get(key), str) or not payload[key]
                for key in ("start_room_id", "destination_room_id")
            )
        ):
            return jsonify({"error": "invalid_route_request"}), 400
        try:
            document = map_store.load()
        except MapStoreError:
            return jsonify({"error": "map_unavailable"}), 503
        if document is None:
            return jsonify({"error": "map_not_created"}), 404
        try:
            route = plan_route(
                document,
                payload["start_room_id"],
                payload["destination_room_id"],
            )
        except RoutePlanningError as error:
            return jsonify({"error": error.code}), 400
        except MapValidationError:
            return jsonify({"error": "map_invalid"}), 503
        return jsonify(route)

    return blueprint


def create_dashboard_app(provider, state_dir, allowed_hosts, map_store=None):
    from flask import Flask, abort, flash, redirect, render_template, request, session
    from flask_wtf import CSRFProtect, FlaskForm
    from wtforms import PasswordField
    from wtforms.validators import InputRequired, Length
    from werkzeug.security import check_password_hash
    from werkzeug.middleware.proxy_fix import ProxyFix
    from cognisight.recognition.web import LoginLimiter, private_secret

    hosts = canonical_hosts(allowed_hosts)
    state_dir = Path(state_dir)
    if map_store is None:
        map_store = IndoorMapStore(state_dir / "indoor-map.json")
    app = Flask(__name__, template_folder="templates", static_folder=None)
    app.secret_key = hmac.new(
        private_secret(state_dir), b"caregiver-dashboard-v1", hashlib.sha256
    ).digest()
    app.config.update(
        SESSION_COOKIE_NAME="cognisight_caregiver",
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=15),
        SESSION_REFRESH_EACH_REQUEST=False,
        MAX_CONTENT_LENGTH=16 * 1024,
        WTF_CSRF_TIME_LIMIT=15 * 60,
    )
    # Exactly one local Caddy proxy; never trust forwarded host or prefix.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=0, x_proto=1, x_host=0, x_port=0, x_prefix=0)
    limiter = LoginLimiter()

    class LoginForm(FlaskForm):
        password = PasswordField(
            "Caregiver password", validators=[InputRequired(), Length(max=256)]
        )

    class ActionForm(FlaskForm):
        pass

    @app.before_request
    def security_boundary():
        original = request.environ.get("werkzeug.proxy_fix.orig", {})
        peer = original.get("REMOTE_ADDR", request.remote_addr)
        if not allowed_request(
            request.host, request.headers.get("Origin"), request.scheme, hosts, peer
        ):
            abort(403)
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            abort(403)

    CSRFProtect(app)

    @app.after_request
    def privacy_headers(response):
        response.headers.update(
            {
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "same-origin",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'",
            }
        )
        return response

    @app.get("/")
    def index():
        return redirect("/dashboard")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        password_file = state_dir / "password.hash"
        if not password_file.is_file() or password_file.is_symlink():
            return (
                render_template(
                    "caregiver_message.html",
                    message="Caregiver password setup is required on the Pi.",
                ),
                503,
            )
        form = LoginForm()
        if request.method == "POST":
            if not limiter.allowed(time.monotonic()):
                return (
                    render_template(
                        "caregiver_message.html",
                        message="Too many attempts. Try again in one minute.",
                    ),
                    429,
                )
            if form.validate_on_submit():
                try:
                    valid = check_password_hash(
                        password_file.read_text().strip(), form.password.data
                    )
                except (OSError, ValueError):
                    valid = False
                if valid:
                    limiter.success()
                    session.clear()
                    session["authenticated"] = True
                    session.permanent = True
                    return redirect("/dashboard")
                limiter.failure(time.monotonic())
                flash("Password not accepted.")
        return render_template("caregiver_login.html", form=form)

    @app.post("/logout")
    def logout():
        if not ActionForm().validate_on_submit():
            abort(400)
        session.clear()
        return redirect("/login")

    @app.context_processor
    def forms():
        return {"logout_form": ActionForm()}

    app.register_blueprint(create_dashboard_blueprint(provider, map_store))
    return app
