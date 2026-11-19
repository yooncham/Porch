"""What Google's Home MCP server says about a home, read and checked.

It has three read tools (list_homes, list_home_resources, list_home_states) and one action tool
(run_home_actions). Every answer is checked for shape before it is used. Nothing Google sends
back in an error (its text, ids, raw records) goes into a message the hub shows: a failure is
described by its type and the tool that failed. The raw answer is kept under
"private_diagnostics" in the report, for the owner to look at on this computer.

Lights can be switched, and only with On and Off. Plugs are listed and read, and switched only
when the owner's binding says so ("switchable": true): a plug says nothing about what it powers.
A TV is listed and read (what it is playing, and whether it is in use), and takes Play and Pause.
"""

import asyncio
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ...models import DeviceType, utcnow
from .session import ACTION_TOOL, INVENTORY_TOOLS, READ_TOOLS

LIGHT_TYPE = "GoogleLightDevice"
PLUG_TYPE = "GooglePluginUnitDevice"
# What Google calls a Google TV Streamer. The TV's own traits belong to a part of the device,
# TV_PART, and their names carry it: "GoogleTvDevice/MediaPlayback".
TV_TYPE = "VideoRemoteControlDevice"
TV_PART = "GoogleTvDevice"
TV_PLAYBACK_TRAIT = f"{TV_PART}/MediaPlayback"
# Google's resource types the hub lists. Any other type is left out, and discovery says so.
HUB_TYPES = {LIGHT_TYPE: DeviceType.ON_OFF_LIGHT, PLUG_TYPE: DeviceType.ON_OFF_PLUG,
             TV_TYPE: DeviceType.CASTING_VIDEO_PLAYER}
# Hub command -> Google Home command, for a light and for a TV.
GOOGLE_COMMANDS = {"On": "OnOff.On", "Off": "OnOff.Off"}
TV_COMMANDS = {"Play": "MediaPlayback.Play", "Pause": "MediaPlayback.Pause"}
# The traits read for every listed device. Google answers with the ones each device has.
STATE_TRAITS = ["OnOff", "Connectivity", "MediaPlayback", "MediaActivityState", "LevelControl", "ColorControl"]
# Google's state names -> the hub's (Matter's Media Playback names, and the TV's activity).
PLAYBACK_STATES = {"PLAYBACK_STATE_ENUM_PLAYING": "Playing", "PLAYBACK_STATE_ENUM_PAUSED": "Paused",
                   "PLAYBACK_STATE_ENUM_NOT_PLAYING": "NotPlaying", "PLAYBACK_STATE_ENUM_BUFFERING": "Buffering"}
ACTIVITY_STATES = {"ACTIVITY_STATE_ACTIVE": "Active", "ACTIVITY_STATE_STANDBY": "Standby",
                   "ACTIVITY_STATE_INACTIVE": "Inactive"}
MAX_PAGES = 20
PREMIUM_REQUIRED_MESSAGE = "At least one Home Premium Advanced subscription is required."
COMPLETE = frozenset({"inventory_read", "empty_inventory", "no_homes"})


class InvalidResponse(ValueError):
    """Google's answer does not have the shape the tool's schema promises."""


class RemoteToolError(RuntimeError):
    def __init__(self, response: dict[str, Any]) -> None:
        super().__init__("Google Home returned a tool error")
        self.response = response
        self.operation: str | None = None


class MissingReadTool(RuntimeError):
    """The account's session does not offer the tools that list a home."""


class PaginationError(RuntimeError):
    """A page token repeated, or the pages never ended."""


class PreflightError(ValueError):
    """Why a device must not be sent a command, found before sending anything."""


class ActionRefused(RuntimeError):
    """Google passed the action on and the device answered that it did not carry it out.
    Its text is the device's status as Google names it, a constant and never free text."""


def safe_error(exc: BaseException) -> dict[str, Any]:
    """An error as its type and failed tool. Never Google's message, arguments, URLs or credentials."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    result: dict[str, Any] = {"error_type": type(exc).__name__}
    if isinstance(exc, RemoteToolError) and exc.operation in READ_TOOLS | {ACTION_TOOL}:
        result["failed_operation"] = exc.operation
        content = exc.response.get("content", [])
        if isinstance(content, list) and any(
                isinstance(item, dict) and item.get("type") == "text"
                and item.get("text") == PREMIUM_REQUIRED_MESSAGE for item in content):
            result["error_reason"] = "premium_advanced_required"
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if type(status) is int and 400 <= status <= 599:
        result["http_status"] = status
    return result


def failure_text(error: dict[str, Any]) -> str:
    """safe_error's fields in words, for a log line or an error the hub returns."""
    text = error.get("error_type", "unknown error")
    if operation := error.get("failed_operation"):
        text += f" in {operation}"
    if error.get("error_reason") == "premium_advanced_required":
        text += ": Google requires a Google Home Premium Advanced subscription for this account"
    elif status := error.get("http_status"):
        text += f", HTTP {status}"
    return text


