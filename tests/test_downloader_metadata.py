from __future__ import annotations

from pathlib import Path
from tempfile import mkdtemp
import shutil
import time
import unittest
from unittest.mock import patch

from downloader import DownloadManager, parse_megacmd_du_summary, parse_megacmd_ls_summary
from storage import JsonStorage


class DownloaderMetadataParsingTests(unittest.TestCase):
    def test_parse_megacmd_ls_ignores_separator_and_keeps_single_file(self) -> None:
        output = "\n".join(
            [
                "FLAGS VERSIONS SIZE DATE NAME",
                "----------",
                "-f- 1 237266063 2026-03-29T00:00:00 [AnimeOnlineNinja] Shingeki OVA 01.mp4",
            ]
        )
        self.assertEqual(
            parse_megacmd_ls_summary(output),
            [
                {
                    "flags": "-f-",
                    "size": 237266063,
                    "name": "[AnimeOnlineNinja] Shingeki OVA 01.mp4",
                }
            ],
        )

    def test_parse_megacmd_du_summary_extracts_size_and_name(self) -> None:
        output = "237266063 [AnimeOnlineNinja] Shingeki OVA 01.mp4"
        self.assertEqual(
            parse_megacmd_du_summary(output),
            (237266063, "[AnimeOnlineNinja] Shingeki OVA 01.mp4"),
        )

    def test_submit_prefers_filecrypt_metadata_overrides_for_names_and_sizes(self) -> None:
        temp_dir = mkdtemp(prefix="download-meta-")
        try:
            root = Path(temp_dir)
            downloads = root / "downloads"
            downloads.mkdir()
            storage = JsonStorage(root / "state.sqlite3", legacy_json_path=root / "jobs.json")
            manager = DownloadManager(
                storage=storage,
                destinations={"downloads": {"label": "Downloads", "path": downloads}},
                backend="fake",
            )
            manager._rebuild_queue_locked = lambda ordered_ids=None: None
            manager.adapter.probe_metadata = lambda url, fallback_prefix: {}

            url = "https://mega.nz/file/FILEONE#KEYONE"
            jobs = manager.submit(
                [url],
                "downloads",
                metadata_overrides={
                    url: {
                        "display_name": "Episode 01.mkv",
                        "bytes_total": 226250000,
                    }
                },
            )

            self.assertEqual(jobs[0].display_name, "Episode 01.mkv")
            self.assertEqual(jobs[0].transfer.bytes_total, 226250000)
            self.assertEqual(jobs[0].metadata_status, "resolved")
            manager.stop()
            manager._worker.join(timeout=1)
            manager._metadata_worker.join(timeout=1)
            del manager
            del storage
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_submit_returns_before_background_metadata_probe_finishes(self) -> None:
        temp_dir = mkdtemp(prefix="download-meta-")
        try:
            root = Path(temp_dir)
            downloads = root / "downloads"
            downloads.mkdir()
            storage = JsonStorage(root / "state.sqlite3", legacy_json_path=root / "jobs.json")
            manager = DownloadManager(
                storage=storage,
                destinations={"downloads": {"label": "Downloads", "path": downloads}},
                backend="fake",
            )
            manager._rebuild_queue_locked = lambda ordered_ids=None: None

            def slow_probe(url, fallback_prefix):
                time.sleep(0.35)
                return {"display_name": "Queued Episode.mkv", "bytes_total": 1048576}

            manager.adapter.probe_metadata = slow_probe
            started = time.monotonic()
            jobs = manager.submit(["https://mega.nz/file/FILEONE#KEYONE"], "downloads")
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 0.2)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if jobs[0].metadata_status == "resolved":
                    break
                time.sleep(0.05)

            self.assertEqual(jobs[0].display_name, "Queued Episode.mkv")
            self.assertEqual(jobs[0].transfer.bytes_total, 1048576)
            self.assertEqual(jobs[0].metadata_status, "resolved")
            manager.stop()
            manager._worker.join(timeout=1)
            manager._metadata_worker.join(timeout=1)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_background_metadata_defers_after_three_attempts(self) -> None:
        temp_dir = mkdtemp(prefix="download-meta-")
        try:
            root = Path(temp_dir)
            downloads = root / "downloads"
            downloads.mkdir()
            storage = JsonStorage(root / "state.sqlite3", legacy_json_path=root / "jobs.json")
            manager = DownloadManager(
                storage=storage,
                destinations={"downloads": {"label": "Downloads", "path": downloads}},
                backend="fake",
            )
            manager._rebuild_queue_locked = lambda ordered_ids=None: None
            manager.adapter.probe_metadata = lambda url, fallback_prefix: {}

            with patch("downloader.METADATA_RETRY_DELAYS_SECONDS", (0.0, 0.0, 0.0)):
                jobs = manager.submit(["https://mega.nz/file/FILEONE#KEYONE"], "downloads")
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if jobs[0].metadata_status == "deferred":
                        break
                    time.sleep(0.05)

            self.assertEqual(jobs[0].metadata_attempts, 3)
            self.assertEqual(jobs[0].metadata_status, "deferred")
            self.assertTrue(jobs[0].metadata_message.startswith("Waiting for download start"))
            manager.stop()
            manager._worker.join(timeout=1)
            manager._metadata_worker.join(timeout=1)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_background_metadata_resolution_creates_auto_extract_set(self) -> None:
        temp_dir = mkdtemp(prefix="download-meta-")
        try:
            root = Path(temp_dir)
            downloads = root / "downloads"
            downloads.mkdir()
            storage = JsonStorage(root / "state.sqlite3", legacy_json_path=root / "jobs.json")
            manager = DownloadManager(
                storage=storage,
                destinations={"downloads": {"label": "Downloads", "path": downloads}},
                backend="fake",
            )
            manager._rebuild_queue_locked = lambda ordered_ids=None: None
            manager.adapter.probe_metadata = lambda url, fallback_prefix: {"display_name": "episode.zip"}

            jobs = manager.submit(
                ["https://mega.nz/file/ARCHIVE#KEY"],
                "downloads",
                auto_extract_enabled=True,
            )
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if manager._auto_extract_sets:
                    break
                time.sleep(0.05)

            self.assertEqual(jobs[0].display_name, "episode.zip")
            auto_extract_set = next(iter(manager._auto_extract_sets.values()))
            self.assertEqual(auto_extract_set.entrypoint_filename, "episode.zip")
            self.assertEqual(auto_extract_set.status, "waiting")
            manager.stop()
            manager._worker.join(timeout=1)
            manager._metadata_worker.join(timeout=1)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
