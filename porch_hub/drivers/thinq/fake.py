"""In-memory ThinQ Connect API and MQTT broker, speaking LG's wire format.

Backs the tests and the offline demo. It behaves like the real service where
the hub depends on it: it refuses start commands while Remote Start is off
(LG error 2301), sends partial DEVICE_STATUS reports after every change, and
applies accepted commands after apply_delay_s, as a cloud round trip would.
"""

import asyncio
import copy
from collections.abc import Callable
from typing import Any

from ...models import utcnow
from ..base import DeviceNotFound, DeviceOffline
from .driver import RemoteControlDisabled
from .profiles import remote_control_enabled


def _enum(*values: str, mode: tuple[str, ...] = ("r", "w")) -> dict[str, Any]:
    return {"type": "enum", "mode": list(mode), "value": {m: list(values) for m in mode}}


def _range(low: int, high: int, mode: tuple[str, ...] = ("r", "w")) -> dict[str, Any]:
    return {"type": "range", "mode": list(mode), "value": {m: {"min": low, "max": high, "step": 1} for m in mode}}


def _laundry_profile(operation_key: str) -> dict[str, Any]:
    return {
        "runState": {"currentState": _enum("POWER_OFF", "INITIAL", "RUNNING", "PAUSE", "END", "ERROR",
                                           "RESERVED", mode=("r",))},
        "operation": {operation_key: _enum("START", "STOP", "POWER_OFF", "WAKE_UP", mode=("w",))},
        "remoteControlEnable": {"remoteControlEnabled": {"type": "boolean", "mode": ["r"]}},
        # Delay start is set in whole hours on the SDK's laundry profiles.
        "timer": {"relativeHourToStart": _range(0, 19), "relativeMinuteToStart": _range(0, 59, mode=("r",))},
    }


def _laundry_status(remote_control_enabled: bool) -> dict[str, Any]:
    return {
        "runState": {"currentState": "POWER_OFF"},
        "remoteControlEnable": {"remoteControlEnabled": remote_control_enabled},
        "timer": {"relativeHourToStart": 0, "relativeMinuteToStart": 0},
    }


# What the machine reports after each operation mode is accepted.
_OPERATION_EFFECTS: dict[str, dict[str, dict[str, Any]]] = {
    "dryerOperationMode": {"START": {"runState": {"currentState": "RUNNING"}},
                           "STOP": {"runState": {"currentState": "INITIAL"}}},
    "washerOperationMode": {"START": {"runState": {"currentState": "RUNNING"}},
                            "STOP": {"runState": {"currentState": "INITIAL"}}},
    "cleanOperationMode": {"START": {"runState": {"currentState": "WORKING"}},
                           "PAUSE": {"runState": {"currentState": "PAUSE"}},
                           "HOMING": {"runState": {"currentState": "HOMING"}}},
    "airPurifierOperationMode": {mode: {"operation": {"airPurifierOperationMode": mode}}
                                 for mode in ("POWER_ON", "POWER_OFF")},
}


