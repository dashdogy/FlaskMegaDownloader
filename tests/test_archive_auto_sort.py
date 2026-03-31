from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from archive_auto_sort import sort_extracted_videos


class ArchiveAutoSortTests(unittest.TestCase):
    def test_sort_extracted_videos_moves_movie_and_tv_and_leaves_unclear(self) -> None:
        with TemporaryDirectory(prefix="archive-auto-sort-") as temp_dir:
            root = Path(temp_dir)
            extracted_dir = root / "extracted"
            extracted_dir.mkdir(parents=True, exist_ok=True)
            movies_dir = root / "Movies"
            tv_dir = root / "TvShows"

            movie_path = extracted_dir / "Movie.Name.2024.mkv"
            tv_path = extracted_dir / "Show.Name.S01E02.mkv"
            unclear_path = extracted_dir / "Mystery.Release.mkv"
            non_video_path = extracted_dir / "poster.jpg"
            movie_path.write_bytes(b"movie")
            tv_path.write_bytes(b"episode")
            unclear_path.write_bytes(b"unknown")
            non_video_path.write_bytes(b"poster")

            def fake_guess(filename: str) -> dict:
                if filename == movie_path.name:
                    return {"type": "movie", "title": "Movie Name"}
                if filename == tv_path.name:
                    return {"type": "episode", "title": "Show Name", "season": 1, "episode": 2}
                return {"type": "unknown"}

            with patch("archive_auto_sort._guess_media_info", side_effect=fake_guess):
                summary = sort_extracted_videos(
                    [movie_path, tv_path, unclear_path, non_video_path],
                    movies_target_path=movies_dir,
                    tv_target_path=tv_dir,
                )

            self.assertEqual(summary.moved_movies, 1)
            self.assertEqual(summary.moved_tv, 1)
            self.assertEqual(summary.skipped_unclear, 1)
            self.assertEqual(summary.skipped_non_video, 1)
            self.assertTrue((movies_dir / movie_path.name).exists())
            self.assertTrue((tv_dir / "Show Name" / tv_path.name).exists())
            self.assertTrue(unclear_path.exists())
            self.assertTrue(non_video_path.exists())

    def test_sort_extracted_videos_leaves_conflicts_in_place(self) -> None:
        with TemporaryDirectory(prefix="archive-auto-sort-conflict-") as temp_dir:
            root = Path(temp_dir)
            extracted_dir = root / "extracted"
            extracted_dir.mkdir(parents=True, exist_ok=True)
            movies_dir = root / "Movies"
            movies_dir.mkdir(parents=True, exist_ok=True)
            tv_dir = root / "TvShows"

            movie_path = extracted_dir / "Movie.Name.2024.mkv"
            movie_path.write_bytes(b"movie")
            (movies_dir / movie_path.name).write_bytes(b"existing")

            with patch("archive_auto_sort._guess_media_info", return_value={"type": "movie", "title": "Movie Name"}):
                summary = sort_extracted_videos(
                    [movie_path],
                    movies_target_path=movies_dir,
                    tv_target_path=tv_dir,
                )

            self.assertEqual(summary.skipped_conflict, 1)
            self.assertTrue(movie_path.exists())
            self.assertEqual((movies_dir / movie_path.name).read_bytes(), b"existing")


if __name__ == "__main__":
    unittest.main()