def _retain_private_error(report: dict[str, Any], exc: BaseException) -> None:
    if isinstance(exc, RemoteToolError):
        report.setdefault("private_diagnostics", []).append({"operation": exc.operation, "response": exc.response})


def tool_payload(result: Any) -> dict[str, Any]:
    """The JSON object a tool call returned. Raises RemoteToolError when Google flagged an error."""
    if not isinstance(result, dict):
        raise InvalidResponse()
    if result.get("isError", result.get("is_error", False)):
        raise RemoteToolError(result)
    structured = result.get("structuredContent", result.get("structured_content"))
    if structured is not None:
        if not isinstance(structured, dict):
            raise InvalidResponse()
        return structured
    payloads = []
    for item in result.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                payloads.append(json.loads(item["text"]))
            except (KeyError, TypeError, ValueError):
                raise InvalidResponse() from None
    if len(payloads) != 1 or not isinstance(payloads[0], dict):
        raise InvalidResponse()
    return payloads[0]


def records(payload: dict[str, Any], key: str, allowed_keys: set[str]) -> list[dict[str, Any]]:
    """payload[key] as a list of objects.

    Protobuf JSON may leave an empty list out, so a missing key is an empty list. An object with
    other, unexpected keys is not: an error object must never pass as an empty home.
    """
    if key not in payload and set(payload) - allowed_keys:
        raise InvalidResponse()
    values = payload.get(key, [])
    if not isinstance(values, list) or any(not isinstance(item, dict) for item in values):
        raise InvalidResponse()
    return values


async def read_tool(client: Any, name: str, arguments: dict[str, Any], deadline_s: float) -> dict[str, Any]:
    if name not in READ_TOOLS:
        raise ValueError("only the read tools may be called here")
    result = await asyncio.wait_for(client.call_tool(name, arguments), deadline_s)
    try:
        return tool_payload(result)
    except RemoteToolError as exc:
        exc.operation = name
        raise


async def read_inventory(client: Any, *, request_timeout: float = 30,
                         authorization_timeout: float | None = None) -> dict[str, Any]:
    """Every home, device and command description the signed-in account can see. Reads only.

    Returns a report whose "status" says how far it got: inventory_read, empty_inventory,
    no_homes, partial (a home could not be read to the end) or connection_failed. It does not
    raise for Google's failures; they are in the report as safe_error fields.
    """
    report: dict[str, Any] = {"observed_at": utcnow().isoformat(), "status": "connection_failed",
                              "tools": [], "homes": []}
    try:
        cursor = None
        seen_cursors: set[str] = set()
        for page_number in range(MAX_PAGES):
            # Google can accept the handshake before asking for sign-in on the first tool listing,
            # so only that request gets the long window an owner needs to sign in.
            deadline = (authorization_timeout if page_number == 0 and authorization_timeout is not None
                        else request_timeout)
            page = await asyncio.wait_for(client.list_tools(cursor), deadline)
            report["tools"].extend(records(page, "tools", {"tools", "nextCursor"}))
            cursor = page.get("nextCursor")
            if cursor is not None and not isinstance(cursor, str):
                raise InvalidResponse()
            if not cursor:
                break
            if cursor in seen_cursors:
                raise PaginationError()
            seen_cursors.add(cursor)
        else:
            raise PaginationError()
        if not INVENTORY_TOOLS.issubset({tool.get("name") for tool in report["tools"]}):
            raise MissingReadTool()

        structures = records(await read_tool(client, "list_homes", {}, request_timeout), "structures", {"structures"})
        for structure in structures:
            if not isinstance(structure.get("structureId"), str) or not structure["structureId"]:
                raise InvalidResponse()
        if not structures:
            report["status"] = "no_homes"
            return report

        for structure in structures:
            home: dict[str, Any] = {"structure": structure, "pages": [], "inventory_complete": False}
            report["homes"].append(home)
            try:
                token = None
                seen_tokens: set[str] = set()
                for _ in range(MAX_PAGES):
                    args = {"structureId": structure["structureId"], "view": "VIEW_FULL", "pageSize": 100}
                    if token:
                        args["pageToken"] = token
                    page = await read_tool(client, "list_home_resources", args, request_timeout)
                    resources = records(page, "resources", {"resources", "resourceMetadata", "nextPageToken"})
                    if any(not isinstance(item.get("id"), str) or not item["id"] for item in resources):
                        raise InvalidResponse()
                    home["pages"].append(page)
                    token = page.get("nextPageToken")
                    if token is not None and not isinstance(token, str):
                        raise InvalidResponse()
                    if not token:
                        home["inventory_complete"] = True
                        break
                    if token in seen_tokens:
                        raise PaginationError()
                    seen_tokens.add(token)
                else:
                    raise PaginationError()
            except Exception as exc:
                home["error"] = safe_error(exc)
                _retain_private_error(report, exc)
        if any(not home["inventory_complete"] for home in report["homes"]):
            report["status"] = "partial"
        elif any(page.get("resources") for home in report["homes"] for page in home["pages"]):
            report["status"] = "inventory_read"
        else:
            report["status"] = "empty_inventory"
    except Exception as exc:
        report.update(safe_error(exc))
        _retain_private_error(report, exc)
    return report


