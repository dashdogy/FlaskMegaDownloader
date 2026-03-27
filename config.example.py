from pathlib import Path


BASE_DIR = Path("/opt/flask-mega-downloader")
DATA_DIR = BASE_DIR / "data"

SECRET_KEY = "replace-this-secret"
HOST = "0.0.0.0"
PORT = 8080
POLL_INTERVAL_MS = 1500

JOB_STORAGE_FILE = DATA_DIR / "jobs.json"
MEGACMD_BINARY = "mega-get"
DOWNLOADER_BACKEND = "auto"

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
