from __future__ import annotations

import hashlib
import hmac
import secrets
from functools import wraps
from typing import Callable, TypeVar

from flask import current_app, flash, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash


F = TypeVar("F", bound=Callable)
CSRF_SESSION_KEY = "_csrf_token"
USER_SESSION_KEY = "admin_authenticated"
PASSWORD_FINGERPRINT_SESSION_KEY = "admin_password_fingerprint"


def auth_enabled() -> bool:
    return bool(current_app.config.get("AUTH_ENABLED", True))


def password_configured() -> bool:
    return bool(str(current_app.config.get("ADMIN_PASSWORD_HASH", "") or "").strip())


def current_password_hash() -> str:
    return str(current_app.config.get("ADMIN_PASSWORD_HASH", "") or "")


def password_hash_fingerprint(password_hash: str | None = None) -> str:
    configured_hash = current_password_hash() if password_hash is None else str(password_hash or "")
    if not configured_hash:
        return ""
    return hashlib.sha256(configured_hash.encode("utf-8")).hexdigest()


def current_user_authenticated() -> bool:
    if not auth_enabled():
        return True
    if not session.get(USER_SESSION_KEY):
        return False
    expected_fingerprint = password_hash_fingerprint()
    session_fingerprint = str(session.get(PASSWORD_FINGERPRINT_SESSION_KEY, ""))
    return bool(
        expected_fingerprint
        and session_fingerprint
        and hmac.compare_digest(session_fingerprint, expected_fingerprint)
    )


def csrf_token() -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return str(token)


def csrf_form_field() -> str:
    return f'<input type="hidden" name="csrf_token" value="{csrf_token()}">'


def validate_csrf_request() -> bool:
    if not auth_enabled():
        return True
    expected = session.get(CSRF_SESSION_KEY)
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    return bool(expected and supplied and hmac.compare_digest(str(expected), str(supplied)))


def login_user(username: str, password: str) -> bool:
    configured_username = str(current_app.config.get("ADMIN_USERNAME", "admin"))
    configured_hash = current_password_hash()
    if not configured_hash:
        return False
    if not hmac.compare_digest(username, configured_username):
        return False
    if not check_password_hash(configured_hash, password):
        return False
    session.clear()
    session[USER_SESSION_KEY] = True
    session[PASSWORD_FINGERPRINT_SESSION_KEY] = password_hash_fingerprint(configured_hash)
    csrf_token()
    return True


def verify_current_password(password: str) -> bool:
    configured_hash = current_password_hash()
    if not configured_hash:
        return False
    return check_password_hash(configured_hash, password)


def logout_user() -> None:
    session.clear()


def wants_json_response() -> bool:
    if request.path.startswith("/api/"):
        return True
    return request.accept_mimetypes.best == "application/json"


def require_authentication():
    if current_user_authenticated():
        return None
    if wants_json_response():
        return jsonify({"error": "Authentication required."}), 401
    return redirect(url_for("login", next=request.full_path if request.query_string else request.path))


def require_csrf():
    if request.method != "POST":
        return None
    if validate_csrf_request():
        return None
    if wants_json_response():
        return jsonify({"error": "Invalid CSRF token."}), 400
    flash("Your session expired. Try the action again.", "error")
    return redirect(request.referrer or url_for("dashboard"))


def login_required(fn: F) -> F:
    @wraps(fn)
    def wrapper(*args, **kwargs):
        auth_response = require_authentication()
        if auth_response is not None:
            return auth_response
        return fn(*args, **kwargs)

    return wrapper  # type: ignore[return-value]
