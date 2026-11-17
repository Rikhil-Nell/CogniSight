"""Shortest paths over the caregiver-authored indoor room graph."""

from collections import deque

from .map_store import validate_map_document


class RoutePlanningError(ValueError):
    """The requested route endpoints are not valid rooms in the map."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def plan_route(document, start_room_id, destination_room_id):
    """Return a shortest unweighted route, or a structured unreachable result."""
    indoor_map = validate_map_document(document)
    if not isinstance(start_room_id, str) or not start_room_id:
        raise RoutePlanningError("invalid_start_room")
    if not isinstance(destination_room_id, str) or not destination_room_id:
        raise RoutePlanningError("invalid_destination_room")

    rooms = {room["id"]: room for room in indoor_map["rooms"]}
    if start_room_id not in rooms:
        raise RoutePlanningError("unknown_start_room")
    if destination_room_id not in rooms:
        raise RoutePlanningError("unknown_destination_room")

    def room_summary(room_id):
        room = rooms[room_id]
        return {"id": room["id"], "name": room["name"]}

    result = {
        "status": "unreachable",
        "start_source": "manual_demo_selection",
        "start_room": room_summary(start_room_id),
        "destination_room": room_summary(destination_room_id),
        "rooms": [],
        "connections": [],
    }

    parents = {start_room_id: None}
    pending = deque([start_room_id])
    adjacency = {room_id: [] for room_id in rooms}
    for connection in indoor_map["connections"]:
        adjacency[connection["from_room_id"]].append(
            (connection["to_room_id"], connection)
        )
        adjacency[connection["to_room_id"]].append(
            (connection["from_room_id"], connection)
        )

    while pending and destination_room_id not in parents:
        current = pending.popleft()
        for neighbor, connection in adjacency[current]:
            if neighbor in parents:
                continue
            parents[neighbor] = (current, connection)
            pending.append(neighbor)

    if destination_room_id not in parents:
        return result

    room_ids = []
    route_connections = []
    current = destination_room_id
    while current is not None:
        room_ids.append(current)
        parent = parents[current]
        if parent is not None:
            previous, connection = parent
            route_connections.append(
                {
                    "id": connection["id"],
                    "from_room_id": previous,
                    "to_room_id": current,
                    "kind": connection["kind"],
                }
            )
            current = previous
        else:
            current = None

    room_ids.reverse()
    route_connections.reverse()
    result["status"] = "found"
    result["rooms"] = [room_summary(room_id) for room_id in room_ids]
    result["connections"] = route_connections
    return result


__all__ = ["RoutePlanningError", "plan_route"]
