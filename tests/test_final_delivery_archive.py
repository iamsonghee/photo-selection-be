"""최종 보정본 ZIP은 원본 크기 업로드 바이트와 파일명을 그대로 보존한다."""
import hashlib
import os
import zipfile
import unittest
from unittest.mock import patch

from app import archive


class FinalDeliveryArchiveTest(unittest.TestCase):
    def test_zip_preserves_delivery_files_and_resolves_duplicate_names(self):
        objects = {
            "versions/project/delivery/v1/one.jpg": b"full-resolution-one",
            "versions/project/delivery/v2/two.jpg": b"full-resolution-two",
        }
        manifest = [
            {"photo_id": "1", "key": next(iter(objects)), "filename": "final.jpg", "byte_size": 19},
            {"photo_id": "2", "key": list(objects)[1], "filename": "final.jpg", "byte_size": 19},
        ]
        with patch.object(archive, "get_r2_object_bytes_sync", side_effect=lambda key: objects[key]):
            path, count = archive._download_and_zip_delivery_sync(manifest)
        try:
            self.assertEqual(count, 2)
            with zipfile.ZipFile(path) as zipped:
                self.assertEqual(zipped.namelist(), ["final.jpg", "final (2).jpg"])
                self.assertEqual(
                    hashlib.sha256(zipped.read("final.jpg")).digest(),
                    hashlib.sha256(objects[manifest[0]["key"]]).digest(),
                )
        finally:
            os.remove(path)

    def test_bin_pack_uses_delivery_byte_size(self):
        entries = [{"byte_size": 60}, {"byte_size": 60}, {"byte_size": 40}]
        with patch.object(archive, "ARCHIVE_PART_MAX_BYTES", 100):
            groups = archive._bin_pack_delivery(entries)
        self.assertEqual([len(group) for group in groups], [1, 2])


if __name__ == "__main__":
    unittest.main()
