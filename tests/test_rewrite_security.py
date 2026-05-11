from __future__ import annotations

import os
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from werkzeug.security import generate_password_hash

import app as app_module
from storage import SQLiteStorage


class RewriteSecurityTests(unittest.TestCase):
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

    def test_auth_and_csrf_gate_mutating_routes(self) -> None:
        with TemporaryDirectory(prefix="rewrite-auth-", ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            config_path = self._write_config(
                root,
                auth_enabled=True,
                password_hash=generate_password_hash("secret123"),
            )
            with patch.dict(os.environ, {"MEGA_DOWNLOADER_CONFIG": str(config_path)}, clear=False):
                flask_app = app_module.create_app()
                client = flask_app.test_client()
                try:
                    self.assertEqual(client.get("/").status_code, 302)
                    self.assertEqual(
                        client.post("/login", data={"username": "admin", "password": "secret123"}).status_code,
                        302,
                    )
                    self.assertEqual(client.get("/").status_code, 200)
                    self.assertEqual(client.post("/jobs/clear").status_code, 302)
                    with client.session_transaction() as session:
                        token = session["_csrf_token"]
                    self.assertEqual(client.post("/jobs/clear", data={"csrf_token": token}).status_code, 302)
                finally:
                    self._stop_app(flask_app)

    def test_submit_rejects_absolute_path_but_root_add_accepts_it(self) -> None:
        with TemporaryDirectory(prefix="rewrite-roots-", ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            external = root / "external"
            config_path = self._write_config(root, auth_enabled=False)
            with patch.dict(os.environ, {"MEGA_DOWNLOADER_CONFIG": str(config_path)}, clear=False):
                flask_app = app_module.create_app()
                client = flask_app.test_client()
                try:
                    rejected = client.post(
                        "/submit",
                        data={
                            "urls": "https://example.local/sample.zip",
                            "destination": "downloads",
                            "destination_path": str(external),
                        },
                    )
                    self.assertEqual(rejected.status_code, 302)
                    self.assertFalse(flask_app.extensions["download_manager"].dashboard_payload()["jobs"])

                    added = client.post(
                        "/favorites",
                        data={"destination": "downloads", "destination_path": str(external)},
                    )
                    self.assertEqual(added.status_code, 302)
                    labels = [item["path"] for item in flask_app.extensions["download_manager"].destination_options()]
                    self.assertIn(str(external.resolve()), labels)
                finally:
                    self._stop_app(flask_app)

    def test_storage_initializes_rewrite_migration_tables(self) -> None:
        with TemporaryDirectory(prefix="rewrite-storage-", ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "state.sqlite3"
            SQLiteStorage(db_path)
            with sqlite3.connect(db_path) as connection:
                table = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'worker_leases'"
                ).fetchone()
            self.assertIsNotNone(table)


if __name__ == "__main__":
    unittest.main()