def inventory_failure(report: dict[str, Any]) -> str | None:
    """Why a report cannot be used as the home's device list, or None when it is complete."""
    if report.get("status") in COMPLETE:
        return None
    errors = [report] if "error_type" in report else [h["error"] for h in report.get("homes", []) if "error" in h]
    detail = "; ".join(dict.fromkeys(failure_text(e) for e in errors)) or report.get("status", "unknown")
    return f"Google Home's device list could not be read ({detail})"


# One device

def validate_onoff_command(device: dict[str, Any], metadata: dict[str, Any] | None, command: str,
                           kind: str = LIGHT_TYPE) -> None:
    """Raise PreflightError unless Google's current description lets this light (or plug) take this
    command. The command must be OnOff.On or OnOff.Off, described for that kind, with no parameters
    and at Google's lowest sensitivity level."""
    noun = "plug" if kind == PLUG_TYPE else "light"
    if device.get("type") != kind or not str(device.get("id", "")).startswith("device@"):
        raise PreflightError("only lights, and plugs the owner allows, can be switched through Google Home")
    if not isinstance(metadata, dict) or metadata.get("type") != kind:
        raise PreflightError(f"Google Home describes no commands for this {noun}")
    trait = metadata.get("traits", {}).get("OnOff", {})
    definition = trait.get("commands", {}).get(command)
    if (command not in GOOGLE_COMMANDS.values()
            or "OnOff" not in device.get("supportedTraits", [])
            or kind not in trait.get("resourceTypes", [])
            or not isinstance(definition, dict)
            or definition.get("sensitivityLevel") != "SENSITIVITY_LEVEL_L1"
            or definition.get("parameters")):
        raise PreflightError(f"Google Home's description of this {noun} does not allow a plain On or Off")


def validate_light_command(device: dict[str, Any], metadata: dict[str, Any] | None, command: str) -> None:
    validate_onoff_command(device, metadata, command, LIGHT_TYPE)


def validate_tv_command(device: dict[str, Any], metadata: dict[str, Any] | None, command: str) -> None:
    """Raise PreflightError unless Google's current description lets this TV take this command.

    The command must be MediaPlayback.Play or MediaPlayback.Pause, described for the TV's own
    part, with no parameters and at Google's lowest sensitivity level.
    """
    if device.get("type") != TV_TYPE or not str(device.get("id", "")).startswith("device@"):
        raise PreflightError("only a TV can be played or paused through Google Home")
    if not isinstance(metadata, dict) or metadata.get("type") != TV_TYPE:
        raise PreflightError("Google Home describes no commands for this TV")
    trait = metadata.get("traits", {}).get(TV_PLAYBACK_TRAIT, {})
    definition = trait.get("commands", {}).get(command)
    if (command not in TV_COMMANDS.values()
            or TV_PLAYBACK_TRAIT not in device.get("supportedTraits", [])
            or TV_PART not in trait.get("resourceTypes", [])
            or not isinstance(definition, dict)
            or definition.get("sensitivityLevel") != "SENSITIVITY_LEVEL_L1"
            or definition.get("parameters")):
        raise PreflightError("Google Home's description of this TV does not allow a plain Play or Pause")


