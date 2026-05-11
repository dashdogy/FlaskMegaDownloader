from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from explorer import move_entries
from plex_permissions import PlexPermissionManager


class PlexPermissionTests(unittest.TestCase):
    def test_grant_uses_setfacl_for_files_and_directories(self) -> None:
        with TemporaryDirectory(prefix="plex-acl-") as temp_dir:
            root = Path(temp_dir)
            file_path = root / "movie.mkv"
            file_path.write_text("payload", encoding="utf-8")

            manager = PlexPermissionManager(plex_user="plex", setfacl_binary="setfacl")
            with (
                patch.object(manager, "_user_exists", return_value=True),
                patch("plex_permissions.shutil.which", return_value="/usr/bin/setfacl"),
                patch("plex_permissions.subprocess.run") as run_command,
            ):
                self.assertTrue(manager.grant(file_path))
                self.assertTrue(manager.grant(root))

            commands = [call.args[0] for call in run_command.call_args_list]
            self.assertIn(["/usr/bin/setfacl", "-m", "u:plex:rw", str(file_path)], commands)
            self.assertIn(
                ["/usr/bin/setfacl", "-R", "-m", "u:plex:rwx", "-m", "d:u:plex:rwx", str(root)],
                commands,
            )

    def test_explorer_move_grants_acl_to_moved_output(self) -> None:
        class Recorder:
            def __init__(self) -> None:
                self.calls: list[tuple[Path, bool, str]] = []

            def grant(self, path: Path, *, recursive: bool = True, feature: str = "plex_permissions") -> bool:
                self.calls.append((Path(path), recursive, feature))
                return True

        with TemporaryDirectory(prefix="plex-move-") as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "downloads"
            target_dir = source_dir / "movies"
            source_dir.mkdir()
            source_file = source_dir / "movie.mkv"
            source_file.write_text("payload", encoding="utf-8")
            recorder = Recorder()

            result = move_entries(
                {"downloads": {"label": "Downloads", "path": source_dir}},
                "downloads",
                "",
                ["movie.mkv"],
                "movies",
                permission_manager=recorder,
            )

            self.assertEqual(result["moved"], ["movie.mkv"])
            resolved_calls = [(path.resolve(), recursive, feature) for path, recursive, feature in recorder.calls]
            self.assertIn((target_dir.resolve(), False, "explorer_move"), resolved_calls)
            self.assertIn(((target_dir / "movie.mkv").resolve(), True, "explorer_move"), resolved_calls)


if __name__ == "__main__":
    unittest.main()
