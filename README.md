# Porch Device Hub

The execution bridge between the Porch Engine and home device platforms (Interface v0).
The Engine is the only caller. Every device is described with Matter names, whichever
platform it lives on.

```
Engine ──HTTP──▶ api.py ──▶ CommandRouter ──▶ DeviceRegistry ──▶ BaseDriver
                             idempotency        merge, one          ├─ ThinQDriver   (lg_thinq)
                             read-back          path per device     ├─ GoogleHomeDriver (google_home)
                             latency                                └─ MatterDriver  (matter)
```

New here? The [field guide](docs/FIELD_GUIDE.md) explains what each part does and walks
through running the whole system step by step.

**This repo also holds the Porch gateway**, the agent-facing layer that sits in front of this hub.
- **[`porch_gateway/`](porch_gateway/README.md):** the MCP server agents connect to, with per-agent keys, scopes, safety rules, owner approvals, dry runs, verification and the audit log. It also holds the HW7 benchmark the team submitted, the demo scripts, and how to run the gateway and hub together locally.
- **[`deploy/`](deploy/README.md):** how the demo server is set up and how to deploy `main` to it.

`engine_lite/` (below) is the hub's own stand-in for the gateway, used by this README's walkthrough.

## See the MVP work

About five minutes, no API key, all devices simulated. You need Python 3.11 or newer and
three terminals, each opened in this folder.

**Once:**

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,engine]"
```

**Terminal 1, the hub:**

```bash
.venv/bin/uvicorn porch_hub.main:app --port 8000
```

Open http://localhost:8000/: the home page links every page with what it is for, and every page
has a **Pages** button in its header that opens the same menu.

**Terminal 2, engine_lite** (a stand-in for the Porch Engine; this terminal is the audit trail):

```bash
.venv/bin/python -m engine_lite
```

Open http://localhost:8000/stage?engine=ws://127.0.0.1:8765/ws/stage for the projector view.

**Terminal 3, the agent:**

```bash
.venv/bin/python -m engine_lite walkthrough
```

A scripted agent (not an AI) makes the same MCP calls Claude would, one scene at a time:

1. It lists the devices.
2. It previews a 4-hour delay on the dryer.
3. It sets the delay; the dryer's read-back confirms it.
4. It asks for a 20-hour delay. The dry run refuses, and nothing is sent.
5. It tries to close the water valve. Its scope doesn't allow that, so it's denied.
6. It asks to unlock the front door, which is held for the owner.
7. You answer as the owner (`y` or `n`).
8. It retries with the approval id. The door unlocks only if you said yes.

Watch the dashboard and Terminal 2 change as each scene runs. Stop everything with Ctrl+C.

**With a real AI agent:** keep Terminals 1 and 2 running and connect Claude Code instead of
the walkthrough. Claude Code needs an Anthropic API key or a plan that allows it.

```bash
claude mcp add --transport http porch http://127.0.0.1:8765/mcp
```

Then ask it, for example, "Delay the dryer 4 hours so it runs off-peak" or "Unlock the front
door". To answer a held unlock as the owner: `.venv/bin/python -m engine_lite approvals`, then
`approve <id>` or `deny <id>`.

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/uvicorn porch_hub.main:app --reload
.venv/bin/pytest
```

The demo home starts with eight simulated devices across all three paths. API docs are at
http://localhost:8000/docs.

## Interface v0

| Call | Returns |
| --- | --- |
| `GET /devices` | Every device, merged across platforms |
| `GET /devices/{id}/state` | Live cluster state, read from the platform now. 503 if offline |
| `POST /devices/{id}/commands` | `{status, state_after, latency_ms, idempotency_key, error}` |
| `POST /devices/{id}/simulate` | Dry run: `{simulated_state_after, would_succeed, estimated_latency_ms, control_path, reason}` |
| `WS /events`, `WS /ws/events` | One JSON event per device change or command, from the moment of connecting |
| `GET /` | The home page: every page, what each is for, and the hub's live status |
| `GET /dashboard` | The live dashboard |
| `GET /stage` | The projector display for live demos |
| `GET /digital-twin` | The 2D floor plan: physical hardware beside its digital copy |
| `GET /home-3d` | The home and its devices in 3D (Three.js), live from the event stream |
| `GET`, `PUT /demo/offline-mode` | `{"enabled": true}` turns the offline backup on without restarting |
| `POST /demo/devices/{id}/attributes` | Offline mode only: set a reading in the digital copy, for example a leak |

