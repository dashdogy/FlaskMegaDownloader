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
RETRYABLE_JOB_STATUSES = {"completed", "failed", "canceled"}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    def to_dict(self) -> dict:
        return asdict(self)


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
