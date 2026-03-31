from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


JOB_STATUSES = {
    "queued",
    "paused",
    "starting",
    "probing",
    "downloading",
    "completed",
    "failed",
    "canceled",
}

ACTIVE_JOB_STATUSES = {"starting", "probing", "downloading"}
RETRYABLE_JOB_STATUSES = {"failed", "canceled"}

MEDIA_JOB_STATUSES = {
    "queued",
    "scanning",
    "compiling",
    "verifying",
    "completed",
    "failed",
    "canceled",
}

ACTIVE_MEDIA_JOB_STATUSES = {"scanning", "compiling", "verifying"}
RETRYABLE_MEDIA_JOB_STATUSES = {"failed", "canceled"}

ARCHIVE_JOB_STATUSES = {
    "queued",
    "probing",
    "extracting",
    "sorting",
    "cleaning",
    "completed",
    "failed",
    "canceled",
}
ACTIVE_ARCHIVE_JOB_STATUSES = {"probing", "extracting", "sorting", "cleaning"}
RETRYABLE_ARCHIVE_JOB_STATUSES = {"failed", "canceled"}

AUTO_EXTRACT_SET_STATUSES = {
    "waiting",
    "ready",
    "queued_for_extract",
    "extracting",
    "completed",
    "failed",
    "canceled",
}
ACTIVE_AUTO_EXTRACT_SET_STATUSES = {"ready", "queued_for_extract", "extracting"}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class EventLogEntry:
    id: int | None = None
    created_at: str = field(default_factory=utcnow_iso)
    level: str = "info"
    subsystem: str = "app"
    feature: str = ""
    message: str = ""
    job_id: str | None = None
    batch_id: str | None = None
    context: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class TransferStatus:
    bytes_done: int = 0
    bytes_total: int | None = None
    percent: float | None = None
    speed_bps: float | None = None
    eta_seconds: int | None = None
    paused: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    last_message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "TransferStatus":
        payload = data or {}
        return cls(
            bytes_done=int(payload.get("bytes_done", 0) or 0),
            bytes_total=payload.get("bytes_total"),
            percent=payload.get("percent"),
            speed_bps=payload.get("speed_bps"),
            eta_seconds=payload.get("eta_seconds"),
            paused=bool(payload.get("paused", False)),
            started_at=payload.get("started_at"),
            finished_at=payload.get("finished_at"),
            last_message=payload.get("last_message", ""),
        )


