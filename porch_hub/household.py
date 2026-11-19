"""The household: who lives here, where each person is now, and the PIN each one approves with.

Porch's gateway uses it for one rule. An agent acts for one person. It may not change a device in a
room where someone else is, while its own person is not in that room, without the permission of
someone who is there. The person in the room decides, not the person who is away.

Where people are is set by hand: this home has no sensor that tells people apart. Each change
needs the PIN of the person it is about, or the owner's, so nobody can mark someone else away to
get around the rule, and an agent has no PIN. Someone marked home with no room counts as being
in every room: Porch cannot tell where they are, so it asks them.

The owner approves with the owner PIN (protection.py). Every other person sets their own PIN on
the hub's computer (deploy/home/household.sh set-pin <person>); it is kept only as a salted hash.

Rooms are the floor plan's rooms (layout.py). A device is in the room the plan places it in, else
the room whose name, or one of its "matches", is the device's room in the hub.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .private_files import config_dir, private_write
from .protection import OwnerPin, PinLocked, PinNotSet, WrongPin, owner_pin_file

HOUSEHOLD_FILE = "household.json"
WHERE = ("away", "home")

__all__ = ["Household", "PersonUnknown", "NotAllowed", "PinLocked", "PinNotSet", "WrongPin",
           "household_file", "rooms_of", "place_of", "places"]


class PersonUnknown(ValueError):
    pass


class NotAllowed(PermissionError):
    pass


def household_file(env: Any = os.environ) -> Path:
    value = env.get("PORCH_HOUSEHOLD_FILE")
    return Path(value).expanduser() if value else config_dir(env) / HOUSEHOLD_FILE


def rooms_of(layout: dict) -> list[dict]:
    """Every room on the plan: id, name, floor, and the other names devices may give it."""
    return [{"id": room["id"], "name": room["name"], "floor": floor["name"], "matches": room.get("matches", [])}
            for floor in layout.get("floors", []) for room in floor.get("rooms", [])]


def place_of(device: dict, layout: dict) -> str | None:
    """The room id a device is in, or None when the plan does not say."""
    rooms = rooms_of(layout)
    known = {room["id"] for room in rooms}
    placed = (layout.get("placements") or {}).get(device["id"])
    if placed and placed.get("room") in known:
        return placed["room"]
    wanted = (device.get("room") or "").strip().lower()
    for room in rooms:
        if wanted and wanted in [name.strip().lower() for name in (room["name"], *room["matches"])]:
            return room["id"]
    return None


def places(devices: list[dict], layout: dict) -> dict[str, Any]:
    """The plan's rooms, and the room each device is in."""
    return {"rooms": [{k: room[k] for k in ("id", "name", "floor")} for room in rooms_of(layout)],
            "devices": {device["id"]: place_of(device, layout) for device in devices}}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Household:
    def __init__(self, path: Path, owner_pin: OwnerPin | None = None) -> None:
        self.path = path
        # The owner's PIN is the one family protection checks: one object, so wrong guesses on
        # either page count together.
        self.owner_pin = owner_pin or OwnerPin(owner_pin_file())
        self._pins: dict[str, OwnerPin] = {}  # one per person, so wrong guesses lock that person's PIN only

    # The file

    def load(self) -> dict:
        try:
            value = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {"people": [], "presence": {}}
        people = [p for p in value.get("people", []) if isinstance(p, dict) and p.get("id")]
        presence = value.get("presence") if isinstance(value.get("presence"), dict) else {}
        return {"people": people, "presence": presence}

    def save(self, value: dict) -> None:
        private_write(self.path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")

    def person(self, person_id: str) -> dict:
        found = next((p for p in self.load()["people"] if p["id"] == person_id), None)
        if found is None:
            raise PersonUnknown(f"no one called {person_id!r} lives in this household")
        return found

    def owner_id(self) -> str | None:
        return next((p["id"] for p in self.load()["people"] if p.get("owner")), None)

    # People

    def add(self, person_id: str, name: str, owner: bool = False) -> dict:
        person_id = person_id.strip().lower()
        if not person_id or not person_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError("a person's id is letters, digits, - and _, for example yoonchan")
        value = self.load()
        if any(p["id"] == person_id for p in value["people"]):
            raise ValueError(f"{person_id} is already in the household")
        if owner and any(p.get("owner") for p in value["people"]):
            raise ValueError("the household already has an owner")
        person = {"id": person_id, "name": name.strip() or person_id.capitalize(), "owner": owner}
        value["people"].append(person)
        self.save(value)
        return person

    def rename(self, person_id: str, name: str) -> None:
        value = self.load()
        for person in value["people"]:
            if person["id"] == person_id:
                person["name"] = name.strip() or person["name"]
                self.save(value)
                return
        raise PersonUnknown(f"no one called {person_id!r} lives in this household")

    def remove(self, person_id: str) -> None:
        value = self.load()
        if not any(p["id"] == person_id for p in value["people"]):
            raise PersonUnknown(f"no one called {person_id!r} lives in this household")
        if any(p["id"] == person_id and p.get("owner") for p in value["people"]):
            raise ValueError("the owner stays in the household")
        pin_file = self.pin_path(person_id)
        value["people"] = [p for p in value["people"] if p["id"] != person_id]
        value["presence"].pop(person_id, None)
        self.save(value)
        pin_file.unlink(missing_ok=True)
        self._pins.pop(person_id, None)

    # PINs

    def pin_path(self, person_id: str) -> Path:
        if self.owner_id() == person_id:
            return self.owner_pin.path
        return self.path.with_name(f"household-pin-{person_id}.json")

    def pin(self, person_id: str) -> OwnerPin:
        self.person(person_id)
        if self.owner_id() == person_id:
            return self.owner_pin
        path = self.pin_path(person_id)
        if person_id not in self._pins or self._pins[person_id].path != path:
            self._pins[person_id] = OwnerPin(path)
        return self._pins[person_id]

    def verify(self, person_id: str, pin: str) -> None:
        """Raise unless pin is this person's PIN. Wrong guesses lock it for a while."""
        self.pin(person_id).verify(pin)

    # Where people are

    def set_place(self, person_id: str, where: str, room: str | None, rooms: list[dict], *, by: str | None,
                  pin: str | None) -> dict:
        """Mark where someone is. by and pin: the person themself or the owner, with their PIN.
        by=None is the hub's computer (its Terminal): the owner's own hand, no PIN asked."""
        self.person(person_id)
        if where not in WHERE:
            raise ValueError("where is away or home")
        known = {r["id"] for r in rooms}
        if where == "away":
            room = None
        elif room is not None and room not in known:
            raise ValueError(f"there is no room {room!r} on this home's plan")
        if by is not None:
            if by not in (person_id, self.owner_id()):
                raise NotAllowed("only the person themself or the owner may say where someone is")
            self.verify(by, pin or "")
        value = self.load()
        value["presence"][person_id] = {"where": where, "room": room, "since": _now(), "set_by": by or "owner, on the hub's computer"}
        self.save(value)
        return value["presence"][person_id]

    def summary(self, rooms: list[dict]) -> dict[str, Any]:
        value = self.load()
        names = {r["id"]: r["name"] for r in rooms}
        people = []
        for person in value["people"]:
            here = value["presence"].get(person["id"], {})
            where = here.get("where")
            people.append({
                "id": person["id"], "name": person["name"], "owner": bool(person.get("owner")),
                "pin_set": self.pin(person["id"]).is_set(),
                # unknown: nobody has said yet, which the rule treats as away
                "where": where if where in WHERE else "unknown",
                "room": here.get("room") if where == "home" else None,
                "room_name": names.get(here.get("room")) if where == "home" else None,
                "since": here.get("since"), "set_by": here.get("set_by"),
            })
        return {"people": people, "rooms": [{k: r[k] for k in ("id", "name", "floor")} for r in rooms],
                "set_by_hand": True}


def main(argv: list[str] | None = None) -> int:
    """deploy/home/household.sh: the household, from the hub's computer (the owner's own hand)."""
    import argparse
    import getpass
    import sys

    from .layout import layout_file, load_layout
    from .protection import MIN_PIN_LENGTH, set_pin

    parser = argparse.ArgumentParser(prog="deploy/home/household.sh",
                                     description="Who lives here, where each person is, and their PINs.")
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="the people, where each is, and whether each has a PIN")
    adding = actions.add_parser("add", help="a person who lives here")
    adding.add_argument("id")
    adding.add_argument("--name", default="")
    adding.add_argument("--owner", action="store_true", help="the household's owner (one only)")
    naming = actions.add_parser("rename", help="the name people see")
    naming.add_argument("id")
    naming.add_argument("name")
    removing = actions.add_parser("remove", help="someone who no longer lives here")
    removing.add_argument("id")
    pinning = actions.add_parser("set-pin", help="the PIN this person approves with (the owner's is owner.sh set-pin)")
    pinning.add_argument("id")
    placing = actions.add_parser("where", help="mark where someone is: away, home, or a room id")
    placing.add_argument("id")
    placing.add_argument("place")
    args = parser.parse_args(argv)

    home = Household(household_file())
    rooms = rooms_of(load_layout(layout_file()))
    try:
        if args.action == "add":
            person = home.add(args.id, args.name, owner=args.owner)
            print(f"Added {person['name']} ({person['id']}){' as the owner' if person['owner'] else ''}.")
            if not person["owner"]:
                print(f"They choose their own PIN on this Mac: deploy/home/household.sh set-pin {person['id']}")
        elif args.action == "rename":
            home.rename(args.id, args.name)
            print(f"{args.id} is now called {args.name}.")
        elif args.action == "remove":
            home.remove(args.id)
            print(f"Removed {args.id}, their place and their PIN.")
        elif args.action == "set-pin":
            person = home.person(args.id)
            if person.get("owner"):
                print("The owner's PIN is the one family protection uses: deploy/home/owner.sh set-pin", file=sys.stderr)
                return 1
            first = getpass.getpass(f"{person['name']}'s PIN, at least {MIN_PIN_LENGTH} characters (hidden): ")
            if first != getpass.getpass("Again (hidden): "):
                print("Stopped: the two did not match. Nothing was changed.", file=sys.stderr)
                return 1
            set_pin(home.pin_path(args.id), first)
            print(f"{person['name']}'s PIN is saved, as a salted hash only. It approves requests held for them "
                  "and marks where they are.")
        elif args.action == "where":
            place = args.place.strip()
            if place in ("away", "home"):
                home.set_place(args.id, place, None, rooms, by=None, pin=None)
            else:
                room = next((r["id"] for r in rooms if place.lower() in (r["id"].lower(), r["name"].lower())), place)
                home.set_place(args.id, "home", room, rooms, by=None, pin=None)
        if args.action in ("list", "where", "add", "rename", "remove"):
            for person in home.summary(rooms)["people"]:
                where = (f"home, in {person['room_name']}" if person["room_name"] else
                         "home, room not set" if person["where"] == "home" else person["where"])
                print(f"  {person['id']:12} {person['name']:16} {'owner  ' if person['owner'] else '       '}"
                      f"{where:28} PIN {'set' if person['pin_set'] else 'not set'}")
            if args.action == "list" and not home.load()["people"]:
                print("  No one yet. Start with: deploy/home/household.sh add <you> --name <Name> --owner")
            if args.action in ("list", "where"):
                print(f"  Rooms: {', '.join(r['id'] for r in rooms)}")
    except (PersonUnknown, NotAllowed, ValueError) as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
