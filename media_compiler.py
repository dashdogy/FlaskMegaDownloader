from __future__ import annotations

import csv
import json
import logging
import math
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import Iterable

from models import (
    ACTIVE_MEDIA_JOB_STATUSES,
    MEDIA_JOB_STATUSES,
    RETRYABLE_MEDIA_JOB_STATUSES,
    MediaJob,
    MediaVerification,
    TransferStatus,
    utcnow_iso,
)
from process_utils import stop_process
from storage import JsonStorage


LOGGER = logging.getLogger(__name__)
TINFO_NAME_IDS = {"2"}
TINFO_DURATION_IDS = {"9"}
TINFO_SIZE_BYTES_IDS = {"11"}
TINFO_SIZE_TEXT_IDS = {"10"}
TINFO_SOURCE_FILE_IDS = {"16"}
TINFO_OUTPUT_FILENAME_IDS = {"27"}
TINFO_TREE_INFO_IDS = {"30"}
TEXT_SIZE_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[KMGTP]?i?B)", re.IGNORECASE)


class MediaCompileError(Exception):
    pass


class MediaCompileCanceled(Exception):
    pass


@dataclass(slots=True)
class BluraySource:
    source_path: Path
    source_relative_path: str
    source_display_name: str


@dataclass(slots=True)
class TitleInfo:
    title_id: int
    name: str | None = None
    duration_seconds: int | None = None
    size_bytes: int | None = None
    source_file_name: str | None = None
    output_file_name: str | None = None
    tree_info: str | None = None


def resolve_binary(binary_name: str) -> str | None:
    if not binary_name:
        return None
    resolved = shutil.which(binary_name)
    if resolved:
        return resolved
    candidate = Path(binary_name).expanduser()
    if candidate.exists():
        return str(candidate.resolve())
    return None


def parse_robot_fields(line: str) -> tuple[str, list[str]] | None:
    if ":" not in line:
        return None
    prefix, payload = line.split(":", 1)
    try:
        fields = next(csv.reader([payload]))
    except (csv.Error, StopIteration):
        return prefix, [payload]
    return prefix, fields


def parse_duration_seconds(value: str | None) -> int | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) not in {2, 3}:
        return None
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        return None
    if len(numbers) == 2:
        minutes, seconds = numbers
        return minutes * 60 + seconds
    hours, minutes, seconds = numbers
    return hours * 3600 + minutes * 60 + seconds


def parse_text_size_bytes(value: str | None) -> int | None:
    if not value:
        return None
    match = TEXT_SIZE_RE.search(str(value))
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
    factor = scale.get(unit)
    if factor is None:
        return None
    return int(number * factor)


def sanitize_mkv_filename(value: str, fallback: str) -> str:
    cleaned = (value or "").strip().strip('"').strip()
    if not cleaned:
        cleaned = fallback
    cleaned = re.sub(r"[<>:\"/\\\\|?*]", "_", cleaned)
    cleaned = cleaned.replace("\x00", "_").strip().rstrip(".")
    if not cleaned:
        cleaned = fallback
    if not cleaned.lower().endswith(".mkv"):
        cleaned = f"{cleaned}.mkv"
    return cleaned


def detect_bluray_source(entry_path: Path, relative_path: str) -> BluraySource | None:
    if not entry_path.is_dir():
        return None
    if (entry_path / "BDMV" / "index.bdmv").is_file():
        return BluraySource(
            source_path=entry_path.resolve(),
            source_relative_path=relative_path,
            source_display_name=entry_path.name or "Blu-ray",
        )
    if entry_path.name.upper() == "BDMV" and (entry_path / "index.bdmv").is_file():
        source_root = entry_path.parent.resolve()
        source_relative = str(Path(relative_path).parent).replace("\\", "/")
        source_relative = "" if source_relative == "." else source_relative
        return BluraySource(
            source_path=source_root,
            source_relative_path=source_relative,
            source_display_name=source_root.name or "Blu-ray",
        )
    return None


def build_source_spec(source_path: Path) -> str:
    return f"file:{source_path}"


