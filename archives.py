from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable

import pyzipper
from process_utils import stop_process
try:
    import rarfile
except ImportError:  # pragma: no cover - exercised via runtime availability checks
    rarfile = None


class ArchiveError(Exception):
    pass


class ArchiveCanceledError(ArchiveError):
    pass


@dataclass(slots=True)
class ArchiveProbe:
    archive_type: str
    bytes_total: int | None
    entry_count: int


SUPPORTED_ARCHIVE_SUFFIXES = {".zip", ".rar", ".7z"}
SEVEN_ZIP_SPLIT_FIRST_RE = re.compile(r"^(?P<base>.+)\.7z\.001$", re.IGNORECASE)
RAR_PART_RE = re.compile(r"^(?P<base>.+)\.part(?P<index>\d+)\.rar$", re.IGNORECASE)
RAR_OLD_STYLE_PART_RE = re.compile(r"^(?P<base>.+)\.r(?P<index>\d{2,})$", re.IGNORECASE)
ZIP_THREAD_RESERVE = 4
ZIP_CHUNK_SIZE = 1024 * 1024


@dataclass(slots=True)
class ZipMemberTarget:
    member_name: str
    target_path: Path
    size: int


def archive_type_for_path(path: Path) -> str | None:
    name = path.name.lower()
    if SEVEN_ZIP_SPLIT_FIRST_RE.match(name):
        return "7z"
    rar_part_match = RAR_PART_RE.match(name)
    if rar_part_match:
        try:
            part_index = int(rar_part_match.group("index"))
        except ValueError:
            return None
        return "rar" if part_index == 1 else None
    if RAR_OLD_STYLE_PART_RE.match(name):
        return None
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
    rar_part_match = RAR_PART_RE.match(path.name)
    if rar_part_match:
        return rar_part_match.group("base")
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


