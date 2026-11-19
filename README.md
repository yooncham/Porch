# Porch Device Hub

> Public copy of Team Porch's code for MIT AI Studio (Fall 2026), made for HW9 from the team's
> private repository at commit `6cb3b9e`. The demo server's address is left out; set
> `PORCH_HOST` to deploy to your own server.

The execution bridge between the Porch Engine and home device platforms (Interface v0).
The Engine is the only caller. Every device is described with Matter names, whichever
platform it lives on.

```
Engine ──HTTP──▶ api.py ──▶ CommandRouter ──▶ DeviceRegistry ──▶ BaseDriver
                             idempotency        merge, one          ├─ ThinQDriver   (lg_thinq)
                             read-back          path per device     ├─ GoogleHomeDriver (google_home)
                             latency                                ├─ MatterDriver  (matter)
                                                                    └─ RingDriver    (ring, read only)
```

Google Home has two drivers: `GoogleHomeDriver` keeps the demo home's devices in memory, and
`GoogleHomeMcpDriver` reaches a real home through Google's Home MCP server: lights are switched,
plugs are read, and a TV is read and can be played and paused. A hub that runs real
devices only also has family protection on: it sends nothing until the owner opens a short
control window, from the **Family protection** button on every page (with the owner PIN) or from
Terminal (`porch_hub/protection.py`, [Family protection](#family-protection)).

New here? The [field guide](docs/FIELD_GUIDE.md) explains what each part does and walks
through running the whole system step by step. To connect a real home's devices, follow
[docs/REAL_HOME.md](docs/REAL_HOME.md).

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
| `GET /devices/{id}/details` | Everything each platform that reaches the device reports, read now, in plain words: one section per platform, rows of group, name and value. Serial numbers, network addresses and ids are left out |
| `POST /devices/{id}/commands` | `{status, state_after, latency_ms, idempotency_key, error}` |
| `POST /devices/{id}/simulate` | Dry run: `{simulated_state_after, would_succeed, estimated_latency_ms, control_path, reason}` |
| `WS /events`, `WS /ws/events` | One JSON event per device change or command, from the moment of connecting |
| `GET /` | The home page: every page, what each is for, and the hub's live status |
| `GET /dashboard` | The live dashboard |
| `GET /stage` | The projector display for live demos |
| `GET /porch` | How Porch works: the agents, Porch's rules and the people at home, and the devices, live |
| `GET /flow` | Live flow: each agent request drawn check by check as it moves through Porch, from the gateway's stage feed |
| `GET /live` | The Live flow and the 3D home side by side (`?view=2d`: the floor plan), both live |
| `POST /household/agents/{id}/devices` | The owner chooses the devices one agent may control: `{devices: [...] or null, pin}` |
| `GET /agents.js` | The household's agents and their requests, live, for the digital twin and the 3D home |
| `GET /household` | Who lives here and where each person is (set by hand) |
| `POST /household/presence` | Mark where someone is: `{person, where, room, by, pin}`; needs that person's PIN or the owner's |
| `POST /household/verify` | Whether a PIN is that person's (the gateway asks before taking a person's approval) |
| `GET /home/places` | The floor plan's rooms and the room each device is in |
| `GET /online` | Whether agents can reach Porch from the internet now, and which |
| `GET /household/agents`, `GET /household/approvals`, `POST /household/approvals/{id}/{approve or decline}` | The gateway's agents and held requests, for the pages; an answer needs the PIN of the person it waits for |
| `GET /digital-twin` | This home's floor plan with every device on it, live from the event stream |
| `GET /home-3d` | The same home and its devices in 3D (Three.js), live from the event stream |
| `GET /home/layout` | The home's plan the two twin pages draw: floors, rooms, fixtures, where devices sit |
| `GET /details.js` | The "Everything the platform reports" panel the two twin pages show for the selected device |
| `GET /home/energy` | What every plug draws now, its energy counter, and its recent power, for the pages' energy panel |
| `GET /protection` | Family protection: whether it is enforced, and the open control window if any |
| `POST /protection/window` | The owner opens a control window: `{device_ids, minutes, pin}`. 401 without the right PIN |
| `POST /protection/close` | Closes the control window now. Needs no PIN: it can only make the home safer |
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
    close the valve, switch a light or the purifier on and off. Only the ones this home has a
    device for are shown.
  - **Form:** any device and any command it supports, with hours and minutes for
    `SetDelayStart`.
  - **Dry run:** the same command through `/simulate`.
  - Each command gets a new idempotency key.
  - A command family protection refuses shows the reason and **Open a control window for it**.
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
- **`protection_changed`**: when a control window is opened or closed through the hub's routes,
  with the same `data` as `GET /protection`.

Requests rejected before running (404, 422, 409) produce no event.

## Digital twin

http://localhost:8000/digital-twin is the home's floor plan with every device on it, drawn as
plain SVG from two things the hub serves: the home's plan (`GET /home/layout`) and its devices
(`GET /devices`, then `/ws/events`). Nothing about one home is written into the page.

- **The plan.** Each floor is drawn to scale with its rooms, stairs, counters and doors. A real
  home describes itself in a JSON file named by `PORCH_HOME_LAYOUT`
  (`deploy/home-layout.example.json` is a two-floor townhome to start from;
  `porch_hub/layout.py` says what each field means). Without one the page draws the demo home,
  whose plan and device places are in `porch_hub/layout.py` too.
- **Where a device sits.** Where the plan's `placements` put it; else in the room whose name, or
  one of its `matches`, equals the device's room in the hub; else in a tray beside the plan
  ("Not on the plan:"), so no device is ever hidden. Things the hub does not list, such as a
  dishwasher, can be drawn greyed as `extras`.
- **Live.** Each device shows its name and state (On, Off, PM2.5, Locked, Running, Playing), a
  light glows while it is on, and an offline device is dimmed. Changes arrive over `/ws/events`.
- **Readings in plain words.** A device's panel shows its readings with a label and a unit
  (Brightness 62%, White tone 2700 K, Filter left 99%, Voltage 121.1 V). The hub keeps Matter's
  names and units; hover a label to see the hub's own name for it.
- **Click a device** for its attributes and commands. A command is sent and read back like
  everywhere else; tick **Dry run only** to ask what would happen and send nothing.
- **Family protection.** A command the hub refuses shows why, with **Open a control window for
  it**: the owner's panel opens with that device ticked.
- **Energy.** A strip above the plan shows what every plug draws ([Energy](#energy)); a plug
  that measures its load also shows its watts on the plan.
- **Agents and Porch.** Above the plan, each of the household's agents that lives in an app
  (`deploy/home/agent.sh set <id> --client "Claude Desktop"`, `ChatGPT`, `Claude phone app`, `Claude on the web`)
  shows with that app and when it last called Porch, beside a Porch node with its checks. Each
  request is drawn as it happens, from the gateway's stage feed: a line from the agent to Porch,
  Porch's verdict in words (allowed, held for someone, refused, and why), then a line from Porch
  to the device it reached, which lights up on the plan; a read fans out to every device it
  covered. Steps are paced so a viewer can follow them (`?pace=2` slows them down).
  `porch_hub/dashboard/agents.js` does this for both twins.
- **Works with no internet**, like every page the hub serves.

## Energy

Both twin pages carry the same energy panel, `<porch-energy>` (`porch_hub/dashboard/energy.js`,
fed by `GET /home/energy`): what every plug draws now, the home's total, each plug's own energy
counter, what it used since the hub started, and a line showing how its power moved.

- **Readings, never estimates.** A device counts when it reports Matter's
  `ElectricalPowerMeasurement.ActivePower` (milliwatts) or
  `ElectricalEnergyMeasurement.CumulativeEnergyImported` (milliwatt-hours). Today that is an
  energy-monitoring plug paired over Matter. The panel shows watts and kWh, and for a plug that
  measures them (`RMSVoltage` in millivolts, `RMSCurrent` in milliamps) the volts it sees and
  the amps it passes.
- **A plug that reports no power is still listed**, with its switch and the reason. Google Home
  reports only whether a plug is on, so a plug the hub reaches only that way has no number.
- **Old readings are not passed on as current.** A device on two paths shows what the paths that
  reach it now report: with its Matter link down, a plug shows its switch from Google Home and
  no watts. And a plug that draws 20 W or more while neither its power nor its counter moves
  for 45 minutes is marked stale: its last reading is greyed, with the time it stopped, and left
  out of the total.
- **History is in memory** (`porch_hub/energy.py`): each meter's power changes for the last six
  hours, kept from the moment the hub starts whether or not a page is open. It is not a billing
  record, and it starts empty after a restart.
- Clicking a meter opens that device on the page.

## The household and its rules

Porch knows who lives in the home (`porch_hub/household.py`) and, set by hand with a PIN, who is
in which room. The gateway applies two household rules on top of its capability rules
(`porch_gateway/household_rules.py`):

- **The person in the room decides.** An agent acts for one person. If someone else is in the
  room a device is in and the agent's own person is not, the request is held until someone in
  the room approves with their PIN. Every page's top bar shows what waits, and answers it; so does
  **How Porch works**. The owner's key cannot answer for them, and no agent can. A yes makes Porch
  carry the request out at once, as the agent that asked. The owner's yes, with the owner PIN,
  also lets that one command through family protection, once: the hub holds that pass only while
  the request is carried out (`FamilyProtection.allow_once`). A request the rules allow but family
  protection stops waits for the owner's yes the same way, rather than failing.
- **Energy agents read.** An agent with `"reads": "energy"` sees energy readings only, has a
  `get_energy` tool, and every request to act is refused.
- **The devices each agent may control are the owner's choice.** On **How Porch works**, each
  agent that controls has **Choose devices**: any device its rights allow (the default), or only
  the devices ticked, saved with the owner PIN. The hub writes `agent-devices.json` beside the
  gateway's settings, and the gateway reads it on every request
  (`porch_gateway/device_grants.py`), so it applies at once. Any other device is refused:
  "Muse may control only Living stand in this home, as the owner chose." Reads are not limited,
  and Porch's rules, the household's and family protection still apply to the devices given. A
  damaged file stops every action rather than guess. From the hub's computer:
  `deploy/home/agent.sh devices muse home-1/living-stand` (`--any`, `--none`).

`http://localhost:8000/porch` shows the whole of it live: the agents and what each may do, Porch's
rules, who is where, requests waiting for an answer, what just happened, and the devices by
platform. Agents on the Claude phone app or claude.ai reach Porch through the online door
(`deploy/home/online.py`), described in docs/REAL_HOME.md.

## Family protection

A hub that runs real devices (`PORCH_REAL_DEVICES_ONLY=1`) sends nothing to a device until the
owner opens a control window: named devices, for at most 30 minutes. A window covers On and Off,
and Play and Pause for a TV; a window opened from a page names the ones its devices take.
Reading always works. `PORCH_FAMILY_PROTECTION=0` turns it off.

- **The button.** Next to **Pages** on every page: amber **FAMILY PROTECTION ON**, or green
  **CONTROL WINDOW OPEN** with the time left and the devices. Clicking it opens a panel to open
  a window (devices, minutes, owner PIN), to change the open one, or to close it.
- **Always allowed.** The owner can name devices that take On and Off at any time, with no
  window (`deploy/home/owner.sh always --device <id>`, taken back with `--none`). It is a
  standing choice in its own file, set only on the hub's computer and never over HTTP, and
  `GET /protection` and every page's label say which devices it covers.
- **The owner PIN** is chosen once in Terminal on the hub's computer
  (`deploy/home/owner.sh set-pin`). The hub keeps only a salted hash, in a file only the owner
  can read. Five wrong PINs lock the route for a minute, then longer.
- **Who can open a window.** Someone with the PIN on a hub page, or someone at the hub
  computer's Terminal (`deploy/home/owner.sh allow --device <id>`). Agents have neither. Closing
  needs no PIN.
- **Where it is checked.** In the command router, at the start of a command and again right
  before it is sent, and in the dry run, so the gateway can tell an agent before trying. A
  refused command answers 403 and nothing is stored under its idempotency key.

[docs/REAL_HOME.md](docs/REAL_HOME.md#family-protection) has the owner's steps.

## Live flow

http://localhost:8100/flow draws each agent request as it moves through Porch, at a pace a viewer
can follow. The agent is on the left and the device on the right. Between them are Porch's three
checks (who is asking, what it may control, who is in the room) and the hub's four steps (family
protection, the digital copy's dry run, the platform that carries it, the read-back). Each step
turns green, amber or red, with one plain sentence under the drawing. With nothing happening, the
page is the architecture: every agent, every step, the four platforms and the devices by room.
[docs/RECORDING.md](docs/RECORDING.md) is a shot list for the demo video.

- **Sources:** the gateway's stage feed (`ws://<host>:8000/ws/stage`, or `?engine=`), whose `flow`
  messages say what Porch did at each step (`porch_gateway/flow.py` lists them), and the hub's
  `/ws/events` for the devices. A device's card changes when the drawing reaches the read-back,
  not before.
- **Keys:** **R** draws the last request again, marked REPLAY, and a click on one under Recent
  draws that one. **Space** pauses, **F** is full screen, and `?pace=1.5` is slower.
- **Nothing private:** the gateway sends no agent key, and the page shows no PIN and no file
  path. A refusal's command for the owner's Terminal is cut off.
- **Each agent's card** says the app it lives in and, when the owner limited it, the devices it
  may control.

http://localhost:8100/live puts the Live flow beside the 3D home (`?view=2d`: beside the floor
plan), for recording: what Porch decides about a request on the right, and where it goes in the
home on the left. Both follow the gateway live, and it passes `?engine=` and `?pace=` to both.
Inside it, the 3D home and the floor plan drop their own side panels, and the menu is shown once.
`/live?record` is the clean frame for the demo video: a slim PORCH bar, the two views without
their headers, one large caption that says what the Live flow says as it says it, and title and
end cards on **T** and **E** ([docs/RECORDING.md](docs/RECORDING.md)).

A `flow` message, one per step:

```json
{"type": "flow", "id": "f-3b9e0c1a2d", "phase": "rules", "decision": "hold", "waiting_for": "Wife",
 "approval_id": "fdf3aa", "steps": [{"key": "who", "title": "Who is asking", "state": "ok",
 "short": "Dot, for Yoonchan", "text": "Porch knows Dot by its own key: it acts for Yoonchan."}, ...]}
```

`phase` is `ask`, `rules`, `dry_run`, `sent`, `verified`, `read`, `approval` or `error`; a step's
`state` is `ok`, `wait`, `stop` or `skip`.

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
- **Device roles** are picked by type: lock, light, appliance, tv, leak, plug, valve. A role the
  home has no device for gets no tile, and lights get up to two. Pin them with
  `?lock=<id>&light=<id>,<id>&appliance=<id>&tv=<id>&leak=<id>&plug=<id>&valve=<id>`, and show
  only some roles with `?only=light,appliance`.
- **Family protection:** on a hub that enforces it, the menu shows "Family protection on" or the
  open control window with its time left, on every page ([Family protection](#family-protection)).
  On the stage, a dry run the hub refuses for that reason raises an alert, so the room sees that
  nothing was sent; its **Open a control window** button opens the owner's panel for that device.
  The stage also announces a window opening, changing and closing.

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

http://localhost:8000/home-3d shows the same home in 3D, built from the same plan as the digital
twin (`GET /home/layout`): each floor's rooms cut away at wall height, floors side by side, with
stairs, counters and doors in their doorways, and a model for each device that shows its live
state. A stick vacuum stands on its charging stand, and the stand is empty while it is in use.
A TV stands with its back to the nearest wall: its screen changes colour while it plays, shows
two bars while it is paused and is dark in standby, and selecting it brings the camera round to
the screen. A lamp lights the room around it, the purifier's fan turns and its ring shows the air
quality, a plug's light is on while it passes power, the leak sensor shows a puddle and an alarm
ring, the valve handle turns, the dryer drum spins, and the robot vacuum leaves its dock. A door
lock takes the plan's door nearest to it: the bolt moves, and the door stands ajar while it is
unlocked.

- **Live.** It follows `/ws/events` like the other pages. A dry run (`command_simulated`) shows a
  blue ring and "dry run" on the device; a confirmed command shows a green ring with the
  read-back latency, and a failure a red one. The last five events are listed bottom left.
- **Interactive.** Drag to orbit, scroll to zoom, click a device (or its label) to see all its
  attributes and send commands. Commands go through the same dry run, execute and read-back
  as everywhere else. **Overview** and **Top** fit the whole home on the screen.
- **Family protection.** A refused command shows why, with **Open a control window for it**.
- **Energy.** The side panel shows the same energy panel as the 2D twin ([Energy](#energy)), and
  a device that measures its load shows its watts on its tag.
- **Agents and Porch.** The agents stand in a row above the house, with Porch between them and
  the home. Each request travels as a beam from the agent to Porch and from Porch to the device,
  which flashes; a caption at the bottom says what Porch decided and why. Same feed and pacing
  as the 2D twin.
- **Works with no internet.** Three.js r169 (MIT) is vendored in
  `porch_hub/dashboard/vendor/three/` and served by the hub at `/vendor/three/`.
- **Any home.** Devices stand where the plan places them, by the same rules as the digital twin;
  one that fits no room stands in a row in front of the house. Models are picked by device type,
  and a plug named "heater" or "lamp" gets that model. A type the scene has no model for appears
  as a labelled box. Things the hub does not list (`extras`) are grey blocks.

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

- **`control_paths`** is ordered local first: Matter, then LG ThinQ, then Google Home, then
  Ring. Path names stay the Interface v0 values (`matter`, `google_home`, `lg_thinq`), plus
  `ring` for doorbells.
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

The keys in `MATTER_BINDINGS` are `<node id>/<endpoint>`. A binding may also give `"name"` (what
the home calls the device; a node usually reports only its product name) and `"read_only": true`
(the hub reads the device and never commands it). An endpoint without a binding gets
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
| Stick cleaner (CordZero) | StickVacuumCleaner | RvcOperationalState, RvcCleanMode, PowerSource | None: LG lets it be read only |
| Air purifier | AirPurifier | OnOff, FanControl, AirQuality, Pm25ConcentrationMeasurement, and when LG reports them Pm1ConcentrationMeasurement, Pm10ConcentrationMeasurement and HepaFilterMonitoring (Condition: filter life left, in percent) | On, Off |

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

## Google Home

`porch_hub/drivers/google_home/` reaches the devices in a Google Home through Google's Home MCP
server (`home.googleapis.com/mcp`), signed in as the home's owner. It reads with
`list_home_resources` and `list_home_states`, and sends with `run_home_actions`, which is
offered only to the session that sends. Setting it up is in
[docs/REAL_HOME.md](docs/REAL_HOME.md#google-home-no-pairing).

| Google's kind | Porch type | Matter clusters | Commands |
| --- | --- | --- | --- |
| GoogleLightDevice | OnOffLight | OnOff, and when the light reports them LevelControl (CurrentLevel: brightness, 1 to 254) and ColorControl (ColorTemperatureMireds: its white tone), both read only | On, Off |
| GooglePluginUnitDevice | OnOffPlug | OnOff | None: a plug says nothing about what it powers |
| VideoRemoteControlDevice (a Google TV Streamer) | CastingVideoPlayer | MediaPlayback (CurrentState: Playing, Paused, NotPlaying, Buffering), PorchMediaExtension (ActivityState: Active, Standby, Inactive) | Play, Pause |

- **Read only by choice.** A binding with `"read_only": true` lists and reads the device and
  offers no command for it, whatever Google describes.
- **Checked before sending.** A command is offered only while Google's own description of the
  device lists it with no parameters at Google's lowest sensitivity level. Right before sending,
  the driver asks Google again for that description and for the device's state. A device that
  already reports what the command asks for is sent nothing.
- **A TV.** Google files a TV's traits under a part of the device (`GoogleTvDevice/MediaPlayback`)
  and marks them experimental. An action names that part as its `type`; Google does not confirm
  one that names the device's own kind. The hub reads what the TV is playing and whether it is
  in use.
  Google also describes On and Off for it, but the TV does not report whether it is on, so the
  hub could not confirm them and does not offer them. With nothing playing, Play and Pause are
  refused before anything is sent. A TV nobody is using reports no activity at all, only that
  nothing is playing, so `ActivityState` is `null` then and the pages say NOT PLAYING.
- **Unknown stays unknown.** A state Google does not report, or reports in a word the hub does
  not know, is `null`: never off, and never "not playing".
- **No push.** The device list is read at most every 30 seconds, and a command is confirmed by
  asking again until Google reports the new state, for up to 15 seconds.

Checked on a real Google TV Streamer (2026-11-15): reading it while it plays and while nothing
plays, and every check before sending (the sending session is offered the action tool, Google's
description allows Pause, and a Pause with nothing playing is refused with nothing sent).

**Play and Pause do not work on that TV yet.** Google accepted a Pause that named the TV's part
and answered it with the TV's own status, `STATUS_ENUM_SUCCESS`. The TV went on playing, on
screen and in Google's report: the hub's read-back still found it Playing after 15 seconds and
answered `failed`, twice. Google's success is therefore not proof that the TV acted, which is
why the hub reads back. A device that answers with any other status is treated as refusing, with no
wait. Google's whole answer to the last action is kept for the owner in the settings folder
(`google-home-last-action.json`, readable by the owner only). Against the tests' stand-in for
Google both commands pass. Until a Pause is seen to work, list such a TV with `"read_only": true`
in its binding: it shows what it is doing and offers no command.

## What is simulated

Without an LG token or a Matter Server URL, all three drivers run on in-memory copies. The same copies serve as the
backup demo mode.

- **LG ThinQ:** `FakeThinQCloud` uses LG's real JSON format, refuses starts when Remote Start
  is off, and sends MQTT reports. The demo home has a dryer, a robot vacuum and an air purifier.
- **Google Home:** `GoogleHomeDriver` enforces Google's block on unlocking. A real home is
  reached by `GoogleHomeMcpDriver`, an MCP client of `home.googleapis.com/mcp`
  ([Google Home](#google-home)); the tests drive it against `tests/fake_google_home_server.py`.
- **Matter:** `MATTER_SERVER_URL` may be just `ws://host:5580`; the hub adds `/ws`. Without `MATTER_SERVER_URL`, `SimulatedMatterDriver` holds the demo lock, leak
  sensor, valve and lamp plug. The tests drive the real `MatterDriver` against
  `tests/fake_matter_server.py`, which speaks the server's protocol over a real WebSocket.

Not built yet: linking accounts and adding devices.
State and idempotency keys are kept in memory in a single process. Move them to SQLite before
running more than one worker.
