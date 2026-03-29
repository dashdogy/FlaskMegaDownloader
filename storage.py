from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from models import FavoriteDestination, Job, MediaJob, MoveFavorite


LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = "1"


def _utc_compact_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")


class SQLiteStorage:
    def __init__(self, path: Path, *, legacy_json_path: Path | None = None):
        self.path = Path(path)
        self.legacy_json_path = Path(legacy_json_path).expanduser().resolve() if legacy_json_path else None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_database(self) -> None:
        database_existed = self.path.exists()
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS download_jobs (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    url TEXT NOT NULL,
                    destination_key TEXT NOT NULL,
                    destination_path TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    destination_relative_path TEXT NOT NULL,
                    destination_is_custom INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error TEXT,
                    transfer_json TEXT NOT NULL,
                    output_tail_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS media_jobs (
                    id TEXT PRIMARY KEY,
                    batch_id TEXT NOT NULL,
                    source_root_key TEXT NOT NULL,
                    source_relative_path TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    source_display_name TEXT NOT NULL,
                    output_destination_key TEXT NOT NULL,
                    output_destination_path TEXT NOT NULL,
                    output_destination_relative_path TEXT NOT NULL,
                    output_destination_is_custom INTEGER NOT NULL,
                    output_file_path TEXT,
                    staging_directory TEXT,
                    staged_output_file_path TEXT,
                    mkv_filename TEXT,
                    title_id INTEGER,
                    title_name TEXT,
                    title_duration_seconds INTEGER,
                    title_size_bytes INTEGER,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    error TEXT,
                    transfer_json TEXT NOT NULL,
                    verification_json TEXT NOT NULL,
                    output_tail_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS favorite_destinations (
                    key TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    path TEXT NOT NULL,
                    favorite INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS hidden_base_destinations (
                    key TEXT PRIMARY KEY
                );

                CREATE TABLE IF NOT EXISTS move_favorites (
                    key TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    path TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            connection.commit()

        if not database_existed:
            self._migrate_legacy_json_if_needed()

    def _migrate_legacy_json_if_needed(self) -> None:
        if not self.legacy_json_path or not self.legacy_json_path.exists():
            return

        try:
            raw_payload = json.loads(self.legacy_json_path.read_text(encoding="utf-8"))
        except Exception as exc:
            quarantine_path = self._rename_legacy_file(f"corrupt.{_utc_compact_timestamp()}")
            LOGGER.warning(
                "Legacy JSON state could not be parsed and was quarantined at %s: %s",
                quarantine_path,
                exc,
            )
            return

        jobs = [Job.from_dict(item) for item in raw_payload.get("jobs", [])]
        favorites = [FavoriteDestination.from_dict(item) for item in raw_payload.get("favorites", [])]
        hidden_base_destinations = [str(item) for item in raw_payload.get("hidden_base_destinations", [])]
        move_favorites = [MoveFavorite.from_dict(item) for item in raw_payload.get("move_favorites", [])]
        media_jobs = [MediaJob.from_dict(item) for item in raw_payload.get("media_jobs", [])]

        with self._lock:
            self.save_state(
                jobs=jobs,
                favorites=favorites,
                hidden_base_destinations=hidden_base_destinations,
                move_favorites=move_favorites,
                media_jobs=media_jobs,
            )
            with self._connect() as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES('migrated_from_json', ?)",
                    (str(self.legacy_json_path),),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO metadata(key, value) VALUES('json_migrated_at', ?)",
                    (datetime.now(timezone.utc).isoformat(),),
                )
                connection.commit()

        migrated_path = self._rename_legacy_file("migrated")
        LOGGER.info("Migrated legacy JSON state from %s to %s", migrated_path, self.path)

    def _rename_legacy_file(self, suffix: str) -> Path:
        assert self.legacy_json_path is not None
        target = self.legacy_json_path.with_name(f"{self.legacy_json_path.name}.{suffix}")
        counter = 1
        while target.exists():
            target = self.legacy_json_path.with_name(f"{self.legacy_json_path.name}.{suffix}.{counter}")
            counter += 1
        self.legacy_json_path.rename(target)
        return target

    def load_jobs(self) -> list[Job]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM download_jobs
                ORDER BY created_at ASC, id ASC
                """
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def load_favorites(self) -> list[FavoriteDestination]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT key, label, path, favorite
                FROM favorite_destinations
                ORDER BY lower(label) ASC, lower(path) ASC
                """
            ).fetchall()
        return [
            FavoriteDestination(
                key=row["key"],
                label=row["label"],
                path=row["path"],
                favorite=bool(row["favorite"]),
            )
            for row in rows
        ]

    def load_hidden_base_destinations(self) -> list[str]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT key FROM hidden_base_destinations ORDER BY key ASC"
            ).fetchall()
        return [str(row["key"]) for row in rows]

    def load_move_favorites(self) -> list[MoveFavorite]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT key, label, path
                FROM move_favorites
                ORDER BY lower(label) ASC, lower(path) ASC
                """
            ).fetchall()
        return [
            MoveFavorite(
                key=row["key"],
                label=row["label"],
                path=row["path"],
            )
            for row in rows
        ]

    def load_media_jobs(self) -> list[MediaJob]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM media_jobs
                ORDER BY created_at ASC, id ASC
                """
            ).fetchall()
        return [self._media_job_from_row(row) for row in rows]

    def save_state(
        self,
        jobs: Iterable[Job],
        favorites: Iterable[FavoriteDestination],
        hidden_base_destinations: Iterable[str],
        move_favorites: Iterable[MoveFavorite] | None = None,
        media_jobs: Iterable[MediaJob] | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN")
            self._replace_download_jobs(connection, jobs)
            self._replace_favorites(connection, favorites)
            self._replace_hidden_destinations(connection, hidden_base_destinations)
            if move_favorites is not None:
                self._replace_move_favorites(connection, move_favorites)
            if media_jobs is not None:
                self._replace_media_jobs(connection, media_jobs)
            connection.commit()

    def save_move_favorites(self, move_favorites: Iterable[MoveFavorite]) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN")
            self._replace_move_favorites(connection, move_favorites)
            connection.commit()

    def save_jobs(self, jobs: Iterable[Job]) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN")
            self._replace_download_jobs(connection, jobs)
            connection.commit()

    def save_media_jobs(self, media_jobs: Iterable[MediaJob]) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN")
            self._replace_media_jobs(connection, media_jobs)
            connection.commit()

    def _replace_download_jobs(self, connection: sqlite3.Connection, jobs: Iterable[Job]) -> None:
        connection.execute("DELETE FROM download_jobs")
        rows = [
            (
                job.id,
                job.batch_id,
                job.url,
                job.destination_key,
                job.destination_path,
                job.display_name,
                job.destination_relative_path,
                int(job.destination_is_custom),
                job.status,
                job.created_at,
                job.updated_at,
                job.error,
                json.dumps(job.transfer.to_dict()),
                json.dumps(job.output_tail),
            )
            for job in jobs
        ]
        connection.executemany(
            """
            INSERT INTO download_jobs(
                id, batch_id, url, destination_key, destination_path, display_name,
                destination_relative_path, destination_is_custom, status, created_at,
                updated_at, error, transfer_json, output_tail_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    def _replace_media_jobs(self, connection: sqlite3.Connection, media_jobs: Iterable[MediaJob]) -> None:
        connection.execute("DELETE FROM media_jobs")
        rows = [
            (
                job.id,
                job.batch_id,
                job.source_root_key,
                job.source_relative_path,
                job.source_path,
                job.source_display_name,
                job.output_destination_key,
                job.output_destination_path,
                job.output_destination_relative_path,
                int(job.output_destination_is_custom),
                job.output_file_path,
                job.staging_directory,
                job.staged_output_file_path,
                job.mkv_filename,
                job.title_id,
                job.title_name,
                job.title_duration_seconds,
                job.title_size_bytes,
                job.status,
                job.created_at,
                job.updated_at,
                job.error,
                json.dumps(job.transfer.to_dict()),
                json.dumps(job.verification.to_dict()),
                json.dumps(job.output_tail),
            )
            for job in media_jobs
        ]
        connection.executemany(
            """
            INSERT INTO media_jobs(
                id, batch_id, source_root_key, source_relative_path, source_path,
                source_display_name, output_destination_key, output_destination_path,
                output_destination_relative_path, output_destination_is_custom, output_file_path,
                staging_directory, staged_output_file_path, mkv_filename, title_id, title_name,
                title_duration_seconds, title_size_bytes, status, created_at, updated_at, error,
                transfer_json, verification_json, output_tail_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    def _replace_favorites(self, connection: sqlite3.Connection, favorites: Iterable[FavoriteDestination]) -> None:
        connection.execute("DELETE FROM favorite_destinations")
        rows = [
            (favorite.key, favorite.label, favorite.path, int(favorite.favorite))
            for favorite in favorites
        ]
        connection.executemany(
            """
            INSERT INTO favorite_destinations(key, label, path, favorite)
            VALUES(?, ?, ?, ?)
            """,
            rows,
        )

    def _replace_hidden_destinations(self, connection: sqlite3.Connection, hidden_base_destinations: Iterable[str]) -> None:
        connection.execute("DELETE FROM hidden_base_destinations")
        rows = [(str(item),) for item in sorted({str(item) for item in hidden_base_destinations})]
        connection.executemany(
            "INSERT INTO hidden_base_destinations(key) VALUES(?)",
            rows,
        )

    def _replace_move_favorites(self, connection: sqlite3.Connection, move_favorites: Iterable[MoveFavorite]) -> None:
        connection.execute("DELETE FROM move_favorites")
        rows = [(favorite.key, favorite.label, favorite.path) for favorite in move_favorites]
        connection.executemany(
            "INSERT INTO move_favorites(key, label, path) VALUES(?, ?, ?)",
            rows,
        )

    def _job_from_row(self, row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            batch_id=row["batch_id"],
            url=row["url"],
            destination_key=row["destination_key"],
            destination_path=row["destination_path"],
            destination_relative_path=row["destination_relative_path"],
            display_name=row["display_name"],
            destination_is_custom=bool(row["destination_is_custom"]),
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            error=row["error"],
            transfer=Job.from_dict({"id": row["id"], "batch_id": row["batch_id"], "url": row["url"], "destination_key": row["destination_key"], "destination_path": row["destination_path"], "display_name": row["display_name"], "transfer": json.loads(row["transfer_json"])}).transfer,
            output_tail=list(json.loads(row["output_tail_json"] or "[]")),
        )

    def _media_job_from_row(self, row: sqlite3.Row) -> MediaJob:
        payload = {
            "id": row["id"],
            "batch_id": row["batch_id"],
            "source_root_key": row["source_root_key"],
            "source_relative_path": row["source_relative_path"],
            "source_path": row["source_path"],
            "source_display_name": row["source_display_name"],
            "output_destination_key": row["output_destination_key"],
            "output_destination_path": row["output_destination_path"],
            "output_destination_relative_path": row["output_destination_relative_path"],
            "output_destination_is_custom": bool(row["output_destination_is_custom"]),
            "output_file_path": row["output_file_path"],
            "staging_directory": row["staging_directory"],
            "staged_output_file_path": row["staged_output_file_path"],
            "mkv_filename": row["mkv_filename"],
            "title_id": row["title_id"],
            "title_name": row["title_name"],
            "title_duration_seconds": row["title_duration_seconds"],
            "title_size_bytes": row["title_size_bytes"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "error": row["error"],
            "transfer": json.loads(row["transfer_json"]),
            "verification": json.loads(row["verification_json"]),
            "output_tail": json.loads(row["output_tail_json"] or "[]"),
        }
        return MediaJob.from_dict(payload)


JsonStorage = SQLiteStorage