Device ids contain slashes (`home-1/front-door-lock`). Use them as they are in the path, or
encode the slash as `%2F`.

```bash
curl -X POST localhost:8000/devices/home-1/laundry-dryer/commands \
  -H 'content-type: application/json' \
  -d '{"command": "SetDelayStart", "params": {"hours": 4}, "idempotency_key": "eng-123"}'
```

How a command is handled:

- **`done` means confirmed.** The router reads state before the command, sends it, then
  polls the platform until the expected attributes appear or the confirm timeout (5 s)
  passes. A platform that accepts a command the device ignores (LG with Remote Start off)
  returns `failed`.
- **Read-back always follows a send attempt.** An acknowledgement can fail after the device
  has already acted, so a send error never skips the read-back:
  - **Confirmed anyway:** if the read-back shows the expected state, the command is `done`.
  - **Not confirmed:** otherwise it is `failed`, with the send error.
  - **Refused:** a refusal gets one fresh read.
- **`state_after` is never older than the command.** It is the last state read after the send
  attempt, or `{}` if no read succeeded.
- **`failed` carries `error`:** a platform refusal (Google blocks unlocking), an offline
  device, or a read-back that never matched.
- **Every driver call is bounded** by `driver_timeout_s` (10 s). A read that times out
  before the send means the command was never sent. A send that times out has an unknown
  outcome, so it is read back like any other ambiguous send.
- **A `Toggle` from an unknown on/off state is refused** without being sent, because its
  outcome couldn't be checked.
- **Idempotency.** A repeated key returns the stored response without touching the device,
  for 24 hours. Reusing a key for a different device, command or params returns 409.
- **Keys survive a restart when `DIGITAL_COPY_PATH` is set.** Every response is saved with
  the digital copy, so an Engine retry after a hub restart replays the answer instead of
  running the command again. A command answered by the copy in offline mode stays simulated
  on any later retry, even after a live restart. Without `DIGITAL_COPY_PATH`, keys are kept
  in memory only.
- **The first request for a key runs the command as its own task.** Cancelling or abandoning
  that request doesn't stop the command. A retry, during or after the run, waits for the same
  task instead of sending the command again. On shutdown the hub waits for running commands to
  finish before stopping the drivers.
- **Request errors are not stored against the key.** These are an unknown device (404), a
  command the device does not expose (422) and bad params (422), so the Engine can fix the
  request and resend it with the same key.
- **One record per physical device.** A device seen on several platforms is listed once,
  with every path in `control_paths`. Commands go to local Matter first and fall back to a
  cloud path only when Matter can't reach the device (see Deduplication).

The commands and the attributes each one must produce are listed in one table in
`porch_hub/commands.py`. The read-back check and the simulated devices both use that table.

## Events

Drivers report every change they receive (Matter attribute reports, LG MQTT reports, command
results) to the registry. The registry records the change and publishes it on the hub's
event bus (`porch_hub/events.py`). Upstream code can subscribe in process with
`registry.events.subscribe()`, or over the `/events` WebSocket. There are three event types:

```json
{"seq": 7, "type": "state_changed", "device_id": "home-1/kitchen-leak-sensor", "source": "matter",
 "at": "2026-10-22T14:05:12Z", "changed": {"BooleanState": {"StateValue": true}}, "clusters": {...}}
{"seq": 8, "type": "online_changed", "device_id": "...", "online": false, ...}
{"seq": 9, "type": "device_event", "device_id": "home-1/front-door-lock", "cluster": "DoorLock",
 "event": "LockOperation", "data": {"lockOperationType": 1, "operationSource": 1}, ...}
```

`state_changed` is sent only when a value actually changes. A command's own read-back
therefore doesn't repeat the event already sent for the device's report.

A subscriber that falls behind is ended rather than allowed to silently miss events, because a
missed leak alarm must not look like a quiet home. The in-process subscription raises
`SubscriberLagged`. `/events` sends `{"type": "resync_required"}` and closes with code 1013.
Either way, read `GET /devices` again, then subscribe again. A slow subscriber never slows the
hub.