def parse_info_titles(output: str) -> list[TitleInfo]:
    titles: dict[int, TitleInfo] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line.startswith("TINFO:"):
            continue
        record = parse_robot_fields(line)
        if not record:
            continue
        _, fields = record
        if len(fields) < 4:
            continue
        try:
            title_id = int(fields[0])
        except ValueError:
            continue
        attribute_id = fields[1]
        value = fields[3]
        title = titles.setdefault(title_id, TitleInfo(title_id=title_id))
        if attribute_id in TINFO_NAME_IDS and value:
            title.name = value
        elif attribute_id in TINFO_DURATION_IDS:
            title.duration_seconds = parse_duration_seconds(value)
        elif attribute_id in TINFO_SIZE_BYTES_IDS:
            try:
                title.size_bytes = int(value)
            except ValueError:
                pass
        elif attribute_id in TINFO_SIZE_TEXT_IDS and title.size_bytes is None:
            title.size_bytes = parse_text_size_bytes(value)
        elif attribute_id in TINFO_SOURCE_FILE_IDS and value:
            title.source_file_name = value
        elif attribute_id in TINFO_OUTPUT_FILENAME_IDS and value:
            title.output_file_name = value
        elif attribute_id in TINFO_TREE_INFO_IDS and value:
            title.tree_info = value

    parsed_titles = list(titles.values())
    parsed_titles.sort(key=lambda item: item.title_id)
    return parsed_titles


def choose_main_feature(titles: Iterable[TitleInfo], minimum_seconds: int) -> TitleInfo:
    candidates = [title for title in titles if (title.duration_seconds or 0) >= minimum_seconds]
    if not candidates:
        raise MediaCompileError(
            f"No Blu-ray title met the minimum main-feature threshold of {minimum_seconds} seconds."
        )
    return max(
        candidates,
        key=lambda item: (
            item.duration_seconds or 0,
            item.size_bytes or 0,
            item.title_id,
        ),
    )


def human_scan_summary(titles: Iterable[TitleInfo]) -> str:
    labels: list[str] = []
    for title in titles:
        duration = title.duration_seconds or 0
        hours = duration // 3600
        minutes = (duration % 3600) // 60
        seconds = duration % 60
        labels.append(f"title {title.title_id}: {hours:d}:{minutes:02d}:{seconds:02d}")
    return ", ".join(labels) or "no titles found"


def parse_mediainfo_json(raw_json: str) -> MediaVerification:
    payload = json.loads(raw_json or "{}")
    tracks = payload.get("media", {}).get("track", [])
    verification = MediaVerification(verified_at=utcnow_iso())
    for track in tracks:
        track_type = str(track.get("@type") or "").lower()
        values = " ".join(str(value) for value in track.values() if value is not None)
        lowered = values.lower()
        if track_type == "video" and verification.video_codec is None:
            verification.video_codec = (
                track.get("Format_Commercial_IfAny")
                or track.get("Format")
                or track.get("CodecID")
            )
        if track_type == "audio" and verification.audio_codec is None:
            verification.audio_codec = (
                track.get("Format_Commercial_IfAny")
                or track.get("Format")
                or track.get("CodecID")
            )
        if track_type == "video" and "dolby vision" in lowered:
            verification.dolby_vision = True
        if track_type == "audio" and "atmos" in lowered:
            verification.dolby_atmos = True
    return verification


