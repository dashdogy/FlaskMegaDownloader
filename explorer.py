from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from models import ExplorerEntry


def normalize_destinations(raw_destinations: dict) -> dict[str, dict]:
    normalized: dict[str, dict] = {}
    for key, value in raw_destinations.items():
        if isinstance(value, dict):
            label = value.get("label", key.replace("_", " ").title())
            path = Path(value["path"]).expanduser().resolve()
        else:
            label = key.replace("_", " ").title()
            path = Path(value).expanduser().resolve()
        normalized[key] = {
            "key": key,
            "label": label,
            "path": path,
        }
    return normalized


def resolve_root(destinations: dict[str, dict], root_key: str) -> dict:
    if root_key not in destinations:
        raise ValueError(f"Unknown destination '{root_key}'.")
    return destinations[root_key]


def path_within_root(root: Path, requested: str | Path) -> Path:
    root = root.resolve()
    requested_path = root if str(requested or "").strip() in {"", "."} else (root / requested).resolve()
    if requested_path != root and root not in requested_path.parents:
        raise ValueError("Path traversal outside allowed root was rejected.")
    return requested_path


def relative_to_root(root: Path, path: Path) -> str:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("Path is outside the configured root.") from exc
    text = str(relative).replace("\\", "/")
    return "" if text == "." else text


def build_breadcrumbs(root_key: str, relative_path: str) -> list[dict]:
    breadcrumbs = [{"label": "Root", "path": "", "root": root_key}]
    if not relative_path:
        return breadcrumbs

    running_parts: list[str] = []
    for part in Path(relative_path).parts:
        running_parts.append(part)
        breadcrumbs.append(
            {
                "label": part,
                "path": "/".join(running_parts),
                "root": root_key,
            }
        )
    return breadcrumbs


def list_directory(
    destinations: dict[str, dict],
    root_key: str,
    relative_path: str = "",
    sort_by: str = "name",
) -> dict:
    root_info = resolve_root(destinations, root_key)
    root = root_info["path"]
    current_path = path_within_root(root, relative_path)
    if not current_path.exists() or not current_path.is_dir():
        raise FileNotFoundError("Requested folder does not exist.")

    entries: list[ExplorerEntry] = []
    for child in current_path.iterdir():
        stats = child.stat()
        modified_at = datetime.fromtimestamp(stats.st_mtime, tz=timezone.utc).astimezone().isoformat()
        entries.append(
            ExplorerEntry(
                name=child.name,
                relative_path=relative_to_root(root, child),
                is_dir=child.is_dir(),
                size=None if child.is_dir() else stats.st_size,
                modified_at=modified_at,
                is_zip=child.is_file() and child.suffix.lower() == ".zip",
            )
        )

    def sort_key(entry: ExplorerEntry):
        if sort_by == "size":
            return (not entry.is_dir, -(entry.size or 0), entry.name.lower())
        if sort_by == "modified":
            return (not entry.is_dir, entry.modified_at or "", entry.name.lower())
        return (not entry.is_dir, entry.name.lower())

    entries.sort(key=sort_key)
    parent_path = ""
    if relative_path:
        parent_path = relative_to_root(root, current_path.parent)

    return {
        "root": root_info,
        "current_path": relative_to_root(root, current_path),
        "current_label": str(current_path),
        "parent_path": parent_path,
        "entries": [entry.to_dict() for entry in entries],
        "breadcrumbs": build_breadcrumbs(root_key, relative_to_root(root, current_path)),
        "sort": sort_by,
    }
