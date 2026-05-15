from __future__ import annotations

import atexit
import hashlib
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from flask import Flask, flash, jsonify, redirect, render_template, request, url_for

import config as default_config
from flask_mega_downloader.config_defaults import apply_runtime_defaults
from flask_mega_downloader.security import (
    csrf_form_field,
    csrf_token,
    current_user_authenticated,
    login_user,
    logout_user,
    password_configured,
    require_authentication,
    require_csrf,
    verify_current_password,
)
from archive_extract_manager import ArchiveExtractManager
from archive_auto_sort import guessit_available
from archives import (
    ArchiveError,
    archive_type_for_path,
    default_archive_target_name,
    is_supported_archive_path,
)
from downloader import DownloadManager, ensure_destination_writable
from event_log import EventLogService, install_event_log_bridge
from explorer import (
    create_directory,
    delete_entries,
    list_directory,
    list_directory_tree,
    move_entries,
    normalize_user_path_input,
    path_within_root,
    preview_move_entries,
    rename_entry,
    relative_to_root,
    resolve_entries_in_directory,
    resolve_move_target,
    validate_entry_name,
)
from filecrypt_resolver import FilecryptResolutionError, expand_submission_urls_with_metadata
from media_compiler import MediaCompileManager, detect_bluray_source
from models import ACTIVE_ARCHIVE_JOB_STATUSES, MoveFavorite, utcnow_iso
from plex_permissions import PlexPermissionManager
from storage import JsonStorage
from werkzeug.security import generate_password_hash


MIN_ADMIN_PASSWORD_LENGTH = 12
ADMIN_PASSWORD_HASH_RE = re.compile(r"(?m)^ADMIN_PASSWORD_HASH\s*=.*$")


def format_bytes(value: int | float | None) -> str:
    if value is None:
        return "Unknown"
    size = float(value)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_datetime(value: str | None) -> str:
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def parse_urls(raw_text: str) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for line in raw_text.splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        urls.append(cleaned)
    return urls


def normalize_destination_path_input(raw_text: str) -> str:
    return normalize_user_path_input(raw_text)


def path_within_scope(scope: str, relative_path: str) -> bool:
    normalized_scope = str(scope or "").strip().replace("\\", "/").strip("/")
    normalized_relative = str(relative_path or "").strip().replace("\\", "/").strip("/")
    if not normalized_scope:
        return True
    return normalized_relative == normalized_scope or normalized_relative.startswith(f"{normalized_scope}/")


def normalize_move_target_input(raw_text: str) -> str:
    return normalize_user_path_input(raw_text)


def summarize_items(items: list[str], limit: int = 3) -> str:
    if not items:
        return ""
    if len(items) <= limit:
        return ", ".join(items)
    visible = ", ".join(items[:limit])
    return f"{visible}, and {len(items) - limit} more"


def pluralize(count: int, singular: str, plural: str | None = None) -> str:
    if count == 1:
        return singular
    return plural or f"{singular}s"


def submission_source_summary(urls: list[str]) -> dict[str, int]:
    summary = {"mega": 0, "filecrypt": 0, "other": 0}
    for url in urls:
        lowered = str(url).lower()
        if "filecrypt.cc" in lowered:
            summary["filecrypt"] += 1
        elif "mega.nz" in lowered or "mega.co.nz" in lowered:
            summary["mega"] += 1
        else:
            summary["other"] += 1
    return summary


def password_hash_is_environment_sourced(config_path: Path) -> bool:
    env_hash = os.environ.get("MEGA_DOWNLOADER_ADMIN_PASSWORD_HASH", "")
    if not env_hash:
        return False
    try:
        content = config_path.read_text(encoding="utf-8")
    except OSError:
        return True
    match = ADMIN_PASSWORD_HASH_RE.search(content)
    if not match:
        return True
    assignment = match.group(0)
    return "os.environ" in assignment or "MEGA_DOWNLOADER_ADMIN_PASSWORD_HASH" in assignment


def write_admin_password_hash(config_path: Path, password_hash: str) -> None:
    replacement = f"ADMIN_PASSWORD_HASH = {password_hash!r}"
    content = config_path.read_text(encoding="utf-8")
    if ADMIN_PASSWORD_HASH_RE.search(content):
        updated = ADMIN_PASSWORD_HASH_RE.sub(replacement, content, count=1)
    else:
        updated = content.rstrip() + "\n\n" + replacement + "\n"
    config_path.write_text(updated, encoding="utf-8")


