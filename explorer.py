from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PureWindowsPath

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


def normalize_relative_path(value: str | Path) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if text in {"", "."}:
        return ""
    return text


def normalize_user_path_input(raw_text: str | Path) -> str:
    cleaned = str(raw_text or "").strip()
    if not cleaned or cleaned == ".":
        return ""
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if os.name != "nt":
        if cleaned.startswith("\\"):
            cleaned = "/" + cleaned.lstrip("\\/")
        cleaned = cleaned.replace("\\", "/")
    return cleaned


def resolve_absolute_input_path(raw_path: str | Path) -> Path:
    cleaned = normalize_user_path_input(raw_path)
    if not cleaned:
        raise ValueError("Path cannot be empty.")
    if os.name != "nt" and PureWindowsPath(cleaned).drive:
        raise ValueError("Windows drive-letter paths are not supported on this server.")
    return Path(cleaned).expanduser().resolve()


def looks_like_absolute_path(raw_path: str) -> bool:
    if not raw_path:
        return False
    path = normalize_user_path_input(raw_path)
    return path.startswith(("/", "\\")) or Path(path).is_absolute() or PureWindowsPath(path).is_absolute()


def validate_entry_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError("Name cannot be empty.")
    if cleaned in {".", ".."}:
        raise ValueError("Name cannot be '.' or '..'.")
    if "/" in cleaned or "\\" in cleaned:
        raise ValueError("Name must be a single file or folder name.")
    return cleaned


def resolve_entries_in_directory(
    destinations: dict[str, dict],
    root_key: str,
    current_relative_path: str,
    relative_paths: list[str] | tuple[str, ...],
) -> tuple[dict, Path, list[tuple[str, Path]]]:
    root_info = resolve_root(destinations, root_key)
    root = root_info["path"]
    current_dir = path_within_root(root, current_relative_path)
    current_relative = relative_to_root(root, current_dir)

    resolved_entries: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for raw_relative_path in relative_paths:
        relative_path = normalize_relative_path(raw_relative_path)
        if not relative_path or relative_path in seen:
            continue

        entry_path = path_within_root(root, relative_path)
        if entry_path == root:
            raise ValueError("The configured explorer root cannot be modified.")

        if relative_to_root(root, entry_path.parent) != current_relative:
            raise ValueError("Selected entry is not in the current explorer folder.")
        if not entry_path.exists():
            raise FileNotFoundError(f"'{relative_path}' no longer exists.")

        seen.add(relative_path)
        resolved_entries.append((relative_path, entry_path))

    if not resolved_entries:
        raise ValueError("Select at least one entry.")

    return root_info, current_dir, resolved_entries


def delete_entries(
    destinations: dict[str, dict],
    root_key: str,
    current_relative_path: str,
    relative_paths: list[str] | tuple[str, ...],
) -> dict:
    _, _, resolved_entries = resolve_entries_in_directory(destinations, root_key, current_relative_path, relative_paths)

    deleted: list[str] = []
    failures: list[str] = []
    for relative_path, entry_path in resolved_entries:
        try:
            if entry_path.is_symlink() or entry_path.is_file():
                entry_path.unlink()
            elif entry_path.is_dir():
                shutil.rmtree(entry_path)
            else:
                entry_path.unlink(missing_ok=True)
            deleted.append(relative_path)
        except OSError as exc:
            failures.append(f"{Path(relative_path).name}: {exc}")

    return {
        "deleted": deleted,
        "failures": failures,
    }


def rename_entry(
    destinations: dict[str, dict],
    root_key: str,
    current_relative_path: str,
    relative_path: str,
    new_name: str,
) -> dict:
    root_info, _, resolved_entries = resolve_entries_in_directory(
        destinations,
        root_key,
        current_relative_path,
        [relative_path],
    )
    original_relative_path, original_path = resolved_entries[0]
    validated_name = validate_entry_name(new_name)
    if original_path.name == validated_name:
        return {
            "old_relative_path": original_relative_path,
            "new_relative_path": original_relative_path,
            "renamed": False,
            "name": validated_name,
        }

    target_path = path_within_root(root_info["path"], Path(original_relative_path).parent / validated_name)
    if target_path.exists():
        raise ValueError(f"'{validated_name}' already exists in this folder.")

    original_path.rename(target_path)
    return {
        "old_relative_path": original_relative_path,
        "new_relative_path": relative_to_root(root_info["path"], target_path),
        "renamed": True,
        "name": validated_name,
    }