def validate_command(device: dict[str, Any], metadata: dict[str, Any] | None, command: str,
                     switchable_plug: bool = False) -> None:
    """Raise PreflightError unless Google's current description lets this device take this command.
    A plug takes one only when the owner has said it may be switched (switchable_plug)."""
    kind = device.get("type")
    if kind == TV_TYPE:
        validate_tv_command(device, metadata, command)
    elif kind == PLUG_TYPE:
        if not switchable_plug:
            raise PreflightError("this plug is read only through Google Home: the owner has not said it may be switched")
        validate_onoff_command(device, metadata, command, PLUG_TYPE)
    else:
        validate_light_command(device, metadata, command)


def answer_shape(value: Any, depth: int = 0) -> Any:
    """A tool answer as its field names and value types only. It carries none of Google's
    values (its text, ids, states), so it can go in a log line."""
    if depth >= 6:
        return "..."
    if isinstance(value, dict):
        return {("<id>" if "@" in str(key) else str(key)[:40]): answer_shape(item, depth + 1)
                for key, item in list(value.items())[:16]}
    if isinstance(value, list):
        return [answer_shape(item, depth + 1) for item in value[:3]]
    return type(value).__name__


def validate_action_result(result: Any, google_id: str) -> None:
    """Raise unless Google acknowledged exactly this device's action, without an error."""
    try:
        payload = tool_payload(result)
    except RemoteToolError as exc:
        exc.operation = ACTION_TOOL
        raise
    responses = records(payload, "homeActionResponses", {"homeActionResponses"})
    if (len(responses) != 1 or responses[0].get("id") != google_id or "error" in responses[0]
            or not isinstance(responses[0].get("response"), dict)):
        raise InvalidResponse()
    # Some commands answer with the device's own status (a TV's Pause: STATUS_ENUM_SUCCESS). Any
    # other status is the device saying no. Success here is still not proof: the hub reads back.
    status = responses[0]["response"].get("status")
    if isinstance(status, str) and not status.endswith("SUCCESS"):
        raise ActionRefused(status if status.isascii() and status.replace("_", "").isalnum() else "a status")


async def read_home_states(client: Any, structure_id: str, google_ids: list[str], deadline_s: float,
                           traits: list[str] | None = STATE_TRAITS) -> dict[str, dict]:
    """Each device's state record, by Google id. Google decides how fresh it is (DEFAULT freshness):
    it may ask the device's maker now or answer from its own copy. traits=None: every trait."""
    arguments: dict[str, Any] = {"structureId": structure_id, "resourceSearch": {"resourceIds": google_ids},
                                 "freshness": "DEFAULT", "showSynonyms": False}
    if traits is not None:
        arguments["filter"] = {"shortenedResourceTraits": traits}
    payload = await read_tool(client, "list_home_states", arguments, deadline_s)
    found: dict[str, dict] = {}
    for state in records(payload, "states", {"states"}):
        google_id = state.get("id")
        if google_id not in google_ids or google_id in found:
            raise InvalidResponse()
        found[google_id] = state
    return found


@dataclass
class Endpoint:
    """One Google Home device, as its inventory describes it."""

    structure_id: str
    resource: dict[str, Any]
    metadata: dict[str, Any] | None  # Google's description of the commands this device takes
    room: str | None  # Google's room, when the device is in exactly one

    @property
    def google_id(self) -> str:
        return self.resource["id"]

    @property
    def name(self) -> str:
        name = self.resource.get("displayName")
        return name if isinstance(name, str) and name else "Unnamed Google Home device"

    @property
    def google_type(self) -> str:
        return str(self.resource.get("type", ""))

    @property
    def device_type(self) -> DeviceType | None:
        """The hub's type for this device, or None when the hub does not list this kind yet."""
        return HUB_TYPES.get(self.google_type)

    @property
    def action_type(self) -> str:
        """The type an action names: the one Google describes the command's trait for. A light's
        OnOff belongs to the light itself; a TV's MediaPlayback belongs to a part of the TV."""
        return TV_PART if self.google_type == TV_TYPE else self.google_type

    def google_command(self, hub_command: str) -> str | None:
        """Google's name for a hub command on this kind of device, or None when this kind never takes it."""
        table = (TV_COMMANDS if self.google_type == TV_TYPE else
                 GOOGLE_COMMANDS if self.google_type in (LIGHT_TYPE, PLUG_TYPE) else {})
        return table.get(hub_command)

    def commands(self, switchable_plug: bool = False) -> list[str]:
        """The hub commands Google's description allows right now: On and Off for a light, and for a
        plug the owner lets be switched; Play and Pause for a TV."""
        allowed = []
        for hub_command in (*GOOGLE_COMMANDS, *TV_COMMANDS):
            google_command = self.google_command(hub_command)
            if google_command is None:
                continue
            try:
                validate_command(self.resource, self.metadata, google_command, switchable_plug)
            except (PreflightError, TypeError, AttributeError):  # a malformed description allows nothing
                continue
            allowed.append(hub_command)
        return allowed


