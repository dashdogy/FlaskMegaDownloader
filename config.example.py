from pathlib import Path


BASE_DIR = Path("/opt/flask-mega-downloader")
DATA_DIR = BASE_DIR / "data"

SECRET_KEY = "replace-this-secret"
AUTH_ENABLED = True
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD_HASH = ""
HOST = "0.0.0.0"
PORT = 8090
POLL_INTERVAL_MS = 1500
EVENT_LOG_MAX_ROWS = 5000
MAX_CONTENT_LENGTH = 1024 * 1024
MAX_URLS_PER_SUBMISSION = 200
DOWNLOAD_WORKERS = 2
ARCHIVE_WORKERS = 1
MEDIA_WORKERS = 1
PLEX_PERMISSIONS_ENABLED = True
PLEX_PERMISSION_STRICT = False
PLEX_USER = "plex"
SETFACL_BINARY = "setfacl"

JOB_STORAGE_FILE = DATA_DIR / "jobs.json"
STATE_DB_FILE = DATA_DIR / "state.sqlite3"
MEGACMD_BINARY = "mega-get"
DOWNLOADER_BACKEND = "auto"
MAKEMKVCON_BINARY = "makemkvcon"
MEDIAINFO_BINARY = "mediainfo"
SEVEN_ZIP_BINARY = "7z"
BLURAY_MIN_TITLE_SECONDS = 2400

ALLOWED_DESTINATIONS = {
    "downloads": {
        "label": "Downloads",
        "path": Path("/srv/mega-downloads"),
    },
    "media": {
        "label": "Media",
        "path": Path("/srv/media/incoming"),
    },
}
