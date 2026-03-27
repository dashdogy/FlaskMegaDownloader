from pathlib import Path
BASE_DIR=Path(r"C:\\Users\\mkrbl\\Documents\\VSCODE\\FlaskMegaDownloader\\tmp_service_probe")
DATA_DIR=BASE_DIR/"data"
SECRET_KEY="x"
HOST="0.0.0.0"
PORT=8080
POLL_INTERVAL_MS=1500
JOB_STORAGE_FILE=DATA_DIR/"jobs.json"
MEGACMD_BINARY="mega-get"
DOWNLOADER_BACKEND="fake"
ALLOWED_DESTINATIONS={"downloads":{"label":"Downloads","path":BASE_DIR/"downloads"},"media":{"label":"Media","path":BASE_DIR/"media"}}
