from __future__ import annotations

from pathlib import Path
from tempfile import mkdtemp
import shutil
import unittest

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
            manager._stop_event.set()
            manager._worker.join(timeout=1)
            del manager
            del storage
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
