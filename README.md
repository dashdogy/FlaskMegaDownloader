# Flask Mega Downloader

Lightweight Flask UI for queueing public MEGA links to configured local folders, monitoring transfer status over JSON polling, browsing only approved download roots, and extracting normal or AES-encrypted ZIP archives safely.

## Proxmox LXC Install Or Update

For an existing Debian or Ubuntu Proxmox LXC, run:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/dashdogy/FlaskMegaDownloader/master/install/proxmox-helper.sh)"
```

The helper script is idempotent:

- First run installs the app into `/opt/flask-mega-downloader`
- Later runs update the managed checkout from GitHub, refresh the virtualenv, reinstall the systemd unit, and restart the service
- `/etc/flask-mega-downloader/config.py` is created only once and then preserved across updates
- MEGAcmd is installed from MEGA's official APT repo and the script prompts for `mega-login` if `www-data` is not already signed in
- Existing conflicting MEGA APT source entries are normalized automatically before package installation

Supported LXC guest OS versions:

- Debian 11, 12, 13
- Ubuntu 20.04, 22.04, 24.04

## Features

- Server-rendered Flask app with a Homepage-inspired soft card UI
- Multi-link submission with whitespace trimming, blank-line removal, and per-batch deduplication
- Background worker thread with persisted JSON job history
- Real `mega-get` support via MEGAcmd, plus an explicit fake backend for development only
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

The repo also includes [`install/proxmox-helper.sh`](/c:/Users/mkrbl/Documents/VSCODE/FlaskMegaDownloader/install/proxmox-helper.sh) for installing or updating the app inside an existing Proxmox LXC.

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

The Proxmox helper installs MEGAcmd and configures a persistent `HOME=/var/www` for the `www-data` service account so the MEGAcmd session survives restarts.

If no session exists, the helper prompts for:

```bash
runuser -u www-data -- env HOME=/var/www mega-login
```

To check the current login state later:

```bash
runuser -u www-data -- env HOME=/var/www mega-whoami
```

In `auto` mode the app uses MEGAcmd when `mega-get` is available. If it is missing, submissions are rejected with a clear error instead of silently generating fake files. Use `MEGA_DOWNLOADER_BACKEND=fake` only when you intentionally want the simulator for UI testing.

Example fake ZIP URLs for local testing:

- `https://example.local/sample.zip`
- `https://example.local/encrypted.zip?pw=secret123`

## JSON API

- `GET /api/jobs`: queue summary plus job and batch details
- `GET /api/explorer?root=downloads&path=subdir`: safe explorer payload

## systemd

The included [`flask-mega-downloader.service`](/c:/Users/mkrbl/Documents/VSCODE/FlaskMegaDownloader/flask-mega-downloader.service) runs the app with Waitress as `www-data`, using:

- `WorkingDirectory=/opt/flask-mega-downloader`
- `MEGA_DOWNLOADER_CONFIG=/etc/flask-mega-downloader/config.py`
- `HOME=/var/www`

Useful recovery commands:

```bash
systemctl status flask-mega-downloader
runuser -u www-data -- env HOME=/var/www mega-whoami
runuser -u www-data -- env HOME=/var/www mega-login
```

## Security Notes

- Explorer navigation is restricted with `Path.resolve()` checks under configured roots only.
- Archive extraction validates every output path before writing to prevent zip slip.
- Download commands use argument lists, not shell expansion.
