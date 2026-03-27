from __future__ import annotations

import atexit
import os
from datetime import datetime
from pathlib import Path

from flask import Flask, flash, jsonify, redirect, render_template, request, url_for

import config as default_config
from archives import ArchiveError, extract_archive
from downloader import DownloadManager
from explorer import list_directory, path_within_root
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
    cleaned = (raw_text or "").strip().replace("\\", "/")
    if not cleaned or cleaned == ".":
        return ""
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(default_config)

    extra_config = os.environ.get("MEGA_DOWNLOADER_CONFIG")
    if extra_config:
        app.config.from_pyfile(extra_config)

    app.secret_key = app.config["SECRET_KEY"]
    app.jinja_env.filters["filesize"] = format_bytes
    app.jinja_env.filters["datetime_local"] = format_datetime

    app.config["JOB_STORAGE_FILE"] = Path(app.config["JOB_STORAGE_FILE"]).expanduser().resolve()
    for destination in app.config["ALLOWED_DESTINATIONS"].values():
        destination["path"] = Path(destination["path"]).expanduser().resolve()
        destination["path"].mkdir(parents=True, exist_ok=True)
    app.config["JOB_STORAGE_FILE"].parent.mkdir(parents=True, exist_ok=True)

    storage = JsonStorage(app.config["JOB_STORAGE_FILE"])
    manager = DownloadManager(
        storage=storage,
        destinations=app.config["ALLOWED_DESTINATIONS"],
        megacmd_binary=app.config["MEGACMD_BINARY"],
        backend=app.config["DOWNLOADER_BACKEND"],
    )
    app.extensions["download_manager"] = manager
    atexit.register(manager.stop)

    @app.context_processor
    def inject_globals():
        return {
            "app_title": "Flask Mega Downloader",
            "poll_interval_ms": app.config["POLL_INTERVAL_MS"],
            "destinations": manager.destination_options(),
        }

    @app.get("/")
    def dashboard():
        return render_template("index.html", dashboard=manager.dashboard_payload())

    @app.post("/submit")
    def submit():
        urls = parse_urls(request.form.get("urls", ""))
        destination_key = request.form.get("destination", "")
        destination_subpath = normalize_destination_path_input(request.form.get("destination_path", ""))
        if not urls:
            flash("Paste at least one MEGA URL.", "error")
            return redirect(url_for("dashboard"))

        try:
            jobs = manager.submit(urls, destination_key, destination_subpath)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))

        flash(f"Queued {len(jobs)} job(s) in batch {jobs[0].batch_id}.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/favorites")
    def add_favorite():
        destination_key = request.form.get("destination", "")
        destination_input = normalize_destination_path_input(request.form.get("destination_path", ""))
        if not destination_input:
            flash("Enter a custom destination path before adding it to favorites.", "error")
            return redirect(url_for("dashboard"))

        try:
            favorite = manager.add_favorite_destination(destination_key, destination_input)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("dashboard"))

        if favorite["created"]:
            flash(f"Added favorite destination: {favorite['path']}", "success")
        else:
            flash(f"Destination already exists in the dropdown: {favorite['path']}", "success")
        return redirect(url_for("dashboard"))

    @app.get("/api/jobs")
    def api_jobs():
        return jsonify(manager.dashboard_payload())

    @app.post("/jobs/<job_id>/cancel")
    def cancel_job(job_id: str):
        try:
            manager.cancel_job(job_id)
            flash("Cancel request sent.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(request.referrer or url_for("dashboard"))

    @app.post("/jobs/<job_id>/retry")
    def retry_job(job_id: str):
        try:
            manager.retry_job(job_id)
            flash("Job re-queued.", "success")
        except ValueError as exc:
            flash(str(exc), "error")
        return redirect(request.referrer or url_for("dashboard"))

    @app.post("/jobs/clear")
    def clear_queue():
        result = manager.clear_queue()
        messages: list[str] = []
        if result["removed"]:
            messages.append(f"Removed {result['removed']} job(s) from the queue.")
        if result["canceling"]:
            messages.append(f"Canceling {result['canceling']} active job(s); they will disappear once cancellation finishes.")
        if not messages:
            messages.append("Queue was already empty.")
        flash(" ".join(messages), "success")
        return redirect(request.referrer or url_for("dashboard"))

    @app.get("/explorer")
    def explorer():
        destination_options = manager.destination_options()
        requested_root = request.args.get("root") or destination_options[0]["key"]
        requested_path = request.args.get("path", "")
        sort_by = request.args.get("sort", "name")
        try:
            payload = list_directory(manager.destinations, requested_root, requested_path, sort_by)
        except (ValueError, FileNotFoundError) as exc:
            flash(str(exc), "error")
            return redirect(url_for("explorer", root=destination_options[0]["key"]))
        return render_template("explorer.html", explorer=payload)

    @app.get("/api/explorer")
    def api_explorer():
        destination_options = manager.destination_options()
        requested_root = request.args.get("root") or destination_options[0]["key"]
        requested_path = request.args.get("path", "")
        sort_by = request.args.get("sort", "name")
        try:
            payload = list_directory(manager.destinations, requested_root, requested_path, sort_by)
        except (ValueError, FileNotFoundError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(payload)

    @app.post("/unzip")
    def unzip():
        root_key = request.form.get("root", "")
        archive_rel = request.form.get("archive_path", "")
        password = request.form.get("password") or None
        target_dir_name = (request.form.get("target_dir") or "").strip()
        archive_parent = str(Path(archive_rel).parent).replace("\\", "/")
        archive_parent = "" if archive_parent == "." else archive_parent

        try:
            root = manager.get_destination_path(root_key)
            archive_path = path_within_root(root, archive_rel)
            if archive_path.suffix.lower() != ".zip" or not archive_path.is_file():
                raise ArchiveError("Only zip files inside an allowed root can be extracted.")

            if target_dir_name:
                if "/" in target_dir_name or "\\" in target_dir_name or target_dir_name in {".", ".."}:
                    raise ArchiveError("Extraction folder must be a single folder name.")
                target_rel = str(Path(archive_rel).parent / target_dir_name)
            else:
                target_rel = str(Path(archive_rel).with_suffix(""))
            target_dir = path_within_root(root, target_rel)

            extracted = extract_archive(archive_path, target_dir, password=password)
            flash(f"Extracted {len(extracted)} file(s) to {target_dir.name}.", "success")
        except (ValueError, ArchiveError) as exc:
            flash(str(exc), "error")
        return redirect(url_for("explorer", root=root_key, path=archive_parent))

    return app


if __name__ == "__main__":
    application = create_app()
    application.run(host=application.config["HOST"], port=application.config["PORT"], debug=False)
