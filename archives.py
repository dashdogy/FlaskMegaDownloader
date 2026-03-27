from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pyzipper


class ArchiveError(Exception):
    pass


def _safe_target_path(destination_dir: Path, member_name: str) -> Path:
    candidate = (destination_dir / member_name).resolve()
    root = destination_dir.resolve()
    if candidate != root and root not in candidate.parents:
        raise ArchiveError(f"Archive member '{member_name}' would extract outside the destination.")
    return candidate


def _extract_with_reader(archive, destination_dir: Path, password: bytes | None = None) -> list[str]:
    extracted: list[str] = []
    if password and hasattr(archive, "setpassword"):
        archive.setpassword(password)
    for member in archive.infolist():
        target_path = _safe_target_path(destination_dir, member.filename)
        if member.is_dir():
            target_path.mkdir(parents=True, exist_ok=True)
            continue

        target_path.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, target_path.open("wb") as handle:
            shutil.copyfileobj(source, handle)
        extracted.append(str(target_path))
    return extracted


def extract_archive(archive_path: Path, destination_dir: Path, password: str | None = None) -> list[str]:
    archive_path = archive_path.resolve()
    destination_dir = destination_dir.resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    encoded_password = password.encode("utf-8") if password else None

    zip_error: Exception | None = None
    try:
        with zipfile.ZipFile(archive_path) as archive:
            return _extract_with_reader(archive, destination_dir, password=encoded_password)
    except (RuntimeError, NotImplementedError, zipfile.BadZipFile) as exc:
        zip_error = exc

    try:
        with pyzipper.AESZipFile(archive_path) as archive:
            return _extract_with_reader(archive, destination_dir, password=encoded_password)
    except (RuntimeError, NotImplementedError, zipfile.BadZipFile, pyzipper.zipfile.BadZipFile) as exc:
        raise ArchiveError(str(exc)) from zip_error or exc