class FakeThinQCloud:
    def __init__(self, latency_s: float = 0.0, apply_delay_s: float = 0.0) -> None:
        self._appliances: dict[str, dict[str, Any]] = {}
        self._latency_s = latency_s
        self._apply_delay_s = apply_delay_s
        self._on_message: Callable[[dict[str, Any]], None] | None = None
        self._on_connection_change: Callable[[bool], None] | None = None
        self.controls: list[tuple[str, dict[str, Any]]] = []  # every control the hub sent

    # Appliances

    def add_dryer(self, lg_id: str, alias: str, remote_control_enabled: bool = True) -> None:
        self._add(lg_id, "DEVICE_DRYER", alias, {"property": _laundry_profile("dryerOperationMode")},
                  _laundry_status(remote_control_enabled))

    def add_washer(self, lg_id: str, alias: str, remote_control_enabled: bool = True) -> None:
        main = {"location": {"locationName": "MAIN"}}
        self._add(lg_id, "DEVICE_WASHER", alias,
                  {"property": [{**main, **_laundry_profile("washerOperationMode")}]},
                  _laundry_status(remote_control_enabled), location="MAIN")

    def add_robot_cleaner(self, lg_id: str, alias: str) -> None:
        self._add(lg_id, "DEVICE_ROBOT_CLEANER", alias,
                  {"property": {"runState": {"currentState": _enum("CHARGING", "WORKING", "PAUSE", "HOMING",
                                                                   "SLEEP", "STANDBY", "ERROR", mode=("r",))},
                                "operation": {"cleanOperationMode": _enum("START", "PAUSE", "HOMING", mode=("w",))},
                                "battery": {"percent": {"type": "number", "mode": ["r"]}}}},
                  {"runState": {"currentState": "CHARGING"}, "battery": {"percent": 100}})

    def add_air_purifier(self, lg_id: str, alias: str) -> None:
        self._add(lg_id, "DEVICE_AIR_PURIFIER", alias,
                  {"property": {"operation": {"airPurifierOperationMode": _enum("POWER_ON", "POWER_OFF")},
                                "airFlow": {"windStrength": _enum("LOW", "MID", "HIGH", "AUTO")},
                                "airQualitySensor": {"PM2": {"type": "number", "mode": ["r"]}}}},
                  {"operation": {"airPurifierOperationMode": "POWER_OFF"},
                   "airFlow": {"windStrength": "AUTO"},
                   "airQualitySensor": {"PM2": 8, "totalPollutionLevel": "GOOD"}})

    def appliances(self) -> list[tuple[str, str, str, dict[str, Any] | list[dict[str, Any]]]]:
        """(LG id, device type, alias, status in LG's wire format) for each appliance. No I/O."""
        return [(lg_id, a["info"]["deviceType"], a["info"]["alias"], self._wire(a, copy.deepcopy(a["status"])))
                for lg_id, a in self._appliances.items()]

    # Things that happen at the machine

    def machine_update(self, lg_id: str, report: dict[str, Any]) -> None:
        """Someone changed the appliance in person; LG pushes the change."""
        self._apply(self._appliances[lg_id], report)

    def set_remote_control(self, lg_id: str, enabled: bool) -> None:
        self.machine_update(lg_id, {"remoteControlEnable": {"remoteControlEnabled": enabled}})

    def set_online(self, lg_id: str, online: bool) -> None:
        self._appliances[lg_id]["online"] = online

    def set_unresponsive(self, lg_id: str, unresponsive: bool = True) -> None:
        """Accept commands but never act on them, like a machine with its door open."""
        self._appliances[lg_id]["unresponsive"] = unresponsive

    def drop_connection(self, connected: bool = False) -> None:
        if self._on_connection_change is not None:
            self._on_connection_change(connected)

    # ThinQCloud

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        self._on_message = None

    async def list_devices(self) -> list[dict[str, Any]]:
        await self._round_trip()
        return [{"deviceId": lg_id, "deviceInfo": copy.deepcopy(a["info"])} for lg_id, a in self._appliances.items()]

    async def get_profile(self, lg_id: str) -> dict[str, Any]:
        await self._round_trip()
        return copy.deepcopy(self._get(lg_id)["profile"])

    async def get_status(self, lg_id: str) -> dict[str, Any] | list[dict[str, Any]]:
        # LG answers with the status as it was when the request arrived; the answer then travels back.
        appliance = self._online(lg_id)
        response = self._wire(appliance, copy.deepcopy(appliance["status"]))
        await self._round_trip()
        return response

    async def control(self, lg_id: str, payload: dict[str, Any]) -> None:
        await self._round_trip()
        appliance = self._online(lg_id)
        self.controls.append((lg_id, copy.deepcopy(payload)))
        body = {k: v for k, v in payload.items() if k != "location"}
        operation = body.pop("operation", {})
        starting = "START" in operation.values() or "timer" in body
        if starting and remote_control_enabled(appliance["status"]) is False:
            raise RemoteControlDisabled()  # LG answers 2301 COMMAND_NOT_SUPPORTED_IN_REMOTE_OFF
        if appliance["unresponsive"]:
            return
        report = copy.deepcopy(body)
        for key, mode in operation.items():
            report.update(copy.deepcopy(_OPERATION_EFFECTS[key][mode]))
        if self._apply_delay_s:
            asyncio.get_running_loop().call_later(self._apply_delay_s, self._apply, appliance, report)
        else:
            self._apply(appliance, report)

    async def subscribe(
        self,
        lg_device_ids: list[str],
        on_message: Callable[[dict[str, Any]], None],
        on_connection_change: Callable[[bool], None],
    ) -> None:
        self._on_message = on_message
        self._on_connection_change = on_connection_change
        on_connection_change(True)

    # Internals

    def _add(self, lg_id: str, device_type: str, alias: str, profile: dict[str, Any],
             status: dict[str, Any], location: str | None = None) -> None:
        self._appliances[lg_id] = {
            "info": {"deviceType": device_type, "modelName": f"FAKE-{device_type[7:]}", "alias": alias,
                     "reportable": True},
            "profile": profile, "status": status, "location": location,
            "online": True, "unresponsive": False, "updated_at": utcnow(),
        }

    def _apply(self, appliance: dict[str, Any], report: dict[str, Any]) -> None:
        for resource, values in report.items():
            appliance["status"].setdefault(resource, {}).update(values)
        appliance["updated_at"] = utcnow()
        if self._on_message is not None:
            lg_id = next(k for k, v in self._appliances.items() if v is appliance)
            self._on_message({"pushType": "DEVICE_STATUS", "deviceId": lg_id,
                              "deviceType": appliance["info"]["deviceType"],
                              "report": self._wire(appliance, copy.deepcopy(report))})

    @staticmethod
    def _wire(appliance: dict[str, Any], data: dict[str, Any]) -> dict[str, Any] | list[dict[str, Any]]:
        """Multi-tub appliances report a list keyed by location."""
        if appliance["location"]:
            return [{"location": {"locationName": appliance["location"]}, **data}]
        return data

    def _get(self, lg_id: str) -> dict[str, Any]:
        try:
            return self._appliances[lg_id]
        except KeyError:
            raise DeviceNotFound(f"LG has no device {lg_id!r}") from None

    def _online(self, lg_id: str) -> dict[str, Any]:
        appliance = self._get(lg_id)
        if not appliance["online"]:
            raise DeviceOffline(f"LG appliance {lg_id} is not connected")
        return appliance

    async def _round_trip(self) -> None:
        if self._latency_s:
            await asyncio.sleep(self._latency_s)
