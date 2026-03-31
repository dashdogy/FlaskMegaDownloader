from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".mov",
    ".m4v",
    ".wmv",
    ".ts",
    ".m2ts",
    ".mpg",
    ".mpeg",
    ".webm",
    ".flv",
}
INVALID_FOLDER_CHARS_RE = re.compile(r'[<>:"/\\\\|?*]+')


class ArchiveAutoSortError(Exception):
    pass


@dataclass(slots=True)
class SortedMediaFile:
    source_path: str
    destination_path: str
    media_kind: str

    def to_dict(self) -> dict:
        return {
            "source_path": self.source_path,
            "destination_path": self.destination_path,
            "media_kind": self.media_kind,
        }


@dataclass(slots=True)
class ArchiveSortSummary:
    moved_movies: int = 0
    moved_tv: int = 0
    skipped_unclear: int = 0
    skipped_conflict: int = 0
    skipped_non_video: int = 0
    already_in_place: int = 0
    failed: int = 0
    moved_files: list[SortedMediaFile] = None

    def __post_init__(self) -> None:
        if self.moved_files is None:
            self.moved_files = []

    def to_dict(self) -> dict:
        return {
            "moved_movies": self.moved_movies,
            "moved_tv": self.moved_tv,
            "skipped_unclear": self.skipped_unclear,
            "skipped_conflict": self.skipped_conflict,
            "skipped_non_video": self.skipped_non_video,
            "already_in_place": self.already_in_place,
            "failed": self.failed,
            "moved_files": [item.to_dict() for item in self.moved_files],
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "ArchiveSortSummary":
        payload = data or {}
        moved_files = [
            SortedMediaFile(
                source_path=item["source_path"],
                destination_path=item["destination_path"],
                media_kind=item["media_kind"],
            )
            for item in payload.get("moved_files", [])
        ]
        return cls(
            moved_movies=int(payload.get("moved_movies", 0) or 0),
            moved_tv=int(payload.get("moved_tv", 0) or 0),
            skipped_unclear=int(payload.get("skipped_unclear", 0) or 0),
            skipped_conflict=int(payload.get("skipped_conflict", 0) or 0),
            skipped_non_video=int(payload.get("skipped_non_video", 0) or 0),
            already_in_place=int(payload.get("already_in_place", 0) or 0),
            failed=int(payload.get("failed", 0) or 0),
            moved_files=moved_files,
        )

    def has_results(self) -> bool:
        return any(
            (
                self.moved_movies,
                self.moved_tv,
                self.skipped_unclear,
                self.skipped_conflict,
                self.skipped_non_video,
                self.already_in_place,
                self.failed,
                self.moved_files,
            )
        )


def guessit_available() -> tuple[bool, str | None]:
    try:
        from guessit import guessit as _guessit  # noqa: F401
    except ImportError:
        return False, "Archive auto-sort is unavailable because the 'guessit' package is not installed."
    return True, None


def _guess_media_info(filename: str) -> dict:
    available, reason = guessit_available()
    if not available:
        raise ArchiveAutoSortError(reason or "Archive auto-sort is unavailable.")
    from guessit import guessit

    return dict(guessit(filename))


def _normalize_series_folder_name(name: str | None) -> str | None:
    cleaned = str(name or "").strip()
    if not cleaned:
        return None
    cleaned = INVALID_FOLDER_CHARS_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".")
    if not cleaned or cleaned in {".", ".."}:
        return None
    return cleaned


def classify_video_path(path: Path) -> dict[str, str | None]:
    info = _guess_media_info(path.name)
    media_type = str(info.get("type") or "").strip().lower()
    if media_type == "episode" or info.get("episode") is not None or info.get("season") is not None:
        series_title = _normalize_series_folder_name(
            info.get("title") or info.get("series") or info.get("show")
        )
        if not series_title:
            return {"kind": "unknown", "series_title": None}
        return {"kind": "tv", "series_title": series_title}
    if media_type == "movie":
        return {"kind": "movie", "series_title": None}
    return {"kind": "unknown", "series_title": None}


def build_sort_summary_message(summary: ArchiveSortSummary) -> str:
    parts: list[str] = []
    if summary.moved_movies:
        parts.append(f"{summary.moved_movies} to Movies")
    if summary.moved_tv:
        parts.append(f"{summary.moved_tv} to TvShows")
    if summary.skipped_unclear:
        parts.append(f"{summary.skipped_unclear} unclear")
    if summary.skipped_conflict:
        parts.append(f"{summary.skipped_conflict} conflict")
    if summary.skipped_non_video:
        parts.append(f"{summary.skipped_non_video} non-video")
    if summary.already_in_place:
        parts.append(f"{summary.already_in_place} already in place")
    if summary.failed:
        parts.append(f"{summary.failed} failed")
    if not parts:
        return "Auto-sort finished with no eligible video files."
    return f"Auto-sort finished: {', '.join(parts)}."


def sort_extracted_videos(
    extracted_paths: list[str | Path],
    *,
    movies_target_path: Path,
    tv_target_path: Path,
    cancel_requested: Callable[[], bool] | None = None,
) -> ArchiveSortSummary:
    summary = ArchiveSortSummary()
    movies_root = Path(movies_target_path).expanduser().resolve()
    tv_root = Path(tv_target_path).expanduser().resolve()
    movies_root.mkdir(parents=True, exist_ok=True)
    tv_root.mkdir(parents=True, exist_ok=True)

    for raw_path in extracted_paths:
        if cancel_requested and cancel_requested():
            raise ArchiveAutoSortError("Archive extraction canceled.")

        source_path = Path(raw_path)
        if not source_path.exists() or not source_path.is_file():
            continue
        if source_path.suffix.lower() not in VIDEO_EXTENSIONS:
            summary.skipped_non_video += 1
            continue

        classification = classify_video_path(source_path)
        media_kind = classification["kind"]
        if media_kind == "movie":
            destination_path = movies_root / source_path.name
        elif media_kind == "tv":
            series_title = classification["series_title"]
            if not series_title:
                summary.skipped_unclear += 1
                continue
            destination_path = tv_root / series_title / source_path.name
        else:
            summary.skipped_unclear += 1
            continue

        resolved_source = source_path.resolve()
        resolved_destination = destination_path.resolve(strict=False)
        if resolved_source == resolved_destination:
            summary.already_in_place += 1
            continue
        if destination_path.exists():
            summary.skipped_conflict += 1
            continue

        destination_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(source_path), str(destination_path))
        except (OSError, shutil.Error):
            summary.failed += 1
            continue

        summary.moved_files.append(
            SortedMediaFile(
                source_path=str(resolved_source),
                destination_path=str(destination_path),
                media_kind=media_kind,
            )
        )
        if media_kind == "movie":
            summary.moved_movies += 1
        else:
            summary.moved_tv += 1

    return summary
