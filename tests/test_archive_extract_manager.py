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
from archive_auto_sort import ArchiveSortSummary
from archives import ArchiveCanceledError, ArchiveDeleteSummary, ArchiveError, ArchiveProbe
from models import MoveFavorite, utcnow_iso
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
    dashboard_jobs: list[dict] = []
    canceled_job_ids: list[str] = []
    clear_results: dict[str, int] = {"removed": 0, "canceling": 0}
    clear_calls: int = 0

    def __init__(self, storage, *, seven_zip_binary: str = "7z", event_logger=None):
        self.storage = storage
        self.seven_zip_binary = seven_zip_binary
        self.event_logger = event_logger
        self.submitted: list[list[dict]] = []
        FakeArchiveExtractManager.instance = self

    def stop(self) -> None:
        return None

    def cancel_job(self, job_id: str):
        FakeArchiveExtractManager.canceled_job_ids.append(job_id)
        return SimpleNamespace(id=job_id)

    def clear_queue(self) -> dict[str, int]:
        FakeArchiveExtractManager.clear_calls += 1
        return dict(FakeArchiveExtractManager.clear_results)

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
                "total_jobs": len(self.dashboard_jobs),
                "queued_jobs": sum(1 for job in self.dashboard_jobs if job["status"] == "queued"),
                "active_jobs": sum(1 for job in self.dashboard_jobs if job["status"] in {"probing", "extracting", "sorting", "cleaning"}),
                "completed_jobs": sum(1 for job in self.dashboard_jobs if job["status"] == "completed"),
                "failed_jobs": sum(1 for job in self.dashboard_jobs if job["status"] == "failed"),
                "canceled_jobs": sum(1 for job in self.dashboard_jobs if job["status"] == "canceled"),
                "throughput_bps": 0.0,
                "bytes_done": 0,
                "bytes_total": 0,
                "has_unknown_total": False,
            },
            "jobs": list(self.dashboard_jobs),
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
                cancel_requested=None,
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

    def test_cancel_queued_job_marks_it_canceled(self) -> None:
        with TemporaryDirectory(prefix="archive-manager-cancel-queued-", ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            storage = SQLiteStorage(root / "state.sqlite3")
            manager = ArchiveExtractManager(storage)
            archive_path = root / "queued.zip"
            archive_path.write_bytes(b"queued")

            try:
                manager.stop()
                manager._worker.join(timeout=1.0)
                jobs = manager.submit(
                    [
                        {
                            "root_key": "downloads",
                            "archive_relative_path": "queued.zip",
                            "archive_path": str(archive_path),
                            "archive_display_name": "queued.zip",
                            "archive_type": "zip",
                            "target_relative_path": "queued",
                            "target_path": str(root / "queued"),
                        }
                    ]
                )
                job = manager.cancel_job(jobs[0].id)
                self.assertEqual(job.status, "canceled")
                self.assertIn("Canceled before extraction started.", job.error or "")
            finally:
                manager.stop()
                manager._worker.join(timeout=1.0)

    def test_auto_sort_runs_after_extract_and_persists_summary(self) -> None:
        with TemporaryDirectory(prefix="archive-manager-sort-", ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            storage = SQLiteStorage(root / "state.sqlite3")
            manager = ArchiveExtractManager(storage, seven_zip_binary="7z-test")
            archive_path = root / "sample.zip"
            archive_path.write_bytes(b"zip")
            target_path = root / "output"
            extracted_video = target_path / "Show.Name.S01E02.mkv"
            movies_target = root / "Movies"
            tv_target = root / "TvShows"

            observed: dict[str, object] = {}
            sort_started = threading.Event()

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
                extracted_video.write_bytes(b"episode")
                if progress_callback:
                    progress_callback(bytes_done=4096, bytes_total=4096)
                return [str(extracted_video)]

            def fake_sort(
                extracted_paths,
                *,
                movies_target_path: Path,
                tv_target_path: Path,
                cancel_requested=None,
            ) -> ArchiveSortSummary:
                observed["sort_paths"] = list(extracted_paths)
                observed["movies_target_path"] = movies_target_path
                observed["tv_target_path"] = tv_target_path
                sort_started.set()
                return ArchiveSortSummary(moved_tv=1)

            try:
                with (
                    patch("archive_extract_manager.probe_archive", side_effect=fake_probe),
                    patch("archive_extract_manager.extract_archive", side_effect=fake_extract),
                    patch("archive_extract_manager.sort_extracted_videos", side_effect=fake_sort),
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
                                "auto_sort_enabled": True,
                                "movies_target_path": str(movies_target),
                                "tv_target_path": str(tv_target),
                            }
                        ]
                    )
                    job_id = jobs[0].id

                    self.assertTrue(sort_started.wait(1.0))
                    self.assertTrue(wait_for(lambda: manager._jobs[job_id].status == "completed"))

                job = manager._jobs[job_id]
                self.assertEqual(observed["sort_paths"], [str(extracted_video)])
                self.assertEqual(observed["movies_target_path"], movies_target)
                self.assertEqual(observed["tv_target_path"], tv_target)
                self.assertEqual(job.sort_summary["moved_tv"], 1)
                self.assertTrue(any("Auto-sort finished" in line for line in job.output_tail))
            finally:
                manager.stop()
                manager._worker.join(timeout=1.0)

    def test_auto_delete_runs_after_successful_auto_sort_move(self) -> None:
        with TemporaryDirectory(prefix="archive-manager-auto-delete-", ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            storage = SQLiteStorage(root / "state.sqlite3")
            manager = ArchiveExtractManager(storage, seven_zip_binary="7z-test")
            archive_path = root / "sample.part1.rar"
            archive_path.write_bytes(b"part1")
            target_path = root / "output"
            movies_target = root / "Movies"
            tv_target = root / "TvShows"

            cleanup_started = threading.Event()

            def fake_probe(path: Path, *, password: str | None = None, seven_zip_binary: str = "7z") -> ArchiveProbe:
                return ArchiveProbe(archive_type="rar", bytes_total=4096, entry_count=1)

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
                extracted = destination / "Movie.Name.2024.mkv"
                extracted.write_bytes(b"movie")
                if progress_callback:
                    progress_callback(bytes_done=4096, bytes_total=4096)
                return [str(extracted)]

            def fake_sort(
                extracted_paths,
                *,
                movies_target_path: Path,
                tv_target_path: Path,
                cancel_requested=None,
            ) -> ArchiveSortSummary:
                return ArchiveSortSummary(moved_movies=1)

            def fake_delete(path: Path, *, cancel_requested=None) -> ArchiveDeleteSummary:
                cleanup_started.set()
                self.assertEqual(path, archive_path)
                return ArchiveDeleteSummary(
                    deleted_paths=[str(archive_path), str(root / "sample.part2.rar")],
                    failed_paths=[],
                )

            try:
                with (
                    patch("archive_extract_manager.probe_archive", side_effect=fake_probe),
                    patch("archive_extract_manager.extract_archive", side_effect=fake_extract),
                    patch("archive_extract_manager.sort_extracted_videos", side_effect=fake_sort),
                    patch("archive_extract_manager.delete_archive_source_files", side_effect=fake_delete),
                ):
                    jobs = manager.submit(
                        [
                            {
                                "root_key": "downloads",
                                "archive_relative_path": "sample.part1.rar",
                                "archive_path": str(archive_path),
                                "archive_display_name": "sample.part1.rar",
                                "archive_type": "rar",
                                "target_relative_path": "sample",
                                "target_path": str(target_path),
                                "auto_sort_enabled": True,
                                "auto_delete_enabled": True,
                                "movies_target_path": str(movies_target),
                                "tv_target_path": str(tv_target),
                            }
                        ]
                    )
                    job_id = jobs[0].id

                    self.assertTrue(cleanup_started.wait(1.0))
                    self.assertTrue(wait_for(lambda: manager._jobs[job_id].status == "completed"))

                job = manager._jobs[job_id]
                self.assertTrue(job.auto_delete_enabled)
                self.assertEqual(job.auto_delete_summary["deleted_count"], 2)
                self.assertEqual(job.auto_delete_summary["failed_count"], 0)
                self.assertTrue(any("Auto-delete deleted 2 archive file(s)." in line for line in job.output_tail))
            finally:
                manager.stop()
                manager._worker.join(timeout=1.0)

    def test_auto_delete_keeps_source_archives_when_no_video_was_moved(self) -> None:
        with TemporaryDirectory(prefix="archive-manager-auto-delete-keep-", ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            storage = SQLiteStorage(root / "state.sqlite3")
            manager = ArchiveExtractManager(storage, seven_zip_binary="7z-test")
            archive_path = root / "sample.zip"
            archive_path.write_bytes(b"zip")
            target_path = root / "output"
            movies_target = root / "Movies"
            tv_target = root / "TvShows"

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
                extracted = destination / "Readme.txt"
                extracted.write_bytes(b"text")
                if progress_callback:
                    progress_callback(bytes_done=4096, bytes_total=4096)
                return [str(extracted)]

            def fake_sort(
                extracted_paths,
                *,
                movies_target_path: Path,
                tv_target_path: Path,
                cancel_requested=None,
            ) -> ArchiveSortSummary:
                return ArchiveSortSummary(skipped_non_video=1)

            try:
                with (
                    patch("archive_extract_manager.probe_archive", side_effect=fake_probe),
                    patch("archive_extract_manager.extract_archive", side_effect=fake_extract),
                    patch("archive_extract_manager.sort_extracted_videos", side_effect=fake_sort),
                    patch("archive_extract_manager.delete_archive_source_files") as delete_mock,
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
                                "auto_sort_enabled": True,
                                "auto_delete_enabled": True,
                                "movies_target_path": str(movies_target),
                                "tv_target_path": str(tv_target),
                            }
                        ]
                    )
                    job_id = jobs[0].id
                    self.assertTrue(wait_for(lambda: manager._jobs[job_id].status == "completed"))

                delete_mock.assert_not_called()
                job = manager._jobs[job_id]
                self.assertEqual(job.auto_delete_summary["kept_reason"], "no_videos_moved")
                self.assertTrue(any("Kept source archives because no video files were moved." in line for line in job.output_tail))
            finally:
                manager.stop()
                manager._worker.join(timeout=1.0)

    def test_cancel_active_job_marks_it_canceled(self) -> None:
        with TemporaryDirectory(prefix="archive-manager-cancel-active-", ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            storage = SQLiteStorage(root / "state.sqlite3")
            manager = ArchiveExtractManager(storage, seven_zip_binary="7z-test")
            archive_path = root / "active.zip"
            archive_path.write_bytes(b"zip")
            target_path = root / "output"

            probe_started = threading.Event()
            release_probe = threading.Event()
            extraction_started = threading.Event()

            def fake_probe(path: Path, *, password: str | None = None, seven_zip_binary: str = "7z") -> ArchiveProbe:
                probe_started.set()
                release_probe.wait(1.0)
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
                extraction_started.set()
                if progress_callback:
                    progress_callback(bytes_done=1024, bytes_total=4096)
                while cancel_requested and not cancel_requested():
                    time.sleep(0.02)
                raise ArchiveCanceledError("Archive extraction canceled.")

            try:
                with (
                    patch("archive_extract_manager.probe_archive", side_effect=fake_probe),
                    patch("archive_extract_manager.extract_archive", side_effect=fake_extract),
                ):
                    jobs = manager.submit(
                        [
                            {
                                "root_key": "downloads",
                                "archive_relative_path": "active.zip",
                                "archive_path": str(archive_path),
                                "archive_display_name": "active.zip",
                                "archive_type": "zip",
                                "target_relative_path": "active",
                                "target_path": str(target_path),
                            }
                        ]
                    )
                    job_id = jobs[0].id
                    self.assertTrue(probe_started.wait(1.0))
                    release_probe.set()
                    self.assertTrue(extraction_started.wait(1.0))
                    manager.cancel_job(job_id)
                    self.assertTrue(wait_for(lambda: manager._jobs[job_id].status == "canceled"))

                job = manager._jobs[job_id]
                self.assertEqual(job.status, "canceled")
                self.assertIn("Archive extraction canceled.", job.error or "")
            finally:
                manager.stop()
                manager._worker.join(timeout=1.0)

    def test_clear_queue_removes_non_active_jobs_and_purges_canceled_active_job(self) -> None:
        with TemporaryDirectory(prefix="archive-manager-clear-", ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            storage = SQLiteStorage(root / "state.sqlite3")
            manager = ArchiveExtractManager(storage, seven_zip_binary="7z-test")
            active_archive = root / "active.zip"
            queued_archive = root / "queued.zip"
            completed_archive = root / "completed.zip"
            active_archive.write_bytes(b"active")
            queued_archive.write_bytes(b"queued")
            completed_archive.write_bytes(b"completed")

            extraction_started = threading.Event()

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
                extraction_started.set()
                if progress_callback:
                    progress_callback(bytes_done=1024, bytes_total=4096)
                while cancel_requested and not cancel_requested():
                    time.sleep(0.02)
                raise ArchiveCanceledError("Archive extraction canceled.")

            try:
                with (
                    patch("archive_extract_manager.probe_archive", side_effect=fake_probe),
                    patch("archive_extract_manager.extract_archive", side_effect=fake_extract),
                ):
                    jobs = manager.submit(
                        [
                            {
                                "root_key": "downloads",
                                "archive_relative_path": "active.zip",
                                "archive_path": str(active_archive),
                                "archive_display_name": "active.zip",
                                "archive_type": "zip",
                                "target_relative_path": "active",
                                "target_path": str(root / "active"),
                            },
                            {
                                "root_key": "downloads",
                                "archive_relative_path": "queued.zip",
                                "archive_path": str(queued_archive),
                                "archive_display_name": "queued.zip",
                                "archive_type": "zip",
                                "target_relative_path": "queued",
                                "target_path": str(root / "queued"),
                            },
                            {
                                "root_key": "downloads",
                                "archive_relative_path": "completed.zip",
                                "archive_path": str(completed_archive),
                                "archive_display_name": "completed.zip",
                                "archive_type": "zip",
                                "target_relative_path": "completed",
                                "target_path": str(root / "completed"),
                            },
                        ]
                    )
                    active_job_id = jobs[0].id
                    queued_job_id = jobs[1].id
                    completed_job_id = jobs[2].id

                    self.assertTrue(extraction_started.wait(1.0))
                    with manager._lock:
                        manager._jobs[completed_job_id].status = "completed"
                        manager._jobs[completed_job_id].transfer.finished_at = utcnow_iso()
                        manager._rebuild_queue_locked()

                    result = manager.clear_queue()

                    self.assertEqual(result, {"removed": 2, "canceling": 1})
                    self.assertNotIn(queued_job_id, manager._jobs)
                    self.assertNotIn(completed_job_id, manager._jobs)
                    self.assertIn(active_job_id, manager._jobs)
                    self.assertIn(active_job_id, manager._purge_on_finish)
                    self.assertTrue(wait_for(lambda: active_job_id not in manager._jobs))
            finally:
                manager.stop()
                manager._worker.join(timeout=1.0)


class ArchiveRequestPathTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeArchiveExtractManager.instance = None
        FakeArchiveExtractManager.dashboard_jobs = []
        FakeArchiveExtractManager.canceled_job_ids = []
        FakeArchiveExtractManager.clear_results = {"removed": 0, "canceling": 0}
        FakeArchiveExtractManager.clear_calls = 0

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

    def test_explorer_extract_queues_auto_sort_targets_from_named_move_favorites(self) -> None:
        with TemporaryDirectory(prefix="archive-route-auto-sort-", ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            downloads_dir = root / "downloads"
            downloads_dir.mkdir(parents=True, exist_ok=True)
            (downloads_dir / "sample.zip").write_bytes(b"zip")
            movies_dir = root / "Movies"
            tv_dir = root / "TvShows"

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
                patch.object(app_module, "guessit_available", return_value=(True, None)),
            ):
                app = app_module.create_app()
                app.extensions["download_manager"].storage.save_move_favorites(
                    [
                        MoveFavorite(key="movies", label="Movies", path=str(movies_dir)),
                        MoveFavorite(key="tv", label="TvShows", path=str(tv_dir)),
                    ]
                )
                client = app.test_client()
                response = client.post(
                    "/explorer/extract",
                    data={
                        "root": "downloads",
                        "current_path": "",
                        "sort": "name",
                        "selected_paths": "sample.zip",
                        "auto_sort_extracted_videos": "1",
                        "auto_delete_source_archives": "1",
                    },
                )

                self.assertEqual(response.status_code, 302)
                manager = FakeArchiveExtractManager.instance
                self.assertIsNotNone(manager)
                prepared_job = manager.submitted[0][0]
                self.assertTrue(prepared_job["auto_sort_enabled"])
                self.assertTrue(prepared_job["auto_delete_enabled"])
                self.assertEqual(prepared_job["movies_target_path"], str(movies_dir.resolve()))
                self.assertEqual(prepared_job["tv_target_path"], str(tv_dir.resolve()))

                app.extensions["download_manager"].stop()
                app.extensions["media_compile_manager"].stop()
                app.extensions["archive_extract_manager"].stop()
                app.extensions["download_manager"]._worker.join(timeout=1.0)
                app.extensions["media_compile_manager"]._worker.join(timeout=1.0)

    def test_explorer_extract_rejects_auto_sort_when_named_favorites_are_missing(self) -> None:
        with TemporaryDirectory(prefix="archive-route-auto-sort-missing-", ignore_cleanup_errors=True) as temp_dir:
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
                patch.object(app_module, "guessit_available", return_value=(True, None)),
            ):
                app = app_module.create_app()
                client = app.test_client()
                response = client.post(
                    "/explorer/extract",
                    data={
                        "root": "downloads",
                        "current_path": "",
                        "sort": "name",
                        "selected_paths": "sample.zip",
                        "auto_sort_extracted_videos": "1",
                    },
                )

                self.assertEqual(response.status_code, 302)
                manager = FakeArchiveExtractManager.instance
                self.assertTrue(manager is None or not manager.submitted)
                with client.session_transaction() as session:
                    messages = session.get("_flashes", [])
                self.assertTrue(any("Movies" in message for _, message in messages))

                app.extensions["download_manager"].stop()
                app.extensions["media_compile_manager"].stop()
                app.extensions["archive_extract_manager"].stop()
                app.extensions["download_manager"]._worker.join(timeout=1.0)
                app.extensions["media_compile_manager"]._worker.join(timeout=1.0)

    def test_explorer_page_renders_related_archive_status_section(self) -> None:
        with TemporaryDirectory(prefix="archive-explorer-status-", ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            downloads_dir = root / "downloads"
            downloads_dir.mkdir(parents=True, exist_ok=True)
            (downloads_dir / "sample.zip").write_bytes(b"zip")

            FakeArchiveExtractManager.dashboard_jobs = [
                {
                    "id": "archive-job-1",
                    "root_key": "downloads",
                    "archive_relative_path": "sample.zip",
                    "archive_path": str(downloads_dir / "sample.zip"),
                    "archive_display_name": "sample.zip",
                    "archive_type": "zip",
                    "target_relative_path": "sample",
                    "target_path": str(downloads_dir / "sample"),
                    "target_display": "sample",
                    "status": "sorting",
                    "status_label": "Sorting",
                    "auto_sort_enabled": True,
                    "sort_summary": {"moved_tv": 1, "moved_movies": 0},
                    "can_cancel": True,
                    "transfer": {
                        "bytes_done": 1024,
                        "bytes_total": 4096,
                        "percent": 25.0,
                        "speed_bps": 512.0,
                        "eta_seconds": 6,
                        "last_message": "Queued archive extraction started.",
                    },
                    "error": None,
                }
            ]

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
                response = client.get("/explorer?root=downloads")
                body = response.get_data(as_text=True)

                self.assertEqual(response.status_code, 200)
                self.assertIn("Archive extraction status", body)
                self.assertIn("sample.zip", body)

                app.extensions["download_manager"].stop()
                app.extensions["media_compile_manager"].stop()
                app.extensions["archive_extract_manager"].stop()
                app.extensions["download_manager"]._worker.join(timeout=1.0)
                app.extensions["media_compile_manager"]._worker.join(timeout=1.0)

    def test_clear_archive_queue_route_preserves_explorer_context(self) -> None:
        with TemporaryDirectory(prefix="archive-clear-route-", ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            downloads_dir = root / "downloads"
            downloads_dir.mkdir(parents=True, exist_ok=True)

            FakeArchiveExtractManager.clear_results = {"removed": 2, "canceling": 1}

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
                    "/archive-jobs/clear",
                    data={
                        "root": "downloads",
                        "current_path": "subdir",
                        "sort": "modified",
                    },
                )

                self.assertEqual(response.status_code, 302)
                self.assertEqual(FakeArchiveExtractManager.clear_calls, 1)
                self.assertIn("/explorer?root=downloads&path=subdir&sort=modified", response.location)

                app.extensions["download_manager"].stop()
                app.extensions["media_compile_manager"].stop()
                app.extensions["archive_extract_manager"].stop()
                app.extensions["download_manager"]._worker.join(timeout=1.0)
                app.extensions["media_compile_manager"]._worker.join(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
