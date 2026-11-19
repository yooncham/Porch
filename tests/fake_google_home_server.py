"""A stand-in for Google's Home MCP server: the answer shapes, and a real MCP server over stdio.

The shapes are the ones seen from a real home (Tapo bulbs and plugs, a Google TV Streamer) and
the ones Google documents for list_home_states and run_home_actions. test_google_home_mcp.py
uses them in memory. The home has a light, a plug and a display, a kind the hub does not list;
the TV is added by the tests that are about it (tv_resource, tv_metadata, tv_state).

Run as a program, it is what the driver's sign-in bridge would be, for the test that goes through
the real MCP SDK:

    python fake_google_home_server.py <state file> [the bridge's own arguments ...]

It honours --ignore-tool as mcp-remote does. The lights' state lives in the state file, so it
carries over from one session's process to the next, and every call is recorded there.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

LIGHT, PLUG, TV = "device@private-light", "device@private-plug", "device@private-tv"
DISPLAY = "device@private-display"
STRUCTURE = "private-home"
TV_TYPE, TV_PART = "VideoRemoteControlDevice", "GoogleTvDevice"


def commands_for(kind: str) -> dict[str, Any]:
    return {"id": f"meta-{kind}", "type": kind, "traits": {"OnOff": {
        "resourceTypes": [kind],
        "commands": {"OnOff.On": {"sensitivityLevel": "SENSITIVITY_LEVEL_L1"},
                     "OnOff.Off": {"sensitivityLevel": "SENSITIVITY_LEVEL_L1"}}}}}


def state_of(google_id: str, kind: str, on: bool = False, online: bool = True,
             level: int | None = None, mireds: int | None = None) -> dict[str, Any]:
    """level and mireds: a light's brightness (1 to 254) and white tone, as a real bulb reports them."""
    link = "ONLINE" if online else "OFFLINE"
    traits: dict[str, Any] = {"OnOff": {"connectivityState": link, "state": {"onOff": on},
                                        "timestamp": "2026-11-14T23:34:00Z"}}
    if level is not None:
        traits["LevelControl"] = {"connectivityState": link, "type": kind, "state": {"currentLevel": level}}
    if mireds is not None:
        traits["ColorControl"] = {"connectivityState": link, "type": kind, "state": {
            "colorMode": "COLOR_MODE_ENUM_COLOR_TEMPERATURE_MIREDS", "colorTemperatureMireds": mireds,
            "currentHue": 0, "currentSaturation": 0}}
    return {"id": google_id, "type": kind, "resourceConnectivityState": link, "timestamp": "2026-11-14T23:34:00Z",
            "traits": traits}


def home_resources() -> list[dict[str, Any]]:
    return [
        {"id": "room@living", "type": "Room", "displayName": "Living Room"},
        {"id": LIGHT, "displayName": "Living stand", "type": "GoogleLightDevice",
         "metadataId": "meta-GoogleLightDevice", "supportedTraits": ["OnOff"], "parentIds": ["room@living"]},
        {"id": PLUG, "displayName": "Plug1", "type": "GooglePluginUnitDevice",
         "metadataId": "meta-GooglePluginUnitDevice", "supportedTraits": ["OnOff"], "parentIds": ["room@living"]},
        {"id": DISPLAY, "displayName": "Kitchen display", "type": "GoogleDisplayDevice"},
    ]


def tv_resource() -> dict[str, Any]:
    """A Google TV Streamer. Its own traits belong to a part of it, and carry that part's name."""
    return {"id": TV, "displayName": "Living Room TV", "type": TV_TYPE, "metadataId": f"meta-{TV_TYPE}",
            "parentIds": ["room@living"], "resourceInfo": {"deviceInfo": {"productName": "Google TV Streamer"}},
            "supportedTraits": [f"{TV_PART}/MediaActivityState", f"{TV_PART}/MediaPlayback", f"{TV_PART}/OnOff",
                                "SpeakerDevice/Volume", "RootNodeDevice/BasicInformation", "SpeakerDevice/Connectivity"]}


