"""What each device's commands take, for describe_home: per device, from what the device itself says.

Until the HW9 cold-start test, describe_home gave one hint for every appliance: SetDelayStart takes
hours and minutes. In run-02 a fresh agent believed it, and a dryer whose LG profile takes whole
hours only refused 11 hours 45 minutes. The hub now reports, per laundry appliance,
PorchLaundryExtension.DelayMinutesSettable, and this module passes it on to the agent.

Pure functions, tested without a running gateway (tests/test_command_params.py).
"""

DELAY = "SetDelayStart"
HOURS = "0-12"
MINUTES = "0-59 (optional)"
# For agents that read the home-wide hint: minutes only where a device's own command_params says so.
GENERAL = {DELAY: {"hours": HOURS, "minutes": "0-59, only where the device's own command_params lists minutes"}}


def for_device(device: dict) -> dict:
    """The parameters this device's commands take. Empty for a device whose commands take none."""
    clusters = device.get("clusters") or {}
    commands = {c for body in clusters.values() for c in body.get("commands", [])}
    if DELAY not in commands:
        return {}
    laundry = (clusters.get("PorchLaundryExtension") or {}).get("attributes") or {}
    if laundry.get("DelayMinutesSettable") is False:
        return {DELAY: {"hours": f"{HOURS}, whole hours only: this appliance does not take minutes"}}
    # True, or a hub too old to say: minutes are offered, and the dry run still checks them.
    return {DELAY: {"hours": HOURS, "minutes": MINUTES}}