## Dashboard

Open http://localhost:8000/dashboard. It's a single page served by the hub itself, with no
separate frontend server.

- **Device registry:** every device across LG ThinQ, Google Home and Matter, with its room,
  type and an online dot. Each path is a badge: the primary one is highlighted and fallbacks
  are dimmed. Every cluster attribute is shown, colored by meaning: Locked, Unlocked, Running,
  a wet leak sensor.
- **Live updates:** cards update as events arrive on `/ws/events` and flash when they change.
  A state-update feed lists each change. The socket reconnects on its own and reloads the
  registry each time, so nothing is missed between connections.
- **Command and latency log:** one row per command, with time, device, command and params,
  latency, the read-back result (Success or Failed, with the reason), the path, and the
  idempotency key. Simulated (offline) commands and replays are tagged.
- **Test controls:**
  - **Presets:** lock or unlock the door, start the dryer, delay it 4 hours, toggle the plug,
    close the valve.
  - **Form:** any device and any command it supports, with hours and minutes for
    `SetDelayStart`.
  - **Dry run:** the same command through `/simulate`.
  - Each command gets a new idempotency key.
- **Offline switch:** a toggle for `DEMO_OFFLINE_MODE` (`PUT /demo/offline-mode`). The hub
  answers once running commands have finished. While offline, the leak sensor can be set wet or
  dry from the page to run the emergency scene.

**Styling.** The page uses Tailwind, loaded from its CDN at a pinned version. The rules for
every class the page uses are also inlined, so with no internet at the venue it looks and works
the same. `tests/test_dashboard.py` checks that every class has an offline rule. Add `?nocdn`
to the URL to view the offline styling.

**Events for the log.** The event stream carries two more types:
- **`command_executed`**: one per command, including replays, with `command`, `params`,
  `status`, `latency_ms`, `idempotency_key`, `simulated`, `replayed`, `error`, and the state
  after in `clusters`.
- **`mode_changed`**: when offline mode switches, with `data: {"offline": true|false}`.

Requests rejected before running (404, 422, 409) produce no event.

## Digital twin

http://localhost:8000/digital-twin is an SVG floor plan of the demo home with four zones, each
device animated from live hub events:

| Zone | Device | Animation |
| --- | --- | --- |
| Entrance | Yale smart lock | Door swings ajar when unlocked; ring green when locked, red when unlocked; shield when a command is blocked |
| Utility room | Leak sensor, shutoff valve | Puddle and ripples when wet; valve lever turns 90° when closed; water in the pipe stops flowing |
| Laundry | LG dryer | Drum spins while running; delay-start countdown badge; energy indicator (high draw, off-peak scheduled, idle) |
| Living room | Space heater on a smart plug | Heat glow while on; a safety-rule override badge when a household rule wins |

**Two layers.** The page shows **Physical**, **Digital copy**, or **Split** (side by side):
- **Physical hardware** shows the state the hub confirmed by read-back.
- **Digital copy** shows each dry run's prediction (`command_simulated`) until the real command
  settles. For example, the copy's door swings open while the real door stays locked.
- **Tags:** PREDICTED, CONFIRMED, and BLOCKED · NOT SENT mark what happened.

**Engine pulses.** An AI Agent box and a Porch Engine box sit above the house:
- **When a command is evaluated (a dry run),** a pulse runs from the agent, through the
  Engine, to the device on the copy.
- **When a command executes,** a green or red pulse runs from the Engine to the device on the
  hardware.
- **When a sensor fires,** a pulse runs from it up to the Engine.

**Clicks.** Clicking the lock, the valve or the heater sends a real command: a dry run first,
then execute and read back.

**Stage scenarios.** The Engine's side is scripted on the page, and the dry runs and commands
are real:
- **Simulate Water Leak:** the leak sensor goes wet, the Engine's emergency rule closes the
  valve locally, and the hub confirms it. This needs offline demo mode, the only mode in which
  the hub lets a reading be set (the panel has the switch).
- **Simulate Lock Attack:** a prompt-injected unlock is dry-run, so the copy shows the door
  opening. It's then blocked at the Engine, and an approval card appears as on the owner's
  phone. Approve runs the real unlock; Deny keeps the door locked.
