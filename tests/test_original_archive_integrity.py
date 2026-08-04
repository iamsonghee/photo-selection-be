"""납품 ZIP은 작가가 올린 원본 바이트·파일명을 보존해야 한다."""
import hashlib
import os
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import patch

from app import archive


class _Response:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def select(self, *_args): return self
    def in_(self, *_args): return self
    def execute(self): return _Response(self.rows)


class _Supabase:
    def __init__(self, rows): self.rows = rows
    def table(self, _name): return _Query(self.rows)


class OriginalArchiveIntegrityTest(unittest.TestCase):
    def test_zip_preserves_original_filename_and_bytes(self):
        originals = {
            "originals/source/project/first.nef": b"RAW\x00original-bytes-1",
            "originals/source/project/second.JPG": b"\xff\xd8original-jpeg-bytes\xff\xd9",
        }
        rows = [
            {"id": "1", "number": 1, "r2_original_url": "originals/source/project/first.nef", "original_filename": "first.nef"},
            {"id": "2", "number": 2, "r2_original_url": "originals/source/project/second.JPG", "original_filename": "second.JPG"},
        ]
        with patch.object(archive, "get_supabase", return_value=_Supabase(rows)), patch.object(
            archive, "get_r2_object_bytes_sync", side_effect=lambda key: originals[key]
        ):
            path, count = archive._download_and_zip_sync(["1", "2"])
        try:
            self.assertEqual(count, 2)
            with zipfile.ZipFile(path) as zf:
                self.assertEqual(zf.namelist(), ["first.nef", "second.JPG"])
                for name, expected in (("first.nef", originals["originals/source/project/first.nef"]), ("second.JPG", originals["originals/source/project/second.JPG"])):
                    self.assertEqual(hashlib.sha256(zf.read(name)).digest(), hashlib.sha256(expected).digest())
        finally:
            os.remove(path)

    def test_prefetch_keeps_manifest_order_with_bounded_parallel_downloads(self):
        originals = {
            "originals/source/project/one.jpg": b"one",
            "originals/source/project/two.jpg": b"two",
            "originals/source/project/three.jpg": b"three",
        }
        rows = [
            {"id": "1", "number": 1, "r2_original_url": "originals/source/project/one.jpg", "original_filename": "one.jpg"},
            {"id": "2", "number": 2, "r2_original_url": "originals/source/project/two.jpg", "original_filename": "two.jpg"},
            {"id": "3", "number": 3, "r2_original_url": "originals/source/project/three.jpg", "original_filename": "three.jpg"},
        ]
        first_two_started = threading.Barrier(2, timeout=1)
        active = 0
        peak_active = 0
        active_lock = threading.Lock()

        def download(key):
            nonlocal active, peak_active
            with active_lock:
                active += 1
                peak_active = max(peak_active, active)
            try:
                if key.endswith(("one.jpg", "two.jpg")):
                    first_two_started.wait()
                return originals[key]
            finally:
                with active_lock:
                    active -= 1

        with patch.object(archive, "get_supabase", return_value=_Supabase(rows)), patch.object(
            archive, "get_r2_object_bytes_sync", side_effect=download
        ), patch.object(archive, "ARCHIVE_DOWNLOAD_CONCURRENCY", 2):
            path, count = archive._download_and_zip_sync(["1", "2", "3"])
        try:
            self.assertEqual(count, 3)
            self.assertGreaterEqual(peak_active, 2)
            with zipfile.ZipFile(path) as zf:
                self.assertEqual(zf.namelist(), ["one.jpg", "two.jpg", "three.jpg"])
                self.assertEqual(zf.read("one.jpg"), b"one")
                self.assertEqual(zf.read("two.jpg"), b"two")
                self.assertEqual(zf.read("three.jpg"), b"three")
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
