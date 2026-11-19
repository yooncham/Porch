"""A home's floor plan for the twin pages: floors, rooms, fixtures, and where devices sit.

The 2D twin and the 3D view both draw from GET /home/layout, so they always show the same home.
A real home describes itself in a JSON file (PORCH_HOME_LAYOUT, normally home-layout.json in the
hub's settings folder; deploy/home-layout.example.json is a starting point). Without one, the
hub serves the plan of the simulated demo home.

Lengths are in feet. Each floor has its own origin at its top-left corner: x runs right, y runs
down the page (toward the back of the home).

A device sits where "placements" puts it: a room id and a spot inside that room, as fractions
of the room's width and depth. A device without a placement is put in the room whose name, or
one of its "matches", equals the device's room in the hub. A device that fits no room is listed
beside the plan, never hidden.
"""

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

log = logging.getLogger(__name__)


class Room(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    x: float = Field(ge=0)
    y: float = Field(ge=0)
    w: float = Field(gt=0)
    d: float = Field(gt=0)
    # How the room is drawn: a wet room is tinted, a store is dimmed, and so on.
    kind: Literal["room", "kitchen", "bath", "hall", "store", "outside"] = "room"
    # Room names the hub's devices may carry that mean this room (Google Home's "Living Room").
    matches: list[str] = Field(default_factory=list)


class Fixture(BaseModel):
    """Something drawn on the plan that is not a room: stairs, a door, a counter, a caption."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["stairs", "door", "counter", "label"]
    x: float = Field(ge=0)
    y: float = Field(ge=0)
    w: float = Field(ge=0, default=0)
    d: float = Field(ge=0, default=0)
    label: str | None = None


class Floor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    width: float = Field(gt=0)
    depth: float = Field(gt=0)
    rooms: list[Room] = Field(min_length=1)
    fixtures: list[Fixture] = Field(default_factory=list)


class Placement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    room: str
    x: float = Field(ge=0, le=1, default=0.5)
    y: float = Field(ge=0, le=1, default=0.5)


class Extra(BaseModel):
    """Something in the home the hub does not list, shown greyed so the plan stays honest about it."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    room: str
    x: float = Field(ge=0, le=1, default=0.5)
    y: float = Field(ge=0, le=1, default=0.5)
    note: str = "Not connected to the hub"


class Layout(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    floors: list[Floor] = Field(min_length=1)
    placements: dict[str, Placement] = Field(default_factory=dict)
    extras: list[Extra] = Field(default_factory=list)

    @model_validator(mode="after")
    def rooms_fit_and_are_known(self) -> "Layout":
        rooms: set[str] = set()
        for floor in self.floors:
            for room in floor.rooms:
                if room.id in rooms:
                    raise ValueError(f"two rooms have the id {room.id!r}")
                rooms.add(room.id)
                if room.x + room.w > floor.width + 1e-6 or room.y + room.d > floor.depth + 1e-6:
                    raise ValueError(f"room {room.id!r} reaches outside {floor.name}")
        for what, places in (("placement", self.placements.items()), ("extra", ((e.name, e) for e in self.extras))):
            for name, place in places:
                if place.room not in rooms:
                    raise ValueError(f"the {what} for {name!r} names a room that is not on the plan: {place.room!r}")
        return self


def _room(id: str, name: str, x: float, y: float, w: float, d: float, kind: str = "room",
          matches: tuple[str, ...] = ()) -> Room:
    return Room(id=id, name=name, x=x, y=y, w=w, d=d, kind=kind, matches=list(matches))


# The simulated demo home (porch_hub.seed): one floor, with a room for each one its devices name.
DEMO_LAYOUT = Layout(
    name="Demo home",
    floors=[Floor(id="ground", name="Ground floor", width=44, depth=28, rooms=[
        _room("entry", "Entry", 0, 0, 10, 10, "hall", ("Entryway",)),
        _room("living", "Living room", 10, 0, 20, 16),
        _room("kitchen", "Kitchen", 30, 0, 14, 16, "kitchen"),
        _room("office", "Office", 0, 10, 10, 18),
        _room("bedroom", "Bedroom", 10, 16, 14, 12),
        _room("laundry", "Laundry", 24, 16, 10, 12, "bath"),
        _room("utility", "Utility", 34, 16, 10, 12, "store"),
    ], fixtures=[
        Fixture(kind="door", x=3, y=0, w=4, d=0),
        Fixture(kind="counter", x=31, y=0.6, w=12, d=2, label="Sink"),
        Fixture(kind="counter", x=1, y=11, w=5, d=2.5, label="Desk"),
    ])],
    # The simulated devices (porch_hub.seed), each where it would stand in this home. The lock is
    # on the front door, the leak sensor on the floor by the sink, the valve on the utility wall.
    placements={
        "home-1/front-door-lock": Placement(room="entry", x=0.5, y=0.0),
        "home-1/robot-vacuum": Placement(room="living", x=0.25, y=0.07),
        "home-1/space-heater-plug": Placement(room="living", x=0.72, y=0.08),
        "home-1/kitchen-leak-sensor": Placement(room="kitchen", x=0.3, y=0.26),
        "home-1/desk-lamp-plug": Placement(room="office", x=0.82, y=0.1),
        "home-1/air-purifier": Placement(room="bedroom", x=0.15, y=0.25),
        "home-1/laundry-dryer": Placement(room="laundry", x=0.5, y=0.14),
        "home-1/main-water-valve": Placement(room="utility", x=0.85, y=0.5),
    },
)


def layout_file(env: Mapping[str, str] = os.environ) -> Path | None:
    value = env.get("PORCH_HOME_LAYOUT")
    return Path(value).expanduser() if value else None


def load_layout(path: Path | None) -> dict:
    """The plan to draw, read now, so an edit to the file shows on the next page load.

    A file that is missing or does not describe a plan is not an error for the pages: they get
    the demo plan and a "problem" saying why, and every device still shows beside it.
    """
    if path is None:
        return {**DEMO_LAYOUT.model_dump(), "source": "demo", "problem": None}
    try:
        layout = Layout.model_validate(json.loads(path.read_text()))
    except FileNotFoundError:
        problem = f"The home layout file {path.name} does not exist yet."
    except (OSError, ValueError, ValidationError) as exc:
        first = exc.errors()[0] if isinstance(exc, ValidationError) else None
        detail = (f"{'.'.join(map(str, first['loc']))}: {first['msg']}" if first else str(exc)).strip(": ")
        problem = f"The home layout file {path.name} cannot be used ({detail})."
        log.warning("%s", problem)
    else:
        return {**layout.model_dump(), "source": "file", "problem": None}
    return {**DEMO_LAYOUT.model_dump(), "source": "demo", "problem": problem}
