from __future__ import annotations

import os
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from werkzeug.security import check_password_hash, generate_password_hash

import app as app_module


ADMIN_HASH_RE = re.compile(r"(?m)^ADMIN_PASSWORD_HASH\s*=\s*(['\"])(.*?)\1\s*$")


class ProfilePasswordTests(unittest.TestCase):
    def _write_config(self, root: Path, *, password_hash: str, env_sourced: bool = False) -> Path:
        downloads = root / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        config_path = root / "config.py"
        hash_line = (
            "ADMIN_PASSWORD_HASH = os.environ.get('MEGA_DOWNLOADER_ADMIN_PASSWORD_HASH', '')"
            if env_sourced
            else f"ADMIN_PASSWORD_HASH = {password_hash!r}"
        )
        imports = "from pathlib import Path\nimport os" if env_sourced else "from pathlib import Path"
        config_path.write_text(
            "\n".join(
                [
                    imports,
                    f"BASE = Path(r'{root}')",
                    "SECRET_KEY = 'test-secret'",
                    "AUTH_ENABLED = True",
                    "ADMIN_USERNAME = 'admin'",
                    hash_line,
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

    def _login(self, client, password: str = "current-password-123") -> str:
        response = client.post("/login", data={"username": "admin", "password": password})
        self.assertEqual(response.status_code, 302)
        with client.session_transaction() as session:
            return session["_csrf_token"]

    def test_profile_requires_login_and_renders_for_admin(self) -> None:
        with TemporaryDirectory(prefix="profile-auth-", ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            config_path = self._write_config(root, password_hash=generate_password_hash("current-password-123"))
            with patch.dict(os.environ, {"MEGA_DOWNLOADER_CONFIG": str(config_path)}, clear=False):
                flask_app = app_module.create_app()
                client = flask_app.test_client()
                try:
                    self.assertEqual(client.get("/profile").status_code, 302)
                    self._login(client)
                    response = client.get("/profile")
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(b"Reset password", response.data)
                finally:
                    self._stop_app(flask_app)

    def test_profile_password_form_validates_csrf_current_password_and_confirmation(self) -> None:
        with TemporaryDirectory(prefix="profile-validation-", ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            config_path = self._write_config(root, password_hash=generate_password_hash("current-password-123"))
            with patch.dict(os.environ, {"MEGA_DOWNLOADER_CONFIG": str(config_path)}, clear=False):
                flask_app = app_module.create_app()
                client = flask_app.test_client()
                try:
                    token = self._login(client)
                    payload = {
                        "current_password": "current-password-123",
                        "new_password": "new-password-123",
                        "confirm_password": "new-password-123",
                    }
                    self.assertEqual(client.post("/profile/password", data=payload).status_code, 302)
                    self.assertEqual(
                        client.post(
                            "/profile/password",
                            data={**payload, "csrf_token": token, "current_password": "wrong-password"},
                        ).status_code,
                        302,
                    )
                    self.assertEqual(
                        client.post(
                            "/profile/password",
                            data={
                                **payload,
                                "csrf_token": token,
                                "new_password": "new-password-123",
                                "confirm_password": "different-password-123",
                            },
                        ).status_code,
                        302,
                    )
                    self.assertEqual(
                        client.post(
                            "/profile/password",
                            data={
                                **payload,
                                "csrf_token": token,
                                "new_password": "short",
                                "confirm_password": "short",
                            },
                        ).status_code,
                        302,
                    )
                    match = ADMIN_HASH_RE.search(config_path.read_text(encoding="utf-8"))
                    self.assertIsNotNone(match)
                    self.assertTrue(check_password_hash(match.group(2), "current-password-123"))
                finally:
                    self._stop_app(flask_app)

    def test_profile_password_change_rewrites_config_and_requires_new_login(self) -> None:
        with TemporaryDirectory(prefix="profile-reset-", ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            config_path = self._write_config(root, password_hash=generate_password_hash("current-password-123"))
            with patch.dict(os.environ, {"MEGA_DOWNLOADER_CONFIG": str(config_path)}, clear=False):
                flask_app = app_module.create_app()
                client = flask_app.test_client()
                try:
                    token = self._login(client)
                    response = client.post(
                        "/profile/password",
                        data={
                            "csrf_token": token,
                            "current_password": "current-password-123",
                            "new_password": "new-password-123",
                            "confirm_password": "new-password-123",
                        },
                    )
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response.headers["Location"], "/login")
                    match = ADMIN_HASH_RE.search(config_path.read_text(encoding="utf-8"))
                    self.assertIsNotNone(match)
                    self.assertTrue(check_password_hash(match.group(2), "new-password-123"))
                    self.assertFalse(check_password_hash(match.group(2), "current-password-123"))

                    self.assertEqual(
                        client.post("/login", data={"username": "admin", "password": "current-password-123"}).status_code,
                        200,
                    )
                    self.assertEqual(
                        client.post("/login", data={"username": "admin", "password": "new-password-123"}).status_code,
                        302,
                    )
                finally:
                    self._stop_app(flask_app)

    def test_old_session_fingerprint_is_invalid_after_hash_changes(self) -> None:
        with TemporaryDirectory(prefix="profile-session-", ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            config_path = self._write_config(root, password_hash=generate_password_hash("current-password-123"))
            with patch.dict(os.environ, {"MEGA_DOWNLOADER_CONFIG": str(config_path)}, clear=False):
                flask_app = app_module.create_app()
                client = flask_app.test_client()
                try:
                    self._login(client)
                    self.assertEqual(client.get("/").status_code, 200)
                    flask_app.config["ADMIN_PASSWORD_HASH"] = generate_password_hash("manual-new-password")
                    self.assertEqual(client.get("/").status_code, 302)
                finally:
                    self._stop_app(flask_app)

    def test_env_sourced_password_hash_cannot_be_changed_from_profile(self) -> None:
        with TemporaryDirectory(prefix="profile-env-", ignore_cleanup_errors=True) as temp_dir:
            root = Path(temp_dir)
            env_hash = generate_password_hash("current-password-123")
            config_path = self._write_config(root, password_hash="", env_sourced=True)
            with patch.dict(
                os.environ,
                {
                    "MEGA_DOWNLOADER_CONFIG": str(config_path),
                    "MEGA_DOWNLOADER_ADMIN_PASSWORD_HASH": env_hash,
                },
                clear=False,
            ):
                flask_app = app_module.create_app()
                client = flask_app.test_client()
                try:
                    token = self._login(client)
                    response = client.post(
                        "/profile/password",
                        data={
                            "csrf_token": token,
                            "current_password": "current-password-123",
                            "new_password": "new-password-123",
                            "confirm_password": "new-password-123",
                        },
                    )
                    self.assertEqual(response.status_code, 302)
                    self.assertIn("os.environ.get", config_path.read_text(encoding="utf-8"))
                    fresh_client = flask_app.test_client()
                    self.assertEqual(
                        fresh_client.post("/login", data={"username": "admin", "password": "new-password-123"}).status_code,
                        200,
                    )
                finally:
                    self._stop_app(flask_app)


if __name__ == "__main__":
    unittest.main()
