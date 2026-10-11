"""R2 대량 삭제는 DeleteObjects 요청 제한을 넘기지 않아야 한다."""
import unittest
from unittest.mock import patch

from app import storage


class _R2Client:
    def __init__(self):
        self.calls = []

    def delete_objects(self, **kwargs):
        self.calls.append(kwargs)
        return {"Deleted": kwargs["Delete"]["Objects"]}


class DeleteR2ObjectsTest(unittest.TestCase):
    def test_splits_more_than_one_thousand_keys(self):
        client = _R2Client()
        keys = [f"photos/test/{index}.jpg" for index in range(2001)]
        with patch.object(storage, "R2_BUCKET_NAME", "test-bucket"), patch.object(
            storage, "get_r2_client", return_value=client
        ):
            deleted = storage.delete_r2_objects(keys)

        self.assertEqual(deleted, 2001)
        self.assertEqual([len(call["Delete"]["Objects"]) for call in client.calls], [1000, 1000, 1])
        self.assertEqual(client.calls[0]["Delete"]["Objects"][0], {"Key": keys[0]})
        self.assertEqual(client.calls[-1]["Delete"]["Objects"][0], {"Key": keys[-1]})

    def test_partial_errors_fail_both_key_and_prefix_deletion(self):
        from unittest.mock import MagicMock
        client = MagicMock()
        client.delete_objects.return_value = {"Errors": [{"Key": "test", "Code": "AccessDenied"}]}
        client.get_paginator.return_value.paginate.return_value = [{"Contents": [{"Key": "test"}]}]
        with patch.object(storage, "R2_BUCKET_NAME", "test-bucket"), patch.object(storage, "get_r2_client", return_value=client):
            for delete, value in ((storage.delete_r2_objects, ["test"]), (storage.delete_r2_objects_by_prefix, "test/")):
                with self.assertRaises(RuntimeError):
                    delete(value)

    def test_missing_bucket_is_not_success(self):
        with patch.object(storage, "R2_BUCKET_NAME", None):
            with self.assertRaises(ValueError):
                storage.delete_r2_objects(["test"])
            with self.assertRaises(ValueError):
                storage.delete_r2_objects_by_prefix("test/")


if __name__ == "__main__":
    unittest.main()