def _raise_if_canceled(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested and cancel_requested():
        raise ArchiveCanceledError("Archive extraction canceled.")


def _read_text_if_exists(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _affinity_cpu_count() -> int | None:
    sched_getaffinity = getattr(os, "sched_getaffinity", None)
    if sched_getaffinity is None:
        return None
    try:
        affinity = sched_getaffinity(0)
    except OSError:
        return None
    if not affinity:
        return None
    return max(1, len(affinity))


def _cgroup_v2_cpu_count(cgroup_root: Path) -> int | None:
    payload = _read_text_if_exists(cgroup_root / "cpu.max")
    if not payload:
        return None
    parts = payload.split()
    if len(parts) < 2 or parts[0].lower() == "max":
        return None
    try:
        quota = int(parts[0])
        period = int(parts[1])
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return max(1, math.ceil(quota / period))


def _cgroup_v1_cpu_count(cgroup_root: Path) -> int | None:
    candidates = [cgroup_root / "cpu", cgroup_root]
    for candidate in candidates:
        quota_text = _read_text_if_exists(candidate / "cpu.cfs_quota_us")
        period_text = _read_text_if_exists(candidate / "cpu.cfs_period_us")
        if quota_text is None or period_text is None:
            continue
        try:
            quota = int(quota_text)
            period = int(period_text)
        except ValueError:
            continue
        if quota <= 0 or period <= 0:
            return None
        return max(1, math.ceil(quota / period))
    return None


def detect_effective_cpu_count(*, cgroup_root: Path = Path("/sys/fs/cgroup")) -> int:
    detected_counts = [
        count
        for count in (
            _affinity_cpu_count(),
            _cgroup_v2_cpu_count(cgroup_root),
            _cgroup_v1_cpu_count(cgroup_root),
        )
        if count is not None
    ]
    if detected_counts:
        return max(1, min(detected_counts))
    return max(1, os.cpu_count() or 1)


def zip_extraction_worker_count(
    *,
    effective_cpu_count: int | None = None,
    reserve_threads: int = ZIP_THREAD_RESERVE,
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> int:
    detected_threads = effective_cpu_count if effective_cpu_count is not None else detect_effective_cpu_count(cgroup_root=cgroup_root)
    return max(1, max(int(detected_threads), 1) - max(int(reserve_threads), 0))


def _prepare_archive_targets(members: list, destination_dir: Path) -> tuple[list[Path], list[ZipMemberTarget], int]:
    directory_targets: list[Path] = []
    file_targets: list[ZipMemberTarget] = []
    seen_targets: dict[str, str] = {}
    total_bytes = 0

    for member in members:
        member_name = _member_name(member)
        target_path = _safe_target_path(destination_dir, member_name)
        dedupe_key = os.path.normcase(str(target_path))
        previous_member = seen_targets.get(dedupe_key)
        if previous_member is not None:
            raise ArchiveError(
                f"Archive members '{previous_member}' and '{member_name}' resolve to the same destination."
            )
        seen_targets[dedupe_key] = member_name

        if _member_is_dir(member):
            directory_targets.append(target_path)
            continue

        size = _member_size(member)
        file_targets.append(
            ZipMemberTarget(
                member_name=member_name,
                target_path=target_path,
                size=size,
            )
        )
        total_bytes += size

    return directory_targets, file_targets, total_bytes


def _ensure_target_directories(destination_dir: Path, directory_targets: list[Path], file_targets: list[ZipMemberTarget]) -> None:
    parent_targets = {entry.target_path.parent for entry in file_targets}
    all_directories = sorted(
        {destination_dir, *directory_targets, *parent_targets},
        key=lambda item: (len(item.parts), str(item)),
    )
    for directory in all_directories:
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except FileExistsError as exc:
            relative_name = directory.relative_to(destination_dir).as_posix() if directory != destination_dir else "."
            raise ArchiveError(f"Archive member '{relative_name}' conflicts with an existing file.") from exc


def _extract_zip_member_with_archive(
    archive_factory,
    archive_path: Path,
    entry: ZipMemberTarget,
    *,
    password: bytes | None = None,
    progress_callback: Callable[..., None] | None = None,
    progress_lock: threading.Lock | None = None,
    progress_state: dict[str, int] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> None:
    try:
        with archive_factory(archive_path) as archive:
            if password and hasattr(archive, "setpassword"):
                archive.setpassword(password)
            with archive.open(entry.member_name) as source, entry.target_path.open("wb") as handle:
                while True:
                    _raise_if_canceled(cancel_requested)
                    chunk = source.read(ZIP_CHUNK_SIZE)
                    if not chunk:
                        break
                    handle.write(chunk)
                    if progress_callback and progress_lock is not None and progress_state is not None:
                        with progress_lock:
                            progress_state["bytes_done"] += len(chunk)
                            progress_callback(
                                bytes_done=progress_state["bytes_done"],
                                bytes_total=progress_state["bytes_total"],
                            )
    except Exception:
        entry.target_path.unlink(missing_ok=True)
        raise


def _extract_single_zip_member(
    archive_path: Path,
    entry: ZipMemberTarget,
    *,
    password: bytes | None = None,
    progress_callback: Callable[..., None] | None = None,
    progress_lock: threading.Lock | None = None,
    progress_state: dict[str, int] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> None:
    zip_error: Exception | None = None
    try:
        _extract_zip_member_with_archive(
            zipfile.ZipFile,
            archive_path,
            entry,
            password=password,
            progress_callback=progress_callback,
            progress_lock=progress_lock,
            progress_state=progress_state,
            cancel_requested=cancel_requested,
        )
        return
    except (RuntimeError, NotImplementedError, zipfile.BadZipFile, KeyError) as exc:
        zip_error = exc

    try:
        _extract_zip_member_with_archive(
            pyzipper.AESZipFile,
            archive_path,
            entry,
            password=password,
            progress_callback=progress_callback,
            progress_lock=progress_lock,
            progress_state=progress_state,
            cancel_requested=cancel_requested,
        )
    except (RuntimeError, NotImplementedError, zipfile.BadZipFile, pyzipper.zipfile.BadZipFile, KeyError) as exc:
        raise ArchiveError(str(exc)) from zip_error or exc


def _extract_zip_archive(
    archive_path: Path,
    destination_dir: Path,
    *,
    password: str | None = None,
    progress_callback: Callable[..., None] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> list[str]:
    encoded_password = password.encode("utf-8") if password else None

    try:
        with zipfile.ZipFile(archive_path) as archive:
            members = _archive_members(archive)
    except (RuntimeError, NotImplementedError, zipfile.BadZipFile):
        try:
            with pyzipper.AESZipFile(archive_path) as archive:
                members = _archive_members(archive)
        except (RuntimeError, NotImplementedError, zipfile.BadZipFile, pyzipper.zipfile.BadZipFile) as exc:
            raise ArchiveError(str(exc)) from exc

    directory_targets, file_targets, total_bytes = _prepare_archive_targets(members, destination_dir)
    _ensure_target_directories(destination_dir, directory_targets, file_targets)

    worker_count = min(zip_extraction_worker_count(), max(len(file_targets), 1))
    if progress_callback:
        thread_label = "thread" if worker_count == 1 else "threads"
        progress_callback(message=f"Using {worker_count} ZIP extraction {thread_label}.")
        progress_callback(bytes_done=0, bytes_total=total_bytes)

    progress_lock = threading.Lock()
    progress_state = {
        "bytes_done": 0,
        "bytes_total": total_bytes,
    }

    if worker_count == 1 or len(file_targets) <= 1:
        for entry in file_targets:
            _raise_if_canceled(cancel_requested)
            _extract_single_zip_member(
                archive_path,
                entry,
                password=encoded_password,
                progress_callback=progress_callback,
                progress_lock=progress_lock,
                progress_state=progress_state,
                cancel_requested=cancel_requested,
            )
        return [str(entry.target_path) for entry in file_targets]

    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="zip-extract") as executor:
        futures = [
            executor.submit(
                _extract_single_zip_member,
                archive_path,
                entry,
                password=encoded_password,
                progress_callback=progress_callback,
                progress_lock=progress_lock,
                progress_state=progress_state,
                cancel_requested=cancel_requested,
            )
            for entry in file_targets
        ]
        for future in as_completed(futures):
            future.result()

    return [str(entry.target_path) for entry in file_targets]


def _extract_with_reader(
    archive,
    destination_dir: Path,
    password: bytes | str | None = None,
    *,
    progress_callback: Callable[..., None] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
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
        _raise_if_canceled(cancel_requested)
        member_name = _member_name(member)
        target_path = _safe_target_path(destination_dir, member_name)
        if _member_is_dir(member):
            target_path.mkdir(parents=True, exist_ok=True)
            continue

        target_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with archive.open(member) as source, target_path.open("wb") as handle:
                while True:
                    _raise_if_canceled(cancel_requested)
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    copied_bytes += len(chunk)
                    if progress_callback:
                        progress_callback(bytes_done=copied_bytes, bytes_total=total_bytes)
        except Exception:
            target_path.unlink(missing_ok=True)
            raise
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
    cancel_requested: Callable[[], bool] | None = None,
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
            if cancel_requested and cancel_requested():
                stop_process(process)
                raise ArchiveCanceledError("Archive extraction canceled.")
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
    cancel_requested: Callable[[], bool] | None = None,
) -> list[str]:
    archive_path = archive_path.resolve()
    destination_dir = destination_dir.resolve()
    destination_dir.mkdir(parents=True, exist_ok=True)
    archive_type = archive_type_for_path(archive_path)
    if archive_type is None:
        raise ArchiveError("Only zip, rar, and 7z archives are supported.")

    if archive_type == "zip":
        return _extract_zip_archive(
            archive_path,
            destination_dir,
            password=password,
            progress_callback=progress_callback,
            cancel_requested=cancel_requested,
        )

    if archive_type == "7z":
        return _extract_7z_archive(
            archive_path,
            destination_dir,
            seven_zip_binary=seven_zip_binary,
            password=password,
            progress_callback=progress_callback,
            cancel_requested=cancel_requested,
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
                cancel_requested=cancel_requested,
            )
    except rarfile.Error as exc:
        message = str(exc)
        lower_message = message.lower()
        if "cannot find working tool" in lower_message or "no suitable tool" in lower_message:
            message = f"{message}. Install 'unar', 'unrar', or '7z' on the server."
        raise ArchiveError(message) from exc
