from __future__ import annotations

from pathlib import Path
from tempfile import mkdtemp
import shutil
import threading
import time
import unittest
from unittest.mock import patch

from downloader import (
    DownloadManager,
    MegaDownloader,
    decrypt_mega_file_attributes,
    parse_megacmd_du_summary,
    parse_megacmd_ls_summary,
)
from storage import JsonStorage


class DownloaderMetadataParsingTests(unittest.TestCase):
    def test_decrypt_mega_file_attributes_for_sample_link(self) -> None:
        attrs = decrypt_mega_file_attributes(
            "g7_uy-xGi7yILSMvk794ZAVyLlPB8gmm0hwS--I_s6iEyxkXg9lLWbKWMp16XHachnNIKX1ZfsXCBa6aKi_35lIF4UbMyuZV-FWjK-07eC8",
            "PpbchjFhtgSs8m6PHkPgEQD8n4pmfpdAggnEkGbP8Is",
        )
        self.assertEqual(
            attrs["n"],
            "La La Land (2016) (2160p BluRay x265 10bit HDR Tigole).mkv",
        )

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

    def test_public_file_api_fallback_resolves_sample_link_metadata(self) -> None:
        class FakeResponse:
            def __init__(self, body: str) -> None:
                self._body = body.encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self) -> bytes:
                return self._body

        downloader = MegaDownloader("mega-get")
        with patch(
            "downloader.urllib.request.urlopen",
            return_value=FakeResponse(
                '[{"s":11247304733,"at":"g7_uy-xGi7yILSMvk794ZAVyLlPB8gmm0hwS--I_s6iEyxkXg9lLWbKWMp16XHachnNIKX1ZfsXCBa6aKi_35lIF4UbMyuZV-FWjK-07eC8"}]'
            ),
        ):
            metadata = downloader.probe_metadata(
                "https://mega.nz/file/2uBxxD6B#PpbchjFhtgSs8m6PHkPgEQD8n4pmfpdAggnEkGbP8Is",
                "sample-link",
            )
        self.assertEqual(
            metadata["display_name"],
            "La La Land (2016) (2160p BluRay x265 10bit HDR Tigole).mkv",
        )
        self.assertEqual(metadata["bytes_total"], 11247304733)

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

    def test_direct_mega_metadata_keeps_retrying_without_deferred(self) -> None:
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

            with patch("downloader.DIRECT_MEGA_METADATA_RETRY_DELAYS_SECONDS", (0.0, 0.0, 0.0)):
                jobs = manager.submit(["https://mega.nz/file/FILEONE#KEYONE"], "downloads")
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if jobs[0].metadata_attempts >= 4:
                        break
                    time.sleep(0.05)

            self.assertGreaterEqual(jobs[0].metadata_attempts, 4)
            self.assertIn(jobs[0].metadata_status, {"pending", "resolving"})
            self.assertNotEqual(jobs[0].metadata_status, "deferred")
            self.assertTrue(jobs[0].metadata_message.startswith("Resolving file name"))
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

    def test_direct_mega_head_job_blocks_queue_until_filename_resolves(self) -> None:
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

            release_probe = threading.Event()
            download_started = threading.Event()

            def gated_probe(url, fallback_prefix):
                if "FILEONE" in url:
                    release_probe.wait(timeout=2.0)
                    return {"display_name": "Movie One.mkv"}
                return {"display_name": "Movie Two.mkv"}

            def fake_download(job, destination_dir, progress_callback, cancel_event, pause_event, process_callback):
                download_started.set()
                progress_callback(status="completed", display_name=job.display_name, message="done")

            manager.adapter.probe_metadata = gated_probe
            manager.adapter.download = fake_download
            jobs = manager.submit(["https://mega.nz/file/FILEONE#KEYONE"], "downloads")

            time.sleep(0.35)
            self.assertEqual(jobs[0].status, "queued")
            payload = manager.dashboard_payload()
            job_payload = next(item for item in payload["jobs"] if item["id"] == jobs[0].id)
            self.assertTrue(job_payload["metadata_blocks_queue"])
            self.assertFalse(download_started.is_set())

            release_probe.set()
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline:
                if download_started.is_set():
                    break
                time.sleep(0.05)

            self.assertTrue(download_started.is_set())
            self.assertEqual(jobs[0].display_name, "Movie One.mkv")
            manager.stop()
            manager._worker.join(timeout=1)
            manager._metadata_worker.join(timeout=1)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_later_jobs_do_not_leapfrog_blocked_direct_mega_head(self) -> None:
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

            release_probe = threading.Event()
            started_jobs: list[str] = []

            def gated_probe(url, fallback_prefix):
                if "FILEONE" in url:
                    release_probe.wait(timeout=2.0)
                    return {"display_name": "Head Movie.mkv"}
                return {"display_name": "Tail Movie.mkv"}

            def fake_download(job, destination_dir, progress_callback, cancel_event, pause_event, process_callback):
                started_jobs.append(job.id)
                progress_callback(status="completed", display_name=job.display_name, message="done")

            manager.adapter.probe_metadata = gated_probe
            manager.adapter.download = fake_download
            jobs = manager.submit(
                [
                    "https://mega.nz/file/FILEONE#KEYONE",
                    "https://mega.nz/file/FILETWO#KEYTWO",
                ],
                "downloads",
                metadata_overrides={"https://mega.nz/file/FILETWO#KEYTWO": {"display_name": "Tail Movie.mkv"}},
            )

            time.sleep(0.35)
            self.assertEqual(started_jobs, [])
            self.assertEqual(jobs[0].status, "queued")
            self.assertEqual(jobs[1].status, "queued")

            release_probe.set()
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline:
                if started_jobs:
                    break
                time.sleep(0.05)

            self.assertTrue(started_jobs)
            self.assertEqual(started_jobs[0], jobs[0].id)
            manager.stop()
            manager._worker.join(timeout=1)
            manager._metadata_worker.join(timeout=1)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def test_filename_resolution_allows_start_even_if_size_is_unknown(self) -> None:
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

            started = threading.Event()

            manager.adapter.probe_metadata = lambda url, fallback_prefix: {"display_name": "Known Name.mkv"}

            def fake_download(job, destination_dir, progress_callback, cancel_event, pause_event, process_callback):
                started.set()
                progress_callback(status="completed", display_name=job.display_name, message="done")

            manager.adapter.download = fake_download
            jobs = manager.submit(["https://mega.nz/file/FILEONE#KEYONE"], "downloads")
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if started.is_set():
                    break
                time.sleep(0.05)

            self.assertTrue(started.is_set())
            self.assertEqual(jobs[0].display_name, "Known Name.mkv")
            self.assertIsNone(jobs[0].transfer.bytes_total)
            manager.stop()
            manager._worker.join(timeout=1)
            manager._metadata_worker.join(timeout=1)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
