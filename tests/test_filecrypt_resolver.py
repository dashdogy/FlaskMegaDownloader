from __future__ import annotations

import unittest

from filecrypt_resolver import (
    FilecryptResolutionError,
    expand_submission_urls,
    expand_submission_urls_with_metadata,
    resolve_filecrypt_links,
    resolve_filecrypt_url,
)


CONTAINER_URL = "https://filecrypt.cc/Container/ABC123.html"
MEGA_MIRROR_URL = "https://filecrypt.cc/Container/ABC123.html?mirror=0"
LINK_ONE_URL = "https://filecrypt.cc/Link/AAAA111111.html"
LINK_TWO_URL = "https://filecrypt.cc/Link/BBBB222222.html"
GO_ONE_URL = "https://filecrypt.cc/Go/token-one.html"
GO_TWO_URL = "https://filecrypt.cc/Go/token-two.html"


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def get_text(self, url: str, *, referer: str | None = None) -> tuple[str, str]:
        self.calls.append(("text", url, referer))
        if url == CONTAINER_URL:
            return (
                """
                <html>
                    <li><a class="online" href="/Container/ABC123.html?mirror=0">mega.nz</a></li>
                    <li><a class="online" href="/Container/ABC123.html?mirror=1">other.host</a></li>
                </html>
                """,
                CONTAINER_URL,
            )
        if url == MEGA_MIRROR_URL:
            return (
                """
                <table>
                    <tr><td class="status"></td><td title="Episode 01.mkv">Episode 01.mkv<span><a href="https://mega.nz">mega.nz</a></span></td><td>226.25 MB</td><td><button data-a="AAAA111111" class="download">Download</button></td></tr>
                    <tr><td class="status"></td><td title="Episode 02.mkv">Episode 02.mkv<span><a href="https://mega.nz">mega.nz</a></span></td><td>269.89 MB</td><td><button class="download" data-b="BBBB222222">Download</button></td></tr>
                    <tr><td><button data-b="BBBB222222" class="download">Duplicate</button></td></tr>
                </table>
                """,
                MEGA_MIRROR_URL,
            )
        if url == LINK_ONE_URL:
            return ("<script>top.location.href='https://filecrypt.cc/Go/token-one.html';</script>", LINK_ONE_URL)
        if url == LINK_TWO_URL:
            return ("<script>top.location.href='https://filecrypt.cc/Go/token-two.html';</script>", LINK_TWO_URL)
        raise AssertionError(f"Unexpected text URL: {url}")

    def get_final_url(self, url: str, *, referer: str | None = None) -> tuple[str, str]:
        self.calls.append(("final", url, referer))
        if url == GO_ONE_URL:
            return (
                "https://mega.nz/file/FILEONE#KEYONE",
                "<html><meta property='og:url' content='https://mega.nz/file/FILEONE'></html>",
            )
        if url == GO_TWO_URL:
            return (
                "https://mega.nz/folder/FOLDERTWO#KEYTWO",
                "<html><meta property='og:url' content='https://mega.nz/folder/FOLDERTWO'></html>",
            )
        raise AssertionError(f"Unexpected final URL: {url}")


class NoMegaMirrorSession(FakeSession):
    def get_text(self, url: str, *, referer: str | None = None) -> tuple[str, str]:
        if url == CONTAINER_URL:
            return ("<html><li><a href='/Container/ABC123.html?mirror=1'>other.host</a></li></html>", CONTAINER_URL)
        return super().get_text(url, referer=referer)


class MetadataFallbackSession(FakeSession):
    def get_text(self, url: str, *, referer: str | None = None) -> tuple[str, str]:
        self.calls.append(("text", url, referer))
        if url == CONTAINER_URL:
            return (
                """
                <html>
                    <li><a class="online" href="/Container/ABC123.html?mirror=0">mega.nz</a></li>
                    <table>
                        <tr><td><button data-a="AAAA111111" class="download">Download</button></td></tr>
                        <tr><td><button data-b="BBBB222222" class="download">Download</button></td></tr>
                    </table>
                </html>
                """,
                CONTAINER_URL,
            )
        return super().get_text(url, referer=referer)


class FilecryptResolverTests(unittest.TestCase):
    def test_resolve_container_to_mega_urls(self) -> None:
        resolved = resolve_filecrypt_url(CONTAINER_URL, session_factory=FakeSession)
        self.assertEqual(
            resolved,
            [
                "https://mega.nz/file/FILEONE#KEYONE",
                "https://mega.nz/folder/FOLDERTWO#KEYTWO",
            ],
        )

    def test_expand_submission_urls_dedupes_after_resolution(self) -> None:
        urls, summary = expand_submission_urls(
            [
                "https://mega.nz/file/FILEONE#KEYONE",
                CONTAINER_URL,
            ],
            session_factory=FakeSession,
        )
        self.assertEqual(
            urls,
            [
                "https://mega.nz/file/FILEONE#KEYONE",
                "https://mega.nz/folder/FOLDERTWO#KEYTWO",
            ],
        )
        self.assertEqual(summary.containers_resolved, 1)
        self.assertEqual(summary.mega_links_resolved, 2)

    def test_expand_submission_urls_with_metadata_keeps_filecrypt_names_and_sizes(self) -> None:
        urls, summary, metadata = expand_submission_urls_with_metadata([CONTAINER_URL], session_factory=FakeSession)
        self.assertEqual(summary.containers_resolved, 1)
        self.assertEqual(
            metadata,
            {
                "https://mega.nz/file/FILEONE#KEYONE": {
                    "display_name": "Episode 01.mkv",
                    "bytes_total": 226250000,
                },
                "https://mega.nz/folder/FOLDERTWO#KEYTWO": {
                    "display_name": "Episode 02.mkv",
                    "bytes_total": 269890000,
                },
            },
        )
        self.assertEqual(
            urls,
            [
                "https://mega.nz/file/FILEONE#KEYONE",
                "https://mega.nz/folder/FOLDERTWO#KEYTWO",
            ],
        )

    def test_resolve_filecrypt_links_returns_metadata(self) -> None:
        links = resolve_filecrypt_links(CONTAINER_URL, session_factory=FakeSession)
        self.assertEqual(links[0].display_name, "Episode 01.mkv")
        self.assertEqual(links[0].bytes_total, 226250000)
        self.assertEqual(links[1].display_name, "Episode 02.mkv")
        self.assertEqual(links[1].bytes_total, 269890000)

    def test_resolver_fetches_mega_mirror_when_initial_page_lacks_names(self) -> None:
        links = resolve_filecrypt_links(CONTAINER_URL, session_factory=MetadataFallbackSession)
        self.assertEqual(links[0].display_name, "Episode 01.mkv")
        self.assertEqual(links[0].bytes_total, 226250000)
        self.assertEqual(links[1].display_name, "Episode 02.mkv")
        self.assertEqual(links[1].bytes_total, 269890000)

    def test_direct_dlc_url_is_rejected(self) -> None:
        with self.assertRaises(FilecryptResolutionError):
            resolve_filecrypt_url("https://filecrypt.cc/DLC/ABC123.dlc", session_factory=FakeSession)

    def test_missing_mega_mirror_fails(self) -> None:
        with self.assertRaises(FilecryptResolutionError):
            resolve_filecrypt_url(CONTAINER_URL, session_factory=NoMegaMirrorSession)


if __name__ == "__main__":
    unittest.main()
