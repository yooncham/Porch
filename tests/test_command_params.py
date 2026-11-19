"""porch_gateway/command_params.py: what each device's commands take, in describe_home, from what the
device itself says. The improvement from the cold-start test's run-02 (usability/runs/run-02/notes.md):
Porch had promised every appliance minutes in a delayed start, and the dryer takes whole hours only.
"""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("command_params", ROOT / "porch_gateway" / "command_params.py")
command_params = importlib.util.module_from_spec(spec)
spec.loader.exec_module(command_params)


def laundry(minutes_settable):
    attributes = {"RemoteStartAllowed": True, "RelativeHourToStart": 0, "RelativeMinuteToStart": 0}
    if minutes_settable is not None:
        attributes["DelayMinutesSettable"] = minutes_settable
    return {"clusters": {"OperationalState": {"attributes": {"OperationalState": "Stopped"}, "commands": ["Start", "Stop"]},
                         "PorchLaundryExtension": {"attributes": attributes, "commands": ["SetDelayStart"]}}}


def test_an_appliance_that_takes_whole_hours_is_not_offered_minutes():
    params = command_params.for_device(laundry(False))["SetDelayStart"]
    assert "minutes" not in params and "whole hours only" in params["hours"] and params["hours"].startswith("0-12")


def test_an_appliance_that_takes_minutes_or_a_hub_too_old_to_say_is_offered_them():
    offered = {"SetDelayStart": {"hours": "0-12", "minutes": "0-59 (optional)"}}
    assert command_params.for_device(laundry(True)) == offered
    assert command_params.for_device(laundry(None)) == offered  # the dry run still checks them


def test_a_device_whose_commands_take_nothing_lists_nothing():
    lamp = {"clusters": {"OnOff": {"attributes": {"OnOff": True}, "commands": ["On", "Off"]}}}
    assert command_params.for_device(lamp) == {} and command_params.for_device({}) == {}


def test_the_home_wide_hint_no_longer_promises_minutes_and_the_gateway_uses_each_devices_own():
    assert "only where the device's own command_params lists minutes" in command_params.GENERAL["SetDelayStart"]["minutes"]
    gateway = (ROOT / "porch_gateway" / "porch_gateway.py").read_text()
    assert "command_params.for_device(d)" in gateway and '"command_params": command_params.GENERAL' in gateway
    assert '"minutes": "0-59 (optional)"}}' not in gateway


def test_the_documentation_says_the_same():
    agents = " ".join((ROOT / "docs" / "AGENTS.md").read_text().split())  # as read, not as wrapped
    assert "whole hours only" in agents and "own `command_params`" in agents
