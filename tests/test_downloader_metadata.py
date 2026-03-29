from __future__ import annotations

import unittest

from downloader import parse_megacmd_du_summary, parse_megacmd_ls_summary


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


if __name__ == "__main__":
    unittest.main()
