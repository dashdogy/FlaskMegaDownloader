from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import app as app_module
from explorer import (
    create_directory,
    delete_entries,
    list_directory,
    list_directory_tree,
    preview_move_entries,
)


class InteractiveExplorerHelperTests(unittest.TestCase):
    def test_create_directory_validates_names_and_grants_plex_acl(self) -> None:
        class Recorder:
            def __init__(self) -> None:
                self.calls: list[tuple[Path, bool, str]] = []

            def grant(self, path: Path, *, recursive: bool = True, feature: str = "plex_permissions") -> bool:
                self.calls.append((Path(path), recursive, feature))
                return True

        with TemporaryDirectory(prefix="interactive-explorer-") as temp_dir:
            root = Path(temp_dir)
            recorder = Recorder()
            result = create_directory(
                {"downloads": {"key": "downloads", "label": "Downloads", "path": root}},
                "downloads",
                "",
                "New Folder",
                permission_manager=recorder,
            )

            self.assertEqual(result["relative_path"], "New Folder")
            self.assertTrue((root / "New Folder").is_dir())
            self.assertIn(((root / "New Folder").resolve(), True, "explorer_mkdir"), [(path.resolve(), recursive, feature) for path, recursive, feature in recorder.calls])

            for invalid_name in ("", ".", "..", "bad/name", "bad\\name"):
                with self.subTest(invalid_name=invalid_name):
                    with self.assertRaises(ValueError):
                        create_directory(
                            {"downloads": {"key": "downloads", "label": "Downloads", "path": root}},
                            "downloads",
                            "",
                            invalid_name,
                        )

    def test_tree_payload_and_entry_metadata_support_interactive_ui(self) -> None:
        with TemporaryDirectory(prefix="interactive-tree-") as temp_dir:
            root = Path(temp_dir)
            (root / "folder" / "child").mkdir(parents=True)
            (root / "movie.zip").write_bytes(b"zip")
            (root / "disc" / "BDMV").mkdir(parents=True)
            (root / "disc" / "BDMV" / "index.bdmv").write_text("bdmv", encoding="utf-8")

            tree = list_directory_tree(
                {"downloads": {"key": "downloads", "label": "Downloads", "path": root}},
                "downloads",
                "",
            )
            payload = list_directory(
                {"downloads": {"key": "downloads", "label": "Downloads", "path": root}},
                "downloads",
                "",
                "name",
            )

        directories = {item["name"]: item for item in tree["directories"]}
        self.assertTrue(directories["folder"]["has_children"])

        entries = {entry["name"]: entry for entry in payload["entries"]}
        self.assertEqual(entries["folder"]["entry_type"], "folder")
        self.assertEqual(entries["movie.zip"]["entry_type"], "archive")
        self.assertTrue(entries["movie.zip"]["can_extract"])
        self.assertEqual(entries["disc"]["entry_type"], "bluray")
        self.assertTrue(entries["disc"]["can_compile_bluray"])

    def test_mutations_reject_root_and_traversal(self) -> None:
        with TemporaryDirectory(prefix="interactive-reject-") as temp_dir:
            root = Path(temp_dir)
            (root / "movie.mkv").write_text("payload", encoding="utf-8")
            destinations = {"downloads": {"key": "downloads", "label": "Downloads", "path": root}}

            with self.assertRaises(ValueError):
                delete_entries(destinations, "downloads", "", ["."])
            with self.assertRaises(ValueError):
                create_directory(destinations, "downloads", "../outside", "folder")
            with self.assertRaises(ValueError):
                preview_move_entries(destinations, "downloads", "", ["movie.mkv"], "../outside")

    def test_move_preview_reports_conflicts_before_execute(self) -> None:
        with TemporaryDirectory(prefix="interactive-move-") as temp_dir:
            root = Path(temp_dir)
            (root / "movie.mkv").write_text("source", encoding="utf-8")
            (root / "target").mkdir()
            (root / "target" / "movie.mkv").write_text("existing", encoding="utf-8")

            preview = preview_move_entries(
                {"downloads": {"key": "downloads", "label": "Downloads", "path": root}},
                "downloads",
                "",
                ["movie.mkv"],
                "target",
            )

        self.assertEqual(preview["conflicts"], ["movie.mkv"])


