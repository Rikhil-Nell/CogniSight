"""Versioned room-graph validation and private local JSON persistence."""

import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import uuid


MAX_MAP_BYTES = 16 * 1024
MAX_ITEMS_PER_KIND = 100


class MapValidationError(ValueError):
    """The supplied document does not conform to the indoor-map schema."""


class MapStoreError(RuntimeError):
    """The local indoor-map file could not be read or safely written."""


def _keys(value, required, context):
    if type(value) is not dict or set(value) != set(required):
        raise MapValidationError(f"{context} has an invalid shape")


def _text(value, context, maximum):
    if not isinstance(value, str):
        raise MapValidationError(f"{context} must be text")
    value = value.strip()
    if not value or len(value) > maximum or any(ord(char) < 32 for char in value):
        raise MapValidationError(f"{context} must contain 1 to {maximum} valid characters")
    return value


def _uuid(value, context):
    if not isinstance(value, str):
        raise MapValidationError(f"{context} must be a UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise MapValidationError(f"{context} must be a canonical UUID") from None
    if str(parsed) != value:
        raise MapValidationError(f"{context} must be a canonical UUID")
    return value


def _items(value, context):
    if type(value) is not list or len(value) > MAX_ITEMS_PER_KIND:
        raise MapValidationError(f"{context} must be a list of at most {MAX_ITEMS_PER_KIND} items")
    return value


def validate_map_document(document):
    """Return a normalized schema-v1 map or raise MapValidationError."""
    _keys(
        document,
        ("schema_version", "map_id", "name", "rooms", "connections", "landmarks"),
        "Map",
    )
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise MapValidationError("schema_version must be 1")

    map_id = _uuid(document["map_id"], "map_id")
    name = _text(document["name"], "Map name", 100)
    identifiers = {map_id}

    rooms = []
    room_ids = set()
    for room in _items(document["rooms"], "rooms"):
        _keys(room, ("id", "name"), "Room")
        room_id = _uuid(room["id"], "Room id")
        if room_id in identifiers:
            raise MapValidationError("IDs must be unique within the map")
        identifiers.add(room_id)
        room_ids.add(room_id)
        rooms.append({"id": room_id, "name": _text(room["name"], "Room name", 100)})

    connections = []
    for connection in _items(document["connections"], "connections"):
        _keys(connection, ("id", "from_room_id", "to_room_id", "kind"), "Connection")
        connection_id = _uuid(connection["id"], "Connection id")
        if connection_id in identifiers:
            raise MapValidationError("IDs must be unique within the map")
        identifiers.add(connection_id)
        from_room = _uuid(connection["from_room_id"], "Connection source room")
        to_room = _uuid(connection["to_room_id"], "Connection destination room")
        if from_room not in room_ids or to_room not in room_ids:
            raise MapValidationError("Connections must reference rooms in this map")
        if from_room == to_room:
            raise MapValidationError("A connection cannot link a room to itself")
        kind = connection["kind"]
        if kind not in ("doorway", "corridor"):
            raise MapValidationError("Connection kind must be doorway or corridor")
        connections.append(
            {
                "id": connection_id,
                "from_room_id": from_room,
                "to_room_id": to_room,
                "kind": kind,
            }
        )

    landmarks = []
    for landmark in _items(document["landmarks"], "landmarks"):
        _keys(landmark, ("id", "room_id", "label"), "Landmark")
        landmark_id = _text(landmark["id"], "Landmark id", 64)
        canonical_landmark_id = landmark_id.casefold()
        if canonical_landmark_id in {item.casefold() for item in identifiers}:
            raise MapValidationError("IDs must be unique within the map")
        identifiers.add(landmark_id)
        room_id = _uuid(landmark["room_id"], "Landmark room")
        if room_id not in room_ids:
            raise MapValidationError("Landmarks must reference rooms in this map")
        landmarks.append(
            {
                "id": landmark_id,
                "room_id": room_id,
                "label": _text(landmark["label"], "Landmark label", 100),
            }
        )

    normalized = {
        "schema_version": 1,
        "map_id": map_id,
        "name": name,
        "rooms": rooms,
        "connections": connections,
        "landmarks": landmarks,
    }
    try:
        encoded = json.dumps(
            normalized, ensure_ascii=False, indent=2, allow_nan=False
        ).encode("utf-8")
    except (UnicodeEncodeError, ValueError):
        raise MapValidationError("Map contains invalid text") from None
    if len(encoded) > MAX_MAP_BYTES:
        raise MapValidationError(f"Map must be no larger than {MAX_MAP_BYTES} bytes")
    return normalized


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MapValidationError("Map JSON contains a duplicate object key")
        result[key] = value
    return result


def parse_map_document(payload):
    """Parse UTF-8 JSON while rejecting ambiguous duplicate object keys."""
    try:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        elif not isinstance(payload, str):
            raise MapValidationError("Map JSON is malformed")
        return json.loads(payload, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MapValidationError("Map JSON is malformed") from None


class IndoorMapStore:
    """Atomically persist one validated home map outside the repository."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def load(self):
        with self._lock:
            try:
                if self.path.is_symlink():
                    raise MapStoreError("Indoor map file must not be a symbolic link")
                if not self.path.exists():
                    return None
                metadata = self.path.stat()
                if not stat.S_ISREG(metadata.st_mode):
                    raise MapStoreError("Indoor map path must be a regular file")
                if metadata.st_size > MAX_MAP_BYTES:
                    raise MapStoreError("Stored indoor map exceeds the size limit")
                with self.path.open("r", encoding="utf-8") as source:
                    document = parse_map_document(source.read())
                return validate_map_document(document)
            except MapStoreError:
                raise
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, MapValidationError):
                raise MapStoreError("Stored indoor map is unreadable or invalid") from None

    def save(self, document):
        normalized = validate_map_document(document)
        payload = json.dumps(
            normalized, ensure_ascii=False, indent=2, allow_nan=False
        ) + "\n"
        with self._lock:
            temporary_path = None
            descriptor = None
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if self.path.is_symlink():
                    raise MapStoreError("Indoor map file must not be a symbolic link")
                descriptor, name = tempfile.mkstemp(
                    prefix=".indoor-map-", dir=str(self.path.parent)
                )
                temporary_path = Path(name)
                os.chmod(temporary_path, 0o600)
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as target:
                    descriptor = None
                    target.write(payload)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary_path, self.path)
                temporary_path = None
            except MapStoreError:
                raise
            except OSError:
                raise MapStoreError("Indoor map could not be saved") from None
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                if temporary_path is not None:
                    try:
                        temporary_path.unlink()
                    except FileNotFoundError:
                        pass
        return normalized
