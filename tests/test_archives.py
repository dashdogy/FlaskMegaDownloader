from __future__ import annotations

import io
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import archives
from archives import (
    ArchiveError,
    archive_type_for_path,
    default_archive_target_name,
    extract_archive,
    is_supported_archive_path,
)
from explorer import list_directory


class FakeRarInfo:
    def __init__(self, filename: str, *, is_dir: bool = False) -> None:
        self.filename = filename
        self._is_dir = is_dir

    def isdir(self) -> bool:
        return self._is_dir


class FakeRarFile:
    last_password = None

    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self) -> "FakeRarFile":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def setpassword(self, password: str | None) -> None:
        FakeRarFile.last_password = password

    def infolist(self) -> list[FakeRarInfo]:
        return [
            FakeRarInfo("folder/", is_dir=True),
            FakeRarInfo("folder/file.txt"),
        ]

    def open(self, member: FakeRarInfo):
        return io.BytesIO(b"rar-payload")


class FailingRarFile:
    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self):
        raise FakeRarError("Cannot find working tool")

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class FakeRarError(Exception):
    pass


class ArchiveTests(unittest.TestCase):
    def test_archive_type_detection_supports_zip_rar_and_7z_entrypoints(self) -> None:
        self.assertEqual(archive_type_for_path(Path("sample.zip")), "zip")
        self.assertEqual(archive_type_for_path(Path("sample.rar")), "rar")
        self.assertEqual(archive_type_for_path(Path("sample.7z")), "7z")
        self.assertEqual(archive_type_for_path(Path("sample.7z.001")), "7z")
        self.assertIsNone(archive_type_for_path(Path("sample.7z.002")))
        self.assertIsNone(archive_type_for_path(Path("sample.txt")))
        self.assertTrue(is_supported_archive_path(Path("movie.rar")) is False)

    def test_default_archive_target_name_supports_7z_split_volumes(self) -> None:
        self.assertEqual(default_archive_target_name(Path("movie.zip")), "movie")
        self.assertEqual(default_archive_target_name(Path("movie.7z")), "movie")
        self.assertEqual(default_archive_target_name(Path("movie.7z.001")), "movie")

    def test_explorer_marks_only_first_7z_volume_as_extractable(self) -> None:
        with TemporaryDirectory(prefix="archive-explorer-") as temp_dir:
            root = Path(temp_dir)
            (root / "movie.7z.001").write_bytes(b"part1")
            (root / "movie.7z.002").write_bytes(b"part2")
            payload = list_directory(
                {"downloads": {"key": "downloads", "label": "Downloads", "path": root}},
                "downloads",
                "",
                "name",
            )

        entries = {entry["name"]: entry for entry in payload["entries"]}
        self.assertTrue(entries["movie.7z.001"]["is_archive"])
        self.assertEqual(entries["movie.7z.001"]["archive_type"], "7z")
        self.assertFalse(entries["movie.7z.002"]["is_archive"])
        self.assertIsNone(entries["movie.7z.002"]["archive_type"])

    def test_extract_archive_supports_rar(self) -> None:
        with TemporaryDirectory(prefix="archive-rar-") as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "sample.rar"
            archive_path.write_bytes(b"placeholder")
            destination = root / "output"

            fake_module = SimpleNamespace(RarFile=FakeRarFile, Error=FakeRarError)
            with patch.object(archives, "rarfile", fake_module):
                extracted = extract_archive(archive_path, destination, password="secret123")

            self.assertEqual(FakeRarFile.last_password, "secret123")
            self.assertEqual(len(extracted), 1)
            extracted_path = Path(extracted[0])
            self.assertTrue(extracted_path.exists())
            self.assertEqual(extracted_path.read_bytes(), b"rar-payload")

    def test_extract_archive_rar_missing_backend_surfaces_hint(self) -> None:
        with TemporaryDirectory(prefix="archive-rar-fail-") as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "sample.rar"
            archive_path.write_bytes(b"placeholder")
            destination = root / "output"

            fake_module = SimpleNamespace(RarFile=FailingRarFile, Error=FakeRarError)
            with patch.object(archives, "rarfile", fake_module):
                with self.assertRaises(ArchiveError) as raised:
                    extract_archive(archive_path, destination)

        self.assertIn("Install 'unar', 'unrar', or '7z' on the server.", str(raised.exception))

    def test_extract_archive_supports_7z(self) -> None:
        calls: list[list[str]] = []

        def fake_run(args, stdout=None, stderr=None, text=None, check=None):
            calls.append(args)
            if args[1] == "l":
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout="\n".join(
                        [
                            "Listing archive: sample.7z",
                            "----------",
                            "Path = folder",
                            "Path = folder/file.txt",
                        ]
                    ),
                    stderr="",
                )
            if args[1] == "x":
                output_arg = next(item for item in args if item.startswith("-o"))
                output_dir = Path(output_arg[2:])
                (output_dir / "folder").mkdir(parents=True, exist_ok=True)
                (output_dir / "folder" / "file.txt").write_bytes(b"sevenzip-payload")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            raise AssertionError(f"Unexpected 7z command: {args}")

        with TemporaryDirectory(prefix="archive-7z-") as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "sample.7z"
            archive_path.write_bytes(b"placeholder")
            destination = root / "output"

            with patch.object(archives.subprocess, "run", side_effect=fake_run):
                extracted = extract_archive(archive_path, destination, password="secret123")

            self.assertEqual(len(extracted), 1)
            extracted_path = Path(extracted[0])
            self.assertTrue(extracted_path.exists())
            self.assertEqual(extracted_path.read_bytes(), b"sevenzip-payload")
            self.assertTrue(any("-psecret123" in command for call in calls for command in call))

    def test_extract_archive_supports_7z_split_first_volume(self) -> None:
        def fake_run(args, stdout=None, stderr=None, text=None, check=None):
            if args[1] == "l":
                return subprocess.CompletedProcess(args, 0, stdout="----------\nPath = film.mkv\n", stderr="")
            if args[1] == "x":
                output_arg = next(item for item in args if item.startswith("-o"))
                output_dir = Path(output_arg[2:])
                output_dir.mkdir(parents=True, exist_ok=True)
                (output_dir / "film.mkv").write_bytes(b"split-payload")
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            raise AssertionError(f"Unexpected 7z command: {args}")

        with TemporaryDirectory(prefix="archive-7z-split-") as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "movie.7z.001"
            archive_path.write_bytes(b"placeholder")
            destination = root / "output"

            with patch.object(archives.subprocess, "run", side_effect=fake_run):
                extracted = extract_archive(archive_path, destination)

            self.assertEqual(Path(extracted[0]).name, "film.mkv")

    def test_extract_archive_7z_missing_binary_fails_cleanly(self) -> None:
        with TemporaryDirectory(prefix="archive-7z-missing-") as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "sample.7z"
            archive_path.write_bytes(b"placeholder")
            destination = root / "output"

            with patch.object(archives.subprocess, "run", side_effect=FileNotFoundError()):
                with self.assertRaises(ArchiveError) as raised:
                    extract_archive(archive_path, destination, seven_zip_binary="7z")

        self.assertIn("7z extraction support is unavailable", str(raised.exception))

    def test_extract_archive_7z_rejects_path_traversal(self) -> None:
        calls: list[list[str]] = []

        def fake_run(args, stdout=None, stderr=None, text=None, check=None):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout="----------\nPath = ../evil.txt\n", stderr="")

        with TemporaryDirectory(prefix="archive-7z-traversal-") as temp_dir:
            root = Path(temp_dir)
            archive_path = root / "sample.7z"
            archive_path.write_bytes(b"placeholder")
            destination = root / "output"

            with patch.object(archives.subprocess, "run", side_effect=fake_run):
                with self.assertRaises(ArchiveError) as raised:
                    extract_archive(archive_path, destination)

        self.assertIn("would extract outside the destination", str(raised.exception))
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
