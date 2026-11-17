import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path

from cognisight.indoor import IndoorMapStore

HAS_WEB = all(
    importlib.util.find_spec(name) is not None for name in ("flask", "flask_wtf")
)


def sample_map():
    return {
        "schema_version": 1,
        "map_id": "2eeaf8f8-a607-4f66-87d1-fb701504e5d7",
        "name": "Synthetic home",
        "rooms": [
            {"id": "7eb44b7a-b55c-46c5-8130-116130dfb343", "name": "Hall"},
            {"id": "02eaf8c4-5c0e-45bd-a113-e301d8f78a49", "name": "Kitchen"},
        ],
        "connections": [
            {
                "id": "fa0b0edb-5afd-44ca-829b-e2273b1499e2",
                "from_room_id": "7eb44b7a-b55c-46c5-8130-116130dfb343",
                "to_room_id": "02eaf8c4-5c0e-45bd-a113-e301d8f78a49",
                "kind": "doorway",
            }
        ],
        "landmarks": [
            {
                "id": "LM-HALL-1",
                "room_id": "7eb44b7a-b55c-46c5-8130-116130dfb343",
                "label": "Hall marker",
            }
        ],
    }


@unittest.skipUnless(HAS_WEB, "Flask/Flask-WTF unavailable; dashboard integration not run")
class IndoorMapWebTests(unittest.TestCase):
    def setUp(self):
        from cognisight.caregiver import create_dashboard_app

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.map_store = IndoorMapStore(self.state / "indoor-map.json")
        self.app = create_dashboard_app(
            lambda: {"schema_version": 1},
            self.state,
            ["cognisight.local"],
            map_store=self.map_store,
        )
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def get(self, path):
        return self.client.get(
            path,
            base_url="https://cognisight.local",
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )

    def authenticate(self):
        with self.client.session_transaction(
            base_url="https://cognisight.local"
        ) as session:
            session["authenticated"] = True

    def csrf_token(self):
        html = self.get("/dashboard/indoor-map").get_data(as_text=True)
        return re.search(
            r'id="map-csrf-token"[^>]*value="([^"]+)"', html
        ).group(1)

    def put_map(self, document, token=None):
        headers = {
            "Origin": "https://cognisight.local",
            "Referer": "https://cognisight.local/dashboard/indoor-map",
        }
        if token is not None:
            headers["X-CSRFToken"] = token
        return self.client.put(
            "/dashboard/indoor-map/data",
            base_url="https://cognisight.local",
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            headers=headers,
            json=document,
        )

    def test_editor_and_data_are_authenticated_and_csrf_protected(self):
        self.assertEqual(self.get("/dashboard/indoor-map").status_code, 302)
        self.assertEqual(self.get("/dashboard/indoor-map/data").status_code, 401)
        self.assertEqual(self.get("/dashboard/indoor-map/export").status_code, 401)
        self.authenticate()
        page = self.get("/dashboard/indoor-map")
        self.assertEqual(page.status_code, 200)
        self.assertIn("does not show the wearer", page.get_data(as_text=True))
        self.assertEqual(self.put_map(sample_map()).status_code, 400)

    def test_save_reload_export_and_indoor_status_remains_unavailable(self):
        self.authenticate()
        document = sample_map()
        response = self.put_map(document, self.csrf_token())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["map"], document)
        self.assertEqual(self.get("/dashboard/indoor-map/data").json, {"map": document})

        export = self.get("/dashboard/indoor-map/export")
        self.assertEqual(export.status_code, 200)
        self.assertIn("attachment;", export.headers["Content-Disposition"])
        self.assertEqual(json.loads(export.get_data(as_text=True)), document)

        # A new store instance simulates the next process loading the private file.
        reloaded = IndoorMapStore(self.state / "indoor-map.json").load()
        self.assertEqual(reloaded, document)
        self.assertEqual(self.get("/dashboard/state").json["indoor"]["status"], "unavailable")

    def test_invalid_map_does_not_replace_saved_map(self):
        self.authenticate()
        token = self.csrf_token()
        original = sample_map()
        self.assertEqual(self.put_map(original, token).status_code, 200)
        invalid = sample_map()
        invalid["connections"][0]["to_room_id"] = "00000000-0000-4000-8000-000000000000"
        response = self.put_map(invalid, token)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.map_store.load(), original)

    def test_duplicate_json_keys_do_not_replace_saved_map(self):
        self.authenticate()
        token = self.csrf_token()
        original = sample_map()
        self.assertEqual(self.put_map(original, token).status_code, 200)
        response = self.client.put(
            "/dashboard/indoor-map/data",
            base_url="https://cognisight.local",
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            headers={
                "Origin": "https://cognisight.local",
                "Referer": "https://cognisight.local/dashboard/indoor-map",
                "X-CSRFToken": token,
            },
            data='{"schema_version":1,"schema_version":1}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.map_store.load(), original)

    def test_empty_map_export_and_corrupt_file_are_reported(self):
        self.authenticate()
        self.assertEqual(self.get("/dashboard/indoor-map/export").status_code, 404)
        (self.state / "indoor-map.json").write_text("{not-json", encoding="utf-8")
        self.assertEqual(self.get("/dashboard/indoor-map/data").status_code, 503)


if __name__ == "__main__":
    unittest.main()
