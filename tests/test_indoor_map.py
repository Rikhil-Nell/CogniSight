import json
import os
import stat
import tempfile
import unittest
import uuid
from pathlib import Path

from cognisight.indoor import (
    IndoorMapStore,
    MapStoreError,
    MapValidationError,
    validate_map_document,
)


def new_id():
    return str(uuid.uuid4())


def valid_map():
    living_room, kitchen = new_id(), new_id()
    return {
        "schema_version": 1,
        "map_id": new_id(),
        "name": "Home",
        "rooms": [
            {"id": living_room, "name": "Hall"},
            {"id": kitchen, "name": "Kitchen"},
        ],
        "connections": [
            {
                "id": new_id(),
                "from_room_id": living_room,
                "to_room_id": kitchen,
                "kind": "doorway",
            }
        ],
        "landmarks": [
            {"id": "LM-01", "room_id": living_room, "label": "Hall marker"}
        ],
    }


class IndoorMapValidationTests(unittest.TestCase):
    def test_valid_document_is_normalized_and_empty_map_is_allowed(self):
        source = valid_map()
        source["name"] = "  Home  "
        normalized = validate_map_document(source)
        self.assertEqual(normalized["name"], "Home")
        self.assertEqual(normalized["connections"][0]["kind"], "doorway")
        self.assertEqual(normalized["landmarks"][0]["id"], "LM-01")
        self.assertEqual(source["name"], "  Home  ")

        empty = {
            "schema_version": 1,
            "map_id": new_id(),
            "name": "New home",
            "rooms": [],
            "connections": [],
            "landmarks": [],
        }
        self.assertEqual(validate_map_document(empty)["rooms"], [])

    def test_duplicate_ids_are_rejected_across_all_map_entities(self):
        document = valid_map()
        document["connections"][0]["id"] = document["rooms"][0]["id"]
        with self.assertRaisesRegex(MapValidationError, "IDs must be unique"):
            validate_map_document(document)

        document = valid_map()
        document["landmarks"].append(
            {"id": "lm-01", "room_id": document["rooms"][0]["id"], "label": "Second"}
        )
        with self.assertRaisesRegex(MapValidationError, "IDs must be unique"):
            validate_map_document(document)

    def test_missing_rooms_self_links_and_unknown_connection_kinds_are_rejected(self):
        invalid_changes = (
            {"from_room_id": new_id()},
            {"to_room_id": new_id()},
            {"to_room_id": None},
            {"kind": "stairs"},
        )
        for change in invalid_changes:
            with self.subTest(change=change):
                document = valid_map()
                document["connections"][0].update(change)
                with self.assertRaises(MapValidationError):
                    validate_map_document(document)

        document = valid_map()
        document["connections"][0]["to_room_id"] = document["connections"][0]["from_room_id"]
        with self.assertRaisesRegex(MapValidationError, "cannot link a room to itself"):
            validate_map_document(document)

    def test_missing_landmark_room_and_invalid_schema_are_rejected(self):
        document = valid_map()
        document["landmarks"][0]["room_id"] = new_id()
        with self.assertRaisesRegex(MapValidationError, "Landmarks must reference rooms"):
            validate_map_document(document)

        document = valid_map()
        document["schema_version"] = True
        with self.assertRaisesRegex(MapValidationError, "schema_version must be 1"):
            validate_map_document(document)

    def test_persistence_round_trip_is_private_and_invalid_save_preserves_previous_map(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "indoor-map.json"
            store = IndoorMapStore(path)
            source = valid_map()
            store.save(source)
            self.assertEqual(IndoorMapStore(path).load(), source)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o077, 0)

            invalid = valid_map()
            invalid["connections"][0]["from_room_id"] = new_id()
            with self.assertRaises(MapValidationError):
                store.save(invalid)
            self.assertEqual(store.load(), source)

    def test_malformed_or_duplicate_key_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "indoor-map.json"
            path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            with self.assertRaises(MapStoreError):
                IndoorMapStore(path).load()

            path.write_text(json.dumps({**valid_map(), "schema_version": 2}), encoding="utf-8")
            with self.assertRaises(MapStoreError):
                IndoorMapStore(path).load()


if __name__ == "__main__":
    unittest.main()