def resolve_move_target(
    destinations: dict[str, dict],
    root_key: str,
    current_relative_path: str,
    target_input: str,
) -> tuple[dict, Path, Path]:
    root_info = resolve_root(destinations, root_key)
    root = root_info["path"]
    current_dir = path_within_root(root, current_relative_path)
    cleaned = normalize_user_path_input(target_input)
    if not cleaned:
        raise ValueError("Enter a move target path.")

    if looks_like_absolute_path(cleaned):
        target_dir = resolve_absolute_input_path(cleaned)
    else:
        target_dir = path_within_root(root, cleaned.replace("\\", "/"))

    return root_info, current_dir, target_dir


def preview_move_entries(
    destinations: dict[str, dict],
    root_key: str,
    current_relative_path: str,
    relative_paths: list[str] | tuple[str, ...],
    target_input: str,
) -> dict:
    _, current_dir, resolved_entries = resolve_entries_in_directory(
        destinations,
        root_key,
        current_relative_path,
        relative_paths,
    )
    _, _, target_dir = resolve_move_target(destinations, root_key, current_relative_path, target_input)

    if target_dir == current_dir:
        raise ValueError("Move target cannot be the current explorer folder.")
    if target_dir.exists() and not target_dir.is_dir():
        raise ValueError(f"Move target '{target_dir}' exists but is not a directory.")

    conflicts: list[str] = []
    for _, entry_path in resolved_entries:
        if entry_path.is_dir() and (target_dir == entry_path or entry_path in target_dir.parents):
            raise ValueError(f"Cannot move folder '{entry_path.name}' into itself or one of its subfolders.")
        if (target_dir / entry_path.name).exists():
            conflicts.append(entry_path.name)

    return {
        "target_dir": target_dir,
        "conflicts": conflicts,
        "entries": resolved_entries,
    }


def _remove_existing_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    if path.is_dir():
        shutil.rmtree(path)
        return
    path.unlink(missing_ok=True)


def move_entries(
    destinations: dict[str, dict],
    root_key: str,
    current_relative_path: str,
    relative_paths: list[str] | tuple[str, ...],
    target_input: str,
    *,
    replace_existing: bool = False,
) -> dict:
    preview = preview_move_entries(destinations, root_key, current_relative_path, relative_paths, target_input)
    target_dir: Path = preview["target_dir"]
    target_dir.mkdir(parents=True, exist_ok=True)

    moved: list[str] = []
    replaced: list[str] = []
    failures: list[str] = []

    for _, entry_path in preview["entries"]:
        target_path = target_dir / entry_path.name
        replaced_existing = False
        try:
            if target_path.exists():
                if not replace_existing:
                    failures.append(f"{entry_path.name}: already exists in the target folder.")
                    continue
                _remove_existing_path(target_path)
                replaced_existing = True

            shutil.move(str(entry_path), str(target_path))
            if replaced_existing:
                replaced.append(entry_path.name)
            else:
                moved.append(entry_path.name)
        except (OSError, shutil.Error) as exc:
            failures.append(f"{entry_path.name}: {exc}")

    return {
        "moved": moved,
        "replaced": replaced,
        "failures": failures,
        "target_dir": str(target_dir),
        "conflicts": preview["conflicts"],
    }


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
    current_relative = relative_to_root(root, current_path)
    if relative_path:
        parent_path = relative_to_root(root, current_path.parent)

    return {
        "root": root_info,
        "current_path": current_relative,
        "current_label": current_relative or root_info["label"],
        "parent_path": parent_path,
        "entries": [entry.to_dict() for entry in entries],
        "breadcrumbs": build_breadcrumbs(root_key, current_relative),
        "sort": sort_by,
    }
