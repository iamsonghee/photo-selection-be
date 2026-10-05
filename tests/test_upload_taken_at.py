"""작가 업로드의 촬영 시각은 형식이 맞을 때만 저장하고, 틀리면 버릴 뿐 업로드를 막지 않는다."""
import unittest

from app.routers import upload


class UploadTakenAtTest(unittest.TestCase):
    def test_valid_value_and_source_are_kept(self):
        self.assertEqual(upload._parse_taken_at("2026-10-05T14:03:09", "exif"), ("2026-10-05T14:03:09", "exif"))
        self.assertEqual(upload._parse_taken_at("2026-10-05T14:03:09", "file"), ("2026-10-05T14:03:09", "file"))

    def test_unknown_source_keeps_time_only(self):
        self.assertEqual(upload._parse_taken_at("2026-10-05T14:03:09", "gps"), ("2026-10-05T14:03:09", None))

    def test_missing_or_malformed_value_is_dropped(self):
        for value in ("", "2026:10:05 14:03:09", "2026-10-05T14:03:09Z", "nope"):
            self.assertEqual(upload._parse_taken_at(value, "exif"), (None, None))


if __name__ == "__main__":
    unittest.main()
