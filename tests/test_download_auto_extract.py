from __future__ import annotations

import shutil
import unittest
from pathlib import Path
from tempfile import mkdtemp
from types import SimpleNamespace

from downloader import DownloadManager
from storage import JsonStorage


class FakeAutoExtractArchiveManager:
    def __init__(self) -> None:
        self.submitted: list[list[dict]] = []
        self.payloads: dict[str, dict] = {}

    def submit(self, prepared_jobs: list[dict]):
        self.submitted.append(prepared_jobs)
        created = []
        for index, item in enumerate(prepared_jobs):
            job_id = f"archive-{len(self.submitted)}-{index}"
            self.payloads[job_id] = {
                "id": job_id,
                "status": "queued",
                "error": None,
                "transfer": {"last_message": "Queued automatically."},
            }
            created.append(
                SimpleNamespace(
                    id=job_id,
                    archive_display_name=item["archive_display_name"],
                    archive_type=item["archive_type"],
                    target_path=item["target_path"],
                )
            )
        return created

    def job_payload(self, job_id: str) -> dict | None:
        return self.payloads.get(job_id)


class DownloadAutoExtractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path(mkdtemp(prefix="download-auto-extract-"))
        self.downloads_dir = self.temp_dir / "downloads"
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.storage = JsonStorage(self.temp_dir / "state.sqlite3", legacy_json_path=self.temp_dir / "jobs.json")
        self.manager = DownloadManager(
            storage=self.storage,
            destinations={"downloads": {"label": "Downloads", "path": self.downloads_dir}},
            backend="fake",
        )
        self.manager.stop()
        self.manager._worker.join(timeout=1.0)
        self.archive_manager = FakeAutoExtractArchiveManager()
        self.manager.attach_archive_manager(self.archive_manager)

    def tearDown(self) -> None:
        self.manager.stop()
        self.manager._worker.join(timeout=1.0)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_standalone_zip_auto_extract_queues_when_download_finishes(self) -> None:
        url = "https://example.invalid/sample.zip"
        jobs = self.manager.submit(
            [url],
            "downloads",
            auto_extract_enabled=True,
            metadata_overrides={url: {"display_name": "sample.zip"}},
        )

        auto_extract_set = next(iter(self.manager._auto_extract_sets.values()))
        self.assertEqual(auto_extract_set.status, "waiting")
        self.assertEqual(auto_extract_set.expected_part_filenames, ["sample.zip"])
        self.assertFalse(self.archive_manager.submitted)

        (self.downloads_dir / "sample.zip").write_bytes(b"zip")
        self.manager._finish_job(jobs[0].id, "completed", None)

        self.assertEqual(len(self.archive_manager.submitted), 1)
        prepared = self.archive_manager.submitted[0][0]
        self.assertEqual(prepared["archive_display_name"], "sample.zip")
        self.assertEqual(prepared["archive_path"], str(self.downloads_dir / "sample.zip"))
        self.assertEqual(prepared["target_path"], str(self.downloads_dir / "sample"))

        auto_extract_set = next(iter(self.manager._auto_extract_sets.values()))
        self.assertEqual(auto_extract_set.status, "queued_for_extract")
        self.assertTrue(auto_extract_set.archive_job_id)

        self.archive_manager.payloads[auto_extract_set.archive_job_id]["status"] = "completed"
        payload = self.manager.dashboard_payload()
        batch_set = payload["batches"][0]["auto_extract_sets"][0]
        self.assertEqual(batch_set["status"], "completed")

    def test_multipart_rar_waits_for_same_batch_and_existing_disk_parts(self) -> None:
        (self.downloads_dir / "movie.part2.rar").write_bytes(b"part2")
        url = "https://example.invalid/movie.part1.rar"
        jobs = self.manager.submit(
            [url],
            "downloads",
            auto_extract_enabled=True,
            metadata_overrides={url: {"display_name": "movie.part1.rar"}},
        )

        auto_extract_set = next(iter(self.manager._auto_extract_sets.values()))
        self.assertEqual(
            auto_extract_set.expected_part_filenames,
            ["movie.part1.rar", "movie.part2.rar"],
        )
        self.assertEqual(auto_extract_set.status, "waiting")

        (self.downloads_dir / "movie.part1.rar").write_bytes(b"part1")
        self.manager._finish_job(jobs[0].id, "completed", None)

        self.assertEqual(len(self.archive_manager.submitted), 1)
        prepared = self.archive_manager.submitted[0][0]
        self.assertEqual(prepared["archive_display_name"], "movie.part1.rar")
        self.assertEqual(prepared["target_path"], str(self.downloads_dir / "movie"))

    def test_unresolved_archive_filename_is_grouped_only_after_resolution(self) -> None:
        self.manager.adapter.probe_metadata = lambda url, fallback_prefix: {}
        url = "https://mega.nz/file/ARCHIVE#KEY"
        jobs = self.manager.submit([url], "downloads", auto_extract_enabled=True)

        self.assertFalse(self.manager._auto_extract_sets)

        self.manager._update_job(jobs[0].id, display_name="episode.zip")
        auto_extract_set = next(iter(self.manager._auto_extract_sets.values()))
        self.assertEqual(auto_extract_set.status, "waiting")
        self.assertEqual(auto_extract_set.entrypoint_filename, "episode.zip")

        (self.downloads_dir / "episode.zip").write_bytes(b"zip")
        self.manager._finish_job(jobs[0].id, "completed", None)
        self.assertEqual(len(self.archive_manager.submitted), 1)

    def test_archive_automation_defaults_persist_across_manager_restart(self) -> None:
        self.manager.update_archive_automation_settings(auto_sort_enabled=True, auto_delete_enabled=True)

        self.manager.stop()
        self.manager._worker.join(timeout=1.0)
        replacement = DownloadManager(
            storage=self.storage,
            destinations={"downloads": {"label": "Downloads", "path": self.downloads_dir}},
            backend="fake",
        )
        replacement.stop()
        replacement._worker.join(timeout=1.0)
        try:
            self.assertEqual(
                replacement.archive_automation_settings_payload(),
                {"auto_sort_enabled": True, "auto_delete_enabled": True},
            )
        finally:
            replacement.stop()
            replacement._worker.join(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