- **Simulate Off-Peak Energy Shift:** Gemini's request is dry-run, allowed, and executed. The
  dryer then counts down to its off-peak start.
- **Agent conflict (heater):** a household rule settles energy versus comfort, and the heater
  shows the override badge.

The floor plan is plain SVG and CSS, and the panels use Tailwind with the same offline
styles as `/dashboard`.

Device roles are picked by type. Pin them with `?lock=<id>&leak=<id>&valve=<id>&appliance=<id>&heater=<id>`.

## Stage display

http://localhost:8000/stage is a high-contrast, dark display for the projector during a live
demo:
- **Header:** the connection pills, `LIVE HARDWARE` or `DIGITAL COPY · OFFLINE`, and a clock.
- **Agents:** the active agents and their scope badges.
- **Safety alerts:** large, color-coded banners. Prompt injection is red, emergency override
  amber, arbitration violet. The newest is large and older ones collapse beneath it.
- **Three panels:**
  - **Physical home** shows live hardware: smart lock, LG appliance, leak sensor, smart plug and
    water valve, each as one large word.
  - **Digital copy** shows each dry run's predicted change before hardware moves. It's then
    marked executed as predicted, executed differently, blocked, or failed.
  - **What happened** is the plain-language audit feed.

It has no CDN, web fonts or remote assets. Type scales with the screen, so 1080p and 720p
projectors show the same layout on one screen without scrolling, and narrow screens get a
single scrolling column.

**Sources:**
- **The hub's `/ws/events`:** device state, dry runs, and hardware confirmations. Every
  `POST /simulate` publishes a `command_simulated` event. The Engine's dry runs therefore appear
  in the Digital copy panel with no Engine change.
- **The Engine's WebSocket:** agents, safety alerts and audit lines. Pass `?engine=ws://host:port/path`
  in the URL, or enter it in the presenter panel.
- **Device roles** are picked by type. Pin them with `?lock=<id>&appliance=<id>&leak=<id>&plug=<id>&valve=<id>`.

**Engine to stage contract (proposed for Interface v0, one JSON message each):**

```json
{"type": "agents", "agents": [{"id": "gemini", "name": "Gemini Agent", "scope_label": "Energy Management Only"}]}
{"type": "agent_connected", "agent": {"id": "claude", "name": "Claude Agent", "scope_label": "Full Home Access"}}
{"type": "agent_disconnected", "agent_id": "claude"}
{"type": "safety", "kind": "prompt_injection", "title": "Prompt injection detected",
 "detail": "Door unlock blocked → Sent approval request to owner phone",
 "agent_id": "claude", "device_id": "home-1/front-door-lock", "command": "UnlockDoor"}
{"type": "audit", "summary": "Gemini shifted LG Dryer cycle to off-peak hours", "agent_id": "gemini",
 "outcome": "done", "savings_usd": 1.40}
```

- **`kind`** is one of `prompt_injection`, `emergency_override` or `arbitration`, which sets the
  banner color. Other kinds show in blue.
- **A `safety` message with `device_id` and `command`** marks the matching dry run as BLOCKED ·
  NOT SENT.
- **`outcome`** is `done`, `blocked`, `pending_approval` or `failed`.
- **`at`** (ISO time) is optional on every message.

**Presenter controls.** Press **P** for the scripted scenes, **1** to **5** to play them, **C** to
clear alerts, and **F** for full screen. The keys work without opening the panel, which matters
when the projector mirrors the presenter's screen. The scenes are:

1. agents join,
2. off-peak laundry,
3. prompt injection,
4. leak emergency,
5. agent conflict.

Each scene sends the messages the Engine would send, and runs real hub dry runs and commands.
While they're in use the Engine pill reads **SCRIPTED**, so the source is never hidden. The leak
scene sets the sensor wet only in offline mode. On live hardware the sensor must actually be
wet.

## 3D home twin

http://localhost:8000/home-3d shows the demo home in 3D: seven rooms cut away at wall height,
furniture, and a model for each device that shows its live state. The door lock's bolt moves,
the leak sensor shows a puddle and an alarm ring, the valve handle turns and the water in the
pipe stops, the lamp and the heater glow, the dryer drum spins, the robot vacuum leaves its dock
and cleans the living room, and the purifier's fan turns.