def create_app() -> Flask:
    package_dir = Path(__file__).resolve().parent
    repo_root = package_dir.parent
    app = Flask(
        __name__,
        template_folder=str(repo_root / "templates"),
        static_folder=str(repo_root / "static"),
    )
    app.config.from_object(default_config)

    extra_config = os.environ.get("MEGA_DOWNLOADER_CONFIG")
    active_config_path = Path(default_config.__file__).expanduser().resolve()
    if extra_config:
        active_config_path = Path(extra_config).expanduser().resolve()
        app.config.from_pyfile(extra_config)
    apply_runtime_defaults(app.config)

    app.secret_key = app.config["SECRET_KEY"]
    app.jinja_env.filters["filesize"] = format_bytes
    app.jinja_env.filters["datetime_local"] = format_datetime

    if (
        Path(app.config["STATE_DB_FILE"]) == default_config.STATE_DB_FILE
        and Path(app.config["JOB_STORAGE_FILE"]).parent != default_config.JOB_STORAGE_FILE.parent
    ):
        app.config["STATE_DB_FILE"] = Path(app.config["JOB_STORAGE_FILE"]).with_name("state.sqlite3")

    app.config["STATE_DB_FILE"] = Path(app.config["STATE_DB_FILE"]).expanduser().resolve()
    app.config["JOB_STORAGE_FILE"] = Path(app.config["JOB_STORAGE_FILE"]).expanduser().resolve()
    for destination in app.config["ALLOWED_DESTINATIONS"].values():
        destination["path"] = Path(destination["path"]).expanduser().resolve()
        destination["path"].mkdir(parents=True, exist_ok=True)
    app.config["STATE_DB_FILE"].parent.mkdir(parents=True, exist_ok=True)
    app.config["JOB_STORAGE_FILE"].parent.mkdir(parents=True, exist_ok=True)

    storage = JsonStorage(
        app.config["STATE_DB_FILE"],
        legacy_json_path=app.config["JOB_STORAGE_FILE"],
    )
    event_logger = EventLogService(storage, max_rows=app.config["EVENT_LOG_MAX_ROWS"])
    bridge_handler = install_event_log_bridge(event_logger)
    permission_manager = PlexPermissionManager(
        enabled=app.config["PLEX_PERMISSIONS_ENABLED"],
        plex_user=app.config["PLEX_USER"],
        setfacl_binary=app.config["SETFACL_BINARY"],
        strict=app.config["PLEX_PERMISSION_STRICT"],
        event_logger=event_logger,
    )
    try:
        manager = DownloadManager(
            storage=storage,
            destinations=app.config["ALLOWED_DESTINATIONS"],
            megacmd_binary=app.config["MEGACMD_BINARY"],
            backend=app.config["DOWNLOADER_BACKEND"],
            download_workers=app.config["DOWNLOAD_WORKERS"],
            permission_manager=permission_manager,
            event_logger=event_logger,
        )
    except TypeError as exc:
        if "download_workers" not in str(exc) and "permission_manager" not in str(exc):
            raise
        manager = DownloadManager(
            storage=storage,
            destinations=app.config["ALLOWED_DESTINATIONS"],
            megacmd_binary=app.config["MEGACMD_BINARY"],
            backend=app.config["DOWNLOADER_BACKEND"],
            event_logger=event_logger,
        )
    try:
        media_manager = MediaCompileManager(
            storage=storage,
            makemkvcon_binary=app.config["MAKEMKVCON_BINARY"],
            mediainfo_binary=app.config["MEDIAINFO_BINARY"],
            bluray_min_title_seconds=app.config["BLURAY_MIN_TITLE_SECONDS"],
            media_workers=app.config["MEDIA_WORKERS"],
            permission_manager=permission_manager,
            event_logger=event_logger,
        )
    except TypeError as exc:
        if "media_workers" not in str(exc) and "permission_manager" not in str(exc):
            raise
        media_manager = MediaCompileManager(
            storage=storage,
            makemkvcon_binary=app.config["MAKEMKVCON_BINARY"],
            mediainfo_binary=app.config["MEDIAINFO_BINARY"],
            bluray_min_title_seconds=app.config["BLURAY_MIN_TITLE_SECONDS"],
            event_logger=event_logger,
        )
    try:
        archive_manager = ArchiveExtractManager(
            storage=storage,
            seven_zip_binary=app.config["SEVEN_ZIP_BINARY"],
            archive_workers=app.config["ARCHIVE_WORKERS"],
            permission_manager=permission_manager,
            event_logger=event_logger,
        )
    except TypeError as exc:
        if "archive_workers" not in str(exc) and "permission_manager" not in str(exc):
            raise
        archive_manager = ArchiveExtractManager(
            storage=storage,
            seven_zip_binary=app.config["SEVEN_ZIP_BINARY"],
            event_logger=event_logger,
        )
    manager.attach_archive_manager(archive_manager)
    app.extensions["download_manager"] = manager
    app.extensions["media_compile_manager"] = media_manager
    app.extensions["archive_extract_manager"] = archive_manager
    app.extensions["event_logger"] = event_logger
    app.extensions["event_log_bridge_handler"] = bridge_handler
    app.extensions["permission_manager"] = permission_manager
    app.extensions["active_config_path"] = active_config_path
    atexit.register(manager.stop)
    atexit.register(media_manager.stop)
    atexit.register(archive_manager.stop)

    for startup_notice in storage.consume_startup_notices():
        event_logger.log(
            startup_notice.get("level", "info"),
            startup_notice.get("subsystem", "storage"),
            startup_notice.get("feature", "startup"),
            startup_notice.get("message", "Storage startup event."),
            context=startup_notice.get("context"),
        )
    event_logger.info(
        "app",
        "startup",
        "Application initialized.",
        context={
            "state_db": str(app.config["STATE_DB_FILE"]),
            "download_backend": manager.backend_name,
            "archive_backend": app.config["SEVEN_ZIP_BINARY"],
            "bluray_backend_available": media_manager.backend_reason is None,
        },
    )
    if manager.backend_reason:
        event_logger.warning(
            "download",
            "backend",
            manager.backend_reason,
            context={"backend": manager.backend_name},
        )
    if media_manager.backend_reason:
        event_logger.warning(
            "bluray",
            "backend",
            media_manager.backend_reason,
            context={"backend": media_manager.backend_payload()["label"]},
        )

    @app.before_request
    def enforce_security():
        if request.endpoint in {"static", "login"}:
            return None
        auth_response = require_authentication()
        if auth_response is not None:
            return auth_response
        return require_csrf()

    def explorer_redirect(root_key: str, current_path: str, sort_by: str):
        return redirect(url_for("explorer", root=root_key, path=current_path, sort=sort_by))

    def redirect_back_or(fallback_endpoint: str = "dashboard", **fallback_values):
        referrer = request.referrer
        if referrer:
            parsed = urlsplit(referrer)
            current = urlsplit(request.host_url)
            if parsed.scheme in {"http", "https"} and parsed.netloc == current.netloc:
                safe_path = parsed.path or "/"
                return redirect(urlunsplit(("", "", safe_path, parsed.query, "")))
        return redirect(url_for(fallback_endpoint, **fallback_values))

    def post_context_redirect(fallback_endpoint: str = "dashboard"):
        root_key = request.form.get("root")
        if root_key:
            return explorer_redirect(
                root_key,
                request.form.get("current_path", ""),
                request.form.get("sort", "name"),
            )
        return redirect_back_or(fallback_endpoint)

    def destination_label_lookup() -> dict[str, str]:
        return {item["key"]: item["label"] for item in manager.destination_options()}

    def dashboard_payload() -> dict:
        payload = manager.dashboard_payload()
        payload["media"] = media_manager.dashboard_payload(destination_label_lookup())
        payload["archives"] = archive_manager.dashboard_payload()
        return payload

    def logs_payload(*, after_id: int | None = None, limit: int = 200) -> dict:
        entries = [entry.to_dict() for entry in event_logger.load(after_id=after_id, limit=limit)]
        last_id = entries[-1]["id"] if entries else after_id
        return {
            "entries": entries,
            "last_id": last_id,
            "updated_at": utcnow_iso(),
        }

    def json_request_payload() -> dict:
        payload = request.get_json(silent=True)
        return payload if isinstance(payload, dict) else {}

    def json_error(message: str, *, status: int = 400, **payload):
        body = {"ok": False, "error": message}
        body.update(payload)
        return jsonify(body), status

    def json_success(**payload):
        body = {"ok": True}
        body.update(payload)
        return jsonify(body)

    def bool_payload_value(payload: dict, key: str, *, default: bool = False) -> bool:
        value = payload.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    def selected_paths_from_payload(payload: dict) -> list[str]:
        raw_paths = payload.get("selected_paths", payload.get("paths", []))
        if isinstance(raw_paths, str):
            return [raw_paths]
        if not isinstance(raw_paths, list):
            return []
        return [str(item) for item in raw_paths]

    def log_event(level: str, subsystem: str, feature: str, message: str, **kwargs):
        event_logger.log(level, subsystem, feature, message, **kwargs)

    def move_favorite_options() -> list[dict]:
        favorites = storage.load_move_favorites()
        favorites.sort(key=lambda item: (item.label.lower(), item.path.lower()))
        return [favorite.to_dict() for favorite in favorites]

    def resolve_archive_auto_sort_targets() -> tuple[Path, Path]:
        available, reason = guessit_available()
        if not available:
            raise ValueError(reason or "Archive auto-sort is unavailable.")

        favorites = storage.load_move_favorites()

        def favorite_for_label(label: str) -> Path:
            matches = [favorite for favorite in favorites if favorite.label.casefold() == label.casefold()]
            if not matches:
                raise ValueError(
                    f"Archive auto-sort requires exactly one saved move favorite named '{label}'."
                )
            if len(matches) > 1:
                raise ValueError(
                    f"Archive auto-sort found multiple saved move favorites named '{label}'. Keep exactly one."
                )
            target_path = Path(matches[0].path).expanduser().resolve()
            ensure_destination_writable(target_path)
            return target_path

        return favorite_for_label("Movies"), favorite_for_label("TvShows")

    def persist_archive_automation_settings(
        *,
        auto_sort_enabled: bool,
        auto_delete_enabled: bool,
    ) -> dict:
        auto_delete_enabled = bool(auto_sort_enabled and auto_delete_enabled)
        if auto_sort_enabled:
            resolve_archive_auto_sort_targets()
        return manager.update_archive_automation_settings(
            auto_sort_enabled=auto_sort_enabled,
            auto_delete_enabled=auto_delete_enabled,
        )

    def persist_archive_automation_settings_from_form(
        *,
        auto_sort_field: str = "archive_auto_sort_enabled",
        auto_delete_field: str = "archive_auto_delete_enabled",
    ) -> dict:
        auto_sort_enabled = request.form.get(auto_sort_field) == "1"
        auto_delete_enabled = request.form.get(auto_delete_field) == "1"
        return persist_archive_automation_settings(
            auto_sort_enabled=auto_sort_enabled,
            auto_delete_enabled=auto_delete_enabled,
        )

    def save_move_favorite(root_key: str, current_path: str, target_input: str) -> dict:
        _, _, target_dir = resolve_move_target(manager.destinations, root_key, current_path, target_input)
        ensure_destination_writable(target_dir)
        resolved_path = str(target_dir)

        favorites = storage.load_move_favorites()
        for favorite in favorites:
            if Path(favorite.path).expanduser().resolve() == target_dir:
                return {
                    "key": favorite.key,
                    "label": favorite.label,
                    "path": favorite.path,
                    "created": False,
                }

        label = target_dir.name or resolved_path
        favorite = MoveFavorite(
            key=f"move_{hashlib.sha1(resolved_path.encode('utf-8')).hexdigest()[:10]}",
            label=label,
            path=resolved_path,
        )
        favorites.append(favorite)
        favorites.sort(key=lambda item: (item.label.lower(), item.path.lower()))
        storage.save_move_favorites(favorites)
        return {
            "key": favorite.key,
            "label": favorite.label,
            "path": favorite.path,
            "created": True,
        }

    def explorer_archive_context(payload: dict) -> tuple[list[dict], dict]:
        archive_dashboard = archive_manager.dashboard_payload()
        explorer_archive_jobs = [
            job
            for job in archive_dashboard["jobs"]
            if job["root_key"] == payload["root"]["key"]
            and (
                path_within_scope(payload["current_path"], job["archive_relative_path"])
                or path_within_scope(payload["current_path"], job["target_relative_path"])
            )
        ]
        explorer_archive_summary = {
            "total_jobs": len(explorer_archive_jobs),
            "queued_jobs": sum(1 for job in explorer_archive_jobs if job["status"] == "queued"),
            "active_jobs": sum(1 for job in explorer_archive_jobs if job["status"] in ACTIVE_ARCHIVE_JOB_STATUSES),
            "completed_jobs": sum(1 for job in explorer_archive_jobs if job["status"] == "completed"),
            "failed_jobs": sum(1 for job in explorer_archive_jobs if job["status"] == "failed"),
            "canceled_jobs": sum(1 for job in explorer_archive_jobs if job["status"] == "canceled"),
        }
        return explorer_archive_jobs, explorer_archive_summary

    def build_explorer_payload(
        root_key: str,
        current_path: str,
        sort_by: str = "name",
        sort_order: str = "asc",
    ) -> dict:
        explorer_payload = list_directory(manager.destinations, root_key, current_path, sort_by, sort_order)
        explorer_archive_jobs, explorer_archive_summary = explorer_archive_context(explorer_payload)
        return {
            "explorer": explorer_payload,
            "roots": manager.destination_options(),
            "move_favorites": move_favorite_options(),
            "media_backend": media_manager.backend_payload(),
            "archive_jobs": explorer_archive_jobs,
            "archive_summary": explorer_archive_summary,
            "archive_automation_settings": manager.archive_automation_settings_payload(),
        }

    def render_explorer_page(
        root_key: str,
        current_path: str,
        sort_by: str,
        *,
        move_confirmation: dict | None = None,
    ):
        destination_options = manager.destination_options()
        if not destination_options:
            flash("No destinations are configured. Restore or add one before opening the explorer.", "error")
            return redirect(url_for("dashboard"))

        sort_order = request.args.get("order", "asc")
        try:
            payload = build_explorer_payload(root_key, current_path, sort_by, sort_order)
        except PermissionError as exc:
            flash(str(exc), "error")
            log_event(
                "error",
                "explorer",
                "open",
                "Explorer open failed because the app user cannot read the folder.",
                context={"root": root_key, "path": current_path, "error": str(exc)},
            )
            return redirect(url_for("dashboard"))
        except (ValueError, FileNotFoundError) as exc:
            flash(str(exc), "error")
            fallback_root = destination_options[0]["key"]
            return redirect(url_for("explorer", root=fallback_root))
        explorer_payload = payload["explorer"]

        return render_template(
            "explorer.html",
            explorer=explorer_payload,
            explorer_payload=payload,
            move_favorites=payload["move_favorites"],
            move_confirmation=move_confirmation,
            media_backend=payload["media_backend"],
            explorer_archive_jobs=payload["archive_jobs"],
            explorer_archive_summary=payload["archive_summary"],
        )

    def extract_archives_in_folder(
        root_key: str,
        current_path: str,
        relative_paths: list[str],
        *,
        password: str | None = None,
        target_dir_name: str | None = None,
        skip_non_archive: bool,
        auto_sort_enabled: bool = False,
        auto_delete_enabled: bool = False,
    ) -> dict:
        root_info, _, resolved_entries = resolve_entries_in_directory(
            manager.destinations,
            root_key,
            current_path,
            relative_paths,
        )
        root = root_info["path"]
        prepared_jobs: list[dict] = []
        skipped: list[str] = []
        failures: list[str] = []
        movies_target_path: Path | None = None
        tv_target_path: Path | None = None

        if auto_sort_enabled:
            movies_target_path, tv_target_path = resolve_archive_auto_sort_targets()
        auto_delete_enabled = auto_sort_enabled and auto_delete_enabled

        if target_dir_name:
            target_dir_name = validate_entry_name(target_dir_name)

        for relative_path, entry_path in resolved_entries:
            archive_type = archive_type_for_path(entry_path)
            if not is_supported_archive_path(entry_path):
                if skip_non_archive:
                    skipped.append(Path(relative_path).name)
                    continue
                raise ArchiveError("Only zip, rar, and 7z files inside an allowed root can be extracted.")

            if target_dir_name:
                target_relative = str(Path(relative_path).parent / target_dir_name)
            else:
                target_relative = str(Path(relative_path).parent / default_archive_target_name(entry_path))
            target_dir = path_within_root(root, target_relative)

            prepared_jobs.append(
                {
                    "root_key": root_key,
                    "archive_relative_path": relative_path,
                    "archive_path": str(entry_path),
                    "archive_display_name": entry_path.name,
                    "archive_type": archive_type,
                    "target_relative_path": relative_to_root(root, target_dir),
                    "target_path": str(target_dir),
                    "archive_password": password,
                    "auto_sort_enabled": auto_sort_enabled,
                    "auto_delete_enabled": auto_delete_enabled,
                    "movies_target_path": str(movies_target_path) if movies_target_path else None,
                    "tv_target_path": str(tv_target_path) if tv_target_path else None,
                }
            )

        jobs = archive_manager.submit(prepared_jobs)
        return {
            "queued": [
                {
                    "archive": job.archive_display_name,
                    "archive_type": job.archive_type,
                    "target": Path(job.target_path).name,
                }
                for job in jobs
            ],
            "skipped": skipped,
            "failures": failures,
        }

    @app.context_processor
    def inject_globals():
        return {
            "app_title": "Flask Mega Downloader",
            "poll_interval_ms": app.config["POLL_INTERVAL_MS"],
            "auth_enabled": app.config["AUTH_ENABLED"],
            "password_configured": password_configured(),
            "current_user_authenticated": current_user_authenticated(),
            "csrf_token": csrf_token,
            "csrf_form_field": csrf_form_field,
            "destinations": manager.destination_options(),
            "has_destinations": manager.has_destinations(),
            "can_restore_base_destinations": manager.can_restore_base_destinations(),
            "queue_sort_options": manager.queue_sort_options(),
            "current_queue_sort": manager.queue_sort_mode,
            "archive_automation_settings": manager.archive_automation_settings_payload(),
        }

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not app.config["AUTH_ENABLED"]:
            return redirect(url_for("dashboard"))
        if current_user_authenticated():
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if login_user(username, password):
                return redirect(request.args.get("next") or url_for("dashboard"))
            flash("Invalid admin username or password.", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        logout_user()
        flash("Signed out.", "success")
        return redirect(url_for("login"))

    @app.get("/")
    def dashboard():
        return render_template("index.html", dashboard=dashboard_payload())

    @app.get("/logs")
    def logs():
        return render_template("logs.html", log_payload=logs_payload())

    @app.get("/roots")
    def roots():
        return render_template("roots.html")

    @app.get("/profile")
    def profile():
        config_path = app.extensions["active_config_path"]
        return render_template(
            "profile.html",
            admin_username=app.config.get("ADMIN_USERNAME", "admin"),
            min_password_length=MIN_ADMIN_PASSWORD_LENGTH,
            password_change_available=(
                bool(app.config["AUTH_ENABLED"])
                and password_configured()
                and not password_hash_is_environment_sourced(config_path)
            ),
            password_hash_environment_sourced=password_hash_is_environment_sourced(config_path),
        )

    @app.post("/profile/password")
    def change_profile_password():
        config_path = app.extensions["active_config_path"]
        if not app.config["AUTH_ENABLED"]:
            flash("Password changes are unavailable while built-in auth is disabled.", "error")
            return redirect(url_for("profile"))
        if password_hash_is_environment_sourced(config_path):
            flash(
                "This password is managed by MEGA_DOWNLOADER_ADMIN_PASSWORD_HASH. Change that environment variable instead.",
                "error",
            )
            return redirect(url_for("profile"))

        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not verify_current_password(current_password):
            flash("Current password is incorrect.", "error")
            return redirect(url_for("profile"))
        if len(new_password) < MIN_ADMIN_PASSWORD_LENGTH:
            flash(f"New password must be at least {MIN_ADMIN_PASSWORD_LENGTH} characters.", "error")
            return redirect(url_for("profile"))
        if new_password != confirm_password:
            flash("New password and confirmation do not match.", "error")
            return redirect(url_for("profile"))

        new_password_hash = generate_password_hash(new_password)
        try:
            write_admin_password_hash(config_path, new_password_hash)
        except OSError as exc:
            flash(f"Could not update the active config file: {exc}", "error")
            return redirect(url_for("profile"))

        app.config["ADMIN_PASSWORD_HASH"] = new_password_hash
        log_event(
            "info",
            "auth",
            "profile",
            "Admin password changed from the profile page.",
            context={"config_path": str(config_path)},
        )
        logout_user()
        flash("Password updated. Sign in with the new password.", "success")
        return redirect(url_for("login"))

    @app.post("/submit")
    def submit():
        submit_mode = request.form.get("submit_mode", "start").strip().lower()
        auto_extract_enabled = submit_mode == "auto_extract"
        urls = parse_urls(request.form.get("urls", ""))
        destination_key = request.form.get("destination", "")
        destination_subpath = normalize_destination_path_input(request.form.get("destination_path", ""))
        max_urls = int(app.config["MAX_URLS_PER_SUBMISSION"])
        if len(urls) > max_urls:
            flash(f"Submit {max_urls} URLs or fewer at a time.", "error")
            log_event(
                "warning",
                "download",
                "submit",
                "Submission rejected because it exceeded the URL limit.",
                context={"url_count": len(urls), "limit": max_urls},
            )
            return redirect(url_for("dashboard"))

        try:
            archive_automation_settings = persist_archive_automation_settings_from_form()
        except ValueError as exc:
            flash(str(exc), "error")
            log_event(
                "error",
                "archive",
                "settings",
                "Archive automation settings update failed during submission.",
                context={"error": str(exc)},
            )
            return redirect(url_for("dashboard"))

        if not urls:
            flash("Paste at least one MEGA or Filecrypt URL.", "error")
            log_event("warning", "download", "submit", "Submission rejected because no URLs were provided.")
            return redirect(url_for("dashboard"))

        log_event(
            "info",
            "download",
            "submit",
            "Processing download submission.",
            context={
                "url_count": len(urls),
                "source_summary": submission_source_summary(urls),
                "destination_key": destination_key,
                "destination_path": destination_subpath,
                "auto_extract_enabled": auto_extract_enabled,
                "archive_automation_settings": archive_automation_settings,
            },
        )

        try:
            urls, resolution_summary, metadata_overrides = expand_submission_urls_with_metadata(urls)
        except FilecryptResolutionError as exc:
            flash(str(exc), "error")
            log_event(
                "error",
                "filecrypt",
                "resolve",
                "Filecrypt resolution failed during submission.",
                context={"error": str(exc)},
            )
            return redirect(url_for("dashboard"))

        if resolution_summary.containers_resolved:
            log_event(
                "info",
                "filecrypt",
                "resolve",
                "Resolved Filecrypt container links into MEGA URLs.",
                context={
                    "containers_resolved": resolution_summary.containers_resolved,
                    "mega_links_resolved": resolution_summary.mega_links_resolved,
                },
            )

        try:
            jobs = manager.submit(
                urls,
                destination_key,
                destination_subpath,
                metadata_overrides=metadata_overrides,
                auto_extract_enabled=auto_extract_enabled,
            )
        except ValueError as exc:
            flash(str(exc), "error")
            log_event(
                "error",
                "download",
                "submit",
                "Download submission failed.",
                context={"error": str(exc), "destination_key": destination_key},
            )
            return redirect(url_for("dashboard"))

        if resolution_summary.containers_resolved:
            flash(
                (
                    f"Resolved {resolution_summary.containers_resolved} Filecrypt "
                    f"{pluralize(resolution_summary.containers_resolved, 'container')} into "
                    f"{resolution_summary.mega_links_resolved} MEGA "
                    f"{pluralize(resolution_summary.mega_links_resolved, 'link')}."
                ),
                "success",
            )
        if auto_extract_enabled:
            flash("Auto Extract is enabled for this batch. Archive extraction will start as soon as each archive set is complete.", "success")
        flash(f"Queued {len(jobs)} job(s) in batch {jobs[0].batch_id}.", "success")
        log_event(
            "info",
            "download",
            "submit",
            "Queued download batch.",
            batch_id=jobs[0].batch_id,
            context={
                "job_count": len(jobs),
                "destination_key": destination_key,
                "auto_extract_enabled": auto_extract_enabled,
                "archive_automation_settings": archive_automation_settings,
            },
        )
        return redirect(url_for("dashboard"))

    @app.post("/archive-automation-settings")
    def save_archive_automation_settings():
        try:
            settings = persist_archive_automation_settings_from_form()
        except ValueError as exc:
            flash(str(exc), "error")
            log_event(
                "error",
                "archive",
                "settings",
                "Archive automation settings update failed.",
                context={"error": str(exc)},
            )
            return post_context_redirect("dashboard")

        flash("Saved archive automation defaults.", "success")
        log_event(
            "info",
            "archive",
            "settings",
            "Saved archive automation defaults.",
            context=settings,
        )
        return post_context_redirect("dashboard")

    @app.post("/favorites")
    def add_favorite():
        destination_key = request.form.get("destination", "")
        destination_input = normalize_destination_path_input(request.form.get("destination_path", ""))
        if not destination_input:
            flash("Enter a custom destination path before adding it to favorites.", "error")
            log_event("warning", "app", "destination_favorite", "Destination favorite add rejected because the path was empty.")
            return post_context_redirect()

        try:
            favorite = manager.add_favorite_destination(destination_key, destination_input)
        except ValueError as exc:
            flash(str(exc), "error")
            log_event(
                "error",
                "app",
                "destination_favorite",
                "Destination favorite add failed.",
                context={"error": str(exc), "destination_key": destination_key, "path": destination_input},
            )
            return post_context_redirect()

        if favorite["created"]:
            flash(f"Added favorite destination: {favorite['path']}", "success")
        else:
            flash(f"Destination already exists in the dropdown: {favorite['path']}", "success")
        log_event(
            "info",
            "app",
            "destination_favorite",
            "Processed destination favorite request.",
            context={"created": favorite["created"], "path": favorite["path"], "destination_key": destination_key},
        )
        return post_context_redirect()

    @app.post("/destinations/<destination_key>/delete")
    def delete_destination(destination_key: str):
        try:
            deleted = manager.delete_destination(
                destination_key,
                extra_in_use=media_manager.destination_in_use(destination_key),
            )
            flash(f"Deleted {deleted['type']} destination: {deleted['label']}.", "success")
            log_event(
                "info",
                "app",
                "destination_delete",
                "Deleted configured destination.",
                context=deleted,
            )
        except ValueError as exc:
            flash(str(exc), "error")
            log_event(
                "error",
                "app",
                "destination_delete",
                "Destination delete failed.",
                context={"destination_key": destination_key, "error": str(exc)},
            )
        return redirect_back_or("dashboard")

    @app.post("/destinations/restore")
    def restore_destinations():
        restored = manager.restore_hidden_base_destinations()
        if restored:
            flash(f"Restored {restored} configured destination(s).", "success")
        else:
            flash("There were no hidden configured destinations to restore.", "success")
        log_event(
            "info",
            "app",
            "destination_restore",
            "Processed destination restore request.",
            context={"restored": restored},
        )
        return redirect_back_or("dashboard")

    @app.get("/api/jobs")
    def api_jobs():
        return jsonify(dashboard_payload())

    @app.get("/api/logs")
    def api_logs():
        raw_after_id = request.args.get("after_id", "").strip()
        after_id = int(raw_after_id) if raw_after_id.isdigit() else None
        return jsonify(logs_payload(after_id=after_id))

    @app.post("/jobs/<job_id>/cancel")
    def cancel_job(job_id: str):
        try:
            manager.cancel_job(job_id)
            flash("Cancel request sent.", "success")
            log_event("info", "download", "cancel", "Download cancel requested.", job_id=job_id)
        except ValueError as exc:
            flash(str(exc), "error")
            log_event("error", "download", "cancel", "Download cancel request failed.", job_id=job_id, context={"error": str(exc)})
        return redirect_back_or("dashboard")

    @app.post("/jobs/<job_id>/retry")
    def retry_job(job_id: str):
        try:
            manager.retry_job(job_id)
            flash("Job re-queued.", "success")
            log_event("info", "download", "retry", "Download retry requested.", job_id=job_id)
        except ValueError as exc:
            flash(str(exc), "error")
            log_event("error", "download", "retry", "Download retry request failed.", job_id=job_id, context={"error": str(exc)})
        return redirect_back_or("dashboard")

    @app.post("/media-jobs/<job_id>/cancel")
    def cancel_media_job(job_id: str):
        try:
            media_manager.cancel_job(job_id)
            flash("Blu-ray cancel request sent.", "success")
            log_event("info", "bluray", "cancel", "Blu-ray cancel requested.", job_id=job_id)
        except ValueError as exc:
            flash(str(exc), "error")
            log_event("error", "bluray", "cancel", "Blu-ray cancel request failed.", job_id=job_id, context={"error": str(exc)})
        return redirect_back_or("dashboard")

    @app.post("/media-jobs/<job_id>/retry")
    def retry_media_job(job_id: str):
        try:
            media_manager.retry_job(job_id)
            flash("Blu-ray job re-queued.", "success")
            log_event("info", "bluray", "retry", "Blu-ray retry requested.", job_id=job_id)
        except ValueError as exc:
            flash(str(exc), "error")
            log_event("error", "bluray", "retry", "Blu-ray retry request failed.", job_id=job_id, context={"error": str(exc)})
        return redirect_back_or("dashboard")

    @app.post("/archive-jobs/<job_id>/cancel")
    def cancel_archive_job(job_id: str):
        try:
            archive_manager.cancel_job(job_id)
            flash("Archive cancel request sent.", "success")
            log_event("info", "archive", "cancel", "Archive cancel requested.", job_id=job_id)
        except ValueError as exc:
            flash(str(exc), "error")
            log_event("error", "archive", "cancel", "Archive cancel request failed.", job_id=job_id, context={"error": str(exc)})
        return post_context_redirect("dashboard")

    @app.post("/archive-jobs/clear")
    def clear_archive_queue():
        result = archive_manager.clear_queue()
        messages: list[str] = []
        if result["removed"]:
            messages.append(f"Removed {result['removed']} archive job(s) from the queue.")
        if result["canceling"]:
            messages.append(
                f"Canceling {result['canceling']} running archive job(s); they will disappear once cancellation finishes."
            )
        if not messages:
            messages.append("Archive extraction queue was already empty.")
        flash(" ".join(messages), "success")
        log_event("info", "archive", "clear", "Processed archive queue clear request.", context=result)
        return post_context_redirect("dashboard")

    @app.post("/jobs/<job_id>/pause")
    def pause_job(job_id: str):
        try:
            manager.pause_job(job_id)
            flash("Pause request sent.", "success")
            log_event("info", "download", "pause", "Download pause requested.", job_id=job_id)
        except ValueError as exc:
            flash(str(exc), "error")
            log_event("error", "download", "pause", "Download pause request failed.", job_id=job_id, context={"error": str(exc)})
        return redirect_back_or("dashboard")

    @app.post("/jobs/<job_id>/resume")
    def resume_job(job_id: str):
        try:
            manager.resume_job(job_id)
            flash("Resume request sent.", "success")
            log_event("info", "download", "resume", "Download resume requested.", job_id=job_id)
        except ValueError as exc:
            flash(str(exc), "error")
            log_event("error", "download", "resume", "Download resume request failed.", job_id=job_id, context={"error": str(exc)})
        return redirect_back_or("dashboard")

    @app.post("/jobs/clear")
    def clear_queue():
        result = manager.clear_queue()
        messages: list[str] = []
        if result["removed"]:
            messages.append(f"Removed {result['removed']} job(s) from the queue.")
        if result["canceling"]:
            messages.append(
                f"Canceling {result['canceling']} running or paused job(s); they will disappear once cancellation finishes."
            )
        if not messages:
            messages.append("Queue was already empty.")
        flash(" ".join(messages), "success")
        log_event("info", "download", "clear", "Processed download queue clear request.", context=result)
        return redirect_back_or("dashboard")

    @app.post("/jobs/toggle-all")
    def toggle_all_jobs():
        toggle = manager.bulk_pause_toggle()
        if not toggle["available"]:
            flash("There were no queued, active, or paused jobs to change.", "success")
            log_event("debug", "download", "bulk_toggle", "Bulk pause toggle ignored because no eligible jobs were found.")
            return redirect_back_or("dashboard")

        if toggle["action"] == "pause":
            result = manager.pause_all()
            flash(f"Paused {result['paused']} queued or active job(s).", "success")
            log_event("info", "download", "bulk_pause", "Paused all eligible downloads.", context=result)
        else:
            result = manager.resume_all()
            flash(f"Resumed {result['resumed']} paused job(s).", "success")
            log_event("info", "download", "bulk_resume", "Resumed all paused downloads.", context=result)
        return redirect_back_or("dashboard")

    @app.post("/jobs/sort")
    def sort_jobs():
        sort_by = request.form.get("sort_by", "")
        try:
            result = manager.sort_queue(sort_by)
        except ValueError as exc:
            flash(str(exc), "error")
            log_event("error", "download", "sort", "Queued download sort failed.", context={"sort_by": sort_by, "error": str(exc)})
            return redirect_back_or("dashboard")

        if result["sorted"]:
            flash(f"Sorted {result['sorted']} queued job(s) by {result['label']}.", "success")
        else:
            flash("There were no queued jobs to sort.", "success")
        log_event("info", "download", "sort", "Processed queued download sort request.", context=result)
        return redirect_back_or("dashboard")

    @app.get("/explorer")
    def explorer():
        destination_options = manager.destination_options()
        if not destination_options:
            flash("No destinations are configured. Restore or add one before opening the explorer.", "error")
            log_event("warning", "explorer", "open", "Explorer open rejected because no destinations are configured.")
            return redirect(url_for("dashboard"))
        requested_root = request.args.get("root") or destination_options[0]["key"]
        requested_path = request.args.get("path", "")
        sort_by = request.args.get("sort", "name")
        return render_explorer_page(requested_root, requested_path, sort_by)

    @app.get("/api/explorer")
    def api_explorer():
        destination_options = manager.destination_options()
        if not destination_options:
            return json_error("No destinations are configured.")
        requested_root = request.args.get("root") or destination_options[0]["key"]
        requested_path = request.args.get("path", "")
        sort_by = request.args.get("sort", "name")
        sort_order = request.args.get("order", "asc")
        try:
            payload = build_explorer_payload(requested_root, requested_path, sort_by, sort_order)
        except PermissionError as exc:
            return json_error(str(exc), status=403)
        except (ValueError, FileNotFoundError) as exc:
            return json_error(str(exc))
        return json_success(**payload)

    @app.get("/api/explorer/tree")
    def api_explorer_tree():
        destination_options = manager.destination_options()
        if not destination_options:
            return json_error("No destinations are configured.")
        requested_root = request.args.get("root") or destination_options[0]["key"]
        requested_path = request.args.get("path", "")
        try:
            payload = list_directory_tree(manager.destinations, requested_root, requested_path)
        except PermissionError as exc:
            return json_error(str(exc), status=403)
        except (ValueError, FileNotFoundError) as exc:
            return json_error(str(exc))
        return json_success(tree=payload)

    @app.post("/api/explorer/folders")
    def api_explorer_create_folder():
        payload = json_request_payload()
        root_key = str(payload.get("root", ""))
        current_path = str(payload.get("current_path", payload.get("path", "")))
        name = str(payload.get("name", ""))
        try:
            created = create_directory(
                manager.destinations,
                root_key,
                current_path,
                name,
                permission_manager=permission_manager,
            )
            explorer_payload = build_explorer_payload(
                root_key,
                current_path,
                str(payload.get("sort", "name")),
                str(payload.get("order", "asc")),
            )
        except (ValueError, FileNotFoundError, OSError) as exc:
            log_event("error", "explorer", "mkdir", "Explorer folder creation failed.", context={"root": root_key, "path": current_path, "error": str(exc)})
            return json_error(str(exc))
        log_event("info", "explorer", "mkdir", "Created explorer folder.", context={"root": root_key, "path": created["relative_path"]})
        return json_success(created=created, **explorer_payload)

    @app.post("/api/explorer/rename")
    def api_explorer_rename():
        payload = json_request_payload()
        root_key = str(payload.get("root", ""))
        current_path = str(payload.get("current_path", payload.get("path", "")))
        sort_by = str(payload.get("sort", "name"))
        sort_order = str(payload.get("order", "asc"))
        relative_path = str(payload.get("entry_path", ""))
        new_name = str(payload.get("new_name", ""))
        try:
            result = rename_entry(manager.destinations, root_key, current_path, relative_path, new_name)
            explorer_payload = build_explorer_payload(root_key, current_path, sort_by, sort_order)
        except (ValueError, FileNotFoundError) as exc:
            log_event("error", "explorer", "rename", "Explorer JSON rename failed.", context={"root": root_key, "path": current_path, "entry_path": relative_path, "error": str(exc)})
            return json_error(str(exc))
        log_event("info", "explorer", "rename", "Processed explorer JSON rename request.", context={"root": root_key, "path": current_path, "renamed": result["renamed"], "name": result["name"]})
        return json_success(result=result, **explorer_payload)

    @app.post("/api/explorer/delete")
    def api_explorer_delete():
        payload = json_request_payload()
        root_key = str(payload.get("root", ""))
        current_path = str(payload.get("current_path", payload.get("path", "")))
        sort_by = str(payload.get("sort", "name"))
        sort_order = str(payload.get("order", "asc"))
        selected_paths = selected_paths_from_payload(payload)
        if not bool_payload_value(payload, "confirm_delete"):
            return json_error("Delete confirmation required.", status=409, requires_confirmation=True, selected_paths=selected_paths)
        try:
            result = delete_entries(manager.destinations, root_key, current_path, selected_paths)
            explorer_payload = build_explorer_payload(root_key, current_path, sort_by, sort_order)
        except (ValueError, FileNotFoundError) as exc:
            log_event("error", "explorer", "delete", "Explorer JSON delete failed.", context={"root": root_key, "path": current_path, "error": str(exc)})
            return json_error(str(exc))
        log_event("info", "explorer", "delete", "Processed explorer JSON delete request.", context={"root": root_key, "path": current_path, "deleted": len(result["deleted"]), "failures": len(result["failures"])})
        return json_success(result=result, **explorer_payload)

    @app.post("/api/explorer/move/preview")
    def api_explorer_move_preview():
        payload = json_request_payload()
        root_key = str(payload.get("root", ""))
        current_path = str(payload.get("current_path", payload.get("path", "")))
        selected_paths = selected_paths_from_payload(payload)
        move_target = normalize_move_target_input(str(payload.get("move_target", "")))
        try:
            preview = preview_move_entries(manager.destinations, root_key, current_path, selected_paths, move_target)
        except (ValueError, FileNotFoundError) as exc:
            log_event("error", "explorer", "move_preview", "Explorer JSON move preview failed.", context={"root": root_key, "path": current_path, "error": str(exc)})
            return json_error(str(exc))
        return json_success(
            target_dir=str(preview["target_dir"]),
            conflicts=preview["conflicts"],
            entries=[{"relative_path": relative_path, "name": path.name} for relative_path, path in preview["entries"]],
            requires_confirmation=bool(preview["conflicts"]),
        )

    @app.post("/api/explorer/move")
    def api_explorer_move():
        payload = json_request_payload()
        root_key = str(payload.get("root", ""))
        current_path = str(payload.get("current_path", payload.get("path", "")))
        sort_by = str(payload.get("sort", "name"))
        sort_order = str(payload.get("order", "asc"))
        selected_paths = selected_paths_from_payload(payload)
        move_target = normalize_move_target_input(str(payload.get("move_target", "")))
        replace_existing = bool_payload_value(payload, "replace_existing")

        try:
            preview = preview_move_entries(manager.destinations, root_key, current_path, selected_paths, move_target)
        except (ValueError, FileNotFoundError) as exc:
            log_event("error", "explorer", "move", "Explorer JSON move preview failed.", context={"root": root_key, "path": current_path, "error": str(exc)})
            return json_error(str(exc))

        if preview["conflicts"] and not replace_existing:
            return json_error(
                "Move target contains existing items.",
                status=409,
                requires_confirmation=True,
                target_dir=str(preview["target_dir"]),
                conflicts=preview["conflicts"],
            )

        try:
            ensure_destination_writable(preview["target_dir"])
            result = move_entries(
                manager.destinations,
                root_key,
                current_path,
                selected_paths,
                move_target,
                replace_existing=replace_existing,
                permission_manager=permission_manager,
            )
            explorer_payload = build_explorer_payload(root_key, current_path, sort_by, sort_order)
        except (ValueError, FileNotFoundError) as exc:
            log_event("error", "explorer", "move", "Explorer JSON move failed.", context={"root": root_key, "path": current_path, "error": str(exc)})
            return json_error(str(exc))
        log_event("info", "explorer", "move", "Processed explorer JSON move request.", context={"target_dir": str(result["target_dir"]), "moved": len(result["moved"]), "replaced": len(result["replaced"]), "failures": len(result["failures"])})
        return json_success(result=result, **explorer_payload)

    @app.post("/api/explorer/move-favorites")
    def api_explorer_move_favorites():
        payload = json_request_payload()
        root_key = str(payload.get("root", ""))
        current_path = str(payload.get("current_path", payload.get("path", "")))
        move_target = normalize_move_target_input(str(payload.get("move_target", "")))
        if not move_target:
            return json_error("Enter a move target path before saving it.")
        try:
            favorite = save_move_favorite(root_key, current_path, move_target)
        except ValueError as exc:
            log_event("error", "explorer", "move_favorite", "Explorer JSON move target favorite add failed.", context={"root": root_key, "path": current_path, "error": str(exc)})
            return json_error(str(exc))
        log_event("info", "explorer", "move_favorite", "Processed explorer JSON move target favorite request.", context={"created": favorite["created"], "path": favorite["path"]})
        return json_success(favorite=favorite, move_favorites=move_favorite_options())

    @app.post("/api/explorer/archive-settings")
    def api_explorer_archive_settings():
        payload = json_request_payload()
        try:
            settings = persist_archive_automation_settings(
                auto_sort_enabled=bool_payload_value(payload, "archive_auto_sort_enabled"),
                auto_delete_enabled=bool_payload_value(payload, "archive_auto_delete_enabled"),
            )
        except ValueError as exc:
            log_event("error", "archive", "settings", "Explorer JSON archive automation settings update failed.", context={"error": str(exc)})
            return json_error(str(exc))
        log_event("info", "archive", "settings", "Saved explorer JSON archive automation defaults.", context=settings)
        return json_success(archive_automation_settings=settings)

    @app.post("/api/explorer/extract")
    def api_explorer_extract():
        payload = json_request_payload()
        root_key = str(payload.get("root", ""))
        current_path = str(payload.get("current_path", payload.get("path", "")))
        sort_by = str(payload.get("sort", "name"))
        sort_order = str(payload.get("order", "asc"))
        selected_paths = selected_paths_from_payload(payload)
        password = str(payload.get("password") or "") or None
        try:
            archive_automation_settings = persist_archive_automation_settings(
                auto_sort_enabled=bool_payload_value(payload, "archive_auto_sort_enabled"),
                auto_delete_enabled=bool_payload_value(payload, "archive_auto_delete_enabled"),
            )
            result = extract_archives_in_folder(
                root_key,
                current_path,
                selected_paths,
                password=password,
                skip_non_archive=True,
                auto_sort_enabled=archive_automation_settings["auto_sort_enabled"],
                auto_delete_enabled=archive_automation_settings["auto_delete_enabled"],
            )
            explorer_payload = build_explorer_payload(root_key, current_path, sort_by, sort_order)
        except (ValueError, FileNotFoundError, ArchiveError) as exc:
            log_event("error", "archive", "queue", "Explorer JSON archive extraction queue request failed.", context={"root": root_key, "path": current_path, "error": str(exc)})
            return json_error(str(exc))
        log_event("info", "archive", "queue", "Processed explorer JSON archive extraction queue request.", context={"root": root_key, "path": current_path, "queued": len(result["queued"]), "skipped": len(result["skipped"]), "failures": len(result["failures"])})
        return json_success(result=result, **explorer_payload)

    @app.post("/api/explorer/compile-bluray")
    def api_explorer_compile_bluray():
        payload = json_request_payload()
        root_key = str(payload.get("root", ""))
        current_path = str(payload.get("current_path", payload.get("path", "")))
        sort_by = str(payload.get("sort", "name"))
        sort_order = str(payload.get("order", "asc"))
        selected_paths = selected_paths_from_payload(payload)
        destination_key = str(payload.get("destination", ""))
        destination_subpath = normalize_destination_path_input(str(payload.get("destination_path", "")))

        backend = media_manager.backend_payload()
        if not backend["available"]:
            return json_error(backend["reason"] or "Blu-ray remux backend is unavailable.")

        try:
            _, _, resolved_entries = resolve_entries_in_directory(
                manager.destinations,
                root_key,
                current_path,
                selected_paths,
            )
            output_destination_path, output_destination_relative_path, output_destination_is_custom = manager.resolve_destination(
                destination_key,
                destination_subpath,
            )
            ensure_destination_writable(output_destination_path)
        except (ValueError, FileNotFoundError) as exc:
            log_event("error", "bluray", "queue", "Explorer JSON Blu-ray selection failed.", context={"root": root_key, "path": current_path, "error": str(exc)})
            return json_error(str(exc))

        valid_sources = []
        skipped: list[str] = []
        for relative_path, entry_path in resolved_entries:
            source = detect_bluray_source(entry_path, relative_path)
            if source is None:
                skipped.append(entry_path.name)
                continue
            valid_sources.append(source)

        if not valid_sources:
            return json_error("Select one or more Blu-ray folders that contain BDMV/index.bdmv.")

        try:
            jobs = media_manager.submit(
                valid_sources,
                source_root_key=root_key,
                output_destination_key=destination_key,
                output_destination_path=output_destination_path,
                output_destination_relative_path=output_destination_relative_path,
                output_destination_is_custom=output_destination_is_custom,
            )
            explorer_payload = build_explorer_payload(root_key, current_path, sort_by, sort_order)
        except ValueError as exc:
            log_event("error", "bluray", "queue", "Explorer JSON Blu-ray queue request failed.", context={"error": str(exc)})
            return json_error(str(exc))

        result = {"queued": len(jobs), "skipped": skipped}
        log_event("info", "bluray", "queue", "Queued explorer JSON Blu-ray remux jobs.", batch_id=jobs[0].batch_id if jobs else None, context={"queued": len(jobs), "skipped": len(skipped), "destination_key": destination_key})
        return json_success(result=result, **explorer_payload)

    @app.post("/explorer/delete")
    def explorer_delete():
        root_key = request.form.get("root", "")
        current_path = request.form.get("current_path", "")
        sort_by = request.form.get("sort", "name")
        selected_paths = request.form.getlist("selected_paths")

        try:
            result = delete_entries(manager.destinations, root_key, current_path, selected_paths)
        except (ValueError, FileNotFoundError) as exc:
            flash(str(exc), "error")
            log_event("error", "explorer", "delete", "Explorer delete failed.", context={"root": root_key, "path": current_path, "error": str(exc)})
            return explorer_redirect(root_key, current_path, sort_by)

        if result["deleted"]:
            flash(f"Deleted {len(result['deleted'])} item(s): {summarize_items([Path(item).name for item in result['deleted']])}.", "success")
        if result["failures"]:
            flash(
                f"Failed to delete {len(result['failures'])} item(s): {summarize_items(result['failures'])}.",
                "error",
            )
        if not result["deleted"] and not result["failures"]:
            flash("No items were deleted.", "error")
        log_event(
            "info",
            "explorer",
            "delete",
            "Processed explorer delete request.",
            context={"root": root_key, "path": current_path, "deleted": len(result["deleted"]), "failures": len(result["failures"])},
        )
        return explorer_redirect(root_key, current_path, sort_by)

    @app.post("/explorer/rename")
    def explorer_rename():
        root_key = request.form.get("root", "")
        current_path = request.form.get("current_path", "")
        sort_by = request.form.get("sort", "name")
        relative_path = request.form.get("entry_path", "")
        new_name = request.form.get("new_name", "")

        try:
            result = rename_entry(manager.destinations, root_key, current_path, relative_path, new_name)
        except (ValueError, FileNotFoundError) as exc:
            flash(str(exc), "error")
            log_event("error", "explorer", "rename", "Explorer rename failed.", context={"root": root_key, "path": current_path, "entry_path": relative_path, "error": str(exc)})
            return explorer_redirect(root_key, current_path, sort_by)

        if result["renamed"]:
            flash(f"Renamed item to {result['name']}.", "success")
        else:
            flash(f"Name already matches {result['name']}.", "success")
        log_event(
            "info",
            "explorer",
            "rename",
            "Processed explorer rename request.",
            context={"root": root_key, "path": current_path, "renamed": result["renamed"], "name": result["name"]},
        )
        return explorer_redirect(root_key, current_path, sort_by)

    @app.post("/explorer/extract")
    def explorer_extract():
        root_key = request.form.get("root", "")
        current_path = request.form.get("current_path", "")
        sort_by = request.form.get("sort", "name")
        selected_paths = request.form.getlist("selected_paths")
        password = request.form.get("password") or None
        try:
            archive_automation_settings = persist_archive_automation_settings_from_form()
        except ValueError as exc:
            flash(str(exc), "error")
            log_event("error", "archive", "settings", "Archive automation settings update failed from explorer.", context={"root": root_key, "path": current_path, "error": str(exc)})
            return explorer_redirect(root_key, current_path, sort_by)

        try:
            result = extract_archives_in_folder(
                root_key,
                current_path,
                selected_paths,
                password=password,
                skip_non_archive=True,
                auto_sort_enabled=archive_automation_settings["auto_sort_enabled"],
                auto_delete_enabled=archive_automation_settings["auto_delete_enabled"],
            )
        except (ValueError, FileNotFoundError, ArchiveError) as exc:
            flash(str(exc), "error")
            log_event("error", "archive", "queue", "Archive extraction queue request failed.", context={"root": root_key, "path": current_path, "error": str(exc)})
            return explorer_redirect(root_key, current_path, sort_by)

        if result["queued"]:
            extracted_labels = [f"{item['archive']} -> {item['target']}" for item in result["queued"]]
            flash(
                f"Queued {len(result['queued'])} archive extraction job(s): "
                f"{summarize_items(extracted_labels)}.",
                "success",
            )
        if result["skipped"]:
            flash(
                f"Skipped {len(result['skipped'])} non-archive item(s): {summarize_items(result['skipped'])}.",
                "success",
            )
        if result["failures"]:
            flash(
                f"Failed to queue {len(result['failures'])} archive(s): {summarize_items(result['failures'])}.",
                "error",
            )
        if not result["queued"] and not result["skipped"] and not result["failures"]:
            flash("No archive extraction jobs were queued.", "error")
        elif archive_automation_settings["auto_sort_enabled"] and result["queued"]:
            flash("Auto-sort will move extracted videos into the saved Movies or TvShows favorites after extraction finishes.", "success")
            if archive_automation_settings["auto_delete_enabled"]:
                flash("Auto-delete will remove the source archive files after a successful auto-sort move.", "success")
        log_event(
            "info",
            "archive",
            "queue",
            "Processed archive extraction queue request.",
            context={
                "root": root_key,
                "path": current_path,
                "queued": len(result["queued"]),
                "skipped": len(result["skipped"]),
                "failures": len(result["failures"]),
                "auto_sort_enabled": archive_automation_settings["auto_sort_enabled"],
                "auto_delete_enabled": archive_automation_settings["auto_delete_enabled"],
            },
        )
        return explorer_redirect(root_key, current_path, sort_by)

    @app.post("/explorer/move-favorites")
    def explorer_move_favorites():
        root_key = request.form.get("root", "")
        current_path = request.form.get("current_path", "")
        sort_by = request.form.get("sort", "name")
        move_target = normalize_move_target_input(request.form.get("move_target", ""))
        if not move_target:
            flash("Enter a move target path before saving it.", "error")
            log_event("warning", "explorer", "move_favorite", "Move target favorite add rejected because the path was empty.")
            return explorer_redirect(root_key, current_path, sort_by)

        try:
            favorite = save_move_favorite(root_key, current_path, move_target)
        except ValueError as exc:
            flash(str(exc), "error")
            log_event("error", "explorer", "move_favorite", "Move target favorite add failed.", context={"root": root_key, "path": current_path, "error": str(exc)})
            return explorer_redirect(root_key, current_path, sort_by)

        if favorite["created"]:
            flash(f"Added move target favorite: {favorite['path']}", "success")
        else:
            flash(f"Move target already exists in saved targets: {favorite['path']}", "success")
        log_event(
            "info",
            "explorer",
            "move_favorite",
            "Processed move target favorite request.",
            context={"created": favorite["created"], "path": favorite["path"]},
        )
        return explorer_redirect(root_key, current_path, sort_by)

    @app.post("/explorer/move")
    def explorer_move():
        root_key = request.form.get("root", "")
        current_path = request.form.get("current_path", "")
        sort_by = request.form.get("sort", "name")
        selected_paths = request.form.getlist("selected_paths")
        move_target = normalize_move_target_input(request.form.get("move_target", ""))
        replace_existing = request.form.get("replace_existing") == "1"

        try:
            preview = preview_move_entries(
                manager.destinations,
                root_key,
                current_path,
                selected_paths,
                move_target,
            )
        except (ValueError, FileNotFoundError) as exc:
            flash(str(exc), "error")
            log_event("error", "explorer", "move", "Explorer move preview failed.", context={"root": root_key, "path": current_path, "error": str(exc)})
            return explorer_redirect(root_key, current_path, sort_by)

        if preview["conflicts"] and not replace_existing:
            log_event(
                "warning",
                "explorer",
                "move_preview",
                "Explorer move requires replace confirmation.",
                context={"target_dir": str(preview["target_dir"]), "conflicts": preview["conflicts"]},
            )
            return render_explorer_page(
                root_key,
                current_path,
                sort_by,
                move_confirmation={
                    "target_input": move_target,
                    "target_path": str(preview["target_dir"]),
                    "selected_paths": selected_paths,
                    "conflicts": preview["conflicts"],
                },
            )

        try:
            ensure_destination_writable(preview["target_dir"])
            result = move_entries(
                manager.destinations,
                root_key,
                current_path,
                selected_paths,
                move_target,
                replace_existing=replace_existing,
                permission_manager=permission_manager,
            )
        except (ValueError, FileNotFoundError) as exc:
            flash(str(exc), "error")
            log_event("error", "explorer", "move", "Explorer move failed.", context={"root": root_key, "path": current_path, "error": str(exc)})
            return explorer_redirect(root_key, current_path, sort_by)

        if result["moved"]:
            flash(
                f"Moved {len(result['moved'])} item(s) to {result['target_dir']}: {summarize_items(result['moved'])}.",
                "success",
            )
        if result["replaced"]:
            flash(
                f"Replaced and moved {len(result['replaced'])} item(s): {summarize_items(result['replaced'])}.",
                "success",
            )
        if result["failures"]:
            flash(
                f"Failed to move {len(result['failures'])} item(s): {summarize_items(result['failures'])}.",
                "error",
            )
        if not result["moved"] and not result["replaced"] and not result["failures"]:
            flash("No items were moved.", "error")
        log_event(
            "info",
            "explorer",
            "move",
            "Processed explorer move request.",
            context={
                "target_dir": str(result["target_dir"]),
                "moved": len(result["moved"]),
                "replaced": len(result["replaced"]),
                "failures": len(result["failures"]),
            },
        )
        return explorer_redirect(root_key, current_path, sort_by)

    @app.post("/explorer/compile-bluray")
    def explorer_compile_bluray():
        root_key = request.form.get("root", "")
        current_path = request.form.get("current_path", "")
        sort_by = request.form.get("sort", "name")
        selected_paths = request.form.getlist("selected_paths")
        destination_key = request.form.get("destination", "")
        destination_subpath = normalize_destination_path_input(request.form.get("destination_path", ""))

        backend = media_manager.backend_payload()
        if not backend["available"]:
            flash(backend["reason"] or "Blu-ray remux backend is unavailable.", "error")
            log_event("warning", "bluray", "queue", backend["reason"] or "Blu-ray remux backend is unavailable.")
            return explorer_redirect(root_key, current_path, sort_by)

        try:
            _, _, resolved_entries = resolve_entries_in_directory(
                manager.destinations,
                root_key,
                current_path,
                selected_paths,
            )
        except (ValueError, FileNotFoundError) as exc:
            flash(str(exc), "error")
            log_event("error", "bluray", "queue", "Blu-ray source selection failed.", context={"root": root_key, "path": current_path, "error": str(exc)})
            return explorer_redirect(root_key, current_path, sort_by)

        try:
            output_destination_path, output_destination_relative_path, output_destination_is_custom = manager.resolve_destination(
                destination_key,
                destination_subpath,
            )
            ensure_destination_writable(output_destination_path)
        except ValueError as exc:
            flash(str(exc), "error")
            log_event("error", "bluray", "queue", "Blu-ray destination resolution failed.", context={"error": str(exc), "destination_key": destination_key})
            return explorer_redirect(root_key, current_path, sort_by)

        valid_sources = []
        skipped: list[str] = []
        for relative_path, entry_path in resolved_entries:
            source = detect_bluray_source(entry_path, relative_path)
            if source is None:
                skipped.append(entry_path.name)
                continue
            valid_sources.append(source)

        if not valid_sources:
            flash("Select one or more Blu-ray folders that contain BDMV/index.bdmv.", "error")
            log_event("warning", "bluray", "queue", "Blu-ray queue request rejected because no valid sources were selected.")
            return explorer_redirect(root_key, current_path, sort_by)

        try:
            jobs = media_manager.submit(
                valid_sources,
                source_root_key=root_key,
                output_destination_key=destination_key,
                output_destination_path=output_destination_path,
                output_destination_relative_path=output_destination_relative_path,
                output_destination_is_custom=output_destination_is_custom,
            )
        except ValueError as exc:
            flash(str(exc), "error")
            log_event("error", "bluray", "queue", "Blu-ray queue request failed.", context={"error": str(exc)})
            return explorer_redirect(root_key, current_path, sort_by)

        flash(f"Queued {len(jobs)} Blu-ray remux job(s).", "success")
        if skipped:
            flash(
                f"Skipped {len(skipped)} item(s) that are not valid Blu-ray folders: {summarize_items(skipped)}.",
                "success",
            )
        log_event(
            "info",
            "bluray",
            "queue",
            "Queued Blu-ray remux jobs.",
            batch_id=jobs[0].batch_id if jobs else None,
            context={"queued": len(jobs), "skipped": len(skipped), "destination_key": destination_key},
        )
        return explorer_redirect(root_key, current_path, sort_by)

    @app.post("/unzip")
    def unzip():
        root_key = request.form.get("root", "")
        archive_rel = request.form.get("archive_path", "")
        password = request.form.get("password") or None
        target_dir_name = (request.form.get("target_dir") or "").strip()
        sort_by = request.form.get("sort", "name")
        archive_parent = str(Path(archive_rel).parent).replace("\\", "/")
        archive_parent = "" if archive_parent == "." else archive_parent

        try:
            result = extract_archives_in_folder(
                root_key,
                archive_parent,
                [archive_rel],
                password=password,
                target_dir_name=target_dir_name or None,
                skip_non_archive=False,
            )
            if result["queued"]:
                queued = result["queued"][0]
                flash(f"Queued extraction from {queued['archive']} to {queued['target']}.", "success")
            if result["failures"]:
                flash(result["failures"][0], "error")
        except (ValueError, FileNotFoundError, ArchiveError) as exc:
            flash(str(exc), "error")
            log_event("error", "archive", "queue", "Single-archive extraction queue request failed.", context={"root": root_key, "archive_path": archive_rel, "error": str(exc)})
        else:
            log_event(
                "info",
                "archive",
                "queue",
                "Processed single-archive extraction queue request.",
                context={"root": root_key, "archive_path": archive_rel, "queued": len(result["queued"]), "failures": len(result["failures"])},
            )
        return explorer_redirect(root_key, archive_parent, sort_by)

    return app


if __name__ == "__main__":
    application = create_app()
    application.run(host=application.config["HOST"], port=application.config["PORT"], debug=False)
