from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path


LOGGER = logging.getLogger(__name__)


class PlexPermissionManager:
    def __init__(
        self,
        *,
        enabled: bool = True,
        plex_user: str = "plex",
        setfacl_binary: str = "setfacl",
        strict: bool = False,
        event_logger=None,
    ) -> None:
        self.enabled = bool(enabled)
        self.plex_user = str(plex_user or "").strip()
        self.setfacl_binary = str(setfacl_binary or "setfacl").strip()
        self.strict = bool(strict)
        self.event_logger = event_logger
        self._user_exists_cache: bool | None = None
        self._reported_unavailable: set[str] = set()

    def grant(self, path: str | Path, *, recursive: bool = True, feature: str = "plex_permissions") -> bool:
        if not self.enabled or not self.plex_user:
            return False

        target_path = Path(path)
        if not target_path.exists():
            return False
        if os.name != "posix":
            self._report_unavailable("unsupported_os", "Plex ACL grants are only supported on POSIX systems.", feature=feature)
            return False
        if not self._user_exists():
            self._report_unavailable(
                "missing_user",
                f"Plex ACL grant skipped because user '{self.plex_user}' does not exist.",
                feature=feature,
            )
            return False

        binary = shutil.which(self.setfacl_binary) or (
            self.setfacl_binary if Path(self.setfacl_binary).exists() else None
        )
        if not binary:
            self._report_unavailable(
                "missing_setfacl",
                f"Plex ACL grant skipped because '{self.setfacl_binary}' is not installed.",
                feature=feature,
            )
            return False

        command = [binary]
        if recursive and target_path.is_dir():
            command.append("-R")
        if target_path.is_dir():
            command.extend(["-m", f"u:{self.plex_user}:rwx", "-m", f"d:u:{self.plex_user}:rwx"])
        else:
            command.extend(["-m", f"u:{self.plex_user}:rw"])
        command.append(str(target_path))

        try:
            subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        except subprocess.CalledProcessError as exc:
            message = f"Plex ACL grant failed for '{target_path}': {exc.stderr.strip() or exc.stdout.strip() or exc}"
            if self.strict:
                raise PermissionError(message) from exc
            self._log("error", feature, message, context={"path": str(target_path), "user": self.plex_user})
            return False

        self._log("debug", feature, "Granted Plex ACL access.", context={"path": str(target_path), "user": self.plex_user})
        return True

    def grant_many(self, paths: list[str | Path], *, feature: str = "plex_permissions") -> int:
        granted = 0
        seen: set[str] = set()
        for raw_path in paths:
            path = Path(raw_path)
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            if self.grant(path, recursive=True, feature=feature):
                granted += 1
        return granted

    def _user_exists(self) -> bool:
        if self._user_exists_cache is True:
            return True
        result = subprocess.run(
            ["id", "-u", self.plex_user],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0:
            self._user_exists_cache = True
            return True
        return False

    def _report_unavailable(self, key: str, message: str, *, feature: str) -> None:
        if key in self._reported_unavailable:
            return
        self._reported_unavailable.add(key)
        if self.strict:
            raise PermissionError(message)
        self._log("warning", feature, message, context={"user": self.plex_user})

    def _log(self, level: str, feature: str, message: str, *, context: dict | None = None) -> None:
        if self.event_logger:
            self.event_logger.log(level, "permissions", feature, message, context=context or {})
            return
        getattr(LOGGER, level, LOGGER.info)(message)