def inventory_endpoints(report: dict[str, Any]) -> dict[str, Endpoint]:
    """The devices in a complete inventory report, by Google id. Rooms and device parts are left out."""
    if (problem := inventory_failure(report)) is not None:
        raise InvalidResponse(problem)
    endpoints: dict[str, Endpoint] = {}
    for home in report["homes"]:
        resources: dict[str, dict] = {}
        metadata: dict[str, dict] = {}
        for page in home["pages"]:
            for item in page.get("resources", []):
                if resources.setdefault(item["id"], item) != item:
                    raise InvalidResponse("Google Home listed one device twice with different details")
            for item in page.get("resourceMetadata", []):
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    if metadata.setdefault(item["id"], item) != item:
                        raise InvalidResponse("Google Home described one device's commands twice, differently")
        for item in resources.values():
            if not item["id"].startswith("device@") or item.get("isPart", False):
                continue
            rooms = {resources[parent]["displayName"] for parent in item.get("parentIds", [])
                     if parent in resources and resources[parent].get("type") == "Room"
                     and isinstance(resources[parent].get("displayName"), str)}
            if item["id"] in endpoints:
                raise InvalidResponse("the same Google Home device appears in two homes")
            endpoints[item["id"]] = Endpoint(home["structure"]["structureId"], deepcopy(item),
                                            deepcopy(metadata.get(item.get("metadataId"))),
                                            next(iter(rooms)) if len(rooms) == 1 else None)
    return endpoints


@dataclass(frozen=True)
class Reading:
    """What one state record says about a device. A value is None unless an online device
    reports it: an unknown state is never passed on as off, or as not playing."""

    online: bool = False
    power: bool | None = None  # a light or a plug
    playback: str | None = None  # a TV: Playing, Paused, NotPlaying or Buffering
    activity: str | None = None  # a TV: Active, Standby or Inactive
    level: int | None = None  # a light: its brightness as Matter's CurrentLevel, 1 to 254
    mireds: int | None = None  # a light set to a white tone: its colour temperature in mireds


def _reported(state: dict[str, Any], trait_name: str, owner: str | None = None) -> dict[str, Any]:
    """One trait's attributes from a state record, or {} when the device does not report them now.
    owner is the part of the device the trait must belong to, when that matters."""
    traits = state.get("traits")
    trait = traits.get(trait_name) if isinstance(traits, dict) else None
    if (not isinstance(trait, dict) or trait.get("connectivityState") != "ONLINE"
            or trait.get("stateReportingOmitted") or (owner is not None and trait.get("type") != owner)):
        return {}
    attributes = trait.get("state")
    return attributes if isinstance(attributes, dict) else {}


def read_state_record(endpoint: Endpoint, state: dict[str, Any] | None) -> Reading:
    """One device's state record as a Reading. A record of another kind of device is not trusted."""
    state = state or {}
    if state.get("type") != endpoint.google_type or state.get("resourceConnectivityState") != "ONLINE":
        return Reading()
    if endpoint.google_type == TV_TYPE:
        playing = _reported(state, "MediaPlayback", TV_PART).get("currentState")
        activity = _reported(state, "MediaActivityState", TV_PART).get("activityState")
        return Reading(online=True,
                       playback=PLAYBACK_STATES.get(playing) if isinstance(playing, str) else None,
                       activity=ACTIVITY_STATES.get(activity) if isinstance(activity, str) else None)
    power = _reported(state, "OnOff").get("onOff")
    level = mireds = None
    if endpoint.google_type == LIGHT_TYPE:
        level = _reported(state, "LevelControl").get("currentLevel")
        color = _reported(state, "ColorControl")
        # The white tone counts only while the light is set to one; in a colour mode it is stale.
        if str(color.get("colorMode", "")).endswith("COLOR_TEMPERATURE_MIREDS"):
            mireds = color.get("colorTemperatureMireds")
    return Reading(online=True, power=power if type(power) is bool else None,
                   level=level if type(level) is int and 1 <= level <= 254 else None,
                   mireds=mireds if type(mireds) is int and mireds > 0 else None)
