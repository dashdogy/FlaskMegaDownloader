from __future__ import annotations

import hashlib
import math
import os
import random
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from pathlib import Path
from pathlib import PureWindowsPath
from queue import Empty, Queue
import shlex
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

import pyzipper

from explorer import normalize_destinations, path_within_root, relative_to_root
from models import ACTIVE_JOB_STATUSES, FavoriteDestination, JOB_STATUSES, RETRYABLE_JOB_STATUSES, Job, TransferStatus, utcnow_iso
from storage import JsonStorage


ProgressCallback = Callable[..., None]
ProcessCallback = Callable[[subprocess.Popen | None], None]


SIZE_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[KMGTP]?i?B)(?:/s)?", re.IGNORECASE)
SIZE_TOKEN_RE = re.compile(r"(?P<size>\d+(?:\.\d+)?)\s*(?P<unit>[KMGTP]?i?B)\b", re.IGNORECASE)
SIZE_PAIR_RE = re.compile(
    r"(?P<done>\d+(?:\.\d+)?)\s*(?P<done_unit>[KMGTP]?i?B)\s*(?:/|of)\s*"
    r"(?P<total>\d+(?:\.\d+)?)\s*(?P<total_unit>[KMGTP]?i?B)\b",
    re.IGNORECASE,
)
TOTAL_SIZE_RE = re.compile(r"\bof\s+(?P<total>\d+(?:\.\d+)?)\s*(?P<unit>[KMGTP]?i?B)\b", re.IGNORECASE)
PERCENT_RE = re.compile(r"(?P<percent>\d{1,3}(?:\.\d+)?)%")
ETA_HMS_RE = re.compile(r"(?P<eta>\d{1,2}:\d{2}(?::\d{2})?)")
ETA_WORD_RE = re.compile(r"(?P<value>\d+)\s*(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?)", re.IGNORECASE)
SPEED_RE = re.compile(r"(?P<speed>\d+(?:\.\d+)?)\s*(?P<unit>[KMGTP]?i?B)/s\b", re.IGNORECASE)
MEGACMD_LS_SUMMARY_RE = re.compile(
    r"^(?P<flags>\S+)\s+(?P<versions>\d+)\s+(?P<size>\d+)\s+(?P<date>\S+)\s+(?P<name>.+)$"
)
MEGACMD_DU_RE = re.compile(r"^\s*(?P<size>\d+)(?:\s+(?P<path>.+?))?\s*$")


class DownloadError(Exception):
    pass


class DownloadCanceled(Exception):
    pass


def current_runtime_user_label() -> str:
    if os.name == "posix":
        try:
            import pwd

            return pwd.getpwuid(os.geteuid()).pw_name
        except (ImportError, KeyError):
            pass
    return os.environ.get("USERNAME") or os.environ.get("USER") or "the app service user"


def permission_fix_hint(destination_path: Path) -> str:
    runtime_user = current_runtime_user_label()
    quoted_path = shlex.quote(str(destination_path))
    return (
        f"Permission denied for destination '{destination_path}'. "
        f"'{runtime_user}' needs write access to that path. "
        f"Fix it with 'sudo mkdir -p {quoted_path}' and either "
        f"'sudo chown -R {runtime_user}:{runtime_user} {quoted_path}' or "
        f"'sudo setfacl -R -m u:{runtime_user}:rwx {quoted_path}'."
    )


def parse_size_to_bytes(value: str | None) -> int | None:
    if not value:
        return None
    match = SIZE_RE.search(value)
    if not match:
        return None
    number = float(match.group("value"))
    unit = match.group("unit").lower()
    scale = {
        "b": 1,
        "kb": 1000,
        "mb": 1000**2,
        "gb": 1000**3,
        "tb": 1000**4,
        "kib": 1024,
        "mib": 1024**2,
        "gib": 1024**3,
        "tib": 1024**4,
    }
    return int(number * scale[unit])


def parse_eta_seconds(text: str | None) -> int | None:
    if not text:
        return None
    hms = ETA_HMS_RE.search(text)
    if hms:
        parts = [int(part) for part in hms.group("eta").split(":")]
        if len(parts) == 2:
            minutes, seconds = parts
            return minutes * 60 + seconds
        hours, minutes, seconds = parts
        return hours * 3600 + minutes * 60 + seconds
    word = ETA_WORD_RE.search(text)
    if not word:
        return None
    value = int(word.group("value"))
    unit = word.group("unit").lower()
    if unit.startswith("hour") or unit.startswith("hr"):
        return value * 3600
    if unit.startswith("minute") or unit.startswith("min"):
        return value * 60
    return value


def infer_display_name(url: str, fallback_prefix: str) -> str:
    parsed = urlparse(url)
    if parsed.netloc.lower().endswith(("mega.nz", "mega.co.nz")):
        return "Resolving file name..."
    name = unquote(Path(parsed.path).name).strip()
    if name:
        return name
    return f"{fallback_prefix}-{parsed.netloc or 'mega'}"


def normalize_remote_display_name(raw_name: str | None) -> str | None:
    if not raw_name:
        return None
    cleaned = raw_name.strip()
    if not cleaned:
        return None
    cleaned = cleaned.rstrip("/")
    if not cleaned:
        return None
    path_name = PureWindowsPath(cleaned).name or PureWindowsPath(cleaned).parts[-1]
    if "/" in cleaned:
        posix_name = cleaned.split("/")[-1].strip()
        if posix_name:
            return posix_name
    return path_name.strip() or cleaned


def normalize_fake_display_name(url: str, fallback_prefix: str) -> str:
    inferred_name = infer_display_name(url, fallback_prefix)
    if inferred_name == "Resolving file name...":
        return fallback_prefix
    return inferred_name


def is_mega_folder_url(url: str) -> bool:
    parsed = urlparse(url)
    combined = f"{parsed.path}#{parsed.fragment}".lower()
    return "/folder/" in combined or combined.startswith("#f!") or "/collection/" in combined


def parse_megacmd_ls_summary(output: str) -> list[dict]:
    entries: list[dict] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("FLAGS"):
            continue
        match = MEGACMD_LS_SUMMARY_RE.match(line)
        if not match:
            fallback_name = normalize_remote_display_name(line)
            if fallback_name and not line.lower().startswith(("info:", "warning:", "error:")):
                entries.append(
                    {
                        "flags": "",
                        "size": 0,
                        "name": fallback_name,
                    }
                )
            continue
        entries.append(
            {
                "flags": match.group("flags"),
                "size": int(match.group("size")),
                "name": match.group("name").strip(),
            }
        )
    return entries


def parse_megacmd_du_summary(output: str) -> tuple[int | None, str | None]:
    size: int | None = None
    path_name: str | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = MEGACMD_DU_RE.match(line)
        if not match:
            continue
        size = int(match.group("size"))
        path_name = normalize_remote_display_name(match.group("path"))
    return size, path_name


def infer_downloaded_display_name(root: Path, before_snapshot: set[str]) -> str | None:
    if not root.exists():
        return None

    created_top_levels: set[str] = set()
    for path in root.rglob("*"):
        relative_path = relative_to_root(root, path)
        if not relative_path or relative_path in before_snapshot:
            continue
        top_level = relative_path.split("/", 1)[0].strip()
        if not top_level or top_level.startswith("."):
            continue
        created_top_levels.add(top_level)

    if not created_top_levels:
        return None

    display_name = sorted(created_top_levels)[0]
    if display_name.lower().endswith(".mega") and len(display_name) > 5:
        return display_name[:-5]
    return display_name


