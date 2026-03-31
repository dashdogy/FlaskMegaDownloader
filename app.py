from __future__ import annotations

import atexit
import hashlib
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from flask import Flask, flash, jsonify, redirect, render_template, request, url_for

import config as default_config
from archive_extract_manager import ArchiveExtractManager
from archive_auto_sort import guessit_available
from archives import (
    ArchiveError,
    archive_type_for_path,
    default_archive_target_name,
    is_supported_archive_path,
)
from downloader import DownloadManager, ensure_destination_writable
from explorer import (
    delete_entries,
    list_directory,
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
from models import ACTIVE_ARCHIVE_JOB_STATUSES, MoveFavorite
from storage import JsonStorage


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


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(default_config)

    extra_config = os.environ.get("MEGA_DOWNLOADER_CONFIG")
    if extra_config:
        app.config.from_pyfile(extra_config)

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
    manager = DownloadManager(
        storage=storage,
        destinations=app.config["ALLOWED_DESTINATIONS"],
        megacmd_binary=app.config["MEGACMD_BINARY"],
        backend=app.config["DOWNLOADER_BACKEND"],
    )
    media_manager = MediaCompileManager(
        storage=storage,
        makemkvcon_binary=app.config["MAKEMKVCON_BINARY"],
        mediainfo_binary=app.config["MEDIAINFO_BINARY"],
        bluray_min_title_seconds=app.config["BLURAY_MIN_TITLE_SECONDS"],
    )
    archive_manager = ArchiveExtractManager(
        storage=storage,
        seven_zip_binary=app.config["SEVEN_ZIP_BINARY"],
    )
    app.extensions["download_manager"] = manager
    app.extensions["media_compile_manager"] = media_manager
    app.extensions["archive_extract_manager"] = archive_manager
    atexit.register(manager.stop)
    atexit.register(media_manager.stop)
    atexit.register(archive_manager.stop)

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

        try:
            payload = list_directory(manager.destinations, root_key, current_path, sort_by)
        except (ValueError, FileNotFoundError) as exc:
            flash(str(exc), "error")
            fallback_root = destination_options[0]["key"]
            return redirect(url_for("explorer", root=fallback_root))

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

        return render_template(
            "explorer.html",
            explorer=payload,
            move_favorites=move_favorite_options(),
            move_confirmation=move_confirmation,
            media_backend=media_manager.backend_payload(),
            explorer_archive_jobs=explorer_archive_jobs,
            explorer_archive_summary=explorer_archive_summary,
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
            "destinations": manager.destination_options(),
            "has_destinations": manager.has_destinations(),
            "can_restore_base_destinations": manager.can_restore_base_destinations(),
            "queue_sort_options": manager.queue_sort_options(),
            "current_queue_sort": manager.queue_sort_mode,
        }

    @app.get("/")
    def dashboard():
        return render_template("index.html", dashboard=dashboard_payload())

    @app.post("/submit")
    def submit():
        urls = parse_urls(request.form.get("urls", ""))
        destination_key = request.form.get("destination", "")
        destination_subpath = normalize_destination_path_input(request.form.get("destination_path", ""))
        if not urls:
            flash("Paste at least one MEGA or Filecrypt URL.", "error")
            return redirect(url_for("dashboard"))

        try:
            urls, resolution_summary, metadata_overrides = expand_submission_urls_with_metadata(urls)
        except FilecryptResolutionError as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))

        try:
            jobs = manager.submit(
                urls,
                destination_key,
                destination_subpath,
                metadata_overrides=metadata_overrides,
            )
        except ValueError as exc:
            flash(str(exc), "error")
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
        flash(f"Queued {len(jobs)} job(s) in batch {jobs[0].batch_id}.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/favorites")
    def add_favorite():
        destination_key = request.form.get("destination", "")
        destination_input = normalize_destination_path_input(request.form.get("destination_path", ""))
        if not destination_input:
            flash("Enter a custom destination path before adding it to favorites.", "error")
            return post_context_redirect()

        try:
            favorite = manager.add_favorite_destination(destination_key, destination_input)
        except ValueError as exc:
            flash(str(exc), "error")
            return post_context_redirect()

        if favorite["created"]:
            flash(f"Added favorite destination: {favorite['path']}", "success")
        else:
            flash(f"Destination already exists in the dropdown: {favorite['path']}", "success")
        return post_context_redirect()

    @app.post("/destinations/<destination_key>/delete")
    def delete_destination(destination_key: str):
        try:
            deleted = manager.delete_destination(
                destination_key,
                extra_in_use=media_manager.destination_in_use(destination_key),
            )
            flash(f"Deleted {deleted['type']} destination: {deleted['label']}.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect_back_or("dashboard")

    @app.post("/destinations/restore")
    def restore_destinations():
        restored = manager.restore_hidden_base_destinations()
        if restored:
            flash(f"Restored {restored} configured destination(s).", "success")
        else:
            flash("There were no hidden configured destinations to restore.", "success")
        return redirect_back_or("dashboard")

    @app.get("/api/jobs")
    def api_jobs():
        return jsonify(dashboard_payload())

    @app.post("/jobs/<job_id>/cancel")
    def cancel_job(job_id: str):
        try:
            manager.cancel_job(job_id)
            flash("Cancel request sent.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect_back_or("dashboard")

    @app.post("/jobs/<job_id>/retry")
    def retry_job(job_id: str):
        try:
            manager.retry_job(job_id)
            flash("Job re-queued.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect_back_or("dashboard")

    @app.post("/media-jobs/<job_id>/cancel")
    def cancel_media_job(job_id: str):
        try:
            media_manager.cancel_job(job_id)
            flash("Blu-ray cancel request sent.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect_back_or("dashboard")

    @app.post("/media-jobs/<job_id>/retry")
    def retry_media_job(job_id: str):
        try:
            media_manager.retry_job(job_id)
            flash("Blu-ray job re-queued.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect_back_or("dashboard")

    @app.post("/archive-jobs/<job_id>/cancel")
    def cancel_archive_job(job_id: str):
        try:
            archive_manager.cancel_job(job_id)
            flash("Archive cancel request sent.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
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
        return post_context_redirect("dashboard")

    @app.post("/jobs/<job_id>/pause")
    def pause_job(job_id: str):
        try:
            manager.pause_job(job_id)
            flash("Pause request sent.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect_back_or("dashboard")

    @app.post("/jobs/<job_id>/resume")
    def resume_job(job_id: str):
        try:
            manager.resume_job(job_id)
            flash("Resume request sent.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
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
        return redirect_back_or("dashboard")

    @app.post("/jobs/toggle-all")
    def toggle_all_jobs():
        toggle = manager.bulk_pause_toggle()
        if not toggle["available"]:
            flash("There were no queued, active, or paused jobs to change.", "success")
            return redirect_back_or("dashboard")

        if toggle["action"] == "pause":
            result = manager.pause_all()
            flash(f"Paused {result['paused']} queued or active job(s).", "success")
        else:
            result = manager.resume_all()
            flash(f"Resumed {result['resumed']} paused job(s).", "success")
        return redirect_back_or("dashboard")

    @app.post("/jobs/sort")
    def sort_jobs():
        sort_by = request.form.get("sort_by", "")
        try:
            result = manager.sort_queue(sort_by)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect_back_or("dashboard")

        if result["sorted"]:
            flash(f"Sorted {result['sorted']} queued job(s) by {result['label']}.", "success")
        else:
            flash("There were no queued jobs to sort.", "success")
        return redirect_back_or("dashboard")

    @app.get("/explorer")
    def explorer():
        destination_options = manager.destination_options()
        if not destination_options:
            flash("No destinations are configured. Restore or add one before opening the explorer.", "error")
            return redirect(url_for("dashboard"))
        requested_root = request.args.get("root") or destination_options[0]["key"]
        requested_path = request.args.get("path", "")
        sort_by = request.args.get("sort", "name")
        return render_explorer_page(requested_root, requested_path, sort_by)

    @app.get("/api/explorer")
    def api_explorer():
        destination_options = manager.destination_options()
        if not destination_options:
            return jsonify({"error": "No destinations are configured."}), 400
        requested_root = request.args.get("root") or destination_options[0]["key"]
        requested_path = request.args.get("path", "")
        sort_by = request.args.get("sort", "name")
        try:
            payload = list_directory(manager.destinations, requested_root, requested_path, sort_by)
        except (ValueError, FileNotFoundError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(payload)

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
            return explorer_redirect(root_key, current_path, sort_by)

        if result["renamed"]:
            flash(f"Renamed item to {result['name']}.", "success")
        else:
            flash(f"Name already matches {result['name']}.", "success")
        return explorer_redirect(root_key, current_path, sort_by)

    @app.post("/explorer/extract")
    def explorer_extract():
        root_key = request.form.get("root", "")
        current_path = request.form.get("current_path", "")
        sort_by = request.form.get("sort", "name")
        selected_paths = request.form.getlist("selected_paths")
        password = request.form.get("password") or None
        auto_sort_enabled = request.form.get("auto_sort_extracted_videos") == "1"

        try:
            result = extract_archives_in_folder(
                root_key,
                current_path,
                selected_paths,
                password=password,
                skip_non_archive=True,
                auto_sort_enabled=auto_sort_enabled,
            )
        except (ValueError, FileNotFoundError, ArchiveError) as exc:
            flash(str(exc), "error")
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
        elif auto_sort_enabled and result["queued"]:
            flash("Auto-sort will move extracted videos into the saved Movies or TvShows favorites after extraction finishes.", "success")
        return explorer_redirect(root_key, current_path, sort_by)

    @app.post("/explorer/move-favorites")
    def explorer_move_favorites():
        root_key = request.form.get("root", "")
        current_path = request.form.get("current_path", "")
        sort_by = request.form.get("sort", "name")
        move_target = normalize_move_target_input(request.form.get("move_target", ""))
        if not move_target:
            flash("Enter a move target path before saving it.", "error")
            return explorer_redirect(root_key, current_path, sort_by)

        try:
            favorite = save_move_favorite(root_key, current_path, move_target)
        except ValueError as exc:
            flash(str(exc), "error")
            return explorer_redirect(root_key, current_path, sort_by)

        if favorite["created"]:
            flash(f"Added move target favorite: {favorite['path']}", "success")
        else:
            flash(f"Move target already exists in saved targets: {favorite['path']}", "success")
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
            return explorer_redirect(root_key, current_path, sort_by)

        if preview["conflicts"] and not replace_existing:
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
            )
        except (ValueError, FileNotFoundError) as exc:
            flash(str(exc), "error")
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
            return explorer_redirect(root_key, current_path, sort_by)

        try:
            output_destination_path, output_destination_relative_path, output_destination_is_custom = manager.resolve_destination(
                destination_key,
                destination_subpath,
            )
            ensure_destination_writable(output_destination_path)
        except ValueError as exc:
            flash(str(exc), "error")
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
            return explorer_redirect(root_key, current_path, sort_by)

        flash(f"Queued {len(jobs)} Blu-ray remux job(s).", "success")
        if skipped:
            flash(
                f"Skipped {len(skipped)} item(s) that are not valid Blu-ray folders: {summarize_items(skipped)}.",
                "success",
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
        return explorer_redirect(root_key, archive_parent, sort_by)

    return app


if __name__ == "__main__":
    application = create_app()
    application.run(host=application.config["HOST"], port=application.config["PORT"], debug=False)