class MediaCompileManager:
    def __init__(
        self,
        storage: JsonStorage,
        makemkvcon_binary: str,
        mediainfo_binary: str,
        bluray_min_title_seconds: int,
    ):
        self.storage = storage
        self.makemkvcon_binary = makemkvcon_binary
        self.mediainfo_binary = mediainfo_binary
        self.bluray_min_title_seconds = max(int(bluray_min_title_seconds), 1)
        self._lock = threading.RLock()
        self._queue: Queue[str] = Queue()
        self._stop_event = threading.Event()
        self._worker = threading.Thread(target=self._worker_loop, name="media-compile-worker", daemon=True)
        self._cancel_events: dict[str, threading.Event] = {}
        self._active_processes: dict[str, subprocess.Popen] = {}
        self._progress_samples: dict[str, tuple[float, int]] = {}
        self._summary_throughput_sample: tuple[float, tuple[str, ...], int] | None = None
        self._jobs: dict[str, MediaJob] = {}
        self._last_persist = 0.0
        self._makemkvcon_path = resolve_binary(self.makemkvcon_binary)
        self._mediainfo_path = resolve_binary(self.mediainfo_binary)
        self.backend_reason = self._build_backend_reason()
        self._load_jobs()
        self._worker.start()

    def _build_backend_reason(self) -> str | None:
        missing: list[str] = []
        if not self._makemkvcon_path:
            missing.append(f"'{self.makemkvcon_binary}' was not found in PATH")
        if not self._mediainfo_path:
            missing.append(f"'{self.mediainfo_binary}' was not found in PATH")
        if not missing:
            return None
        return ". ".join(missing) + ". Install MakeMKV CLI and MediaInfo to enable Blu-ray remux jobs."

    def backend_payload(self) -> dict:
        return {
            "available": self.backend_reason is None,
            "label": "MakeMKV + MediaInfo" if self.backend_reason is None else "Blu-ray backend unavailable",
            "reason": self.backend_reason,
        }

    def stop(self) -> None:
        self._stop_event.set()
        for cancel_event in self._cancel_events.values():
            cancel_event.set()
        with self._lock:
            for process in list(self._active_processes.values()):
                stop_process(process)

    def _load_jobs(self) -> None:
        loaded_jobs = self.storage.load_media_jobs()
        with self._lock:
            for job in loaded_jobs:
                if job.status in ACTIVE_MEDIA_JOB_STATUSES:
                    job.status = "failed"
                    job.error = "Compile interrupted by service restart. Retry to start again."
                    job.transfer.finished_at = utcnow_iso()
                self._jobs[job.id] = job
            self._rebuild_queue_locked()

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

    def submit(
        self,
        sources: Iterable[BluraySource],
        *,
        source_root_key: str,
        output_destination_key: str,
        output_destination_path: Path,
        output_destination_relative_path: str,
        output_destination_is_custom: bool,
    ) -> list[MediaJob]:
        if self.backend_reason:
            raise ValueError(self.backend_reason)

        batch_id = uuid.uuid4().hex[:12]
        new_jobs: list[MediaJob] = []
        with self._lock:
            for source in sources:
                job = MediaJob(
                    id=uuid.uuid4().hex,
                    batch_id=batch_id,
                    source_root_key=source_root_key,
                    source_relative_path=source.source_relative_path,
                    source_path=str(source.source_path),
                    source_display_name=source.source_display_name,
                    output_destination_key=output_destination_key,
                    output_destination_path=str(output_destination_path),
                    output_destination_relative_path=output_destination_relative_path,
                    output_destination_is_custom=output_destination_is_custom,
                )
                self._jobs[job.id] = job
                new_jobs.append(job)
            self._rebuild_queue_locked()
            self._persist_locked(force=True)
        return new_jobs

    def destination_in_use(self, destination_key: str) -> bool:
        with self._lock:
            return any(
                job.output_destination_key == destination_key and job.status in {"queued", *ACTIVE_MEDIA_JOB_STATUSES}
                for job in self._jobs.values()
            )

    def cancel_job(self, job_id: str) -> MediaJob:
        with self._lock:
            job = self._require_job(job_id)
            if job.status == "queued":
                job.status = "canceled"
                job.error = "Canceled before the remux started."
                job.transfer.finished_at = utcnow_iso()
                job.touch()
                self._rebuild_queue_locked()
                self._persist_locked(force=True)
                return job
            if job.status not in ACTIVE_MEDIA_JOB_STATUSES:
                raise ValueError("Only queued or active Blu-ray jobs can be canceled.")
            cancel_event = self._cancel_events.setdefault(job_id, threading.Event())
            cancel_event.set()
            process = self._active_processes.get(job_id)
            if process and process.poll() is None:
                stop_process(process)
            job.error = "Cancel request sent."
            job.touch()
            self._persist_locked(force=True)
            return job

    def retry_job(self, job_id: str) -> MediaJob:
        with self._lock:
            job = self._require_job(job_id)
            if job.status not in RETRYABLE_MEDIA_JOB_STATUSES:
                raise ValueError("Only failed or canceled Blu-ray jobs can be retried.")
            job.status = "queued"
            job.error = None
            job.title_id = None
            job.title_name = None
            job.title_duration_seconds = None
            job.title_size_bytes = None
            job.output_file_path = None
            job.staging_directory = None
            job.staged_output_file_path = None
            job.mkv_filename = None
            job.transfer = TransferStatus()
            job.verification = MediaVerification()
            job.output_tail.clear()
            job.touch()
            self._cancel_events.pop(job_id, None)
            self._active_processes.pop(job_id, None)
            self._progress_samples.pop(job_id, None)
            self._rebuild_queue_locked()
            self._persist_locked(force=True)
            return job

    def dashboard_payload(self, destination_lookup: dict[str, str] | None = None) -> dict:
        destination_lookup = destination_lookup or {}
        with self._lock:
            queued_job_ids = self._queued_job_ids_locked()
            queued_jobs = [self._jobs[job_id] for job_id in queued_job_ids if job_id in self._jobs]
            active_jobs = sorted(
                [job for job in self._jobs.values() if job.status in ACTIVE_MEDIA_JOB_STATUSES],
                key=lambda item: item.updated_at,
                reverse=True,
            )
            finished_jobs = sorted(
                [job for job in self._jobs.values() if job.status not in {"queued", *ACTIVE_MEDIA_JOB_STATUSES}],
                key=lambda item: item.updated_at,
                reverse=True,
            )
            jobs = active_jobs + queued_jobs + finished_jobs
            summary_throughput_bps = self._summary_throughput_bps_locked(active_jobs)
            job_dicts = [self._job_payload(job, destination_lookup) for job in jobs]

        summary = {
            "total_jobs": 0,
            "queued_jobs": 0,
            "active_jobs": 0,
            "completed_jobs": 0,
            "failed_jobs": 0,
            "canceled_jobs": 0,
            "throughput_bps": summary_throughput_bps or 0.0,
            "bytes_done": 0,
            "bytes_total": 0,
            "has_unknown_total": False,
        }

        for job in job_dicts:
            summary["total_jobs"] += 1
            if job["status"] == "queued":
                summary["queued_jobs"] += 1
            elif job["status"] in ACTIVE_MEDIA_JOB_STATUSES:
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

        return {
            "backend": self.backend_payload(),
            "summary": summary,
            "jobs": job_dicts,
            "updated_at": utcnow_iso(),
        }

    def _summary_throughput_bps_locked(self, active_jobs: list[MediaJob]) -> float | None:
        if not active_jobs:
            self._summary_throughput_sample = None
            return None

        now = time.monotonic()
        active_job_ids = tuple(sorted(job.id for job in active_jobs))
        total_bytes_done = sum(max(job.transfer.bytes_done, 0) for job in active_jobs)
        fallback_speed = sum(job.transfer.speed_bps or 0.0 for job in active_jobs) or None
        sample = self._summary_throughput_sample

        if sample is None:
            self._summary_throughput_sample = (now, active_job_ids, total_bytes_done)
            return fallback_speed

        sample_time, sample_job_ids, sample_bytes_done = sample
        self._summary_throughput_sample = (now, active_job_ids, total_bytes_done)
        if sample_job_ids != active_job_ids:
            return fallback_speed

        elapsed = now - sample_time
        byte_delta = total_bytes_done - sample_bytes_done
        if elapsed > 0 and byte_delta > 0:
            return byte_delta / elapsed
        return fallback_speed

    def _job_payload(self, job: MediaJob, destination_lookup: dict[str, str]) -> dict:
        payload = job.to_dict()
        destination_label = destination_lookup.get(job.output_destination_key, job.output_destination_key)
        payload["output_destination_label"] = destination_label
        payload["output_destination_display"] = (
            str(job.output_destination_path)
            if job.output_destination_is_custom
            else (
                f"{destination_label} / {job.output_destination_relative_path}"
                if job.output_destination_relative_path
                else destination_label
            )
        )
        payload["source_display"] = (
            f"{job.source_display_name} / {job.source_relative_path}"
            if job.source_relative_path
            else job.source_display_name
        )
        payload["can_cancel"] = job.status == "queued" or job.status in ACTIVE_MEDIA_JOB_STATUSES
        payload["can_retry"] = job.status in RETRYABLE_MEDIA_JOB_STATUSES
        payload["status_label"] = job.status.replace("_", " ").title()
        return payload

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                job_id = self._queue.get(timeout=0.25)
            except Empty:
                continue

            try:
                with self._lock:
                    job = self._jobs.get(job_id)
                    if not job or job.status != "queued":
                        continue
                    cancel_event = self._cancel_events[job_id] = threading.Event()
                    self._progress_samples[job_id] = (time.monotonic(), 0)
                    job.status = "scanning"
                    job.error = None
                    job.transfer.paused = False
                    job.transfer.started_at = utcnow_iso()
                    job.transfer.finished_at = None
                    job.transfer.bytes_done = 0
                    job.transfer.bytes_total = None
                    job.transfer.percent = None
                    output_destination = Path(job.output_destination_path)
                    output_destination.mkdir(parents=True, exist_ok=True)
                    staging_directory = output_destination / ".remux-staging" / job.id
                    if staging_directory.exists():
                        shutil.rmtree(staging_directory, ignore_errors=True)
                    staging_directory.mkdir(parents=True, exist_ok=True)
                    job.staging_directory = str(staging_directory)
                    job.staged_output_file_path = None
                    job.touch()
                    self._persist_locked(force=True)

                final_status = "completed"
                final_error: str | None = None
                try:
                    title = self._scan_job(job_id, cancel_event)
                    self._compile_job(job_id, cancel_event, title)
                    self._verify_job(job_id, cancel_event)
                except MediaCompileCanceled as exc:
                    final_status = "canceled"
                    final_error = str(exc)
                except Exception as exc:
                    final_status = "failed"
                    final_error = str(exc)

                self._finish_job(job_id, status=final_status, error=final_error)
            except Exception as exc:
                LOGGER.exception("Media worker failed while handling job %s", job_id)
                try:
                    self._finish_job(job_id, status="failed", error=str(exc))
                except Exception:
                    LOGGER.exception("Media worker could not persist failure state for job %s", job_id)
            finally:
                with self._lock:
                    self._cancel_events.pop(job_id, None)
                    self._active_processes.pop(job_id, None)
                    self._progress_samples.pop(job_id, None)

    def _set_active_process(self, job_id: str, process: subprocess.Popen | None) -> None:
        with self._lock:
            if process is None:
                self._active_processes.pop(job_id, None)
            else:
                self._active_processes[job_id] = process

    def _cleanup_staging_directory(self, job: MediaJob) -> None:
        if not job.staging_directory:
            return
        staging_directory = Path(job.staging_directory)
        if staging_directory.exists():
            shutil.rmtree(staging_directory, ignore_errors=True)

    def _scan_job(self, job_id: str, cancel_event: threading.Event) -> TitleInfo:
        job = self._require_job(job_id)
        command = [
            self._makemkvcon_path or self.makemkvcon_binary,
            "--messages=-stdout",
            "--progress=-same",
            "-r",
            "info",
            build_source_spec(Path(job.source_path)),
        ]
        output = self._run_robot_command(job_id, command, cancel_event, stage="scanning")
        titles = parse_info_titles(output)
        if not titles:
            raise MediaCompileError("MakeMKV did not report any Blu-ray titles for this source.")
        try:
            title = choose_main_feature(titles, self.bluray_min_title_seconds)
        except MediaCompileError as exc:
            raise MediaCompileError(f"{exc} Scan result: {human_scan_summary(titles)}.") from exc

        fallback_name = f"{job.source_display_name}_t{title.title_id:02d}.mkv"
        mkv_filename = sanitize_mkv_filename(title.output_file_name or title.name or fallback_name, fallback_name)
        output_file_path = Path(job.output_destination_path) / mkv_filename
        if output_file_path.exists():
            raise MediaCompileError(f"Output file already exists: {output_file_path}")

        self._update_job(
            job_id,
            status="scanning",
            title_id=title.title_id,
            title_name=title.name or title.tree_info or title.source_file_name or f"Title {title.title_id}",
            title_duration_seconds=title.duration_seconds,
            title_size_bytes=title.size_bytes,
            mkv_filename=mkv_filename,
            output_file_path=str(output_file_path),
            bytes_total=title.size_bytes,
            bytes_done=0,
            percent=0,
            message=f"Selected title {title.title_id} for remux.",
        )
        return title

    def _compile_job(self, job_id: str, cancel_event: threading.Event, title: TitleInfo) -> None:
        job = self._require_job(job_id)
        output_dir = Path(job.output_destination_path)
        if not job.staging_directory:
            raise MediaCompileError("Missing remux staging directory.")
        staging_directory = Path(job.staging_directory)
        output_dir.mkdir(parents=True, exist_ok=True)
        staging_directory.mkdir(parents=True, exist_ok=True)
        self._update_job(job_id, status="compiling", message="Starting MakeMKV remux.")
        command = [
            self._makemkvcon_path or self.makemkvcon_binary,
            "--messages=-stdout",
            "--progress=-same",
            "-r",
            "mkv",
            build_source_spec(Path(job.source_path)),
            str(title.title_id),
            str(staging_directory),
        ]
        self._run_robot_command(job_id, command, cancel_event, stage="compiling")

        created_files = sorted(staging_directory.glob("*.mkv"))
        if len(created_files) != 1:
            raise MediaCompileError(
                "MakeMKV remux staging did not produce exactly one MKV file."
                if created_files
                else "MakeMKV finished without producing an MKV file."
            )
        selected_output = created_files[0]
        self._update_job(job_id, staged_output_file_path=str(selected_output), mkv_filename=selected_output.name)

    def _verify_job(self, job_id: str, cancel_event: threading.Event) -> None:
        job = self._require_job(job_id)
        staged_output_file_path = Path(job.staged_output_file_path or "")
        final_output_file_path = Path(job.output_file_path or "")
        if not staged_output_file_path.exists():
            raise MediaCompileError("Compiled MKV file is missing before verification.")
        self._update_job(job_id, status="verifying", percent=100, message="Running MediaInfo verification.")
        if cancel_event.is_set():
            raise MediaCompileCanceled("Canceled by user.")

        result = subprocess.run(
            [self._mediainfo_path or self.mediainfo_binary, "--Output=JSON", str(staged_output_file_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if cancel_event.is_set():
            raise MediaCompileCanceled("Canceled by user.")
        if result.returncode != 0:
            raise MediaCompileError(result.stdout.strip() or "MediaInfo verification failed.")

        if final_output_file_path.exists():
            raise MediaCompileError(f"Output file already exists: {final_output_file_path}")
        final_output_file_path.parent.mkdir(parents=True, exist_ok=True)

        verification = parse_mediainfo_json(result.stdout)
        shutil.move(str(staged_output_file_path), str(final_output_file_path))
        self._update_job(
            job_id,
            status="verifying",
            verification=verification,
            output_file_path=str(final_output_file_path),
            staged_output_file_path=None,
            bytes_done=final_output_file_path.stat().st_size,
            percent=100,
            message="Verification finished.",
        )
        self._cleanup_staging_directory(job)

    def _run_robot_command(
        self,
        job_id: str,
        command: list[str],
        cancel_event: threading.Event,
        *,
        stage: str,
    ) -> str:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._set_active_process(job_id, process)
        output_lines: list[str] = []
        try:
            if process.stdout is not None:
                for raw_line in process.stdout:
                    if cancel_event.is_set():
                        stop_process(process)
                        raise MediaCompileCanceled("Canceled by user.")
                    line = raw_line.strip()
                    if not line:
                        continue
                    output_lines.append(line)
                    self._handle_robot_line(job_id, line, stage=stage)
            if cancel_event.is_set():
                stop_process(process)
                raise MediaCompileCanceled("Canceled by user.")
            return_code = process.wait()
            if cancel_event.is_set():
                stop_process(process)
                raise MediaCompileCanceled("Canceled by user.")
        finally:
            self._set_active_process(job_id, None)

        if return_code != 0:
            raise MediaCompileError(output_lines[-1] if output_lines else f"MakeMKV exited with code {return_code}.")
        return "\n".join(output_lines)

    def _handle_robot_line(self, job_id: str, line: str, *, stage: str) -> None:
        record = parse_robot_fields(line)
        if not record:
            self._update_job(job_id, status=stage, message=line)
            return

        prefix, fields = record
        if prefix == "MSG" and len(fields) >= 4:
            self._update_job(job_id, status=stage, message=fields[3])
            return
        if prefix == "PRGV" and len(fields) >= 3:
            try:
                total = int(fields[1])
                maximum = int(fields[2])
            except ValueError:
                return
            percent = (total / maximum * 100.0) if maximum > 0 else None
            self._update_job(job_id, status=stage, percent=percent)
            return
        if prefix in {"PRGC", "PRGT"} and fields:
            self._update_job(job_id, status=stage, message=fields[-1])

    def _update_job(self, job_id: str, **kwargs) -> None:
        with self._lock:
            job = self._require_job(job_id)
            status = kwargs.get("status")
            if status in MEDIA_JOB_STATUSES:
                job.status = status
            if "title_id" in kwargs and kwargs["title_id"] is not None:
                job.title_id = int(kwargs["title_id"])
            if "title_name" in kwargs and kwargs["title_name"]:
                job.title_name = str(kwargs["title_name"])
            if "title_duration_seconds" in kwargs and kwargs["title_duration_seconds"] is not None:
                job.title_duration_seconds = int(kwargs["title_duration_seconds"])
            if "title_size_bytes" in kwargs and kwargs["title_size_bytes"] is not None:
                job.title_size_bytes = int(kwargs["title_size_bytes"])
            if "mkv_filename" in kwargs and kwargs["mkv_filename"]:
                job.mkv_filename = str(kwargs["mkv_filename"])
            if "output_file_path" in kwargs and kwargs["output_file_path"]:
                job.output_file_path = str(kwargs["output_file_path"])
            if "staging_directory" in kwargs:
                job.staging_directory = str(kwargs["staging_directory"]) if kwargs["staging_directory"] else None
            if "staged_output_file_path" in kwargs:
                job.staged_output_file_path = (
                    str(kwargs["staged_output_file_path"]) if kwargs["staged_output_file_path"] else None
                )
            if "verification" in kwargs and kwargs["verification"] is not None:
                job.verification = kwargs["verification"]

            transfer = job.transfer
            speed_provided = "speed_bps" in kwargs and kwargs["speed_bps"] not in {None, 0}
            if "bytes_done" in kwargs and kwargs["bytes_done"] is not None:
                transfer.bytes_done = int(kwargs["bytes_done"])
            if "bytes_total" in kwargs and kwargs["bytes_total"] is not None:
                transfer.bytes_total = int(kwargs["bytes_total"])
            if "percent" in kwargs and kwargs["percent"] is not None:
                transfer.percent = max(0.0, min(100.0, float(kwargs["percent"])))
                if transfer.bytes_total:
                    transfer.bytes_done = max(
                        transfer.bytes_done,
                        int(transfer.bytes_total * (transfer.percent / 100.0)),
                    )
            if "speed_bps" in kwargs:
                transfer.speed_bps = kwargs["speed_bps"]
            if "eta_seconds" in kwargs:
                transfer.eta_seconds = kwargs["eta_seconds"]
            message = kwargs.get("message")
            if message:
                job.append_output(str(message))

            self._derive_transfer_metrics(job_id, transfer, speed_provided=speed_provided)
            job.touch()
            self._persist_locked(force=False)

    def _derive_transfer_metrics(self, job_id: str, transfer: TransferStatus, *, speed_provided: bool) -> None:
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
            elif transfer.speed_bps and transfer.speed_bps > 0:
                remaining = max(transfer.bytes_total - transfer.bytes_done, 0)
                transfer.eta_seconds = math.ceil(remaining / transfer.speed_bps) if remaining else 0

    def _finish_job(self, job_id: str, *, status: str, error: str | None) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.status = status
            job.error = error
            job.transfer.finished_at = utcnow_iso()
            if status == "completed":
                if job.output_file_path and Path(job.output_file_path).exists():
                    final_size = Path(job.output_file_path).stat().st_size
                    job.transfer.bytes_done = final_size
                    job.transfer.bytes_total = final_size
                elif job.transfer.bytes_total is not None:
                    job.transfer.bytes_done = job.transfer.bytes_total
                job.transfer.percent = 100.0
                job.transfer.eta_seconds = 0
                self._cleanup_staging_directory(job)
            else:
                self._cleanup_staging_directory(job)
            job.staged_output_file_path = None
            job.staging_directory = None
            job.transfer.paused = False
            job.transfer.speed_bps = None
            job.touch()
            self._persist_locked(force=True)

    def _persist_locked(self, force: bool) -> None:
        now = time.monotonic()
        if not force and now - self._last_persist < 0.5:
            return
        self.storage.save_media_jobs(self._jobs.values())
        self._last_persist = now

    def _require_job(self, job_id: str) -> MediaJob:
        if job_id not in self._jobs:
            raise ValueError(f"Unknown Blu-ray job '{job_id}'.")
        return self._jobs[job_id]