@dataclass(slots=True)
class Job:
    id: str
    batch_id: str
    url: str
    destination_key: str
    destination_path: str
    display_name: str
    destination_relative_path: str = ""
    destination_is_custom: bool = False
    auto_extract_enabled: bool = False
    archive_auto_sort_enabled: bool = False
    archive_auto_delete_enabled: bool = False
    archive_movies_target_path: str | None = None
    archive_tv_target_path: str | None = None
    status: str = "queued"
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)
    error: str | None = None
    transfer: TransferStatus = field(default_factory=TransferStatus)
    output_tail: list[str] = field(default_factory=list)

    def append_output(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        self.output_tail.append(line)
        self.output_tail = self.output_tail[-12:]
        self.transfer.last_message = line

    def touch(self) -> None:
        self.updated_at = utcnow_iso()

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["transfer"] = self.transfer.to_dict()
        return payload

    @classmethod
    def from_dict(cls, data: dict) -> "Job":
        return cls(
            id=data["id"],
            batch_id=data["batch_id"],
            url=data["url"],
            destination_key=data["destination_key"],
            destination_path=data["destination_path"],
            destination_relative_path=data.get("destination_relative_path", ""),
            display_name=data.get("display_name") or data["url"],
            destination_is_custom=bool(data.get("destination_is_custom", False)),
            auto_extract_enabled=bool(data.get("auto_extract_enabled", False)),
            archive_auto_sort_enabled=bool(data.get("archive_auto_sort_enabled", False)),
            archive_auto_delete_enabled=bool(data.get("archive_auto_delete_enabled", False)),
            archive_movies_target_path=data.get("archive_movies_target_path"),
            archive_tv_target_path=data.get("archive_tv_target_path"),
            status=data.get("status", "queued"),
            created_at=data.get("created_at", utcnow_iso()),
            updated_at=data.get("updated_at", utcnow_iso()),
            error=data.get("error"),
            transfer=TransferStatus.from_dict(data.get("transfer")),
            output_tail=list(data.get("output_tail", [])),
        )


@dataclass(slots=True)
class ExplorerEntry:
    name: str
    relative_path: str
    is_dir: bool
    size: int | None
    modified_at: str | None
    is_zip: bool = False
    is_archive: bool = False
    archive_type: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class ArchiveJob:
    id: str
    batch_id: str
    root_key: str
    archive_relative_path: str
    archive_path: str
    archive_display_name: str
    archive_type: str
    target_relative_path: str
    target_path: str
    archive_password: str | None = None
    auto_sort_enabled: bool = False
    auto_delete_enabled: bool = False
    movies_target_path: str | None = None
    tv_target_path: str | None = None
    sort_summary: dict = field(default_factory=dict)
    auto_delete_summary: dict = field(default_factory=dict)
    status: str = "queued"
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)
    error: str | None = None
    transfer: TransferStatus = field(default_factory=TransferStatus)
    output_tail: list[str] = field(default_factory=list)

    def append_output(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        self.output_tail.append(line)
        self.output_tail = self.output_tail[-12:]
        self.transfer.last_message = line

    def touch(self) -> None:
        self.updated_at = utcnow_iso()

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload.pop("archive_password", None)
        payload["transfer"] = self.transfer.to_dict()
        return payload

    @classmethod
    def from_dict(cls, data: dict) -> "ArchiveJob":
        return cls(
            id=data["id"],
            batch_id=data["batch_id"],
            root_key=data["root_key"],
            archive_relative_path=data["archive_relative_path"],
            archive_path=data["archive_path"],
            archive_display_name=data["archive_display_name"],
            archive_type=data["archive_type"],
            target_relative_path=data["target_relative_path"],
            target_path=data["target_path"],
            archive_password=data.get("archive_password"),
            auto_sort_enabled=bool(data.get("auto_sort_enabled", False)),
            auto_delete_enabled=bool(data.get("auto_delete_enabled", False)),
            movies_target_path=data.get("movies_target_path"),
            tv_target_path=data.get("tv_target_path"),
            sort_summary=dict(data.get("sort_summary") or {}),
            auto_delete_summary=dict(data.get("auto_delete_summary") or {}),
            status=data.get("status", "queued"),
            created_at=data.get("created_at", utcnow_iso()),
            updated_at=data.get("updated_at", utcnow_iso()),
            error=data.get("error"),
            transfer=TransferStatus.from_dict(data.get("transfer")),
            output_tail=list(data.get("output_tail", [])),
        )


@dataclass(slots=True)
class FavoriteDestination:
    key: str
    label: str
    path: str
    favorite: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "FavoriteDestination":
        return cls(
            key=data["key"],
            label=data["label"],
            path=data["path"],
            favorite=bool(data.get("favorite", True)),
        )


@dataclass(slots=True)
class MoveFavorite:
    key: str
    label: str
    path: str

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "MoveFavorite":
        return cls(
            key=data["key"],
            label=data["label"],
            path=data["path"],
        )


@dataclass(slots=True)
class ArchiveAutomationSettings:
    auto_sort_enabled: bool = False
    auto_delete_enabled: bool = False

    def normalized(self) -> "ArchiveAutomationSettings":
        return ArchiveAutomationSettings(
            auto_sort_enabled=bool(self.auto_sort_enabled),
            auto_delete_enabled=bool(self.auto_sort_enabled and self.auto_delete_enabled),
        )

    def to_dict(self) -> dict:
        normalized = self.normalized()
        return {
            "auto_sort_enabled": normalized.auto_sort_enabled,
            "auto_delete_enabled": normalized.auto_delete_enabled,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "ArchiveAutomationSettings":
        payload = data or {}
        return cls(
            auto_sort_enabled=bool(payload.get("auto_sort_enabled", False)),
            auto_delete_enabled=bool(payload.get("auto_delete_enabled", False)),
        ).normalized()


@dataclass(slots=True)
class AutoExtractSet:
    id: str
    batch_id: str
    destination_key: str
    destination_path: str
    destination_relative_path: str = ""
    destination_is_custom: bool = False
    set_key: str = ""
    archive_type: str = ""
    multipart_style: str = ""
    archive_relative_path: str = ""
    entrypoint_filename: str = ""
    expected_part_filenames: list[str] = field(default_factory=list)
    job_ids: list[str] = field(default_factory=list)
    archive_job_id: str | None = None
    auto_sort_enabled: bool = False
    auto_delete_enabled: bool = False
    movies_target_path: str | None = None
    tv_target_path: str | None = None
    target_relative_path: str = ""
    target_path: str | None = None
    status: str = "waiting"
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)
    error: str | None = None
    last_message: str = ""

    def touch(self) -> None:
        self.updated_at = utcnow_iso()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AutoExtractSet":
        return cls(
            id=data["id"],
            batch_id=data["batch_id"],
            destination_key=data["destination_key"],
            destination_path=data["destination_path"],
            destination_relative_path=data.get("destination_relative_path", ""),
            destination_is_custom=bool(data.get("destination_is_custom", False)),
            set_key=data.get("set_key", ""),
            archive_type=data.get("archive_type", ""),
            multipart_style=data.get("multipart_style", ""),
            archive_relative_path=data.get("archive_relative_path", ""),
            entrypoint_filename=data.get("entrypoint_filename", ""),
            expected_part_filenames=list(data.get("expected_part_filenames", [])),
            job_ids=list(data.get("job_ids", [])),
            archive_job_id=data.get("archive_job_id"),
            auto_sort_enabled=bool(data.get("auto_sort_enabled", False)),
            auto_delete_enabled=bool(data.get("auto_delete_enabled", False)),
            movies_target_path=data.get("movies_target_path"),
            tv_target_path=data.get("tv_target_path"),
            target_relative_path=data.get("target_relative_path", ""),
            target_path=data.get("target_path"),
            status=data.get("status", "waiting"),
            created_at=data.get("created_at", utcnow_iso()),
            updated_at=data.get("updated_at", utcnow_iso()),
            error=data.get("error"),
            last_message=data.get("last_message", ""),
        )


@dataclass(slots=True)
class MediaVerification:
    dolby_vision: bool = False
    dolby_atmos: bool = False
    video_codec: str | None = None
    audio_codec: str | None = None
    verified_at: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "MediaVerification":
        payload = data or {}
        return cls(
            dolby_vision=bool(payload.get("dolby_vision", False)),
            dolby_atmos=bool(payload.get("dolby_atmos", False)),
            video_codec=payload.get("video_codec"),
            audio_codec=payload.get("audio_codec"),
            verified_at=payload.get("verified_at"),
        )


@dataclass(slots=True)
class MediaJob:
    id: str
    batch_id: str
    source_root_key: str
    source_relative_path: str
    source_path: str
    source_display_name: str
    output_destination_key: str
    output_destination_path: str
    output_destination_relative_path: str = ""
    output_destination_is_custom: bool = False
    output_file_path: str | None = None
    staging_directory: str | None = None
    staged_output_file_path: str | None = None
    mkv_filename: str | None = None
    title_id: int | None = None
    title_name: str | None = None
    title_duration_seconds: int | None = None
    title_size_bytes: int | None = None
    status: str = "queued"
    created_at: str = field(default_factory=utcnow_iso)
    updated_at: str = field(default_factory=utcnow_iso)
    error: str | None = None
    transfer: TransferStatus = field(default_factory=TransferStatus)
    verification: MediaVerification = field(default_factory=MediaVerification)
    output_tail: list[str] = field(default_factory=list)

    def append_output(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        self.output_tail.append(line)
        self.output_tail = self.output_tail[-18:]
        self.transfer.last_message = line

    def touch(self) -> None:
        self.updated_at = utcnow_iso()

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["transfer"] = self.transfer.to_dict()
        payload["verification"] = self.verification.to_dict()
        return payload

    @classmethod
    def from_dict(cls, data: dict) -> "MediaJob":
        return cls(
            id=data["id"],
            batch_id=data["batch_id"],
            source_root_key=data["source_root_key"],
            source_relative_path=data["source_relative_path"],
            source_path=data["source_path"],
            source_display_name=data.get("source_display_name") or data["source_path"],
            output_destination_key=data["output_destination_key"],
            output_destination_path=data["output_destination_path"],
            output_destination_relative_path=data.get("output_destination_relative_path", ""),
            output_destination_is_custom=bool(data.get("output_destination_is_custom", False)),
            output_file_path=data.get("output_file_path"),
            staging_directory=data.get("staging_directory"),
            staged_output_file_path=data.get("staged_output_file_path"),
            mkv_filename=data.get("mkv_filename"),
            title_id=data.get("title_id"),
            title_name=data.get("title_name"),
            title_duration_seconds=data.get("title_duration_seconds"),
            title_size_bytes=data.get("title_size_bytes"),
            status=data.get("status", "queued"),
            created_at=data.get("created_at", utcnow_iso()),
            updated_at=data.get("updated_at", utcnow_iso()),
            error=data.get("error"),
            transfer=TransferStatus.from_dict(data.get("transfer")),
            verification=MediaVerification.from_dict(data.get("verification")),
            output_tail=list(data.get("output_tail", [])),
        )
