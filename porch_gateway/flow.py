"""What Porch does with one request, step by step, in words. The hub's live view (/flow) draws it.

The gateway sends these on its stage feed as {"type": "flow", "id": ..., "phase": ...} while a
request moves through it:

  ask       who asked, with which tool, for which device and command, and the reason it gave
  rules     Porch's three checks and its verdict (allow, hold, waiting, deny)
  dry_run   the hub's digital copy: whether it would work, and why not (family protection, say)
  sent      the hub's answer: done or failed, the platform that carried it, how long it took
  verified  Porch read the device back itself and compared it with the prediction
  read      for a read: what the agent was shown, and from which platforms
  approval  someone answered a held request (the id is the held request's)
  error     the request stopped on an error before Porch decided

Pure functions, so the wording is tested without a running gateway (tests/test_flow.py).
"""

from typing import Any

CAPABILITY_WORDS = {
    "lighting": "Light", "appliance": "Appliance", "heating": "Heater", "unknown_load": "Plug",
    "entry": "Door lock", "water_shutoff": "Water main", "sensor": "Sensor", "network": "Router plug",
}
# A device the home has not said anything about (an unknown load) is named by what it is.
TYPE_WORDS = {"OnOffPlug": "Plug", "OnOffLight": "Light", "CastingVideoPlayer": "TV", "AirPurifier": "Air purifier",
              "StickVacuumCleaner": "Vacuum", "RoboticVacuumCleaner": "Vacuum", "Doorbell": "Doorbell"}
TITLES = {"who": "Who is asking", "may": "What it may control", "room": "Who is in the room"}


def kind(cap: str, device_type: str | None = None) -> str:
    if cap == "unknown_load":
        return TYPE_WORDS.get(device_type or "", "Device")
    return CAPABILITY_WORDS.get(cap, cap.replace("_", " ").capitalize())


def step(key: str, state: str, short: str, text: str, **more: Any) -> dict:
    """One check. state: ok (passed), wait (held for someone), stop (refused), skip (not reached)."""
    return {"key": key, "title": TITLES[key], "state": state, "short": short, "text": text, **more}


def who_step(agent: dict) -> dict:
    acts = agent.get("acts_for") or "the household"
    energy = agent.get("reads") == "energy"
    return step("who", "ok", f"{agent['name']}, for {acts}",
                f"Porch knows {agent['name']} by its own key: it acts for {acts}"
                + (", and it reads energy only." if energy else "."))


def answered(key: str, held: str, approval: tuple[str, str] | None, waiting_for: str) -> dict:
    """A check that held the request, and what has happened to the hold since."""
    if approval is None:
        return step(key, "wait", f"Waiting for {waiting_for}", held)
    decision, text = approval
    if decision == "allow":
        by = text.split(" approved")[0] if " approved" in text else waiting_for
        return step(key, "ok", f"{by} said yes", text, held=held)
    if decision == "deny":
        return step(key, "stop", "Refused", text, held=held)
    return step(key, "wait", f"Waiting for {waiting_for}", text, held=held)


def rule_steps(*, agent: dict, cap: str, rule: tuple[str, str] | None, decision: str, why: str,
               asked: list[dict], room: str | None, household: bool, mine: dict | None, mine_here: bool,
               safe: bool, approval: tuple[str, str] | None = None, device_type: str | None = None) -> list[dict]:
    """Porch's three checks for one action, as the live view shows them.

    rule: decide()'s verdict on the agent's rights and the home's rules for this kind of device,
    or None when Porch refused before asking it (an energy agent). decision, why and asked:
    decide_for()'s verdict, before any approval. approval: check_approval()'s answer when the
    agent came back with an approval id.
    """
    steps = [who_step(agent)]
    label = kind(cap, device_type)
    if rule is None:
        return steps + [step("may", "stop", "No authority: reads only", why),
                        step("room", "skip", "Not reached", "Not reached.")]
    rule_decision, rule_why = rule
    if rule_decision == "deny":
        return steps + [step("may", "stop", f"{label} · refused", rule_why),
                        step("room", "skip", "Not reached", "Not reached.")]
    if rule_decision == "hold":  # the owner must say yes first; the room rule is not asked after that
        return steps + [answered("may", rule_why, approval, "the owner"),
                        step("room", "skip", "Owner decides", "The owner's approval covers this request.")]
    steps.append(step("may", "ok", f"{label} · allowed", rule_why))
    if decision == "hold" and asked:
        names = " or ".join(p["name"] for p in asked)
        steps.append(answered("room", why, approval, names))
    elif safe:
        steps.append(step("room", "ok", "Safer: never held", "Making the home safer is never held for anyone."))
    elif not household:
        steps.append(step("room", "ok", "Rule off", "This hub keeps no household, so no one else is asked."))
    elif mine_here and mine:
        steps.append(step("room", "ok", f"{mine['name']} is there", f"{mine['name']} is in the {room}, so no one else is asked."))
    else:
        place = f"the {room}" if room else "the home"
        steps.append(step("room", "ok", "No one else there", f"Nobody else is in {place}, so no one else is asked."))
    return steps


def read_steps(agent: dict, what: str) -> list[dict]:
    """The checks for a read: the agent is known, sees what it may, and nobody is asked."""
    energy = agent.get("reads") == "energy"
    return [who_step(agent),
            step("may", "ok", "Energy meters only" if energy else "Reads what it may", what),
            step("room", "skip", "Nobody asked", "Reading changes nothing, so nobody is asked.")]


def agent_card(agent: dict) -> dict:
    """What the live view shows of an agent: who it is and the app it lives in. Never its key."""
    return {k: agent.get(k) for k in ("id", "name", "acts_for", "person", "reads", "client")}


def device_card(device: dict, room: str | None = None) -> dict:
    return {"id": device["id"], "name": device["name"], "room": room or device.get("room"),
            "type": device.get("device_type"), "platform": device.get("primary_control_path")}
