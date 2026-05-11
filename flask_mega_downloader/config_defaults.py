from __future__ import annotations

import os
from typing import Any


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError:
            value = default
    return max(minimum, min(maximum, value))


def apply_runtime_defaults(config: dict[str, Any]) -> None:
    """Fill in new rewrite-era config keys for old preserved config files."""

    config.setdefault("AUTH_ENABLED", _env_bool("MEGA_DOWNLOADER_AUTH_ENABLED", True))
    config.setdefault("ADMIN_USERNAME", os.environ.get("MEGA_DOWNLOADER_ADMIN_USERNAME", "admin"))
    config.setdefault("ADMIN_PASSWORD_HASH", os.environ.get("MEGA_DOWNLOADER_ADMIN_PASSWORD_HASH", ""))
    config.setdefault("SESSION_COOKIE_HTTPONLY", True)
    config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
    config.setdefault("MAX_CONTENT_LENGTH", _env_int("MEGA_DOWNLOADER_MAX_CONTENT_LENGTH", 1024 * 1024, minimum=64 * 1024, maximum=16 * 1024 * 1024))
    config.setdefault("MAX_URLS_PER_SUBMISSION", _env_int("MEGA_DOWNLOADER_MAX_URLS_PER_SUBMISSION", 200, minimum=1, maximum=2000))
    config.setdefault("DOWNLOAD_WORKERS", _env_int("MEGA_DOWNLOADER_DOWNLOAD_WORKERS", 2, minimum=1, maximum=8))
    config.setdefault("ARCHIVE_WORKERS", _env_int("MEGA_DOWNLOADER_ARCHIVE_WORKERS", 1, minimum=1, maximum=4))
    config.setdefault("MEDIA_WORKERS", _env_int("MEGA_DOWNLOADER_MEDIA_WORKERS", 1, minimum=1, maximum=2))
    config.setdefault("PLEX_PERMISSIONS_ENABLED", _env_bool("MEGA_DOWNLOADER_PLEX_PERMISSIONS_ENABLED", True))
    config.setdefault("PLEX_PERMISSION_STRICT", _env_bool("MEGA_DOWNLOADER_PLEX_PERMISSION_STRICT", False))
    config.setdefault("PLEX_USER", os.environ.get("MEGA_DOWNLOADER_PLEX_USER", "plex"))
    config.setdefault("SETFACL_BINARY", os.environ.get("SETFACL_BINARY", "setfacl"))
