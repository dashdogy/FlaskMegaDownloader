from __future__ import annotations

from archive_auto_sort import guessit_available
from archive_extract_manager import ArchiveExtractManager
from downloader import DownloadManager
from filecrypt_resolver import expand_submission_urls_with_metadata
from flask_mega_downloader import web
from media_compiler import MediaCompileManager


def create_app():
    # Keep the historical root import surface patchable for tests and extensions.
    web.ArchiveExtractManager = ArchiveExtractManager
    web.DownloadManager = DownloadManager
    web.MediaCompileManager = MediaCompileManager

    def _expand_submission_urls_with_metadata(*args, **kwargs):
        return expand_submission_urls_with_metadata(*args, **kwargs)

    web.expand_submission_urls_with_metadata = _expand_submission_urls_with_metadata
    web.guessit_available = guessit_available
    return web.create_app()


if __name__ == "__main__":
    application = create_app()
    application.run(host=application.config["HOST"], port=application.config["PORT"], debug=False)
