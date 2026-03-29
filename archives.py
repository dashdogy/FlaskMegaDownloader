from __future__ import annotations

import re
import shutil
import subprocess
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable

import pyzipper
try:
    import rarfile
except ImportError:  # pragma: no cover - exercised via runtime availability checks
    rarfile = None


class ArchiveError(Exception):
    pass


@dataclass(slots=True)
class ArchiveProbe:
    archive_type: str
    bytes_total: int | None
    entry_count: int


SUPPORTED_ARCHIVE_SUFFIXES = {".zip", ".rar", ".7z"}
SEVEN_ZIP_SPLIT_FIRST_RE = re.compile(r"^(?P<base>.+)\.7z\.001$", re.IGNORECASE)


def archive_type_for_path(path: Path) -> str | None:
    name = path.name.lower()
    if SEVEN_ZIP_SPLIT_FIRST_RE.match(name):
        return "7z"
    suffix = path.suffix.lower()
    if suffix in SUPPORTED_ARCHIVE_SUFFIXES:
        return suffix[1:]
    return None


def is_supported_archive_path(path: Path) -> bool:
    return path.is_file() and archive_type_for_path(path) is not None


def default_archive_target_name(path: Path) -> str:
    match = SEVEN_ZIP_SPLIT_FIRST_RE.match(path.name)
    if match:
        return match.group("base")
    archive_type = archive_type_for_path(path)
    if archive_type is None:
        raise ArchiveError("Only zip, rar, and 7z archives are supported.")
    return path.with_suffix("").name


def _safe_target_path(destination_dir: Path, member_name: str) -> Path:
    normalized_member_name = member_name.replace("\\", "/")
    candidate = (destination_dir / normalized_member_name).resolve()
    root = destination_dir.resolve()
    if candidate != root and root not in candidate.parents:
        raise ArchiveError(f"Archive member '{member_name}' would extract outside the destination.")
    return candidate


def _remove_existing_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    if path.is_dir():
        shutil.rmtree(path)
        return
    path.unlink(missing_ok=True)


def _member_name(member) -> str:
    name = getattr(member, "filename", None) or getattr(member, "name", None)
    if not name:
        raise ArchiveError("Archive entry is missing a filename.")
    return str(name)


def _member_is_dir(member) -> bool:
    if hasattr(member, "is_dir"):
        return bool(member.is_dir())
    if hasattr(member, "isdir"):
        return bool(member.isdir())
    return _member_name(member).endswith("/")


def _member_size(member) -> int:
    for attribute in ("file_size", "unpacked_size", "size"):
        value = getattr(member, attribute, None)
        if value is not None:
            try:
                return max(int(value), 0)
            except (TypeError, ValueError):
                return 0
    return 0


def _archive_members(archive) -> list:
    return list(archive.infolist())


def _extract_with_reader(
    archive,
    destination_dir: Path,
    password: bytes | str | None = None,
    *,
    progress_callback: Callable[..., None] | None = None,
) -> list[str]:
    extracted: list[str] = []
    if password and hasattr(archive, "setpassword"):
        archive.setpassword(password)
    members = _archive_members(archive)
    total_bytes = sum(_member_size(member) for member in members if not _member_is_dir(member))
    copied_bytes = 0
    if progress_callback:
        progress_callback(bytes_done=0, bytes_total=total_bytes)
    for member in members:
        member_name = _member_name(member)
        target_path = _safe_target_path(destination_dir, member_name)
        if _member_is_dir(member):
            target_path.mkdir(parents=True, exist_ok=True)
            continue

        target_path.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, target_path.open("wb") as handle:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                copied_bytes += len(chunk)
                if progress_callback:
                    progress_callback(bytes_done=copied_bytes, bytes_total=total_bytes)
        extracted.append(str(target_path))
    return extracted