class InteractiveExplorerRouteTests(unittest.TestCase):
    def _write_config(self, root: Path, *, auth_enabled: bool, password_hash: str = "") -> Path:
        downloads = root / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        config_path = root / "config.py"
        config_path.write_text(
            "\n".join(
                [
                    "from pathlib import Path",
                    f"BASE = Path(r'{root}')",
                    "SECRET_KEY = 'test-secret'",
                    f"AUTH_ENABLED = {auth_enabled!r}",
                    "ADMIN_USERNAME = 'admin'",
                    f"ADMIN_PASSWORD_HASH = {password_hash!r}",
                    "DATA_DIR = BASE / 'data'",
                    "JOB_STORAGE_FILE = DATA_DIR / 'jobs.json'",
                    "STATE_DB_FILE = DATA_DIR / 'state.sqlite3'",
                    "DOWNLOADER_BACKEND = 'fake'",
                    "SEVEN_ZIP_BINARY = '7z'",
                    "ALLOWED_DESTINATIONS = {'downloads': {'key': 'downloads', 'label': 'Downloads', 'path': BASE / 'downloads'}}",
                ]
            ),
            encoding="utf-8",
        )
        return config_path

    def _stop_app(self, flask_app) -> None:
        for key in ("download_manager", "media_compile_manager", "archive_extract_manager"):
            flask_app.extensions[key].stop()

    def test_api_explorer_returns_json_payload_for_file_manager(self) -> None:
        with TemporaryDirectory(prefix="interactive-api-", ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            config_path = self._write_config(root, auth_enabled=False)
            (root / "downloads" / "sample.zip").write_bytes(b"zip")
            with patch.dict(os.environ, {"MEGA_DOWNLOADER_CONFIG": str(config_path)}, clear=False):
                flask_app = app_module.create_app()
                client = flask_app.test_client()
                try:
                    response = client.get("/api/explorer?root=downloads&sort=type&order=desc")
                    payload = response.get_json()
                finally:
                    self._stop_app(flask_app)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["explorer"]["sort"], "type")
        self.assertEqual(payload["explorer"]["order"], "desc")
        self.assertEqual(payload["roots"][0]["key"], "downloads")
        self.assertEqual(payload["explorer"]["entries"][0]["entry_type"], "archive")

    def test_api_delete_requires_confirmation_and_preserves_file(self) -> None:
        with TemporaryDirectory(prefix="interactive-delete-", ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            config_path = self._write_config(root, auth_enabled=False)
            target = root / "downloads" / "sample.mkv"
            target.write_text("payload", encoding="utf-8")
            with patch.dict(os.environ, {"MEGA_DOWNLOADER_CONFIG": str(config_path)}, clear=False):
                flask_app = app_module.create_app()
                client = flask_app.test_client()
                try:
                    response = client.post(
                        "/api/explorer/delete",
                        json={
                            "root": "downloads",
                            "current_path": "",
                            "selected_paths": ["sample.mkv"],
                        },
                    )
                    payload = response.get_json()
                    file_still_exists = target.exists()
                finally:
                    self._stop_app(flask_app)

        self.assertEqual(response.status_code, 409)
        self.assertFalse(payload["ok"])
        self.assertTrue(payload["requires_confirmation"])
        self.assertTrue(file_still_exists)

    def test_api_create_folder_preserves_explorer_context(self) -> None:
        with TemporaryDirectory(prefix="interactive-context-", ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            config_path = self._write_config(root, auth_enabled=False)
            (root / "downloads" / "subdir").mkdir()
            with patch.dict(os.environ, {"MEGA_DOWNLOADER_CONFIG": str(config_path)}, clear=False):
                flask_app = app_module.create_app()
                client = flask_app.test_client()
                try:
                    response = client.post(
                        "/api/explorer/folders",
                        json={
                            "root": "downloads",
                            "current_path": "subdir",
                            "name": "nested",
                            "sort": "modified",
                            "order": "desc",
                        },
                    )
                    payload = response.get_json()
                finally:
                    self._stop_app(flask_app)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["created"]["relative_path"], "subdir/nested")
        self.assertEqual(payload["explorer"]["current_path"], "subdir")
        self.assertEqual(payload["explorer"]["sort"], "modified")
        self.assertEqual(payload["explorer"]["order"], "desc")

    def test_json_mutations_are_csrf_protected_when_auth_is_enabled(self) -> None:
        with TemporaryDirectory(prefix="interactive-csrf-", ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            config_path = self._write_config(
                root,
                auth_enabled=True,
                password_hash=generate_password_hash("secret123456"),
            )
            with patch.dict(os.environ, {"MEGA_DOWNLOADER_CONFIG": str(config_path)}, clear=False):
                flask_app = app_module.create_app()
                client = flask_app.test_client()
                try:
                    client.post("/login", data={"username": "admin", "password": "secret123456"})
                    rejected = client.post(
                        "/api/explorer/folders",
                        json={"root": "downloads", "current_path": "", "name": "blocked"},
                    )
                    with client.session_transaction() as session:
                        token = session["_csrf_token"]
                    accepted = client.post(
                        "/api/explorer/folders",
                        json={"root": "downloads", "current_path": "", "name": "allowed"},
                        headers={"X-CSRF-Token": token},
                    )
                finally:
                    self._stop_app(flask_app)

        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.get_json()["error"], "Invalid CSRF token.")
        self.assertEqual(accepted.status_code, 200)


if __name__ == "__main__":
    unittest.main()