def snapshot_relative_paths(root: Path) -> set[str]:
    if not root.exists():
        return set()

    snapshot: set[str] = set()
    for path in root.rglob("*"):
        snapshot.add(relative_to_root(root, path))
    return snapshot


def cleanup_paths_created_since(root: Path, before_snapshot: set[str]) -> list[Path]:
    if not root.exists():
        return []

    created_paths: list[Path] = []
    for path in root.rglob("*"):
        relative_path = relative_to_root(root, path)
        if relative_path and relative_path not in before_snapshot:
            created_paths.append(path)

    removed_paths: list[Path] = []
    for path in sorted(created_paths, key=lambda item: len(item.parts), reverse=True):
        try:
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink(missing_ok=True)
        except OSError:
            continue
        removed_paths.append(path)
    return removed_paths


def find_megacmd_companion_binary(primary_binary: str, companion_name: str) -> str | None:
    candidates: list[Path | str] = []
    resolved_primary = shutil.which(primary_binary)
    if resolved_primary:
        primary_path = Path(resolved_primary)
        candidates.append(primary_path.with_name(f"{companion_name}{primary_path.suffix}"))
        candidates.append(primary_path.with_name(companion_name))

    primary_path = Path(primary_binary)
    if primary_path.name:
        candidates.append(primary_path.with_name(f"{companion_name}{primary_path.suffix}"))
        candidates.append(primary_path.with_name(companion_name))

    candidates.extend([companion_name, f"{companion_name}.bat"])

    for candidate in candidates:
        candidate_text = str(candidate)
        if Path(candidate_text).exists():
            return candidate_text
        resolved = shutil.which(candidate_text)
        if resolved:
            return resolved
    return None


