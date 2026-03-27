from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Iterable

from models import Job


class JsonStorage:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def load_jobs(self) -> list[Job]:
        with self._lock:
            if not self.path.exists():
                return []
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        return [Job.from_dict(item) for item in raw.get("jobs", [])]

    def save_jobs(self, jobs: Iterable[Job]) -> None:
        payload = {"jobs": [job.to_dict() for job in jobs]}
        temp_path = self.path.with_suffix(".tmp")
        with self._lock:
            temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temp_path.replace(self.path)
