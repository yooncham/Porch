"""porch_gateway/household_rules.py: who an agent acts for, who must be asked, what an energy agent sees.

The room rule, in the household's own words: "my personal agents can control the devices, but if
my wife is at home in the living room and I am at school, my wife's permission is required."
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "porch_gateway" / "household_rules.py"
spec = importlib.util.spec_from_file_location("household_rules", SCRIPT)
rules = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rules)

DOT = {"id": "dot", "name": "Dot", "acts_for": "Yoonchan", "person": "yoonchan"}
HERS = {"id": "wife-agent", "name": "Her agent", "acts_for": "Wife", "person": "wife"}
ENERGY = {"id": "energy-agent", "name": "Energy agent", "acts_for": "the household", "reads": "energy"}


def people(me=("away", None), wife=("home", "living")):
    return [{"id": "yoonchan", "name": "Yoonchan", "where": me[0], "room": me[1]},
            {"id": "wife", "name": "Wife", "where": wife[0], "room": wife[1]}]


def asked(agent, room, **where):
    return [p["id"] for p in rules.must_ask(agent, room, people(**where))]


def test_the_agents_person_comes_from_its_person_id_or_the_name_it_acts_for():
    folks = people()
    assert rules.person_of(DOT, folks)["id"] == "yoonchan"
    assert rules.person_of({"acts_for": "wife"}, folks)["id"] == "wife"  # by name, any case
    assert rules.person_of({"acts_for": "Erik"}, folks) is None and rules.person_of(ENERGY, folks) is None


def test_wife_in_the_living_room_and_me_at_school_means_her_permission():
    assert asked(DOT, "living") == ["wife"]
    why = rules.why_ask(DOT, "Living room", rules.must_ask(DOT, "living", people()), people())
    assert why == "Wife is in the Living room, and Yoonchan is not, so Wife's permission is needed."


@pytest.mark.parametrize("me, wife, room, expected", [
    (("home", "living"), ("home", "living"), "living", []),          # I am in the room too: no one to ask
    (("home", "bedroom"), ("home", "living"), "living", ["wife"]),   # home, but not in that room
    (("away", None), ("away", None), "living", []),                  # nobody is home
    (("away", None), ("home", "bedroom"), "living", []),             # she is in another room
    (("away", None), ("home", None), "living", ["wife"]),            # home, room not set: may be anywhere
    (("away", None), ("home", "living"), None, ["wife"]),            # the plan does not place the device
    (("unknown", None), ("unknown", None), "living", []),            # nobody said: counts as away
    (("home", None), ("home", "living"), "living", ["wife"]),        # I am home with no room: not surely there
])
def test_who_must_be_asked(me, wife, room, expected):
    assert asked(DOT, room, me=me, wife=wife) == expected


def test_the_rule_runs_both_ways_for_her_agent():
    assert [p["id"] for p in rules.must_ask(HERS, "bedroom", people(me=("home", "bedroom"), wife=("away", None)))] == ["yoonchan"]
    assert rules.must_ask(HERS, "living", people()) == []  # she is in the room herself


def test_an_agent_that_acts_for_nobody_in_the_household_asks_whoever_is_there():
    guest = {"id": "concierge", "acts_for": "a visitor"}
    assert [p["id"] for p in rules.must_ask(guest, "living", people(me=("home", "living")))] == ["yoonchan", "wife"]


def test_an_energy_agent_sees_energy_readings_and_the_switch_only():
    assert rules.reads_energy_only(ENERGY) and not rules.reads_energy_only(DOT)
    plug = {"clusters": {"OnOff": {}, "ElectricalPowerMeasurement": {}}}
    lamp = {"clusters": {"OnOff": {}, "LevelControl": {}}}
    assert rules.is_meter(plug) and not rules.is_meter(lamp)
    state = {"OnOff.OnOff": True, "ElectricalPowerMeasurement.ActivePower": 124513,
             "ElectricalPowerMeasurement.RMSVoltage": 121104, "ElectricalEnergyMeasurement.CumulativeEnergyImported": 3776000,
             "LevelControl.CurrentLevel": 157, "DoorLock.LockState": "Locked"}
    assert rules.energy_state(state) == {"OnOff.OnOff": True, "ElectricalPowerMeasurement.ActivePower": 124513,
                                         "ElectricalPowerMeasurement.RMSVoltage": 121104,
                                         "ElectricalEnergyMeasurement.CumulativeEnergyImported": 3776000}