def run_megacmd_transfers_command(primary_binary: str, action_flag: str) -> None:
    transfers_binary = find_megacmd_companion_binary(primary_binary, "mega-transfers")
    if not transfers_binary:
        return

    try:
        subprocess.run(
            [transfers_binary, action_flag, "-a", "--only-downloads"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def parse_progress_line(line: str) -> dict:
    update: dict = {}
    size_tokens: list[int] = []
    speed_match = SPEED_RE.search(line)
    if speed_match:
        update["speed_bps"] = float(parse_size_to_bytes(speed_match.group(0)) or 0)

    line_without_speed = SPEED_RE.sub("", line)

    size_pair = SIZE_PAIR_RE.search(line_without_speed)
    if size_pair:
        update["bytes_done"] = parse_size_to_bytes(f"{size_pair.group('done')} {size_pair.group('done_unit')}")
        update["bytes_total"] = parse_size_to_bytes(f"{size_pair.group('total')} {size_pair.group('total_unit')}")
    else:
        total_match = TOTAL_SIZE_RE.search(line_without_speed)
        if total_match:
            update["bytes_total"] = parse_size_to_bytes(f"{total_match.group('total')} {total_match.group('unit')}")

        size_tokens = [
            parse_size_to_bytes(f"{match.group('size')} {match.group('unit')}")
            for match in SIZE_TOKEN_RE.finditer(line_without_speed)
        ]
        size_tokens = [value for value in size_tokens if value is not None]

        if "bytes_total" not in update and len(size_tokens) >= 2:
            update["bytes_done"] = size_tokens[0]
            update["bytes_total"] = size_tokens[1]
        elif "bytes_total" not in update and len(size_tokens) == 1 and "percent" not in update:
            update["bytes_done"] = size_tokens[0]

    percent = PERCENT_RE.search(line)
    if percent:
        update["percent"] = float(percent.group("percent"))
        if update.get("bytes_total") is not None and update.get("bytes_done") is None:
            update["bytes_done"] = int(update["bytes_total"] * (update["percent"] / 100.0))
        elif update.get("bytes_total") is None and len(size_tokens) == 1:
            update["bytes_total"] = size_tokens[0]
            update["bytes_done"] = int(update["bytes_total"] * (update["percent"] / 100.0))

    eta = parse_eta_seconds(line)
    if eta is not None:
        update["eta_seconds"] = eta

    return update


def clamp_percent(value: float | int | None) -> float | None:
    if value is None:
        return None
    return max(0.0, min(100.0, float(value)))


def infer_percent_from_messages(messages: list[str]) -> float | None:
    for message in reversed(messages):
        match = PERCENT_RE.search(message or "")
        if match:
            return clamp_percent(match.group("percent"))
    return None


def looks_like_absolute_path(raw_path: str) -> bool:
    if not raw_path:
        return False
    path = raw_path.strip()
    return path.startswith(("/", "\\")) or Path(path).is_absolute() or PureWindowsPath(path).is_absolute()


def ensure_destination_writable(destination_path: Path) -> None:
    try:
        destination_path.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise ValueError(permission_fix_hint(destination_path)) from exc

    if not destination_path.is_dir():
        raise ValueError(f"Destination '{destination_path}' exists but is not a directory.")

    probe_path: str | None = None
    try:
        with NamedTemporaryFile(dir=destination_path, prefix=".write-test-", delete=False) as handle:
            probe_path = handle.name
    except PermissionError as exc:
        raise ValueError(permission_fix_hint(destination_path)) from exc
    finally:
        if probe_path:
            try:
                Path(probe_path).unlink(missing_ok=True)
            except OSError:
                pass


class MegaDownloader:
    def __init__(self, binary_name: str = "mega-get"):
        self.binary_name = binary_name

    def available(self) -> bool:
        return shutil.which(self.binary_name) is not None

    def _run_metadata_command(
        self,
        companion_name: str,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> str:
        binary = find_megacmd_companion_binary(self.binary_name, companion_name)
        if not binary:
            raise DownloadError(f"'{companion_name}' was not found in PATH.")

        result = subprocess.run(
            [binary, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
        output = (result.stdout or "").strip()
        if result.returncode != 0:
            raise DownloadError(output or f"{companion_name} exited with code {result.returncode}.")
        return output

    def _merge_metadata_from_outputs(
        self,
        url: str,
        fallback_prefix: str,
        *,
        ls_output: str | None = None,
        du_output: str | None = None,
        display_name: str | None = None,
        bytes_total: int | None = None,
    ) -> dict:
        resolved_name = display_name or infer_display_name(url, fallback_prefix)
        resolved_bytes_total = bytes_total

        ls_entries = parse_megacmd_ls_summary(ls_output or "")
        if ls_entries:
            if len(ls_entries) == 1:
                entry = ls_entries[0]
                single_name = normalize_remote_display_name(entry["name"])
                if single_name:
                    resolved_name = single_name
                if not str(entry["flags"]).startswith("d") and int(entry["size"]) > 0:
                    resolved_bytes_total = int(entry["size"])
            else:
                top_levels = {
                    normalize_remote_display_name(str(entry["name"]).split("/", 1)[0])
                    for entry in ls_entries
                }
                top_levels = {item for item in top_levels if item}
                if len(top_levels) == 1:
                    resolved_name = sorted(top_levels)[0]

        du_size, du_name = parse_megacmd_du_summary(du_output or "")
        if du_size is not None:
            resolved_bytes_total = du_size
        if du_name:
            resolved_name = du_name

        return {
            "display_name": resolved_name,
            "bytes_total": resolved_bytes_total,
        }

    def _probe_metadata_via_isolated_public_session(self, url: str, fallback_prefix: str) -> dict:
        with TemporaryDirectory(prefix="mega-public-meta-") as temp_home:
            env = dict(os.environ)
            env["HOME"] = temp_home
            env["XDG_CONFIG_HOME"] = temp_home
            env["XDG_DATA_HOME"] = temp_home
            env["XDG_CACHE_HOME"] = temp_home
            ls_output = ""
            du_output = ""
            pwd_output = ""
            logged_in = False
            try:
                self._run_metadata_command("mega-login", [url], env=env, timeout=30)
                logged_in = True
                ls_output = self._run_metadata_command(
                    "mega-ls",
                    ["-l", "--time-format=ISO6081_WITH_TIME", "/"],
                    env=env,
                    timeout=30,
                )
                du_output = self._run_metadata_command("mega-du", ["/"], env=env, timeout=30)
                try:
                    pwd_output = self._run_metadata_command("mega-pwd", [], env=env, timeout=10)
                except DownloadError:
                    pwd_output = ""
            finally:
                if logged_in:
                    try:
                        self._run_metadata_command("mega-logout", [], env=env, timeout=10)
                    except DownloadError:
                        pass

        metadata = self._merge_metadata_from_outputs(
            url,
            fallback_prefix,
            ls_output=ls_output,
            du_output=du_output,
        )
        pwd_name = normalize_remote_display_name(pwd_output)
        if pwd_name and is_mega_folder_url(url):
            metadata["display_name"] = pwd_name
        elif metadata["display_name"] == "Resolving file name..." and pwd_name:
            metadata["display_name"] = pwd_name
        return metadata

    def probe_metadata(self, url: str, fallback_prefix: str) -> dict:
        ls_error: str | None = None
        du_error: str | None = None

        try:
            ls_output = self._run_metadata_command(
                "mega-ls",
                ["-l", "--time-format=ISO6081_WITH_TIME", url],
            )
        except DownloadError as exc:
            ls_error = str(exc)
        else:
            ls_output = ls_output

        try:
            du_output = self._run_metadata_command("mega-du", [url])
        except DownloadError as exc:
            du_error = str(exc)
            du_output = ""
        else:
            du_output = du_output

        metadata = self._merge_metadata_from_outputs(
            url,
            fallback_prefix,
            ls_output=ls_output if not ls_error else "",
            du_output=du_output,
        )

        needs_public_folder_fallback = is_mega_folder_url(url) and (
            metadata["display_name"] == "Resolving file name..." or metadata["bytes_total"] is None
        )
        if needs_public_folder_fallback:
            try:
                isolated_metadata = self._probe_metadata_via_isolated_public_session(url, fallback_prefix)
            except DownloadError:
                isolated_metadata = {}
            else:
                if isolated_metadata.get("display_name"):
                    metadata["display_name"] = isolated_metadata["display_name"]
                if isolated_metadata.get("bytes_total") is not None:
                    metadata["bytes_total"] = isolated_metadata["bytes_total"]

        if metadata["bytes_total"] is None and ls_error and du_error:
            raise DownloadError(f"Could not resolve metadata. ls: {ls_error} du: {du_error}")

        return metadata

    def _terminate_process(self, process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def download(
        self,
        job: Job,
        destination_dir: Path,
        progress_callback: ProgressCallback,
        cancel_event: threading.Event,
        pause_event: threading.Event,
        process_callback: ProcessCallback,
    ) -> None:
        binary = shutil.which(self.binary_name)
        if not binary:
            raise DownloadError(f"'{self.binary_name}' was not found in PATH.")

        before_names = {child.name for child in destination_dir.iterdir()}
        before_snapshot = snapshot_relative_paths(destination_dir)
        command = [binary, job.url, str(destination_dir)]
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        process_callback(process)
        progress_callback(status="starting", message=f"Launching {self.binary_name}.")
        saw_progress = False
        discovered_display_name = False
        real_progress_seen = threading.Event()
        auto_kick_stop = threading.Event()
        auto_kick_thread = threading.Thread(
            target=self._auto_restart_if_stalled,
            args=(process, cancel_event, pause_event, real_progress_seen, auto_kick_stop),
            name=f"mega-autokick-{job.id[:8]}",
            daemon=True,
        )
        auto_kick_thread.start()

        try:
            if process.stdout is not None:
                for raw_line in process.stdout:
                    line = raw_line.strip()
                    if cancel_event.is_set():
                        self._terminate_process(process)
                        raise DownloadCanceled("Canceled by user.")
                    if not line:
                        continue
                    parsed = parse_progress_line(line)
                    if not discovered_display_name:
                        created_name = infer_downloaded_display_name(destination_dir, before_snapshot)
                        if created_name:
                            parsed["display_name"] = created_name
                            discovered_display_name = True
                    if (
                        parsed.get("percent") not in {None, 0}
                        or (parsed.get("bytes_done") or 0) > 0
                        or (parsed.get("bytes_total") or 0) > 0
                    ):
                        real_progress_seen.set()
                    status = "downloading" if parsed else "probing"
                    progress_callback(status=status, message=line, **parsed)
                    if parsed:
                        saw_progress = True

            return_code = process.wait()
            if cancel_event.is_set():
                raise DownloadCanceled("Canceled by user.")
        except DownloadCanceled:
            cleanup_paths_created_since(destination_dir, before_snapshot)
            raise
        finally:
            auto_kick_stop.set()
            process_callback(None)
        if return_code != 0:
            raise DownloadError(f"{self.binary_name} exited with code {return_code}.")

        after_names = {child.name for child in destination_dir.iterdir()}
        created_names = sorted(after_names - before_names)
        if created_names:
            progress_callback(display_name=created_names[0])

        progress_callback(
            status="completed" if saw_progress else "probing",
            message="Download finished.",
        )

    def _auto_restart_if_stalled(
        self,
        process: subprocess.Popen,
        cancel_event: threading.Event,
        pause_event: threading.Event,
        real_progress_seen: threading.Event,
        stop_event: threading.Event,
    ) -> None:
        if stop_event.wait(1.5):
            return
        if (
            stop_event.is_set()
            or cancel_event.is_set()
            or pause_event.is_set()
            or real_progress_seen.is_set()
            or process.poll() is not None
        ):
            return

        run_megacmd_transfers_command(self.binary_name, "-p")

        if stop_event.wait(0.6):
            return
        if cancel_event.is_set() or pause_event.is_set() or process.poll() is not None:
            return

        run_megacmd_transfers_command(self.binary_name, "-r")


class FakeDownloader:
    def probe_metadata(self, url: str, fallback_prefix: str) -> dict:
        seed = int(hashlib.sha256(url.encode("utf-8")).hexdigest()[:8], 16)
        rng = random.Random(seed)
        total_bytes = rng.randint(12, 48) * 1024 * 1024
        filename = normalize_fake_display_name(url, fallback_prefix)
        if "." not in filename:
            filename = f"{filename}.bin"
        return {
            "display_name": filename,
            "bytes_total": total_bytes,
        }

    def download(
        self,
        job: Job,
        destination_dir: Path,
        progress_callback: ProgressCallback,
        cancel_event: threading.Event,
        pause_event: threading.Event,
        process_callback: ProcessCallback,
    ) -> None:
        process_callback(None)
        parsed_url = urlparse(job.url)
        query = parse_qs(parsed_url.query)
        seed = int(hashlib.sha256(job.url.encode("utf-8")).hexdigest()[:8], 16)
        rng = random.Random(seed)
        total_bytes = rng.randint(12, 48) * 1024 * 1024
        filename = normalize_fake_display_name(job.url, f"download-{job.id[:8]}")
        if "." not in filename:
            filename = f"{filename}.bin"
        password = query.get("pw", [None])[0]

        progress_callback(status="starting", display_name=filename, message="Using fake downloader backend.")
        progress_callback(status="probing", bytes_total=total_bytes, message="Estimated file size discovered.")

        bytes_done = 0
        started = time.monotonic()
        chunk_count = 20
        step_size = max(total_bytes // chunk_count, 1)
        pause_reported = False
        while bytes_done < total_bytes:
            if cancel_event.is_set():
                raise DownloadCanceled("Canceled by user.")
            while pause_event.is_set():
                if not pause_reported:
                    progress_callback(
                        status="paused",
                        speed_bps=None,
                        eta_seconds=None,
                        message="Paused by user.",
                    )
                    pause_reported = True
                if cancel_event.wait(0.25):
                    raise DownloadCanceled("Canceled by user.")
                if not pause_event.is_set():
                    progress_callback(
                        status="downloading",
                        speed_bps=None,
                        eta_seconds=None,
                        message="Resuming download.",
                    )
                    pause_reported = False
                    break
            time.sleep(rng.uniform(0.15, 0.35))
            if cancel_event.is_set():
                raise DownloadCanceled("Canceled by user.")
            if pause_event.is_set():
                continue
            bytes_done = min(bytes_done + step_size, total_bytes)
            elapsed = max(time.monotonic() - started, 0.001)
            speed = bytes_done / elapsed
            remaining = max(total_bytes - bytes_done, 0)
            eta_seconds = int(remaining / speed) if speed else None
            progress_callback(
                status="downloading",
                bytes_done=bytes_done,
                bytes_total=total_bytes,
                speed_bps=speed,
                eta_seconds=eta_seconds,
                message="Simulating download progress.",
            )

        target_path = destination_dir / filename
        if target_path.suffix.lower() == ".zip":
            self._write_zip(target_path, password=password)
        else:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with target_path.open("wb") as handle:
                handle.truncate(total_bytes)

        progress_callback(
            status="completed",
            display_name=target_path.name,
            bytes_done=total_bytes,
            bytes_total=total_bytes,
            speed_bps=None,
            eta_seconds=0,
            message="Fake download finished.",
        )

    def _write_zip(self, target_path: Path, password: str | None = None) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        payload = "Downloaded by the fake adapter.\n"
        if password:
            with pyzipper.AESZipFile(
                target_path,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                encryption=pyzipper.WZ_AES,
            ) as archive:
                archive.setpassword(password.encode("utf-8"))
                archive.writestr("README.txt", payload)
        else:
            with zipfile.ZipFile(target_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("README.txt", payload)


class UnavailableDownloader:
    def __init__(self, reason: str):
        self.reason = reason

    def download(
        self,
        job: Job,
        destination_dir: Path,
        progress_callback: ProgressCallback,
        cancel_event: threading.Event,
        pause_event: threading.Event,
        process_callback: ProcessCallback,
    ) -> None:
        process_callback(None)
        raise DownloadError(self.reason)

    def probe_metadata(self, url: str, fallback_prefix: str) -> dict:
        raise DownloadError(self.reason)


class DownloadManager:
    QUEUE_SORT_LABELS = {
        "oldest": "Oldest first",
        "newest": "Newest first",
        "name_asc": "Name A-Z",
        "name_desc": "Name Z-A",
        "size_asc": "Smallest first",
        "size_desc": "Largest first",
    }

    def __init__(
        self,
        storage: JsonStorage,
        destinations: dict,
        megacmd_binary: str = "mega-get",
        backend: str = "auto",
    ):
        self.storage = storage
        self.base_destinations = normalize_destinations(destinations)
        self.favorite_destinations: dict[str, dict] = {}
        self.hidden_base_destination_keys: set[str] = set()
        self.destinations = dict(self.base_destinations)
        self.megacmd_binary = megacmd_binary
        self.backend_name = backend
        self._lock = threading.RLock()
        self._queue: Queue[str] = Queue()
        self._stop_event = threading.Event()
        self._worker = threading.Thread(target=self._worker_loop, name="download-worker", daemon=True)
        self._cancel_events: dict[str, threading.Event] = {}
        self._pause_events: dict[str, threading.Event] = {}
        self._active_processes: dict[str, subprocess.Popen] = {}
        self._progress_samples: dict[str, tuple[float, int]] = {}
        self._purge_on_finish: set[str] = set()
        self._last_persist = 0.0
        self._jobs: dict[str, Job] = {}
        self.queue_sort_mode = "oldest"
        self.backend_reason: str | None = None
        self.adapter = self._select_adapter()
        self._load_hidden_base_destinations()
        self._load_favorites()
        self._load_jobs()
        self._worker.start()

    def _select_adapter(self):
        mega = MegaDownloader(self.megacmd_binary)
        if self.backend_name == "fake":
            self.backend_name = "fake"
            return FakeDownloader()
        if mega.available():
            self.backend_name = "mega"
            return mega
        self.backend_reason = (
            f"'{self.megacmd_binary}' was not found in PATH. "
            "Install MEGAcmd or set MEGA_DOWNLOADER_BACKEND=fake only for development."
        )
        self.backend_name = "unavailable"
        return UnavailableDownloader(self.backend_reason)

    def _load_jobs(self) -> None:
        loaded_jobs = self.storage.load_jobs()
        with self._lock:
            for job in loaded_jobs:
                if job.status in ACTIVE_JOB_STATUSES:
                    job.status = "queued"
                    job.error = "Recovered after service restart."
                    job.transfer.finished_at = None
                elif job.status == "paused":
                    job.error = "Recovered after service restart. Resume to continue."
                    job.transfer.finished_at = None
                    job.transfer.paused = True
                self._jobs[job.id] = job
            self._rebuild_queue_locked()

    def _load_favorites(self) -> None:
        loaded_favorites = self.storage.load_favorites()
        with self._lock:
            for favorite in loaded_favorites:
                resolved = Path(favorite.path).expanduser().resolve()
                self.favorite_destinations[favorite.key] = {
                    "key": favorite.key,
                    "label": favorite.label,
                    "path": resolved,
                    "favorite": True,
                }
            self._refresh_destinations()

    def _load_hidden_base_destinations(self) -> None:
        hidden_keys = self.storage.load_hidden_base_destinations()
        with self._lock:
            self.hidden_base_destination_keys = {key for key in hidden_keys if key in self.base_destinations}
            self._refresh_destinations()

    def _refresh_destinations(self) -> None:
        visible_base_destinations = {
            key: value
            for key, value in self.base_destinations.items()
            if key not in self.hidden_base_destination_keys
        }
        self.destinations = {**visible_base_destinations, **self.favorite_destinations}

    def _favorite_models(self) -> list[FavoriteDestination]:
        return [
            FavoriteDestination(
                key=item["key"],
                label=item["label"],
                path=str(item["path"]),
                favorite=True,
            )
            for item in self.favorite_destinations.values()
        ]

    def stop(self) -> None:
        self._stop_event.set()
        for cancel_event in self._cancel_events.values():
            cancel_event.set()
        for pause_event in self._pause_events.values():
            pause_event.clear()

    def has_destinations(self) -> bool:
        return bool(self.destinations)

    def queue_sort_options(self) -> list[dict[str, str]]:
        return [
            {"value": value, "label": label}
            for value, label in self.QUEUE_SORT_LABELS.items()
        ]

    def _queued_job_ids_locked(self) -> list[str]:
        with self._queue.mutex:
            current_order = list(self._queue.queue)

        ordered_ids: list[str] = []
        seen: set[str] = set()
        for job_id in current_order:
            job = self._jobs.get(job_id)
            if not job or job.status != "queued" or job_id in seen:
                continue
            ordered_ids.append(job_id)
            seen.add(job_id)

        for job in self._jobs.values():
            if job.status == "queued" and job.id not in seen:
                ordered_ids.append(job.id)
                seen.add(job.id)

        return ordered_ids

    def _rebuild_queue_locked(self, ordered_job_ids: list[str] | None = None) -> None:
        if ordered_job_ids is None:
            ordered_job_ids = self._queued_job_ids_locked()

        with self._queue.mutex:
            self._queue.queue.clear()
            self._queue.queue.extend(ordered_job_ids)
            if ordered_job_ids:
                self._queue.not_empty.notify_all()

    def can_restore_base_destinations(self) -> bool:
        return bool(self.hidden_base_destination_keys)

    def destination_options(self) -> list[dict]:
        options: list[dict] = []
        total_destinations = len(self.destinations)
        for key, item in self.base_destinations.items():
            if key in self.hidden_base_destination_keys:
                continue
            options.append(
                {
                    "key": item["key"],
                    "label": item["label"],
                    "path": str(item["path"]),
                    "favorite": False,
                    "deletable": total_destinations > 1,
                }
            )
        for item in self.favorite_destinations.values():
            options.append(
                {
                    "key": item["key"],
                    "label": item["label"],
                    "path": str(item["path"]),
                    "favorite": True,
                    "deletable": total_destinations > 1,
                }
            )
        return options

    def get_destination_path(self, destination_key: str) -> Path:
        if destination_key not in self.destinations:
            raise ValueError(f"Unknown destination '{destination_key}'.")
        return self.destinations[destination_key]["path"]

    def resolve_destination(self, destination_key: str, destination_subpath: str = "") -> tuple[Path, str, bool]:
        root = self.get_destination_path(destination_key)
        normalized_subpath = (destination_subpath or "").strip()
        if looks_like_absolute_path(normalized_subpath):
            resolved_path = Path(normalized_subpath).expanduser().resolve()
            return resolved_path, "", True

        normalized_subpath = normalized_subpath.replace("\\", "/")
        resolved_path = path_within_root(root, normalized_subpath)
        relative_path = relative_to_root(root, resolved_path)
        return resolved_path, relative_path, False

    def submit(self, urls: list[str], destination_key: str, destination_subpath: str = "") -> list[Job]:
        if self.backend_name == "unavailable":
            raise ValueError(self.backend_reason or "The downloader backend is unavailable.")
        if not self.destinations:
            raise ValueError("No destinations are configured. Restore or add a destination before submitting downloads.")
        destination_path, destination_relative_path, destination_is_custom = self.resolve_destination(destination_key, destination_subpath)
        ensure_destination_writable(destination_path)
        batch_id = uuid.uuid4().hex[:12]
        prepared_jobs: list[dict] = []
        for url in urls:
            job_id = uuid.uuid4().hex
            fallback_prefix = f"job-{job_id[:8]}"
            try:
                metadata = self.adapter.probe_metadata(url, fallback_prefix)
            except DownloadError:
                metadata = {}
            prepared_jobs.append(
                {
                    "id": job_id,
                    "url": url,
                    "display_name": metadata.get("display_name") or infer_display_name(url, fallback_prefix),
                    "bytes_total": metadata.get("bytes_total"),
                }
            )
        new_jobs: list[Job] = []
        with self._lock:
            for prepared in prepared_jobs:
                job = Job(
                    id=prepared["id"],
                    batch_id=batch_id,
                    url=prepared["url"],
                    destination_key=destination_key,
                    destination_path=str(destination_path),
                    destination_relative_path=destination_relative_path,
                    display_name=str(prepared["display_name"]),
                    destination_is_custom=destination_is_custom,
                    transfer=TransferStatus(
                        bytes_total=prepared["bytes_total"],
                        started_at=None,
                    ),
                )
                self._jobs[job.id] = job
                new_jobs.append(job)
            self._rebuild_queue_locked()
            self._persist_locked(force=True)
        return new_jobs

    def add_favorite_destination(self, destination_key: str, destination_input: str) -> dict:
        if not self.destinations:
            raise ValueError("No destinations are configured. Restore a destination before adding a favorite.")
        destination_path, _, _ = self.resolve_destination(destination_key, destination_input)
        ensure_destination_writable(destination_path)

        with self._lock:
            for item in self.destinations.values():
                if item["path"] == destination_path:
                    return {
                        "key": item["key"],
                        "label": item["label"],
                        "path": str(item["path"]),
                        "favorite": bool(item.get("favorite", False)),
                        "created": False,
                    }

            label = destination_path.name or str(destination_path)
            key = f"favorite_{hashlib.sha1(str(destination_path).encode('utf-8')).hexdigest()[:10]}"
            favorite = {
                "key": key,
                "label": label,
                "path": destination_path,
                "favorite": True,
            }
            self.favorite_destinations[key] = favorite
            self._refresh_destinations()
            self._persist_locked(force=True)
            return {
                "key": key,
                "label": label,
                "path": str(destination_path),
                "favorite": True,
                "created": True,
            }

    def delete_destination(self, destination_key: str) -> dict:
        with self._lock:
            if destination_key not in self.destinations:
                raise ValueError(f"Unknown destination '{destination_key}'.")
            if len(self.destinations) <= 1:
                raise ValueError("At least one destination must remain. Restore or add another destination before deleting this one.")
            if any(
                job.destination_key == destination_key and job.status in {"queued", "paused", *ACTIVE_JOB_STATUSES}
                for job in self._jobs.values()
            ):
                raise ValueError("That destination is still in use by a queued, paused, or active job.")

            destination = self.destinations[destination_key]
            if destination_key in self.favorite_destinations:
                self.favorite_destinations.pop(destination_key, None)
                deleted_type = "favorite"
            else:
                self.hidden_base_destination_keys.add(destination_key)
                deleted_type = "configured"

            self._refresh_destinations()
            self._persist_locked(force=True)
            return {
                "key": destination_key,
                "label": destination["label"],
                "path": str(destination["path"]),
                "type": deleted_type,
            }

    def restore_hidden_base_destinations(self) -> int:
        with self._lock:
            restored = len(self.hidden_base_destination_keys)
            if not restored:
                return 0
            self.hidden_base_destination_keys.clear()
            self._refresh_destinations()
            self._persist_locked(force=True)
            return restored

    def build_explorer_target(self, job: Job) -> tuple[str | None, str]:
        if not job.destination_is_custom:
            if job.destination_key not in self.destinations:
                return None, ""
            return job.destination_key, job.destination_relative_path

        destination_path = Path(job.destination_path).expanduser().resolve()
        for key, item in self.destinations.items():
            root = item["path"]
            if destination_path == root or root in destination_path.parents:
                return key, relative_to_root(root, destination_path)
        return None, ""

    def _cancel_active_mega_downloads(self) -> None:
        self._run_mega_transfers_command("-c")

    def _pause_active_mega_downloads(self) -> None:
        self._run_mega_transfers_command("-p")

    def _resume_active_mega_downloads(self) -> None:
        self._run_mega_transfers_command("-r")

    def _run_mega_transfers_command(self, action_flag: str) -> None:
        if self.backend_name != "mega":
            return
        run_megacmd_transfers_command(self.megacmd_binary, action_flag)

    def _request_cancel_locked(self, job_id: str) -> Job:
        job = self._require_job(job_id)
        cancel_event = self._cancel_events.setdefault(job_id, threading.Event())
        pause_event = self._pause_events.get(job_id)
        if pause_event:
            pause_event.clear()
        cancel_event.set()
        self._cancel_active_mega_downloads()
        process = self._active_processes.get(job_id)
        if process and process.poll() is None:
            process.terminate()
        return job

    def _has_live_runtime_locked(self, job_id: str) -> bool:
        process = self._active_processes.get(job_id)
        if process and process.poll() is None:
            return True
        return job_id in self._cancel_events or job_id in self._pause_events

    def _request_pause_locked(self, job_id: str) -> Job:
        job = self._require_job(job_id)
        if job.status == "queued":
            job.status = "paused"
            job.error = None
            job.transfer.paused = True
            job.transfer.speed_bps = None
            job.transfer.eta_seconds = None
            if job.transfer.percent is None and job.transfer.bytes_total:
                job.transfer.percent = clamp_percent((job.transfer.bytes_done / job.transfer.bytes_total) * 100.0)
            job.touch()
            self._rebuild_queue_locked()
            self._persist_locked(force=True)
            return job
        if job.status not in ACTIVE_JOB_STATUSES:
            raise ValueError("Only queued or active jobs can be paused.")

        pause_event = self._pause_events.setdefault(job_id, threading.Event())
        pause_event.set()
        self._pause_active_mega_downloads()
        job.status = "paused"
        job.error = None
        job.transfer.paused = True
        job.transfer.speed_bps = None
        job.transfer.eta_seconds = None
        if job.transfer.percent is None:
            if job.transfer.bytes_total:
                job.transfer.percent = clamp_percent((job.transfer.bytes_done / job.transfer.bytes_total) * 100.0)
            else:
                job.transfer.percent = infer_percent_from_messages(job.output_tail)
        job.append_output("Paused by user.")
        job.touch()
        self._rebuild_queue_locked()
        self._persist_locked(force=True)
        return job

    def _request_resume_locked(self, job_id: str) -> Job:
        job = self._require_job(job_id)
        if job.status != "paused":
            raise ValueError("Only paused jobs can be resumed.")

        pause_event = self._pause_events.get(job_id)
        has_active_process = self._has_live_runtime_locked(job_id)
        job.error = None
        job.transfer.paused = False
        job.transfer.finished_at = None

        if pause_event and has_active_process:
            pause_event.clear()
            self._resume_active_mega_downloads()
            job.status = "downloading"
            job.append_output("Resuming download.")
            job.touch()
            self._rebuild_queue_locked()
            self._persist_locked(force=True)
            return job

        job.status = "queued"
        job.append_output("Queued to resume.")
        job.touch()
        self._rebuild_queue_locked()
        self._persist_locked(force=True)
        return job

    def cancel_job(self, job_id: str) -> Job:
        with self._lock:
            job = self._require_job(job_id)
            if job.status == "queued":
                job.status = "canceled"
                job.error = "Canceled before the download started."
                job.transfer.finished_at = utcnow_iso()
                job.touch()
                self._rebuild_queue_locked()
                self._persist_locked(force=True)
                return job
            if job.status == "paused" and not self._has_live_runtime_locked(job_id):
                job.status = "canceled"
                job.error = "Canceled while paused."
                job.transfer.paused = False
                job.transfer.finished_at = utcnow_iso()
                job.touch()
                self._pause_events.pop(job_id, None)
                self._rebuild_queue_locked()
                self._persist_locked(force=True)
                return job

            return self._request_cancel_locked(job_id)

    def pause_job(self, job_id: str) -> Job:
        with self._lock:
            return self._request_pause_locked(job_id)

    def resume_job(self, job_id: str) -> Job:
        with self._lock:
            return self._request_resume_locked(job_id)

    def clear_queue(self) -> dict[str, int]:
        with self._lock:
            removed = 0
            canceling = 0
            for job_id, job in list(self._jobs.items()):
                if job.status in ACTIVE_JOB_STATUSES or (job.status == "paused" and self._has_live_runtime_locked(job_id)):
                    canceling += 1
                    self._purge_on_finish.add(job_id)
                    self._request_cancel_locked(job_id)
                    continue

                removed += 1
                self._jobs.pop(job_id, None)
                self._cancel_events.pop(job_id, None)
                self._pause_events.pop(job_id, None)
                self._active_processes.pop(job_id, None)
                self._progress_samples.pop(job_id, None)
                self._purge_on_finish.discard(job_id)

            self._rebuild_queue_locked()
            self._persist_locked(force=True)
            return {"removed": removed, "canceling": canceling}

    def retry_job(self, job_id: str) -> Job:
        with self._lock:
            job = self._require_job(job_id)
            if job.status not in RETRYABLE_JOB_STATUSES:
                raise ValueError("Only completed, failed, or canceled jobs can be retried.")
            job.status = "queued"
            job.error = None
            job.output_tail.clear()
            job.transfer = TransferStatus()
            job.touch()
            self._cancel_events.pop(job_id, None)
            self._pause_events.pop(job_id, None)
            self._progress_samples.pop(job_id, None)
            self._purge_on_finish.discard(job_id)
            self._rebuild_queue_locked()
            self._persist_locked(force=True)
            return job

    def pause_all(self) -> dict[str, int]:
        with self._lock:
            paused = 0
            for job in self._jobs.values():
                if job.status == "queued" or job.status in ACTIVE_JOB_STATUSES:
                    self._request_pause_locked(job.id)
                    paused += 1
            self._rebuild_queue_locked()
            self._persist_locked(force=True)
            return {"paused": paused}

    def resume_all(self) -> dict[str, int]:
        with self._lock:
            resumed = 0
            for job in self._jobs.values():
                if job.status == "paused":
                    self._request_resume_locked(job.id)
                    resumed += 1
            self._rebuild_queue_locked()
            self._persist_locked(force=True)
            return {"resumed": resumed}

    def bulk_pause_toggle(self) -> dict[str, str | bool]:
        with self._lock:
            has_pauseable = any(
                job.status == "queued" or job.status in ACTIVE_JOB_STATUSES
                for job in self._jobs.values()
            )
            has_paused = any(job.status == "paused" for job in self._jobs.values())

        if has_pauseable:
            return {"action": "pause", "label": "Pause All", "available": True}
        if has_paused:
            return {"action": "resume", "label": "Resume All", "available": True}
        return {"action": "pause", "label": "Pause All", "available": False}

    def sort_queue(self, sort_by: str) -> dict[str, int | str]:
        with self._lock:
            if sort_by not in self.QUEUE_SORT_LABELS:
                raise ValueError("Unknown queue sort mode.")

            queued_jobs = [self._jobs[job_id] for job_id in self._queued_job_ids_locked() if job_id in self._jobs]
            if not queued_jobs:
                self.queue_sort_mode = sort_by
                return {"sorted": 0, "sort_by": sort_by, "label": self.QUEUE_SORT_LABELS[sort_by]}

            if sort_by == "oldest":
                queued_jobs.sort(key=lambda job: (job.created_at, job.display_name.casefold(), job.id))
            elif sort_by == "newest":
                queued_jobs.sort(key=lambda job: (job.created_at, job.display_name.casefold(), job.id), reverse=True)
            elif sort_by == "name_asc":
                queued_jobs.sort(key=lambda job: (job.display_name.casefold(), job.created_at, job.id))
            elif sort_by == "name_desc":
                queued_jobs.sort(key=lambda job: (job.display_name.casefold(), job.created_at, job.id), reverse=True)
            elif sort_by == "size_asc":
                queued_jobs.sort(
                    key=lambda job: (
                        job.transfer.bytes_total is None,
                        job.transfer.bytes_total or 0,
                        job.display_name.casefold(),
                        job.created_at,
                        job.id,
                    )
                )
            elif sort_by == "size_desc":
                queued_jobs.sort(
                    key=lambda job: (
                        job.transfer.bytes_total is None,
                        -(job.transfer.bytes_total or 0),
                        job.display_name.casefold(),
                        job.created_at,
                        job.id,
                    )
                )

            self.queue_sort_mode = sort_by
            self._rebuild_queue_locked([job.id for job in queued_jobs])
            self._persist_locked(force=True)
            return {"sorted": len(queued_jobs), "sort_by": sort_by, "label": self.QUEUE_SORT_LABELS[sort_by]}

    def dashboard_payload(self) -> dict:
        with self._lock:
            queued_job_ids = self._queued_job_ids_locked()
            queued_jobs = [self._jobs[job_id] for job_id in queued_job_ids if job_id in self._jobs]
            active_jobs = sorted(
                [job for job in self._jobs.values() if job.status in {"paused", *ACTIVE_JOB_STATUSES}],
                key=lambda item: item.updated_at,
                reverse=True,
            )
            finished_jobs = sorted(
                [job for job in self._jobs.values() if job.status not in {"queued", "paused", *ACTIVE_JOB_STATUSES}],
                key=lambda item: item.updated_at,
                reverse=True,
            )
            jobs = active_jobs + queued_jobs + finished_jobs
            destination_lookup = {item["key"]: item["label"] for item in self.destination_options()}
            job_dicts = [self._job_payload(job, destination_lookup) for job in jobs]

        batches: dict[str, dict] = {}
        summary = {
            "total_jobs": 0,
            "queued_jobs": 0,
            "paused_jobs": 0,
            "active_jobs": 0,
            "completed_jobs": 0,
            "failed_jobs": 0,
            "canceled_jobs": 0,
            "throughput_bps": 0.0,
            "bytes_done": 0,
            "bytes_total": 0,
            "has_unknown_total": False,
        }

        for job in job_dicts:
            summary["total_jobs"] += 1
            if job["status"] == "queued":
                summary["queued_jobs"] += 1
            elif job["status"] == "paused":
                summary["paused_jobs"] += 1
            elif job["status"] in ACTIVE_JOB_STATUSES:
                summary["active_jobs"] += 1
            elif job["status"] == "completed":
                summary["completed_jobs"] += 1
            elif job["status"] == "failed":
                summary["failed_jobs"] += 1
            elif job["status"] == "canceled":
                summary["canceled_jobs"] += 1

            summary["bytes_done"] += job["transfer"]["bytes_done"]
            if job["transfer"]["bytes_total"] is None:
                summary["has_unknown_total"] = True
            else:
                summary["bytes_total"] += job["transfer"]["bytes_total"]
            if job["transfer"]["speed_bps"] is not None:
                summary["throughput_bps"] += job["transfer"]["speed_bps"]

            batch = batches.setdefault(
                job["batch_id"],
                {
                    "id": job["batch_id"],
                    "jobs": [],
                    "job_count": 0,
                    "bytes_done": 0,
                    "bytes_total": 0,
                    "speed_bps": 0.0,
                    "eta_seconds": None,
                    "has_unknown_total": False,
                    "status_counts": {},
                },
            )
            batch["jobs"].append(job)
            batch["job_count"] += 1
            batch["bytes_done"] += job["transfer"]["bytes_done"]
            if job["transfer"]["bytes_total"] is None:
                batch["has_unknown_total"] = True
            else:
                batch["bytes_total"] += job["transfer"]["bytes_total"]
            if job["transfer"]["speed_bps"] is not None:
                batch["speed_bps"] += job["transfer"]["speed_bps"]
            if job["transfer"]["eta_seconds"] is not None:
                batch["eta_seconds"] = max(batch["eta_seconds"] or 0, job["transfer"]["eta_seconds"])
            batch["status_counts"][job["status"]] = batch["status_counts"].get(job["status"], 0) + 1

        return {
            "backend": {
                "name": self.backend_name,
                "label": {
                    "mega": "MEGAcmd",
                    "fake": "Fake development adapter",
                    "unavailable": "MEGAcmd unavailable",
                }.get(self.backend_name, self.backend_name),
                "reason": self.backend_reason,
            },
            "summary": summary,
            "jobs": job_dicts,
            "batches": list(batches.values()),
            "queue_sort": self.queue_sort_mode,
            "bulk_pause_toggle": self.bulk_pause_toggle(),
            "updated_at": utcnow_iso(),
        }

    def _job_payload(self, job: Job, destination_lookup: dict[str, str]) -> dict:
        payload = job.to_dict()
        payload["destination_label"] = destination_lookup.get(job.destination_key, job.destination_key)
        payload["destination_display"] = str(job.destination_path) if job.destination_is_custom else (
            f"{payload['destination_label']} / {job.destination_relative_path}"
            if job.destination_relative_path
            else payload["destination_label"]
        )
        explorer_root, explorer_path = self.build_explorer_target(job)
        payload["explorer_root"] = explorer_root
        payload["explorer_path"] = explorer_path
        payload["can_cancel"] = job.status == "queued" or job.status in ACTIVE_JOB_STATUSES
        payload["can_pause"] = job.status == "queued" or job.status in ACTIVE_JOB_STATUSES
        payload["can_resume"] = job.status == "paused"
        if job.status == "paused":
            payload["can_cancel"] = True
        payload["can_retry"] = job.status in RETRYABLE_JOB_STATUSES
        payload["status_label"] = job.status.replace("_", " ").title()
        return payload

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                job_id = self._queue.get(timeout=0.25)
            except Empty:
                continue

            with self._lock:
                job = self._jobs.get(job_id)
                if not job or job.status != "queued":
                    continue
                cancel_event = self._cancel_events[job_id] = threading.Event()
                pause_event = self._pause_events[job_id] = threading.Event()
                self._progress_samples[job_id] = (time.monotonic(), 0)
                job.status = "starting"
                job.error = None
                job.transfer.paused = False
                job.transfer.started_at = utcnow_iso()
                job.transfer.finished_at = None
                job.touch()
                destination_dir, _, _ = self.resolve_destination(
                    job.destination_key,
                    job.destination_path if job.destination_is_custom else job.destination_relative_path,
                )
                destination_dir.mkdir(parents=True, exist_ok=True)
                self._persist_locked(force=True)

            try:
                self.adapter.download(
                    job=job,
                    destination_dir=destination_dir,
                    progress_callback=lambda **kwargs: self._update_job(job.id, **kwargs),
                    cancel_event=cancel_event,
                    pause_event=pause_event,
                    process_callback=lambda process: self._set_active_process(job.id, process),
                )
            except DownloadCanceled as exc:
                self._finish_job(job.id, status="canceled", error=str(exc))
            except Exception as exc:
                self._finish_job(job.id, status="failed", error=str(exc))
            else:
                self._finish_job(job.id, status="completed", error=None)
            finally:
                with self._lock:
                    self._cancel_events.pop(job_id, None)
                    self._pause_events.pop(job_id, None)
                    self._active_processes.pop(job_id, None)
                    self._progress_samples.pop(job_id, None)

    def _set_active_process(self, job_id: str, process: subprocess.Popen | None) -> None:
        with self._lock:
            if process is None:
                self._active_processes.pop(job_id, None)
            else:
                self._active_processes[job_id] = process

    def _update_job(self, job_id: str, **kwargs) -> None:
        with self._lock:
            job = self._require_job(job_id)
            status = kwargs.get("status")
            pause_event = self._pause_events.get(job_id)
            pause_requested = bool(pause_event and pause_event.is_set())
            if status in JOB_STATUSES and not (pause_requested and status in ACTIVE_JOB_STATUSES):
                job.status = status
            if job.status == "paused":
                job.transfer.paused = True
            if "display_name" in kwargs and kwargs["display_name"]:
                job.display_name = kwargs["display_name"]

            transfer = job.transfer
            speed_provided = "speed_bps" in kwargs and kwargs["speed_bps"] not in {None, 0}
            eta_provided = "eta_seconds" in kwargs and kwargs["eta_seconds"] is not None
            bytes_done_provided = "bytes_done" in kwargs and kwargs["bytes_done"] is not None
            if "bytes_done" in kwargs and kwargs["bytes_done"] is not None:
                transfer.bytes_done = int(kwargs["bytes_done"])
            if "bytes_total" in kwargs and kwargs["bytes_total"] is not None:
                transfer.bytes_total = int(kwargs["bytes_total"])
            if "percent" in kwargs and kwargs["percent"] is not None:
                transfer.percent = clamp_percent(kwargs["percent"])
            if "speed_bps" in kwargs:
                transfer.speed_bps = kwargs["speed_bps"]
            if "eta_seconds" in kwargs:
                transfer.eta_seconds = kwargs["eta_seconds"]
            message = kwargs.get("message")
            if message:
                job.append_output(message)
            percent = kwargs.get("percent")
            if percent is not None and transfer.bytes_total and not bytes_done_provided:
                derived_bytes_done = int(transfer.bytes_total * (percent / 100.0))
                transfer.bytes_done = max(transfer.bytes_done, derived_bytes_done)
            elif transfer.bytes_total and transfer.bytes_total > 0:
                transfer.percent = clamp_percent((transfer.bytes_done / transfer.bytes_total) * 100.0)

            if pause_requested:
                transfer.paused = True
                transfer.speed_bps = None
                transfer.eta_seconds = None
            elif status in ACTIVE_JOB_STATUSES:
                transfer.paused = False

            self._derive_transfer_metrics(
                job_id,
                transfer,
                speed_provided=speed_provided,
                eta_provided=eta_provided,
            )

            job.touch()
            self._persist_locked(force=False)

    def _derive_transfer_metrics(
        self,
        job_id: str,
        transfer: TransferStatus,
        speed_provided: bool,
        eta_provided: bool,
    ) -> None:
        now = time.monotonic()
        current_bytes_done = transfer.bytes_done
        sample = self._progress_samples.get(job_id)
        derived_speed: float | None = None

        if sample is None:
            self._progress_samples[job_id] = (now, current_bytes_done)
        else:
            sample_time, sample_bytes = sample
            if current_bytes_done != sample_bytes:
                elapsed = now - sample_time
                byte_delta = current_bytes_done - sample_bytes
                if elapsed > 0 and byte_delta > 0:
                    derived_speed = byte_delta / elapsed
                self._progress_samples[job_id] = (now, current_bytes_done)

        if not speed_provided and derived_speed is not None:
            transfer.speed_bps = derived_speed

        if transfer.bytes_total is not None:
            if transfer.bytes_done >= transfer.bytes_total:
                transfer.eta_seconds = 0
            elif not eta_provided and transfer.speed_bps and transfer.speed_bps > 0:
                remaining = max(transfer.bytes_total - transfer.bytes_done, 0)
                transfer.eta_seconds = math.ceil(remaining / transfer.speed_bps) if remaining else 0

    def _finish_job(self, job_id: str, status: str, error: str | None) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return

            if job_id in self._purge_on_finish:
                self._jobs.pop(job_id, None)
                self._cancel_events.pop(job_id, None)
                self._pause_events.pop(job_id, None)
                self._active_processes.pop(job_id, None)
                self._progress_samples.pop(job_id, None)
                self._purge_on_finish.discard(job_id)
                self._persist_locked(force=True)
                return

            job.status = status
            job.error = error
            job.transfer.finished_at = utcnow_iso()
            if status == "completed" and job.transfer.bytes_total is not None:
                job.transfer.bytes_done = job.transfer.bytes_total
                job.transfer.eta_seconds = 0
            if status == "completed":
                job.transfer.percent = 100.0
            job.transfer.paused = False
            job.transfer.speed_bps = None
            job.touch()
            self._persist_locked(force=True)

    def _persist_locked(self, force: bool) -> None:
        now = time.monotonic()
        if not force and now - self._last_persist < 0.5:
            return
        self.storage.save_state(
            self._jobs.values(),
            self._favorite_models(),
            self.hidden_base_destination_keys,
        )
        self._last_persist = now

    def _require_job(self, job_id: str) -> Job:
        if job_id not in self._jobs:
            raise ValueError(f"Unknown job '{job_id}'.")
        return self._jobs[job_id]
