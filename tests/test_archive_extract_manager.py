from __future__ import annotations

import os
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import app as app_module
from archive_extract_manager import ArchiveExtractManager
from archives import ArchiveError, ArchiveProbe
from storage import SQLiteStorage


def wait_for(predicate, *, timeout: float = 3.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class FakeArchiveExtractManager:
    instance: "FakeArchiveExtractManager | None" = None

    def __init__(self, storage, *, seven_zip_binary: str = "7z"):
        self.storage = storage
        self.seven_zip_binary = seven_zip_binary
        self.submitted: list[list[dict]] = []
        FakeArchiveExtractManager.instance = self

    def stop(self) -> None:
        return None

    def submit(self, prepared_jobs: list[dict]):
        self.submitted.append(prepared_jobs)
        return [
            SimpleNamespace(
                archive_display_name=item["archive_display_name"],
                archive_type=item["archive_type"],
                target_path=item["target_path"],
            )
            for item in prepared_jobs
        ]

    def dashboard_payload(self) -> dict:
        return {
            "summary": {
                "total_jobs": 0,
                "queued_jobs": 0,
                "active_jobs": 0,
                "completed_jobs": 0,
                "failed_jobs": 0,
                "throughput_bps": 0.0,
                "bytes_done": 0,
                "bytes_total": 0,
                "has_unknown_total": False,
            },
            "jobs": [],
            "updated_at": "",
        }


class ArchiveExtractManagerTests(unittest.TestCase):
    def test_worker_enters_probing_before_extract_and_uses_password(self) -> None:
        with TemporaryDirectory(prefix="archive-manager-", ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            storage = SQLiteStorage(root / "state.sqlite3")
            manager = ArchiveExtractManager(storage, seven_zip_binary="7z-test")
            archive_path = root / "sample.zip"
            archive_path.write_bytes(b"zip")
            target_path = root / "output"

            probe_started = threading.Event()
            release_probe = threading.Event()
            observed: dict[str, tuple] = {}

            def fake_probe(path: Path, *, password: str | None = None, seven_zip_binary: str = "7z") -> ArchiveProbe:
                observed["probe"] = (path, password, seven_zip_binary)
                probe_started.set()
                release_probe.wait(2.0)
                return ArchiveProbe(archive_type="zip", bytes_total=4096, entry_count=1)

            def fake_extract(
                path: Path,
                destination: Path,
                password: str | None = None,
                *,
                seven_zip_binary: str = "7z",
                progress_callback=None,
            ) -> list[str]:
                observed["extract"] = (path, destination, password, seven_zip_binary)
                if progress_callback:
                    progress_callback(bytes_done=1024, bytes_total=4096)
                    progress_callback(bytes_done=4096, bytes_total=4096)
                return [str(destination / "file.txt")]

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
                                "archive_password": "secret123",
                            }
                        ]
                    )
                    job_id = jobs[0].id

                    self.assertTrue(probe_started.wait(1.0))
                    self.assertTrue(wait_for(lambda: manager._jobs[job_id].status == "probing"))
                    payload = manager.dashboard_payload()
                    self.assertEqual(payload["summary"]["active_jobs"], 1)
                    self.assertEqual(payload["jobs"][0]["status"], "probing")
                    self.assertEqual(payload["jobs"][0]["transfer"]["last_message"], "Inspecting archive...")

                    release_probe.set()
                    self.assertTrue(wait_for(lambda: manager._jobs[job_id].status == "completed"))

                job = manager._jobs[job_id]
                self.assertEqual(observed["probe"], (archive_path, "secret123", "7z-test"))
                self.assertEqual(observed["extract"], (archive_path, target_path, "secret123", "7z-test"))
                self.assertEqual(job.transfer.bytes_total, 4096)
                self.assertEqual(job.transfer.bytes_done, 4096)
                self.assertIsNone(job.archive_password)
            finally:
                manager.stop()
                manager._worker.join(timeout=1.0)

    def test_probe_failure_fails_job_asynchronously(self) -> None:
        with TemporaryDirectory(prefix="archive-manager-fail-", ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            storage = SQLiteStorage(root / "state.sqlite3")
            manager = ArchiveExtractManager(storage)
            archive_path = root / "broken.zip"
            archive_path.write_bytes(b"broken")

            try:
                with patch("archive_extract_manager.probe_archive", side_effect=ArchiveError("Bad password or corrupt archive.")):
                    jobs = manager.submit(
                        [
                            {
                                "root_key": "downloads",
                                "archive_relative_path": "broken.zip",
                                "archive_path": str(archive_path),
                                "archive_display_name": "broken.zip",
                                "archive_type": "zip",
                                "target_relative_path": "broken",
                                "target_path": str(root / "broken"),
                                "archive_password": "secret123",
                            }
                        ]
                    )
                    job_id = jobs[0].id
                    self.assertTrue(wait_for(lambda: manager._jobs[job_id].status == "failed"))

                job = manager._jobs[job_id]
                self.assertEqual(job.status, "failed")
                self.assertIn("Bad password or corrupt archive.", job.error or "")
                self.assertIsNone(job.archive_password)
            finally:
                manager.stop()
                manager._worker.join(timeout=1.0)


class ArchiveRequestPathTests(unittest.TestCase):
    def test_unzip_route_queues_without_sync_probe(self) -> None:
        with TemporaryDirectory(prefix="archive-route-", ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            downloads_dir = root / "downloads"
            downloads_dir.mkdir(parents=True, exist_ok=True)
            (downloads_dir / "sample.zip").write_bytes(b"zip")

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

            with (
                patch.dict(os.environ, {"MEGA_DOWNLOADER_CONFIG": str(config_path)}, clear=False),
                patch.object(app_module, "ArchiveExtractManager", FakeArchiveExtractManager),
            ):
                app = app_module.create_app()
                client = app.test_client()
                response = client.post(
                    "/unzip",
                    data={
                        "root": "downloads",
                        "archive_path": "sample.zip",
                        "password": "secret123",
                        "sort": "name",
                    },
                )

                self.assertEqual(response.status_code, 302)
                manager = FakeArchiveExtractManager.instance
                self.assertIsNotNone(manager)
                prepared_job = manager.submitted[0][0]
                self.assertEqual(prepared_job["archive_password"], "secret123")
                self.assertNotIn("bytes_total", prepared_job)

                app.extensions["download_manager"].stop()
                app.extensions["media_compile_manager"].stop()
                app.extensions["archive_extract_manager"].stop()
                app.extensions["download_manager"]._worker.join(timeout=1.0)
                app.extensions["media_compile_manager"]._worker.join(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
