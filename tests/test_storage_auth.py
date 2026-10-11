"""Internal deletion must never reach R2 without server authentication."""
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.routers import storage


class StorageAuthTest(unittest.TestCase):
    def test_internal_authentication(self):
        app = FastAPI()
        app.include_router(storage.router)
        client = TestClient(app)
        with patch.object(storage, 'INTERNAL_PRESIGN_SECRET', 'test-secret'), patch.object(storage, 'delete_r2_objects', return_value=1) as delete:
            for headers in ({}, {'Authorization': 'Bearer wrong'}):
                self.assertEqual(client.post('/delete', json={'keys': ['test']}, headers=headers).status_code, 403)
                delete.assert_not_called()
            response = client.post('/delete', json={'keys': ['test']}, headers={'Authorization': 'Bearer test-secret'})
            self.assertEqual(response.json(), {'deleted': 1})
            delete.assert_called_once_with(['test'])
        with patch.object(storage, 'INTERNAL_PRESIGN_SECRET', None), patch.object(storage, 'delete_r2_objects') as delete:
            self.assertEqual(client.post('/delete', json={'keys': ['test']}).status_code, 503)
            delete.assert_not_called()