- **Live.** It follows `/ws/events` like the other pages. A dry run (`command_simulated`) shows a
  blue ring and "dry run" on the device; a confirmed command shows a green ring with the
  read-back latency, and a failure a red one. The last five events are listed bottom left.
- **Interactive.** Drag to orbit, scroll to zoom, click a device (or its label) to see all its
  attributes and send commands. Commands go through the same dry run, execute and read-back
  as everywhere else.
- **Works with no internet.** Three.js r169 (MIT) is vendored in
  `porch_hub/dashboard/vendor/three/` and served by the hub at `/vendor/three/`.
- **Other homes.** Devices are placed by type, and two plugs are told apart by name ("heater",
  "lamp"). A device the scene has no model for appears as a labelled box in its room.

The approach follows [Clawblox](https://github.com/nacloos/clawblox)'s renderers: the server owns
the state and streams it, and the browser only turns the latest state into a Three.js scene.

## Benchmark (HW7)

`bench/` measures one claim: **an agent that reaches the home through Porch ends with the right
device state and an accurate report more often than the same agent using the devices' platform
APIs directly, and never carries out an action the owner's rules forbid.** The write-up is in
[docs/HW7_BENCHMARK.md](docs/HW7_BENCHMARK.md).

```bash
.venv/bin/python -m bench cases                      # the 16 cases and what each checks
.venv/bin/python -m bench run --trials 3             # porch vs baseline, about 5 minutes
.venv/bin/python -m bench run --fast --trials 1      # no simulated latency, under a minute
```

- **Two conditions, same tools.** `porch` is engine_lite plus the hub (scope, owner approval,
  dry run, read-back, idempotency). `baseline` wires the same `list_devices` and `perform_action`
  tools straight to each platform. Same agent, same home, same injected fault.
- **Faults.** `porch_hub/faults.py` wraps any driver: `lost_ack`, `no_effect`, `slow`, `offline`,
  `reject`. It also counts the commands that physically reached each device.
- **Checks fixed in advance** in `bench/cases.py`: the device ends in the right state (read past
  any fault), the agent's belief matches that state, and the device got the command at most once.
- **Evidence.** Each run writes `bench/results/<id>/`: `cases.json` with its hash, `runs.jsonl`
  with every tool call and engine_lite's audit trail, `meta.json` and `summary.md`. Only
  `reference-*` runs are kept in git.
- **The agent** is scripted (`bench/agent.py`): it does what the task says and retries a tool call
  once after a 6 s timeout. Model agents can plug into the same `run(case, env)` interface.

## Deduplication

One physical device can reach the hub several ways. A Yale lock, for example, can be in
Google Home through the cloud and also paired to Porch's Matter fabric. Each driver reports
its own record of the device. `merge_device_records` (`porch_hub/dedup.py`) matches these
records and merges them into one. Records match on the first of these that applies:

1. the same Porch id (set with bindings),
2. the same serial number, ignoring case, spaces and dashes,
3. the same MAC address, in any format (`AA:BB:..`, `aabb.ccdd..`, or the base64 bytes Matter
   sends),
4. the same device type, name and room, ignoring case and punctuation in any script
   (`현관문`, full-width letters, accents). Both rooms must be known, so "Unassigned" never
   matches.

Records of different device types never merge, except through a shared Porch id.

Some records are never merged:

- **Same platform.** Two records from the same platform are always two devices.
- **Shared identifiers.** A serial number or MAC shared by several records on one platform
  (the outlets of a power strip, the devices behind a bridge) is not used for those records.
- **Conflicting groups.** A match that would put two records from one platform into the same
  device is skipped.
- **Conflicting identifiers.** Weaker evidence never overrides stronger evidence against a
  match. Different serial numbers keep two records apart, however alike their MACs or names
  are. Different MACs keep them apart, however alike their names are. A serial number known on
  only one side doesn't block a match.

The merged record:

```json
{"id": "home-1/front-door-lock", "primary_control_path": "matter",
 "control_paths": ["matter", "google_home"], "serial_number": "YRD450-0042", "online": true, ...}
```

- **`control_paths`** is ordered local first: Matter, then LG ThinQ, then Google Home. Path
  names stay the Interface v0 values (`matter`, `google_home`, `lg_thinq`).
- **`primary_control_path`** is the first path in that order that is reachable. It is local
  Matter while Matter is up. If Matter can't reach the device, the next cloud path takes over
  until Matter is back.
- **`id`** stays the best path's id whichever path is active. `online` is true while any path
  works.
- **Name and state:** the name and state come from the best path. The room is the first one
  known on any path. Clusters only one path has are added.

How commands and updates use the paths:

- **Fallback.** A command tries the primary path first. The hub moves to the next path only
  if the command never reached the device: the path was offline, or failed before sending. A
  command that a path refused (Google blocking an unlock, LG with Remote Start off), or whose
  outcome is unknown, is never repeated on another path, so nothing runs twice.
- **`control_path` in the response** names the path that carried the command. Errors name
  the path they came from, for example `matter: unreachable; google_home: Google Home blocks
  UnlockDoor`.
- **Updates from other paths.** A secondary path's reports don't overwrite the primary's
  state. A slow Google poll can't undo what local Matter just reported. A secondary's
  read-back after a command it just ran does count.
- **`online_changed`** is sent only when the device as a whole becomes reachable or
  unreachable, not when a single path goes up or down.

## Digital copy and offline demo mode

The registry keeps an up-to-date copy of every device's state. `porch_hub/digital_copy.py`
uses that copy in two ways that never touch hardware.

**Dry run.** `POST /devices/{id}/simulate` takes `{"command", "params"}`, the same as a
command but without an idempotency key, and leaves both the devices and the copy unchanged.

```json
{"simulated_state_after": {"DoorLock": {"attributes": {"LockState": "Unlocked"}, ...}},
 "would_succeed": true, "estimated_latency_ms": 150, "control_path": "matter", "reason": null}
```

- **`would_succeed`** applies the same platform rules the live path would, checked without
  contacting the platform: Google's block on unlocking, LG's Remote Start switch and profile
  limits, and whether a Matter device supports the command. It also checks that some path to
  the device is reachable.
- **`reason`** says why a command would fail. An unsupported command or bad params is a
  `would_succeed: false` with a reason, not a 422.
- **`estimated_latency_ms`** is a running average of measured live commands on that path. It
  starts at Matter 150 ms, Google 1500 ms and LG 2500 ms.

**Offline backup.** Set `DEMO_OFFLINE_MODE=1` at startup, or switch it on and off at runtime
with `PUT /demo/offline-mode`. While it is on:

- **Commands** change the digital copy at once and return `"status": "done"` with
  `"simulated": true`. No driver is called.
- **The UI keeps working:** `GET /devices`, `GET /devices/{id}/state` and `/events` serve the
  copy, so the stage UI and the Engine keep working.
- **Every device is shown reachable** over its best path.
- **The copy's own rules still apply.** A dryer whose Remote Start was off still refuses to
  start. Offline mode answers as the real home would, minus the network.
- **Validation is unchanged:** an unknown device is still a 404, and a bad request still a 422.
- **Scenes:** `POST /demo/devices/{id}/attributes` with `{"cluster": "BooleanState",
  "attribute": "StateValue", "value": true}` stands in for a leak sensor going wet.
- **Switching on mid-command.** New commands go to the copy from that moment. A command
  already running on a real device finishes, read-back included, before the copy takes over.
  Then the drivers are stopped, so nothing reaches hardware or the network while offline.
- **Turning it off** starts the drivers again and re-reads every device. Commands keep going
  to the copy until that is done, so none reaches a driver that isn't ready. The differences
  between the copy and the real home are sent as events, so the UI resyncs.

**For Demo Day**, set `DIGITAL_COPY_PATH` (for example `~/.config/porch-hub/digital-copy.json`).
The hub saves the copy there while it runs, on shutdown, and after every offline change. A
hub started with `DEMO_OFFLINE_MODE=1` loads the devices and state from the last rehearsal.
Without a saved copy, it starts from the built-in demo home. In both cases no driver is called,
not even to list devices:

```bash
DIGITAL_COPY_PATH=~/.config/porch-hub/digital-copy.json .venv/bin/uvicorn porch_hub.main:app   # rehearsal
DEMO_OFFLINE_MODE=1 DIGITAL_COPY_PATH=~/.config/porch-hub/digital-copy.json .venv/bin/uvicorn porch_hub.main:app
```

A platform that can't start, because there's no network or the token is bad, is logged and
retried in the background instead of stopping the hub. Its devices appear once it starts. A
platform whose listing fails keeps its known devices, marked offline, so their other paths take
over.

More about the saved copy:
- **Device changes** reported by the platforms are saved too, at most every 10 seconds and never
  losing the last one.
- **A damaged snapshot** is kept beside the new one as `<name>.corrupt-<time>` instead of being
  overwritten.
- **A mode switch** runs to completion even if the request that started it is cancelled.

## Adding a platform connector

Subclass `BaseDriver` (`porch_hub/drivers/base.py`), set `control_path`, and implement
`list_devices`, `read_state` and `send_command`. Translate native fields into Matter clusters
(`porch_hub/clusters.py`) and raise `DeviceOffline`, `CommandRejected` or `DeviceNotFound`
instead of platform exceptions. Override `preflight` with the platform's rules, checked
without I/O, so dry runs and offline mode predict refusals. Call `_publish_state`, `_publish_online` or `_publish_event`
when the platform pushes a change. Pass the driver to `create_app([...])`.

## Matter

`porch_hub/drivers/matter/` connects to the Open Home Foundation Matter Server (matterjs-server)
over its WebSocket API. Run the server on the home node, add devices to Porch's fabric, then:

```bash
npx matter-server        # serves ws://localhost:5580/ws
export MATTER_SERVER_URL=ws://localhost:5580/ws
export MATTER_BINDINGS='{"1/1": {"porch_id": "home-1/front-door-lock", "room": "Entry"}}'
.venv/bin/uvicorn porch_hub.main:app
```

The keys in `MATTER_BINDINGS` are `<node id>/<endpoint>`. An endpoint without a binding gets
the id `home-1/matter-<node>-<endpoint>` and the room `Unassigned`.

| Matter cluster | Porch attribute | Commands | Device types |
| --- | --- | --- | --- |
| DoorLock (0x0101) | LockState: Locked, Unlocked, NotFullyLocked, Unlatched | LockDoor, UnlockDoor (timed invokes) | Door Lock |
| OnOff (0x0006) | OnOff | On, Off, Toggle | On/Off Plug-in Unit, On/Off and Dimmable Light |
| BooleanState (0x0045) | StateValue: true means wet | none | Water Leak Detector |
| ValveConfigurationAndControl (0x0081) | CurrentState: Open, Closed, Transitioning | Open, Close | Water Valve |

- **Live updates.** After `start_listening` the server pushes every attribute change.
  `attribute_updated` reports for these clusters update the registry immediately.
  `node_event` messages are forwarded as `device_event`s: DoorLock LockOperation (which shows
  a manual turn), DoorLockAlarm, and BooleanState StateChange. StateChange also sets the
  leak state.
- **Commands** are offered only if the device's AcceptedCommandList includes them. Each one is
  confirmed by a fresh `read_attribute` from the device.
- **BooleanState** is used only on endpoints that declare the Water Leak Detector device type.
  A contact sensor uses the same cluster and is skipped.
- **Reconnects.** The hub can start before the Matter Server is up. It retries with backoff,
  marks Matter devices offline while disconnected, and takes a full snapshot after every
  reconnect. Changes made during the gap therefore reach the registry and the event stream.

Still to confirm against a real server and devices: the exact JSON for enum and struct values,
and whether a lock needs a PIN (the hub sends an empty payload).

## LG ThinQ

`porch_hub/drivers/thinq/` connects to LG through LG's official `thinqconnect` SDK. It uses
`ThinQApi` for REST calls and `ThinQMQTTClient` for push updates over AWS IoT Core. To use a
real LG account:

```bash
.venv/bin/pip install -e ".[lg]"
export LG_THINQ_PAT=...        # from connect-pat.lgthinq.com
export LG_THINQ_COUNTRY=US
.venv/bin/uvicorn porch_hub.main:app
```

With `LG_THINQ_PAT` set, the hub uses the live account in place of the simulated LG devices.
On first run it creates one LG client id and saves it in `~/.config/porch-hub/thinq-client-id`;
creating many client ids can get an account blocked. To give appliances Porch ids and rooms,
set `LG_THINQ_BINDINGS` (format in `connect.py`). Appliances without a binding get an id built
from their name and the room `Unassigned`.

| LG type | Porch type | Matter clusters | Commands |
| --- | --- | --- | --- |
| Dryer, Washer | LaundryDryer, LaundryWasher | OperationalState, PorchLaundryExtension | Start, Stop, SetDelayStart |
| Robot cleaner | RoboticVacuumCleaner | RvcOperationalState, PowerSource | Start, Pause, GoHome |
| Air purifier | AirPurifier | OnOff, FanControl, AirQuality, Pm25ConcentrationMeasurement | On, Off |

- **Remote Start guard.** `Start` and `SetDelayStart` first check the appliance's
  `remoteControlEnabled` flag. If it is off, they fail with "Remote Control switch disabled on
  physical appliance" and nothing is sent to LG. LG's own error for this case (code 2301) is
  turned into the same message.
- **The flag must explicitly read true.** A washer or dryer that stops reporting it isn't
  started. An appliance reporting an error isn't started either. Dry runs and offline mode
  apply the same rules.
- **Profile check.** Each command is checked against the appliance's LG profile before it is
  sent. The field must be writable, and the value must be allowed: in the enum list, or a whole
  number within the range, on its step and not excluded. For example, delay start is limited to
  0 to 19 hours, in whole hours.
- **States the hub doesn't recognize** read as `Unknown`, never as `Stopped`, so a read-back
  can't confirm a Stop from a state it can't interpret. `VendorRunState` keeps LG's own name.
  Battery level is reported in Matter's half-percent units (`BatPercentRemaining` 200 = full).
- **Read-back** polls LG's status API once a second for up to 10 seconds. LG blocks clients
  that call too often.
- **MQTT sync.** LG pushes partial status reports. Each one is merged into the appliance's last
  known status and sent to the registry, so `GET /devices` stays current without calling LG.
  After an MQTT reconnect the driver re-reads every appliance, because reports sent during the
  outage are lost. Appliances added to the LG account after startup get push updates only
  after the next restart.
- **Stale reads.** A status read that comes back after a newer push doesn't overwrite it. The
  read's own answer still confirms the command. The Matter driver does the same for each
  attribute.
- **MQTT transport** (`drivers/thinq/mqtt.py`) builds on LG's `ThinQMQTTClient`. It keeps the
  SDK's broker lookup, client registration and certificate steps, and changes four things:
  - **No blocking:** it connects and subscribes without blocking the hub.
  - **Every topic:** it subscribes to every topic LG's certificate allows, not just the first.
  - **Resubscribes:** it resubscribes after a reconnect in which the broker lost the session.
  - **Clean shutdown:** it closes the native connection and removes the client registration
    on shutdown.

Still to confirm on real appliances: LG's state and enum names, which vary by model; whether
`STOP` stops or pauses a cycle; and whether setting a delay start also needs a Start.

## What is simulated

Without an LG token or a Matter Server URL, all three drivers run on in-memory copies. The same copies serve as the
backup demo mode.

- **LG ThinQ:** `FakeThinQCloud` uses LG's real JSON format, refuses starts when Remote Start
  is off, and sends MQTT reports. The demo home has a dryer, a robot vacuum and an air purifier.
- **Google Home:** `GoogleHomeDriver` enforces Google's block on unlocking. Next step: an MCP
  client for `home.googleapis.com/mcp`, polling `list_home_states`.
- **Matter:** `MATTER_SERVER_URL` may be just `ws://host:5580`; the hub adds `/ws`. Without `MATTER_SERVER_URL`, `SimulatedMatterDriver` holds the demo lock, leak
  sensor, valve and lamp plug. The tests drive the real `MatterDriver` against
  `tests/fake_matter_server.py`, which speaks the server's protocol over a real WebSocket.

Not built yet: linking accounts and adding devices.
State and idempotency keys are kept in memory in a single process. Move them to SQLite before
running more than one worker.
