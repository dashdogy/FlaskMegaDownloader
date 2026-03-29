# Flask Mega Downloader

Lightweight Flask UI for queueing public MEGA links to configured local folders, monitoring transfer status over JSON polling, browsing only approved download roots, extracting normal or AES-encrypted ZIP archives safely, and remuxing decrypted Blu-ray folder backups into MKV files.

## Proxmox LXC Install Or Update

For an existing Debian or Ubuntu Proxmox LXC, run:

```bash
bash -c "$(curl -H 'Cache-Control: no-cache' -fsSL https://raw.githubusercontent.com/dashdogy/FlaskMegaDownloader/master/install/proxmox-helper.sh)"
```

If you have just pushed helper-script changes and want to avoid stale raw GitHub content, use the `Cache-Control: no-cache` header as shown above. If needed, you can also pin the raw URL to a specific commit SHA.

The helper script is idempotent:

- First run installs the app into `/opt/flask-mega-downloader`
- Later runs update the managed checkout from GitHub, refresh the virtualenv, reinstall the systemd unit, and restart the service
- `/etc/flask-mega-downloader/config.py` is created only once and then preserved across updates
- MEGAcmd is installed from MEGA's official APT repo and the script prompts for `mega-login` if `www-data` is not already signed in
- `mediainfo` is installed automatically and the helper makes a best-effort attempt to build and install MakeMKV CLI from the official MakeMKV download site
- If MakeMKV still needs manual registration, beta-key activation, or a manual fix, the helper warns and finishes without failing the whole app install
- If the systemd service is not already enabled, the helper prompts to enable it so the app starts automatically after LXC reboot
- Existing conflicting MEGA APT source entries are normalized automatically before package installation

Supported LXC guest OS versions:

- Debian 11, 12, 13
- Ubuntu 20.04, 22.04, 24.04

## Features

- Server-rendered Flask app with a Homepage-inspired soft card UI
- Multi-link submission with whitespace trimming, blank-line removal, and per-batch deduplication
- Background worker threads with SQLite-backed persisted state
- Real `mega-get` support via MEGAcmd, plus an explicit fake backend for development only
- Separate Blu-ray remux queue using MakeMKV CLI plus MediaInfo verification
- Polling JSON API for live status updates every 1500 ms
- Safe file explorer rooted inside configured destinations only
- ZIP extraction with `zipfile` and AES/password support via `pyzipper`
- Explorer-driven Blu-ray remux submission for BDMV folder backups only
- Pause, resume, cancel, and retry job actions
- Custom destination paths, including a saved favorites list for paths you reuse

## Project Layout

- `app.py`: app factory, routes, filters
- `server.py`: Waitress launcher that reads host/port from app config
- `models.py`: dataclasses for jobs and explorer entries
- `downloader.py`: queue manager plus MEGAcmd/fake adapters
- `media_compiler.py`: Blu-ray scan, remux, and verification queue
- `archives.py`: secure ZIP extraction helpers
- `explorer.py`: safe file browser helpers
- `process_utils.py`: shared subprocess shutdown helpers
- `storage.py`: SQLite state storage plus one-time `jobs.json` migration
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

The default app binds to `0.0.0.0:8090`.

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
- `MAKEMKVCON_BINARY`: override the `makemkvcon` executable name/path
- `MEDIAINFO_BINARY`: override the `mediainfo` executable name/path
- `MEGA_DOWNLOADER_BLURAY_MIN_TITLE_SECONDS`: minimum title length used when auto-selecting the main feature
- `MEGA_DOWNLOADER_HOST`: HTTP bind host
- `MEGA_DOWNLOADER_PORT`: HTTP bind port

Useful config keys:

- `STATE_DB_FILE`: primary SQLite runtime state path
- `JOB_STORAGE_FILE`: legacy JSON migration source for first boot after upgrade

Custom absolute download paths are supported, and you can save reusable custom paths into the destination dropdown from the dashboard. The running app user must be able to create and write to that directory. In the packaged systemd setup, that user is `www-data`. If a custom path is not writable, the app shows a fix-up hint you can run on the host.

Runtime state now lives in `STATE_DB_FILE` as SQLite. On first boot after upgrading, the app imports the legacy `jobs.json` automatically if present, then renames it so the migration is not retried. A corrupt legacy JSON file is quarantined and the app starts with empty state instead of crashing.

## Real MEGA Integration

The Proxmox helper installs MEGAcmd and configures a persistent `HOME=/var/www` for the `www-data` service account so the MEGAcmd session survives restarts.

If no session exists, the helper interactively prompts for:

- MEGA email
- MEGA password
- optional MFA code

Manual login later uses the MEGAcmd CLI form that requires explicit arguments:

```bash
runuser -u www-data -- env HOME=/var/www mega-login your@email.example 'your-password'
```

To check the current login state later:

```bash
runuser -u www-data -- env HOME=/var/www mega-whoami
```

In `auto` mode the app uses MEGAcmd when `mega-get` is available. If it is missing, submissions are rejected with a clear error instead of silently generating fake files. Use `MEGA_DOWNLOADER_BACKEND=fake` only when you intentionally want the simulator for UI testing.

Example fake ZIP URLs for local testing:

- `https://example.local/sample.zip`
- `https://example.local/encrypted.zip?pw=secret123`

## Blu-ray Remux

Blu-ray remux jobs are submitted from the explorer by selecting one or more decrypted Blu-ray folders and using `Compile Selected Blu-rays`.

v1 behavior:

- Source type is BDMV-folder backups only
- The app auto-selects the main feature by minimum duration, then longest duration, then largest size, then highest title id
- Output is a lossless MKV remux with all tracks kept
- Output goes to the selected configured destination or a custom absolute path
- Existing output files are not overwritten
- Completed jobs are verified with MediaInfo, and the dashboard records Dolby Vision and Dolby Atmos when present

If either `makemkvcon` or `mediainfo` is missing, the dashboard and explorer show `Blu-ray backend unavailable` with the reason instead of queueing broken jobs.

## JSON API

- `GET /api/jobs`: queue summary plus job and batch details, plus Blu-ray remux queue data
- `GET /api/explorer?root=downloads&path=subdir`: safe explorer payload

## systemd

The included [`flask-mega-downloader.service`](/c:/Users/mkrbl/Documents/VSCODE/FlaskMegaDownloader/flask-mega-downloader.service) runs the app with Waitress as `www-data`, using:

- `WorkingDirectory=/opt/flask-mega-downloader`
- `MEGA_DOWNLOADER_CONFIG=/etc/flask-mega-downloader/config.py`
- `HOME=/var/www`
- `/opt/flask-mega-downloader/server.py` as the Waitress launcher, so `HOST` and `PORT` come from app config instead of being duplicated in the unit file

Useful recovery commands:

```bash
systemctl status flask-mega-downloader
runuser -u www-data -- env HOME=/var/www mega-whoami
runuser -u www-data -- env HOME=/var/www mega-login your@email.example 'your-password'
makemkvcon --help
mediainfo --Version
```

## Security Notes

- Explorer navigation is restricted with `Path.resolve()` checks under configured roots only.
- Archive extraction validates every output path before writing to prevent zip slip.
- Download commands use argument lists, not shell expansion.
