from pathlib import Path
import os


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("MEGA_DOWNLOADER_DATA_DIR", BASE_DIR / "data")).resolve()
DOWNLOADS_DIR = Path(os.environ.get("MEGA_DOWNLOADER_DOWNLOADS_DIR", BASE_DIR / "downloads")).resolve()
MEDIA_DIR = Path(os.environ.get("MEGA_DOWNLOADER_MEDIA_DIR", DOWNLOADS_DIR / "media")).resolve()

SECRET_KEY = os.environ.get("MEGA_DOWNLOADER_SECRET_KEY", "change-me")
AUTH_ENABLED = os.environ.get("MEGA_DOWNLOADER_AUTH_ENABLED", "1").lower() not in {"0", "false", "no", "off"}
ADMIN_USERNAME = os.environ.get("MEGA_DOWNLOADER_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.environ.get("MEGA_DOWNLOADER_ADMIN_PASSWORD_HASH", "")
HOST = os.environ.get("MEGA_DOWNLOADER_HOST", "0.0.0.0")
PORT = int(os.environ.get("MEGA_DOWNLOADER_PORT", "8090"))
POLL_INTERVAL_MS = int(os.environ.get("MEGA_DOWNLOADER_POLL_INTERVAL_MS", "1500"))
EVENT_LOG_MAX_ROWS = int(os.environ.get("MEGA_DOWNLOADER_EVENT_LOG_MAX_ROWS", "5000"))
MAX_CONTENT_LENGTH = int(os.environ.get("MEGA_DOWNLOADER_MAX_CONTENT_LENGTH", str(1024 * 1024)))
MAX_URLS_PER_SUBMISSION = int(os.environ.get("MEGA_DOWNLOADER_MAX_URLS_PER_SUBMISSION", "200"))
DOWNLOAD_WORKERS = int(os.environ.get("MEGA_DOWNLOADER_DOWNLOAD_WORKERS", "2"))
ARCHIVE_WORKERS = int(os.environ.get("MEGA_DOWNLOADER_ARCHIVE_WORKERS", "1"))
MEDIA_WORKERS = int(os.environ.get("MEGA_DOWNLOADER_MEDIA_WORKERS", "1"))
PLEX_PERMISSIONS_ENABLED = os.environ.get("MEGA_DOWNLOADER_PLEX_PERMISSIONS_ENABLED", "1").lower() not in {"0", "false", "no", "off"}
PLEX_PERMISSION_STRICT = os.environ.get("MEGA_DOWNLOADER_PLEX_PERMISSION_STRICT", "0").lower() not in {"0", "false", "no", "off"}
PLEX_USER = os.environ.get("MEGA_DOWNLOADER_PLEX_USER", "plex")
SETFACL_BINARY = os.environ.get("SETFACL_BINARY", "setfacl")

JOB_STORAGE_FILE = DATA_DIR / "jobs.json"
STATE_DB_FILE = DATA_DIR / "state.sqlite3"
MEGACMD_BINARY = os.environ.get("MEGACMD_BINARY", "mega-get")
DOWNLOADER_BACKEND = os.environ.get("MEGA_DOWNLOADER_BACKEND", "auto")
MAKEMKVCON_BINARY = os.environ.get("MAKEMKVCON_BINARY", "makemkvcon")
MEDIAINFO_BINARY = os.environ.get("MEDIAINFO_BINARY", "mediainfo")
SEVEN_ZIP_BINARY = os.environ.get("SEVEN_ZIP_BINARY", "7z")
BLURAY_MIN_TITLE_SECONDS = int(os.environ.get("MEGA_DOWNLOADER_BLURAY_MIN_TITLE_SECONDS", "2400"))

ALLOWED_DESTINATIONS = {
    "downloads": {
        "label": "Primary Downloads",
        "path": DOWNLOADS_DIR,
    },
    "media": {
        "label": "Media Library",
        "path": MEDIA_DIR,
    },
}
