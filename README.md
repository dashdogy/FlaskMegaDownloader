# Flask Mega Downloader

Lightweight Flask UI for queueing public MEGA links to configured local folders, monitoring transfer status over JSON polling, browsing only approved download roots, and extracting normal or AES-encrypted ZIP archives safely.

## Features

- Server-rendered Flask app with a Homepage-inspired soft card UI
- Multi-link submission with whitespace trimming, blank-line removal, and per-batch deduplication
- Background worker thread with persisted JSON job history
- Automatic adapter selection: real `mega-get` when available, fake downloader fallback for development
- Polling JSON API for live status updates every 1500 ms
- Safe file explorer rooted inside configured destinations only
- ZIP extraction with `zipfile` and AES/password support via `pyzipper`
- Cancel and retry job actions

## Project Layout

- `app.py`: app factory, routes, filters
- `models.py`: dataclasses for jobs and explorer entries
- `downloader.py`: queue manager plus MEGAcmd/fake adapters
- `archives.py`: secure ZIP extraction helpers
- `explorer.py`: safe file browser helpers
- `storage.py`: JSON persistence
- `templates/`: dashboard and explorer views
- `static/`: CSS and polling UI logic

## Quick Start

1. Create and activate a virtual environment.
2. Install Python dependencies.
3. Edit `config.py` or point `MEGA_DOWNLOADER_CONFIG` at a copy of `config.example.py`.
4. Start the app.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

The default app binds to `0.0.0.0:8080`.

## Configuration

`config.py` defines the allowed roots:

```python
ALLOWED_DESTINATIONS = {
    "downloads": {"label": "Downloads", "path": Path("/srv/mega-downloads")},
    "media": {"label": "Media", "path": Path("/srv/media/incoming")},
}
```

Useful environment variables:

- `MEGA_DOWNLOADER_CONFIG`: absolute path to an alternate config file
- `MEGA_DOWNLOADER_BACKEND`: `auto`, `mega`, or `fake`
- `MEGACMD_BINARY`: override the `mega-get` executable name/path
- `MEGA_DOWNLOADER_HOST`: HTTP bind host
- `MEGA_DOWNLOADER_PORT`: HTTP bind port

## Real MEGA Integration

Install MEGAcmd inside the LXC and keep `mega-get` on `PATH`. In `auto` mode the app will use MEGAcmd when available. If `mega-get` is missing, submissions are rejected with a clear error instead of silently generating fake files. Use `MEGA_DOWNLOADER_BACKEND=fake` only when you intentionally want the simulator for UI testing.

Example fake ZIP URLs for local testing:

- `https://example.local/sample.zip`
- `https://example.local/encrypted.zip?pw=secret123`

## JSON API

- `GET /api/jobs`: queue summary plus job and batch details
- `GET /api/explorer?root=downloads&path=subdir`: safe explorer payload

## systemd

The included [`flask-mega-downloader.service`](/c:/Users/mkrbl/Documents/VSCODE/FlaskMegaDownloader/flask-mega-downloader.service) runs the app with Waitress. Adjust the paths for your deployment.

## Security Notes

- Explorer navigation is restricted with `Path.resolve()` checks under configured roots only.
- Archive extraction validates every output path before writing to prevent zip slip.
- Download commands use argument lists, not shell expansion.
