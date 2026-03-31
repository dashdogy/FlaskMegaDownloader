from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from typing import Any
from urllib.parse import urlsplit

from models import EventLogEntry
from storage import JsonStorage


URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
SECRET_TOKEN_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|cookie|cookies|authorization|auth(?:entication)?|session)\b\s*[:=]\s*([^\s,;]+)"
)


def _event_level_name(level: int | str) -> str:
    if isinstance(level, str):
        lowered = level.lower().strip()
        if lowered in {"debug", "info", "warning", "error"}:
            return lowered
        if lowered == "warn":
            return "warning"
        return "info"
    if level >= logging.ERROR:
        return "error"
    if level >= logging.WARNING:
        return "warning"
    if level >= logging.INFO:
        return "info"
    return "debug"


def _subsystem_for_logger(logger_name: str) -> str:
    lowered = (logger_name or "").lower()
    if "downloader" in lowered:
        return "download"
    if "archive" in lowered:
        return "archive"
    if "media" in lowered or "makemkv" in lowered:
        return "bluray"
    if "filecrypt" in lowered:
        return "filecrypt"
    if "storage" in lowered:
        return "storage"
    if "explorer" in lowered:
        return "explorer"
    return "app"


class EventLogService:
    def __init__(self, storage: JsonStorage, *, max_rows: int = 5000):
        self.storage = storage
        self.max_rows = int(max_rows)

    def debug(self, subsystem: str, feature: str, message: str, **kwargs) -> EventLogEntry:
        return self.log("debug", subsystem, feature, message, **kwargs)

    def info(self, subsystem: str, feature: str, message: str, **kwargs) -> EventLogEntry:
        return self.log("info", subsystem, feature, message, **kwargs)

    def warning(self, subsystem: str, feature: str, message: str, **kwargs) -> EventLogEntry:
        return self.log("warning", subsystem, feature, message, **kwargs)

    def error(self, subsystem: str, feature: str, message: str, **kwargs) -> EventLogEntry:
        return self.log("error", subsystem, feature, message, **kwargs)

    def log(
        self,
        level: int | str,
        subsystem: str,
        feature: str,
        message: str,
        *,
        job_id: str | None = None,
        batch_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> EventLogEntry:
        entry = EventLogEntry(
            level=_event_level_name(level),
            subsystem=(subsystem or "app").strip().lower(),
            feature=(feature or "general").strip().lower(),
            message=self.sanitize_text(message),
            job_id=self.sanitize_text(job_id) if job_id else None,
            batch_id=self.sanitize_text(batch_id) if batch_id else None,
            context=self.sanitize_context(context or {}),
        )
        return self.storage.append_event_log(entry, max_rows=self.max_rows)

    def load(self, *, limit: int = 200, after_id: int | None = None) -> list[EventLogEntry]:
        return self.storage.load_event_logs(limit=limit, after_id=after_id)

    @classmethod
    def sanitize_text(cls, value: Any) -> str:
        text = str(value or "")
        text = SECRET_TOKEN_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
        return URL_RE.sub(cls._replace_url, text)

    @classmethod
    def sanitize_context(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key)
                lowered = key_text.lower()
                if lowered in {"password", "passwd", "pwd", "cookie", "cookies", "authorization", "auth", "session"}:
                    sanitized[key_text] = "<redacted>"
                else:
                    sanitized[key_text] = cls.sanitize_context(item)
            return sanitized
        if isinstance(value, (list, tuple, set)):
            return [cls.sanitize_context(item) for item in value]
        if isinstance(value, (int, float, bool)):
            return value
        if isinstance(value, str):
            return cls.sanitize_text(value)
        try:
            json.dumps(value)
        except TypeError:
            return cls.sanitize_text(value)
        return value

    @staticmethod
    def _replace_url(match: re.Match[str]) -> str:
        url = match.group(0)
        parsed = urlsplit(url)
        host = parsed.netloc.lower()
        if "mega." in host:
            label = "mega-url"
        elif "filecrypt." in host:
            label = "filecrypt-url"
        else:
            label = "url"
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        return f"<{label}:{digest}>"


class EventLogBridgeHandler(logging.Handler):
    _state = threading.local()

    def __init__(self, service: EventLogService):
        super().__init__(level=logging.WARNING)
        self.service = service
        self._event_log_bridge = True

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._state, "active", False):
            return
        self._state.active = True
        try:
            context: dict[str, Any] = {"logger": record.name}
            if record.exc_info and record.exc_info[0]:
                context["exception_type"] = record.exc_info[0].__name__
            self.service.log(
                record.levelno,
                _subsystem_for_logger(record.name),
                "logger",
                record.getMessage(),
                context=context,
            )
        finally:
            self._state.active = False


def install_event_log_bridge(service: EventLogService) -> EventLogBridgeHandler:
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        if getattr(handler, "_event_log_bridge", False):
            root_logger.removeHandler(handler)
    bridge = EventLogBridgeHandler(service)
    root_logger.addHandler(bridge)
    return bridge
