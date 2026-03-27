from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Iterable

from models import FavoriteDestination, Job


class JsonStorage:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _load_payload(self) -> dict:
        with self._lock:
            if not self.path.exists():
                return {}
            return json.loads(self.path.read_text(encoding="utf-8"))

    def load_jobs(self) -> list[Job]:
        raw = self._load_payload()
        return [Job.from_dict(item) for item in raw.get("jobs", [])]

    def load_favorites(self) -> list[FavoriteDestination]:
        raw = self._load_payload()
        return [FavoriteDestination.from_dict(item) for item in raw.get("favorites", [])]

    def load_hidden_base_destinations(self) -> list[str]:
        raw = self._load_payload()
        return [str(item) for item in raw.get("hidden_base_destinations", [])]

    def save_state(
        self,
        jobs: Iterable[Job],
        favorites: Iterable[FavoriteDestination],
        hidden_base_destinations: Iterable[str],
    ) -> None:
        payload = {
            "jobs": [job.to_dict() for job in jobs],
            "favorites": [favorite.to_dict() for favorite in favorites],
            "hidden_base_destinations": sorted({str(item) for item in hidden_base_destinations}),
        }
        temp_path = self.path.with_suffix(".tmp")
        with self._lock:
            temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temp_path.replace(self.path)

    def save_jobs(self, jobs: Iterable[Job]) -> None:
        self.save_state(jobs, [], [])
