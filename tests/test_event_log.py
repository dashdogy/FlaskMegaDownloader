from __future__ import annotations

import logging
import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import app as app_module
from archive_extract_manager import ArchiveExtractManager
from archives import ArchiveProbe
from event_log import EventLogService
from filecrypt_resolver import FilecryptResolutionSummary
from storage import SQLiteStorage


def wait_for(predicate, *, timeout: float = 3.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class EventLogServiceTests(unittest.TestCase):
    def test_retention_and_redaction(self) -> None:
        with TemporaryDirectory(prefix="event-log-service-", ignore_cleanup_errors=True) as temp_dir:
            storage = SQLiteStorage(Path(temp_dir) / "state.sqlite3")
            service = EventLogService(storage, max_rows=3)

            for index in range(5):
                service.info(
                    "download",
                    "submit",
                    f"Queued https://mega.nz/file/ABC{index}#KEY{index} password=secret{index}",
                    context={
                        "url": f"https://filecrypt.cc/Container/{index}.html",
                        "cookie": f"session{index}",
                    },
                )

            entries = storage.load_event_logs(limit=10)

        self.assertEqual(len(entries), 3)
        self.assertTrue(all("mega.nz" not in entry.message for entry in entries))
        self.assertTrue(all("secret" not in entry.message for entry in entries))
        self.assertTrue(all("filecrypt.cc" not in str(entry.context) for entry in entries))
        self.assertTrue(all("session" not in str(entry.context) for entry in entries))


class EventLogRouteTests(unittest.TestCase):
    def _create_app(self, root: Path):
        downloads_dir = root / "downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)
        config_path = root / "test_config.py"
        config_path.write_text(
            "\n".join(
                [
                    "from pathlib import Path",
                    f"BASE = Path(r'{root}')",
                    "SECRET_KEY = 'test-secret'",
                    "DATA_DIR = BASE / 'data'",
                    "JOB_STORAGE_FILE = DATA_DIR / 'jobs.json'",
                    "STATE_DB_FILE = DATA_DIR / 'state.sqlite3'",
                    "DOWNLOADER_BACKEND = 'fake'",
                    "SEVEN_ZIP_BINARY = '7z'",
                    "EVENT_LOG_MAX_ROWS = 5000",
                    "ALLOWED_DESTINATIONS = {",
                    "    'downloads': {",
                    "        'key': 'downloads',",
                    "        'label': 'Downloads',",
                    "        'path': BASE / 'downloads',",
                    "    }",
                    "}",
                ]
            ),
            encoding="utf-8",
        )
        env = patch.dict(os.environ, {"MEGA_DOWNLOADER_CONFIG": str(config_path)}, clear=False)
        env.start()
        app = app_module.create_app()
        self.addCleanup(env.stop)
        self.addCleanup(self._stop_app, app)
        return app

    def _stop_app(self, app):
        app.extensions["download_manager"].stop()
        app.extensions["media_compile_manager"].stop()
        app.extensions["archive_extract_manager"].stop()
        app.extensions["download_manager"]._worker.join(timeout=1.0)
        app.extensions["media_compile_manager"]._worker.join(timeout=1.0)
        app.extensions["archive_extract_manager"]._worker.join(timeout=1.0)

    def test_logs_page_and_incremental_api(self) -> None:
        with TemporaryDirectory(prefix="event-log-route-", ignore_cleanup_errors=True) as temp_dir:
            app = self._create_app(Path(temp_dir))
            client = app.test_client()
            event_logger = app.extensions["event_logger"]
            event_logger.info("app", "startup", "Initial log line.")
            event_logger.info("archive", "queued", "Queued archive extraction.")

            response = client.get("/logs")
            api_response = client.get("/api/logs")

            self.assertEqual(response.status_code, 200)
            self.assertIn("Live debug event stream", response.get_data(as_text=True))
            self.assertIn(">Logs<", response.get_data(as_text=True))

            payload = api_response.get_json()
            self.assertTrue(len(payload["entries"]) >= 2)
            last_id = payload["last_id"]

            event_logger.info("download", "completed", "Download finished.")
            incremental = client.get(f"/api/logs?after_id={last_id}")
            next_payload = incremental.get_json()
            self.assertEqual(len(next_payload["entries"]), 1)
            self.assertEqual(next_payload["entries"][0]["message"], "Download finished.")

    def test_submit_and_bridge_logging(self) -> None:
        with TemporaryDirectory(prefix="event-log-submit-", ignore_cleanup_errors=True) as temp_dir:
            app = self._create_app(Path(temp_dir))
            client = app.test_client()
            storage = app.extensions["download_manager"].storage

            with patch.object(
                app_module,
                "expand_submission_urls_with_metadata",
                return_value=(
                    ["https://mega.nz/file/FILEONE#KEYONE"],
                    FilecryptResolutionSummary(containers_resolved=1, mega_links_resolved=1),
                    {"https://mega.nz/file/FILEONE#KEYONE": {"display_name": "Episode 01.mkv", "bytes_total": 1024}},
                ),
            ):
                response = client.post(
                    "/submit",
                    data={
                        "urls": "https://filecrypt.cc/Container/ABC123.html",
                        "destination": "downloads",
                        "destination_path": "",
                    },
                )

            self.assertEqual(response.status_code, 302)
            entries = storage.load_event_logs(limit=100)
            messages = [entry.message for entry in entries]
            self.assertTrue(any("Processing download submission." == message for message in messages))
            self.assertTrue(any("Resolved Filecrypt container links into MEGA URLs." == message for message in messages))
            self.assertTrue(any("Queued download batch." == message for message in messages))

            logger = logging.getLogger("archive_extract_manager")
            try:
                raise RuntimeError("boom")
            except RuntimeError:
                logger.exception("Archive worker failed while handling job %s", "job-123")

            entries = storage.load_event_logs(limit=200)
            bridge_entries = [entry for entry in entries if entry.feature == "logger"]
            self.assertTrue(any("Archive worker failed while handling job job-123" in entry.message for entry in bridge_entries))


class ArchiveManagerEventLogTests(unittest.TestCase):
    def test_archive_manager_emits_completion_log(self) -> None:
        with TemporaryDirectory(prefix="event-log-archive-manager-", ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            storage = SQLiteStorage(root / "state.sqlite3")
            event_logger = EventLogService(storage, max_rows=5000)
            manager = ArchiveExtractManager(storage, seven_zip_binary="7z-test", event_logger=event_logger)
            archive_path = root / "sample.zip"
            archive_path.write_bytes(b"zip")
            target_path = root / "output"

            def fake_probe(path: Path, *, password: str | None = None, seven_zip_binary: str = "7z") -> ArchiveProbe:
                return ArchiveProbe(archive_type="zip", bytes_total=4096, entry_count=1)

            def fake_extract(
                path: Path,
                destination: Path,
                password: str | None = None,
                *,
                seven_zip_binary: str = "7z",
                progress_callback=None,
                cancel_requested=None,
            ) -> list[str]:
                destination.mkdir(parents=True, exist_ok=True)
                extracted_path = destination / "file.txt"
                extracted_path.write_text("payload", encoding="utf-8")
                if progress_callback:
                    progress_callback(bytes_done=4096, bytes_total=4096)
                return [str(extracted_path)]

            try:
                with (
                    patch("archive_extract_manager.probe_archive", side_effect=fake_probe),
                    patch("archive_extract_manager.extract_archive", side_effect=fake_extract),
                ):
                    jobs = manager.submit(
                        [
                            {
                                "root_key": "downloads",
                                "archive_relative_path": "sample.zip",
                                "archive_path": str(archive_path),
                                "archive_display_name": "sample.zip",
                                "archive_type": "zip",
                                "target_relative_path": "sample",
                                "target_path": str(target_path),
                            }
                        ]
                    )
                    self.assertTrue(wait_for(lambda: manager._jobs[jobs[0].id].status == "completed"))

                entries = storage.load_event_logs(limit=100)
                self.assertTrue(any(entry.feature == "completed" and entry.subsystem == "archive" for entry in entries))
            finally:
                manager.stop()
                manager._worker.join(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