def _run_7z_command(binary: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            [binary, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ArchiveError(f"7z extraction support is unavailable because '{binary}' was not found on PATH.") from exc

    if result.returncode != 0:
        message = (result.stderr or result.stdout or "7z command failed").strip()
        raise ArchiveError(message)
    return result


def _list_7z_entries(
    archive_path: Path,
    *,
    seven_zip_binary: str,
    password: str | None = None,
) -> list[dict[str, int | str | bool]]:
    args = ["l", "-slt", str(archive_path)]
    if password:
        args.insert(1, f"-p{password}")
    result = _run_7z_command(seven_zip_binary, args)
    output = result.stdout or ""
    entries_section = output.split("----------", 1)[1] if "----------" in output else output
    entries: list[dict[str, int | str | bool]] = []
    current: dict[str, str] = {}
    for raw_line in entries_section.splitlines():
        line = raw_line.strip()
        if not line:
            if current.get("Path"):
                member_name = current["Path"].strip()
                attributes = current.get("Attributes", "").strip()
                is_dir = attributes.startswith("D") or member_name.endswith("/")
                try:
                    size = int(current.get("Size", "0") or 0)
                except ValueError:
                    size = 0
                entries.append(
                    {
                        "name": member_name,
                        "size": 0 if is_dir else size,
                        "is_dir": is_dir,
                    }
                )
            current = {}
            continue
        if " = " not in line:
            continue
        key, value = line.split(" = ", 1)
        current[key.strip()] = value.strip()
    if current.get("Path"):
        member_name = current["Path"].strip()
        attributes = current.get("Attributes", "").strip()
        is_dir = attributes.startswith("D") or member_name.endswith("/")
        try:
            size = int(current.get("Size", "0") or 0)
        except ValueError:
            size = 0
        entries.append({"name": member_name, "size": 0 if is_dir else size, "is_dir": is_dir})
    if not entries:
        raise ArchiveError("7z did not report any archive entries.")
    return entries


def _probe_reader_archive(archive, *, password: bytes | str | None = None) -> ArchiveProbe:
    if password and hasattr(archive, "setpassword"):
        archive.setpassword(password)
    members = _archive_members(archive)
    total_bytes = sum(_member_size(member) for member in members if not _member_is_dir(member))
    entry_count = sum(1 for member in members if not _member_is_dir(member))
    return ArchiveProbe(archive_type="unknown", bytes_total=total_bytes, entry_count=entry_count)


def probe_archive(archive_path: Path, *, password: str | None = None, seven_zip_binary: str = "7z") -> ArchiveProbe:
    archive_path = archive_path.resolve()
    archive_type = archive_type_for_path(archive_path)
    if archive_type is None:
        raise ArchiveError("Only zip, rar, and 7z archives are supported.")

    if archive_type == "zip":
        encoded_password = password.encode("utf-8") if password else None
        try:
            with zipfile.ZipFile(archive_path) as archive:
                probe = _probe_reader_archive(archive, password=encoded_password)
                probe.archive_type = "zip"
                return probe
        except (RuntimeError, NotImplementedError, zipfile.BadZipFile):
            with pyzipper.AESZipFile(archive_path) as archive:
                probe = _probe_reader_archive(archive, password=encoded_password)
                probe.archive_type = "zip"
                return probe

    if archive_type == "7z":
        entries = _list_7z_entries(archive_path, seven_zip_binary=seven_zip_binary, password=password)
        total_bytes = sum(int(entry["size"]) for entry in entries if not entry["is_dir"])
        entry_count = sum(1 for entry in entries if not entry["is_dir"])
        return ArchiveProbe(archive_type="7z", bytes_total=total_bytes, entry_count=entry_count)

    if rarfile is None:
        raise ArchiveError("RAR extraction support is unavailable because the 'rarfile' package is not installed.")
    with rarfile.RarFile(archive_path) as archive:
        probe = _probe_reader_archive(archive, password=password)
        probe.archive_type = "rar"
        return probe


def _promote_staged_tree(staging_dir: Path, destination_dir: Path) -> list[str]:
    extracted: list[str] = []
    for staged_path in sorted(staging_dir.rglob("*"), key=lambda item: (len(item.parts), str(item))):
        relative_name = staged_path.relative_to(staging_dir).as_posix()
        target_path = _safe_target_path(destination_dir, relative_name)
        if staged_path.is_dir():
            if target_path.exists() and not target_path.is_dir():
                raise ArchiveError(f"Archive member '{relative_name}' conflicts with an existing file.")
            target_path.mkdir(parents=True, exist_ok=True)
            continue

        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            if target_path.is_dir():
                raise ArchiveError(f"Archive member '{relative_name}' conflicts with an existing directory.")
            _remove_existing_path(target_path)
        shutil.move(str(staged_path), str(target_path))
        extracted.append(str(target_path))
    return extracted


def _directory_size_bytes(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
    return total


def _extract_7z_archive(
    archive_path: Path,
    destination_dir: Path,
    *,
    seven_zip_binary: str,
    password: str | None = None,
    progress_callback: Callable[..., None] | None = None,
) -> list[str]:
    entries = _list_7z_entries(archive_path, seven_zip_binary=seven_zip_binary, password=password)
    total_bytes = sum(int(entry["size"]) for entry in entries if not entry["is_dir"])
    for entry in entries:
        _safe_target_path(destination_dir, str(entry["name"]))

    with TemporaryDirectory(prefix="sevenzip-stage-", dir=destination_dir.parent) as staging_dir_text:
        staging_dir = Path(staging_dir_text).resolve()
        args = ["x", "-y", f"-o{staging_dir}", str(archive_path)]
        if password:
            args.insert(1, f"-p{password}")
        try:
            process = subprocess.Popen(
                [seven_zip_binary, *args],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as exc:
            raise ArchiveError(f"7z extraction support is unavailable because '{seven_zip_binary}' was not found on PATH.") from exc

        if progress_callback:
            progress_callback(bytes_done=0, bytes_total=total_bytes)
        while process.poll() is None:
            if progress_callback:
                progress_callback(
                    bytes_done=min(_directory_size_bytes(staging_dir), total_bytes),
                    bytes_total=total_bytes,
                )
            time.sleep(0.25)
        _, stderr_output = process.communicate()
        if process.returncode != 0:
            message = (stderr_output or "").strip() or f"7z extraction failed with exit code {process.returncode}."
            raise ArchiveError(message)
        if progress_callback:
            progress_callback(bytes_done=total_bytes, bytes_total=total_bytes)
        return _promote_staged_tree(staging_dir, destination_dir)


def extract_archive(
    archive_path: Path,
    destination_dir: Path,
    password: str | None = None,
    *,
    seven_zip_binary: str = "7z",
    progress_callback: Callable[..., None] | None = None,
) -> list[str]:
    archive_path = archive_path.resolve()
    destination_dir = destination_dir.resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    archive_type = archive_type_for_path(archive_path)
    if archive_type is None:
        raise ArchiveError("Only zip, rar, and 7z archives are supported.")

    if archive_type == "zip":
        encoded_password = password.encode("utf-8") if password else None

        zip_error: Exception | None = None
        try:
            with zipfile.ZipFile(archive_path) as archive:
                return _extract_with_reader(
                    archive,
                    destination_dir,
                    password=encoded_password,
                    progress_callback=progress_callback,
                )
        except (RuntimeError, NotImplementedError, zipfile.BadZipFile) as exc:
            zip_error = exc

        try:
            with pyzipper.AESZipFile(archive_path) as archive:
                return _extract_with_reader(
                    archive,
                    destination_dir,
                    password=encoded_password,
                    progress_callback=progress_callback,
                )
        except (RuntimeError, NotImplementedError, zipfile.BadZipFile, pyzipper.zipfile.BadZipFile) as exc:
            raise ArchiveError(str(exc)) from zip_error or exc

    if archive_type == "7z":
        return _extract_7z_archive(
            archive_path,
            destination_dir,
            seven_zip_binary=seven_zip_binary,
            password=password,
            progress_callback=progress_callback,
        )

    if rarfile is None:
        raise ArchiveError("RAR extraction support is unavailable because the 'rarfile' package is not installed.")

    try:
        with rarfile.RarFile(archive_path) as archive:
            return _extract_with_reader(
                archive,
                destination_dir,
                password=password,
                progress_callback=progress_callback,
            )
    except rarfile.Error as exc:
        message = str(exc)
        lower_message = message.lower()
        if "cannot find working tool" in lower_message or "no suitable tool" in lower_message:
            message = f"{message}. Install 'unar', 'unrar', or '7z' on the server."
        raise ArchiveError(message) from exc
