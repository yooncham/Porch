"""The household owner's side of Porch.

  .venv/bin/python owner.py list                  held requests waiting for you
  .venv/bin/python owner.py approve [<id>]        approve one; without an id, the only waiting request
                                                  (the agent then retries with the id)
  .venv/bin/python owner.py decline <id>
  .venv/bin/python owner.py home | away           whether someone is home (heating rule)
  .venv/bin/python owner.py leak | dry            leak sensor wet or dry (switches the hub to offline mode)
  .venv/bin/python owner.py reset                 devices back to the starting state, audit and approvals cleared
"""

import json
import os
import sys
import uuid

import httpx

CONFIG = json.load(open(os.path.expanduser(
    os.environ.get("PORCH_GATEWAY_CONFIG") or __file__.replace("owner.py", "porch_config.json"))))
PORCH = "http://127.0.0.1:8000"
HUB = CONFIG["hub_url"]
HEADERS = {"x-owner-key": CONFIG["owner_key"]}

# The demo home's starting state, restored through the Device Hub.
BASELINE = [
    ("home-1/front-door-lock", "LockDoor", {}),
    ("home-1/desk-lamp-plug", "On", {}),
    ("home-1/space-heater-plug", "Off", {}),
    ("home-1/main-water-valve", "Open", {}),
    ("home-1/laundry-dryer", "SetDelayStart", {"hours": 0}),
]


def show(r: httpx.Response) -> None:
    print(json.dumps(r.json(), indent=2))


def set_leak(wet: bool) -> None:
    # The hub lets a sensor reading be set only in offline (digital copy) mode.
    httpx.put(f"{HUB}/demo/offline-mode", json={"enabled": wet}, timeout=30)
    if wet:
        show(httpx.post(f"{HUB}/demo/devices/home-1/kitchen-leak-sensor/attributes",
                        json={"cluster": "BooleanState", "attribute": "StateValue", "value": True}))
    print("leak sensor:", "WET (hub in offline mode)" if wet else "dry (hub back on live drivers)")


cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
if cmd == "list":
    show(httpx.get(f"{PORCH}/owner/approvals", headers=HEADERS))
elif cmd in ("approve", "decline"):
    if len(sys.argv) > 2:
        approval_id = sys.argv[2]
    else:  # no id given: answer the one request that is waiting
        pending = httpx.get(f"{PORCH}/owner/approvals", headers=HEADERS).json()
        if len(pending) != 1:
            sys.exit(f"{len(pending)} requests are waiting; give the approval id. Run: owner.py list")
        approval_id = pending[0]["id"]
        print(f"{cmd} {approval_id}: {pending[0]['command']} on {pending[0]['device_name']}")
    show(httpx.post(f"{PORCH}/owner/approvals/{approval_id}/{cmd}", headers=HEADERS))
elif cmd in ("home", "away"):
    show(httpx.post(f"{PORCH}/owner/occupancy", headers=HEADERS, json={"occupied": cmd == "home"}))
elif cmd in ("leak", "dry"):
    set_leak(cmd == "leak")
elif cmd == "reset":
    set_leak(False)
    for device_id, command, params in BASELINE:
        r = httpx.post(f"{HUB}/devices/{device_id}/commands", timeout=30,
                       json={"command": command, "params": params, "idempotency_key": f"reset-{uuid.uuid4().hex}"})
        print(f"{device_id:28} {command:14} {r.json().get('status')}")
    httpx.post(f"{PORCH}/owner/reset", headers=HEADERS)
    print("Porch audit and approvals cleared; nobody home.")
else:
    print(__doc__)
