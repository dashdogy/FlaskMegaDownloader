from pathlib import Path
import os


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("MEGA_DOWNLOADER_DATA_DIR", BASE_DIR / "data")).resolve()
DOWNLOADS_DIR = Path(os.environ.get("MEGA_DOWNLOADER_DOWNLOADS_DIR", BASE_DIR / "downloads")).resolve()
MEDIA_DIR = Path(os.environ.get("MEGA_DOWNLOADER_MEDIA_DIR", DOWNLOADS_DIR / "media")).resolve()

SECRET_KEY = os.environ.get("MEGA_DOWNLOADER_SECRET_KEY", "change-me")
HOST = os.environ.get("MEGA_DOWNLOADER_HOST", "0.0.0.0")
PORT = int(os.environ.get("MEGA_DOWNLOADER_PORT", "8090"))
POLL_INTERVAL_MS = int(os.environ.get("MEGA_DOWNLOADER_POLL_INTERVAL_MS", "1500"))

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
