from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from pathlib import Path
from queue import Empty, Queue

from archives import ArchiveError, extract_archive
from models import ACTIVE_ARCHIVE_JOB_STATUSES, ARCHIVE_JOB_STATUSES, ArchiveJob, TransferStatus, utcnow_iso
from storage import JsonStorage


LOGGER = logging.getLogger(__name__)


class ArchiveExtractManager:
    def __init__(self, storage: JsonStorage, *, seven_zip_binary: str = "7z"):
        self.storage = storage
        self.seven_zip_binary = seven_zip_binary
        self._lock = threading.RLock()
        self._queue: Queue[str] = Queue()
        self._stop_event = threading.Event()
        self._worker = threading.Thread(target=self._worker_loop, name="archive-extract-worker", daemon=True)
        self._progress_samples: dict[str, tuple[float, int]] = {}
        self._summary_throughput_sample: tuple[float, tuple[str, ...], int] | None = None
        self._jobs: dict[str, ArchiveJob] = {}
        self._last_persist = 0.0
        self._load_jobs()
        self._worker.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _load_jobs(self) -> None:
        loaded_jobs = self.storage.load_archive_jobs()
        with self._lock:
            for job in loaded_jobs:
                if job.status in ACTIVE_ARCHIVE_JOB_STATUSES:
                    job.status = "failed"
                    job.error = "Extraction interrupted by service restart."
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

    def _rebuild_queue_locked(self) -> None:
        ordered_job_ids = self._queued_job_ids_locked()
        with self._queue.mutex:
            self._queue.queue.clear()
            self._queue.queue.extend(ordered_job_ids)
            if ordered_job_ids:
                self._queue.not_empty.notify_all()

    def submit(self, prepared_jobs: list[dict]) -> list[ArchiveJob]:
        if not prepared_jobs:
            return []
        batch_id = uuid.uuid4().hex[:12]
        created: list[ArchiveJob] = []
        with self._lock:
            for prepared in prepared_jobs:
                job = ArchiveJob(
                    id=uuid.uuid4().hex,
                    batch_id=batch_id,
                    root_key=prepared["root_key"],
                    archive_relative_path=prepared["archive_relative_path"],
                    archive_path=prepared["archive_path"],
                    archive_display_name=prepared["archive_display_name"],
                    archive_type=prepared["archive_type"],
                    target_relative_path=prepared["target_relative_path"],
                    target_path=prepared["target_path"],
                    transfer=TransferStatus(bytes_total=prepared.get("bytes_total")),
                )
                self._jobs[job.id] = job
                created.append(job)
            self._rebuild_queue_locked()
            self._persist_locked(force=True)
        return created

    def dashboard_payload(self) -> dict:
        with self._lock:
            queued_job_ids = self._queued_job_ids_locked()
            queued_jobs = [self._jobs[job_id] for job_id in queued_job_ids if job_id in self._jobs]
            active_jobs = sorted(
                [job for job in self._jobs.values() if job.status in ACTIVE_ARCHIVE_JOB_STATUSES],
                key=lambda item: item.updated_at,
                reverse=True,
            )
            finished_jobs = sorted(
                [job for job in self._jobs.values() if job.status not in {"queued", *ACTIVE_ARCHIVE_JOB_STATUSES}],
                key=lambda item: item.updated_at,
                reverse=True,
            )
            jobs = active_jobs + queued_jobs + finished_jobs
            summary_throughput_bps = self._summary_throughput_bps_locked(active_jobs)
            job_dicts = [self._job_payload(job) for job in jobs]

        summary = {
            "total_jobs": 0,
            "queued_jobs": 0,
            "active_jobs": 0,
            "completed_jobs": 0,
            "failed_jobs": 0,
            "throughput_bps": summary_throughput_bps or 0.0,
            "bytes_done": 0,
            "bytes_total": 0,
            "has_unknown_total": False,
        }
        for job in job_dicts:
            summary["total_jobs"] += 1
            if job["status"] == "queued":
                summary["queued_jobs"] += 1
            elif job["status"] in ACTIVE_ARCHIVE_JOB_STATUSES:
                summary["active_jobs"] += 1
            elif job["status"] == "completed":
                summary["completed_jobs"] += 1
            elif job["status"] == "failed":
                summary["failed_jobs"] += 1
            summary["bytes_done"] += job["transfer"]["bytes_done"]
            if job["transfer"]["bytes_total"] is None:
                summary["has_unknown_total"] = True
            else:
                summary["bytes_total"] += job["transfer"]["bytes_total"]

        return {
            "summary": summary,
            "jobs": job_dicts,
            "updated_at": utcnow_iso(),
        }

    def _summary_throughput_bps_locked(self, active_jobs: list[ArchiveJob]) -> float | None:
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

    def _job_payload(self, job: ArchiveJob) -> dict:
        payload = job.to_dict()
        payload["target_display"] = job.target_relative_path or Path(job.target_path).name
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
                    self._progress_samples[job_id] = (time.monotonic(), 0)
                    job.status = "extracting"
                    job.error = None
                    job.transfer.started_at = utcnow_iso()
                    job.transfer.finished_at = None
                    job.transfer.bytes_done = 0
                    job.transfer.percent = 0.0 if job.transfer.bytes_total else None
                    job.append_output("Queued archive extraction started.")
                    job.touch()
                    self._persist_locked(force=True)

                final_status = "completed"
                final_error: str | None = None
                try:
                    extract_archive(
                        Path(job.archive_path),
                        Path(job.target_path),
                        seven_zip_binary=self.seven_zip_binary,
                        progress_callback=lambda **kwargs: self._update_job(job.id, **kwargs),
                    )
                except Exception as exc:
                    final_status = "failed"
                    final_error = str(exc)

                self._finish_job(job_id, status=final_status, error=final_error)
            except Exception as exc:
                LOGGER.exception("Archive worker failed while handling job %s", job_id)
                try:
                    self._finish_job(job_id, status="failed", error=str(exc))
                except Exception:
                    LOGGER.exception("Archive worker could not persist failure state for job %s", job_id)
            finally:
                with self._lock:
                    self._progress_samples.pop(job_id, None)

    def _update_job(self, job_id: str, **kwargs) -> None:
        with self._lock:
            job = self._require_job(job_id)
            status = kwargs.get("status")
            if status in ARCHIVE_JOB_STATUSES:
                job.status = status
            transfer = job.transfer
            speed_provided = "speed_bps" in kwargs and kwargs["speed_bps"] not in {None, 0}
            if "bytes_done" in kwargs and kwargs["bytes_done"] is not None:
                transfer.bytes_done = int(kwargs["bytes_done"])
            if "bytes_total" in kwargs and kwargs["bytes_total"] is not None:
                transfer.bytes_total = int(kwargs["bytes_total"])
            if transfer.bytes_total:
                transfer.percent = max(0.0, min(100.0, (transfer.bytes_done / transfer.bytes_total) * 100.0))
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
                if job.transfer.bytes_total is not None:
                    job.transfer.bytes_done = job.transfer.bytes_total
                job.transfer.percent = 100.0
                job.transfer.eta_seconds = 0
                job.append_output("Archive extraction finished.")
            else:
                job.append_output(error or "Archive extraction failed.")
            job.transfer.speed_bps = None
            job.touch()
            self._persist_locked(force=True)

    def _persist_locked(self, force: bool) -> None:
        now = time.monotonic()
        if not force and now - self._last_persist < 0.5:
            return
        self.storage.save_archive_jobs(self._jobs.values())
        self._last_persist = now

    def _require_job(self, job_id: str) -> ArchiveJob:
        if job_id not in self._jobs:
            raise ValueError(f"Unknown archive job '{job_id}'.")
        return self._jobs[job_id]