def tv_metadata() -> dict[str, Any]:
    plain = {"sensitivityLevel": "SENSITIVITY_LEVEL_L1"}
    return {"id": f"meta-{TV_TYPE}", "type": TV_TYPE, "traits": {
        f"{TV_PART}/MediaPlayback": {"experimental": True, "resourceTypes": [TV_PART], "commands": {
            "MediaPlayback.Play": dict(plain), "MediaPlayback.Pause": dict(plain), "MediaPlayback.Stop": dict(plain),
            "MediaPlayback.Seek": {**plain, "parameters": {"position": {"required": True}}}}},
        f"{TV_PART}/OnOff": {"experimental": True, "resourceTypes": [TV_PART], "commands": {
            "OnOff.On": dict(plain), "OnOff.Off": dict(plain), "OnOff.Toggle": dict(plain)}},
        f"{TV_PART}/MediaActivityState": {"experimental": True, "resourceTypes": [TV_PART]},
    }}


def tv_state(playback: str = "PLAYING", activity: str | None = "ACTIVE", online: bool = True) -> dict[str, Any]:
    """What the TV reports: what it is playing and whether it is in use. It takes On and Off but
    does not report whether it is on, so its OnOff state has no onOff. A TV nobody is using
    (activity None, as seen with nothing playing) reports no activity at all."""
    link = "ONLINE" if online else "OFFLINE"
    in_use = ({"activityState": f"ACTIVITY_STATE_{activity}", "attributeList": [3, 65531, 65532]}
              if activity else {"attributeList": [65531, 65532]})
    return {"id": TV, "type": TV_TYPE, "resourceConnectivityState": link, "timestamp": "2026-11-16T00:38:08Z",
            "traits": {
                "Connectivity": {"state": {"connectivity": link}, "type": TV_TYPE},
                "MediaActivityState": {"connectivityState": link, "type": TV_PART,
                                       "state": {**in_use, "featureMap": 0}},
                "MediaPlayback": {"connectivityState": link, "type": TV_PART, "state": {
                    "acceptedCommandList": [0, 1, 2, 4, 5, 8, 9, 11], "attributeList": [0, 65529, 65531, 65532],
                    "currentState": f"PLAYBACK_STATE_ENUM_{playback}", "featureMap": 1}},
                "OnOff": {"connectivityState": link, "type": TV_PART, "state": {
                    "acceptedCommandList": [0, 1, 2], "attributeList": [65529, 65531, 65532], "featureMap": 0}},
            }}


def home_metadata() -> list[dict[str, Any]]:
    return [commands_for("GoogleLightDevice"), commands_for("GooglePluginUnitDevice")]


KINDS = {LIGHT: "GoogleLightDevice", PLUG: "GooglePluginUnitDevice"}


def main() -> None:
    from mcp.server.mcpserver import MCPServer

    state_file = Path(sys.argv[1])
    ignored = {sys.argv[i + 1] for i, arg in enumerate(sys.argv[:-1]) if arg == "--ignore-tool"}

    def record(tool: str, change: dict[str, bool] | None = None) -> dict[str, Any]:
        data = json.loads(state_file.read_text())
        data["calls"].append({"pid": os.getpid(), "tool": tool, "ignored": sorted(ignored)})
        data["on"].update(change or {})
        state_file.write_text(json.dumps(data))
        return data

    server = MCPServer("fake-google-home")

    async def list_homes() -> dict[str, Any]:
        record("list_homes")
        return {"structures": [{"structureId": STRUCTURE}]}

    async def list_home_resources(structureId: str, view: str = "", pageSize: int = 100,
                                  pageToken: str | None = None) -> dict[str, Any]:
        record("list_home_resources")
        return {"resources": home_resources(), "resourceMetadata": home_metadata()}

    async def list_home_states(structureId: str, resourceSearch: dict[str, Any], filter: dict[str, Any] | None = None,
                               freshness: str = "", showSynonyms: bool = False) -> dict[str, Any]:
        on = record("list_home_states")["on"]
        return {"states": [state_of(i, KINDS[i], on[i]) for i in resourceSearch["resourceIds"] if i in KINDS]}

    async def run_home_actions(structureId: str, homeActionRequests: list[dict[str, Any]]) -> dict[str, Any]:
        request = homeActionRequests[0]
        record("run_home_actions", {request["id"]: request["command"] == "OnOff.On"})
        return {"homeActionResponses": [{"id": request["id"], "response": {}}]}

    async def save_automation(structureId: str = "") -> dict[str, Any]:
        record("save_automation")
        return {}

    for tool in (list_homes, list_home_resources, list_home_states, run_home_actions, save_automation):
        if tool.__name__ not in ignored:
            server.tool()(tool)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
