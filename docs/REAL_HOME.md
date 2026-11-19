# Connecting a real home

How to put real devices behind the Porch Device Hub, written for Yoonchan's home first. The same
steps work for any home with LG ThinQ appliances and Matter devices. The scripts in
`deploy/home/` do the Mac side.

**Contents:** [What connects how](#what-connects-how) · [0. Set up the Mac](#0-set-up-the-mac) ·
[1. LG appliances](#1-lg-appliances) · [2. Matter Server](#2-start-the-matter-server) ·
[3. Pair the Matter devices](#3-pair-each-matter-device-with-porch) ·
[Google Home](#google-home-no-pairing) · [Ring doorbell](#ring-doorbell) ·
[4. Name the devices](#4-name-the-devices) · [5. Start Porch](#5-start-porch) ·
[Family protection](#family-protection) · [Your floor plan](#your-floor-plan) ·
[Energy readings](#energy-readings) ·
[6. Check it](#6-check-it-end-to-end) · [Your own agents](#your-own-agents) ·
[The household](#the-household-whose-room-it-is) · [Your agents in ChatGPT and on claude.ai](#your-agents-in-chatgpt-and-on-claudeai) ·
[Demo Day](#demo-day-moving-the-home-to-the-table) ·
[Not connected yet](#not-connected-yet) · [When something breaks](#when-something-breaks)

## What connects how

| Device | Path into the hub | What agents can do through Porch |
| --- | --- | --- |
| LG PuriCare 360 | LG's official ThinQ Connect API (cloud, with push updates) | On, Off. Read air quality, PM1, PM2.5, PM10, fan mode and how much filter life is left |
| Tapo L535E bulbs | Matter over Wi-Fi, through Porch's own Matter Server on the Mac. Or Google Home's cloud, with no pairing ([Google Home](#google-home-no-pairing)) | On and Off (Toggle too over Matter). Through Google Home: read the brightness and the white tone. Setting them is not built yet |
| Tapo P110M plugs | Matter over Wi-Fi, same Matter Server. Through Google Home they are listed and read only | Over Matter: On, Off, Toggle, and power (mW), energy (mWh), voltage (mV) and current (mA) when the plug reports them. Through Google Home: see whether it is on |
| LG cordless stick vacuum (CordZero) | LG's ThinQ Connect API, read only: LG offers no control for it | See whether it is charging, charged or in use, its cleaning mode, and its battery level while in use |
| Ring doorbell | Ring's partner API (cloud); see [Ring doorbell](#ring-doorbell) | See that it is there and online. No button or motion events and no video yet |
| Ring Indoor Cam | Ring lists it; the hub leaves it out | Nothing yet |
| Google TV Streamer | Google Home's cloud, with no pairing ([Google Home](#google-home-no-pairing)) | See what it is playing (playing, paused, nothing) and whether it is in use. No control yet: Google accepts Pause and the TV ignores it, so it is listed read only |

The Tapo devices have two ways in. Matter (steps 2 and 3) is local and fast, switches plugs and
reads their power, but each device has to be paired once. Google Home needs no pairing for
devices already in the Google Home app, but it switches lights only and is slower.

Every device keeps working in its own app. Matter lets a device belong to several apps at once,
so each Tapo bulb and plug stays in the Tapo app (and Google Home, if you add it there) and is
also paired with Porch's Matter Server. Porch then controls it on the home network, gets every
state change as it happens, and reads it back after each command.

## 0. Set up the Mac

You need a Mac (Apple Silicon, macOS 14 or newer) on the same Wi-Fi network as the devices, with
Node.js 22.13 or newer and uv: `brew install node uv` if `node --version` or `uv --version`
fails. Then, once:

```bash
git clone https://github.com/yooncham/porch-device-hub-yc.git ~/Code/porch-device-hub-yc
cd ~/Code/porch-device-hub-yc
deploy/home/setup_mac.sh
```

`setup_mac.sh` checks Node.js and uv, creates the hub's and the gateway's Python environments,
and creates `~/.config/porch-hub/` with two files only you can read:

- `home.env`: your LG token and the hub's settings (from `deploy/home.env.example`).
- `porch_config.json`: the gateway's config for this home. It starts as the demo's agents and
  keys, marked `"hardware": "physical"`, with no plug mapped yet.

Run it again any time; it never overwrites those files. Never commit them or paste the token
anywhere.

## 1. LG appliances

1. In the LG ThinQ app, check that the PuriCare 360 is added and online.
2. Create a personal access token at https://connect-pat.lgthinq.com, signed in with the same LG
   account. Give it these scopes: view devices, view device status, device control, event
   subscription, push notifications.
3. Open `~/.config/porch-hub/home.env` and put it after `LG_THINQ_PAT=`.
4. In the same file, set `LG_THINQ_COUNTRY` to the country of that LG account (for example `KR`),
   not where you live now. With the wrong country, or a token pasted twice, LG answers "not
   allowed" (its codes 1305 and 1309), and `discover.sh` says what to check.

The hub creates one LG client id the first time, in `~/.config/porch-hub/thinq-client-id`. Keep
that file: LG can block accounts that keep creating new clients. LG also answers "API calls
exceeded" after unusual call volumes, so don't restart the hub in a tight loop.

## 2. Start the Matter Server

In its own Terminal tab, and leave it running:

```bash
cd ~/Code/porch-device-hub-yc
deploy/home/matter_server.sh
```

This runs the Open Home Foundation Matter Server (the one Home Assistant uses) on this Mac only,
on its Wi-Fi interface. Its dashboard is at http://127.0.0.1:5580. The server keeps Porch's
Matter credentials in `~/.config/porch-hub/matter`: back that folder up. If it is lost, every
device has to be paired again.

The Mac must stay awake while Porch runs: keep it on power, or run `caffeinate -dims` in another
tab.

## 3. Pair each Matter device with Porch

Do this once per bulb and per plug. Each one is already set up in the Tapo app.

1. **Get a code from the Tapo app.** Open the device, then its **Settings** → **Matter Setup
   Code**, and generate a code. A code works for 15 minutes and for one app only, so make a new
   one each time ([TP-Link's FAQ](https://www.omadanetworks.com/us/support/faq/3573/)).
2. **Give it to Porch's Matter Server.** In the dashboard at http://127.0.0.1:5580, click
   **Commission node** → **Commission existing device**, paste the code into **Share code**, and
   click **Commission**. When it finishes, the device shows as a new node (1, 2, 3, ...).
3. **Google Home too, if you want it there:** make another code in the Tapo app and add the
   device in Google Home with it.

Install any firmware update the Tapo app offers first: energy readings over Matter came to the
P110M in a firmware update.

A device that is only in Google Home can be shared the same way: in the Google Home app, touch
and hold it, open Settings → **Linked Matter apps & services** → **Link apps & services** →
**Use pairing code** ([Google's help page](https://support.google.com/googlehome/answer/13127223)).

## Google Home (no pairing)

Devices already in the Google Home app can join the hub without pairing anything. The hub asks
Google's Home MCP server, Google's own API for this, signed in as you
([Google's guide](https://developers.home.google.com/mcp/home)).

What this path gives, and what it does not:

- **Lights** can be switched on and off. **Plugs** are listed and read, not switched.
- **A TV** (a Google TV Streamer) is listed and read: what it is playing (Playing, Paused,
  NotPlaying, Buffering) and whether it is in use (Active, Standby, Inactive). It offers **Play**
  and **Pause**, which the hub confirms by asking Google what the TV is playing afterwards. On
  the Google TV Streamer they do not work yet: Google answers a Pause with success, the TV
  goes on playing, and the hub answers "failed" (README, "Google Home"). Keep
  `"read_only": true` on the TV's binding (step 4) until a Pause is seen to work: the TV then
  shows what it is doing and has no Play or Pause button.
  Google describes more for a TV (power, volume, apps, keys) and marks all of it experimental.
  The hub leaves those out: the TV does not report whether it is on or how loud it is, so the
  hub could not confirm them. With nothing playing, Play and Pause are refused before anything
  is sent. When nobody is using the TV, Google reports only that nothing is playing, and the
  pages say NOT PLAYING.
- Other kinds, such as a speaker or a display, are left out, and `deploy/home/discover.sh` says so.
- There are no power or energy readings and no push. The hub asks Google for the device list at
  most every 30 seconds, and a command takes a few seconds.
- Google decides how fresh a state is: it may ask the device's maker or answer from its own copy.
- For plugs you can switch, for readings and for speed, pair the device over Matter (step 3). A
  device on both paths is one device in the hub when `MATTER_BINDINGS` and
  `GOOGLE_HOME_BINDINGS` give it the same Porch id. The hub then uses Matter while it is up and
  Google Home when it is not.

Google asks for three things on its side:

1. A **Google Home Premium Advanced** subscription on the Google account and home. Without it
   Google answers "At least one Home Premium Advanced subscription is required."
2. A **Google Cloud project** with the Home API (`home.googleapis.com`) enabled. Its OAuth
   consent screen is **External**, in **Testing**, with your Google account as a test user and
   one scope: `https://www.googleapis.com/auth/home.platform.v2`.
3. An **OAuth client** in that project: type **Web application**, authorized redirect URI
   `http://127.0.0.1:8989/oauth/callback`, with "Use this client for an AI-powered agent" ticked.

Then, on the Mac:

1. Save the client's id and secret as `~/.config/porch-hub/google-home/google-home-client.json`
   and make it readable only by you:

   ```json
   {"client_id": "<...>.apps.googleusercontent.com", "client_secret": "<...>"}
   ```

   ```bash
   chmod 600 ~/.config/porch-hub/google-home/google-home-client.json
   ```

2. Sign in. A browser opens; choose the Google account your home is in. The grant Google shows
   covers reading the home and controlling its devices. This step only reads.

   ```bash
   deploy/home/connect.sh google
   ```

   It ends with the home's devices by name and what the hub does with each. The sign-in is saved
   in that folder, so the hub does not ask again.
3. In `home.env`, uncomment `GOOGLE_HOME_DIRECTORY`. Then run `deploy/home/discover.sh` and copy
   its `GOOGLE_HOME_BINDINGS` line into `home.env` (step 4).

Never paste the client secret, anything from that folder, or `google-home-last-check.json` into a
chat. If you have already signed in with another copy of the hub, copying its
`google-home-client.json` and `google-home-auth` folder into `~/.config/porch-hub/google-home/`
(`cp -Rp`, which keeps them private) should stand in for steps 1 and 2. If a browser still opens,
sign in again.

## Ring doorbell

The hub lists the doorbells of a Ring account and whether each is online. It only reads: no
button or motion events and no video yet (see [Not connected yet](#not-connected-yet)).

Ring gives access through a **private Ring Partner app** that you create and link to your own
Ring account ([Ring's guide](https://developer.amazon.com/docs/ring/get-started.html)). Linking
is done once and is not part of this repository. The hub needs three values from it: the app's
client id, its client secret, and the refresh token Ring issued when the account was linked.

1. Save them, typed at hidden prompts:

   ```bash
   deploy/home/connect.sh ring
   ```

   This writes `~/.config/porch-hub/ring-authorization.json`, readable only by you.
2. In `home.env`, uncomment `RING_AUTHORIZATION_FILE`. Then run `deploy/home/discover.sh` and
   copy its `RING_BINDINGS` line into `home.env` (step 4).

Ring can replace the refresh token when it is used, and the hub saves the new one in that file.
So only one program should hold the link. If another copy of the hub already has it, stop using
Ring there before starting here, or set `RING_AUTHORIZATION_FILE` in both to the same file.

## 4. Name the devices

Matter has no rooms, and LG's ids say nothing about where a device is. Bindings give each device
a Porch id and a room. List what the platforms report (this only reads):

```bash
deploy/home/discover.sh
```

It prints a table for LG (each appliance's full device id and the id the hub gives it) and one
for the Matter Server (every endpoint, which Porch device it becomes, and which plugs report
power and energy), then a binding line for each. With Google Home or Ring connected it prints
their devices and binding lines too. On a first run it looks like this:

```
  node/endpoint  Matter device types   name   online  hub id             hub type    readings
  1/1            Extended Color Light  L535E  yes     home-1/matter-1-1  OnOffLight  -
  3/1            On/Off Plug-in Unit   P110M  yes     home-1/matter-3-1  OnOffPlug   energy, power
```

Copy the two binding lines into `home.env`, replacing any older ones, and edit each id and room
to say what the device really is:

```bash
LG_THINQ_BINDINGS='{"<LG device id>": {"porch_id": "home-1/air-purifier", "room": "Living room"}}'
MATTER_BINDINGS='{"1/1": {"porch_id": "home-1/entry-light", "room": "Entry"}, "2/1": {"porch_id": "home-1/living-room-lamp", "room": "Living room"}, "3/1": {"porch_id": "home-1/desk-lamp-plug", "room": "Office"}}'
```

- The purifier's suggested id, `home-1/air-purifier`, is the one the demo, the gateway and the
  dashboards already use.
- Name a plug after what it powers: `home-1/desk-lamp-plug` only for a plug that powers a desk
  lamp, `home-1/space-heater-plug` only for one that powers a heater.
- Every id must be different. The hub logs "two devices share the id" if two bindings repeat one.
- A Matter binding can carry two more things:
  - `"name": "Plug1"`: what your home calls the device. Without it a Matter device shows the
    name its node reports, usually the product's ("Smart Wi-Fi Plug"), the same for every such
    plug.
  - `"read_only": true`: the hub reads the device and never switches it. Use it for a plug whose
    load must stay on. Its watts still show on the energy panel, and it is left out of the
    family protection panel because there is nothing to open a window for.
- A Google Home binding can carry `"read_only": true` as well: the hub lists and reads the
  device and offers no command for it, whatever Google describes. Use it for a TV whose Play
  and Pause Google accepts but the TV ignores.
- A plug through Google Home is read only unless its binding says `"switchable": true`. Google
  says nothing about what a plug powers, so the hub switches one only when you say it may. A
  plug on both Matter and Google Home needs it on its Google binding too: when Matter cannot
  reach the plug (the Mac away from home), Google Home is the path that switches it.
- **The plug that powers the Wi-Fi router.** Keep it `"read_only": true` on both bindings, or
  make it switchable and map it to `"network"` in the gateway ([step 5](#5-start-porch)), so no
  agent switches it without you. Switching it off takes the whole home offline, the plug
  included: nothing can switch it back on from outside, only someone at home with the plug's
  own button.
- Run `discover.sh` again whenever you add a device. It keeps your bindings as they are and
  suggests ids only for the new ones.

## 5. Start Porch

Tell the gateway what each plug powers, in `~/.config/porch-hub/porch_config.json`. Its rules
are per capability: lights are `lighting` on their own and the purifier is an `appliance`, but a
plug that is not mapped is an `unknown_load`, and no agent may control it until you map it:

```json
"device_capabilities": {
  "home-1/desk-lamp-plug": "lighting",
  "home-1/living-wifi": "network"
}
```

The energy agent may use `lighting` and `appliance`; the concierge agent `lighting`, `heating`
and `entry`. A `heating` plug turns on only when someone is home. `network` is for the plug that
powers the router: an agent's On and Off for it both wait for your approval, and an Off says it
would take the home offline. An agent may ask for it only if its scopes include `network`, as
`deploy/home/agent.sh add` gives with `--scopes all`. Without the mapping that plug is an
`unknown_load`, and an agent may switch an unknown load off without asking.

Then, in another Terminal tab (the Matter Server keeps running in its own):

```bash
deploy/home/start.sh
```

It loads `home.env`, points the gateway at your `porch_config.json`, and starts the hub on port
8100 and the gateway on 8000. `home.env` sets `PORCH_REAL_DEVICES_ONLY=1`, so the hub lists only
your real devices, with none of the simulated demo devices mixed in.

**Or everything in one window:**

```bash
deploy/home/run_all.sh
```

It starts the Matter Server (when `home.env` uses one), waits for it, then starts the hub and the
gateway, and keeps the Mac awake while they run. Ctrl+C stops all of it. The Matter Server's own
output goes to `~/.config/porch-hub/logs/matter-server.log`.

Run Porch in the **Terminal app**, not in a terminal inside another app. macOS gives access to
your local network per app, which the Matter Server needs to reach the devices, and programs
started inside another app stop when that app quits. The first time, macOS asks whether Terminal
may find devices on your local network: allow it.

## Family protection

People live in this home, so a hub that runs real devices sends nothing to a device until you,
the owner, open a control window. `PORCH_REAL_DEVICES_ONLY=1` in `home.env` turns this on.

**Choose an owner PIN once**, in Terminal on this Mac. It asks twice and shows nothing as you
type; 6 characters or more:

```bash
deploy/home/owner.sh set-pin
```

**Then use the Family protection button**, next to **Pages** at the top of every hub page:

- Amber **FAMILY PROTECTION ON**: nothing can be switched. Click it, tick the devices, choose 5
  to 30 minutes, type the PIN, **Open control window**.
- Green **CONTROL WINDOW OPEN 4:12 · Living stand**: those devices take On and Off (a TV:
  Play and Pause) until the countdown ends. Click it to **Close now and protect** (no PIN), or **Change devices or time**
  (a new window replaces the open one, so it asks for the PIN again).
- When a page shows a refusal ("Family protection is on"), the **Open a control window** button
  beside it opens the same panel with that device already ticked. It is on the dashboard, the
  digital twin, the 3D home and the stage.

The same from Terminal, with no PIN (a command typed on this Mac is already the owner's):

```bash
deploy/home/owner.sh allow --device home-1/living-room-lamp     # for 5 minutes
deploy/home/owner.sh allow --device home-1/living-room-lamp --device home-1/air-purifier --minutes 15
deploy/home/owner.sh protect        # close the window now
deploy/home/owner.sh protection     # is a window open?
```

- A window names exact devices, lasts 1 to 30 minutes, and allows On and Off, and Play and Pause
  for a TV. One opened from a page names only the ones its devices take. From Terminal you can
  name other commands with `--command`.
- While it is closed, a command through the hub is refused with "Family protection is on" and
  nothing is sent. A dry run says the same, so the gateway tells an agent before trying.
  Reading devices always works.
- **Who can open a window:** someone with the PIN on a hub page, or someone at this Mac's
  Terminal. Agents have neither: the hub's route for it refuses without the PIN, and five wrong
  PINs lock it for a minute, then longer. Closing needs no PIN, because it can only make the
  home safer.
- **Keep the PIN to yourself.** Type it only into the panel. Do not put it in a chat, a prompt
  or a file an agent can read. The hub stores a salted hash of it
  (`~/.config/porch-hub/owner-pin.json`), never the PIN. To change it, run `set-pin` again.
- The window itself is the file `~/.config/porch-hub/permission.json`. A program running as you
  on this Mac could write that file.
- It covers commands sent through this hub. The devices' own apps are not affected, and a
  command already sent is not undone when the window closes.
- The hub does not know who is home. Open a window only after checking who is near the device.
- To turn it off, for a demo table with nobody around the devices, set
  `PORCH_FAMILY_PROTECTION=0` in `home.env` and restart.

**Always allowed devices.** If your own agents should be able to act while you are away from
the keyboard, name the devices they may switch at any time, with no window:

```bash
deploy/home/owner.sh always --device home-1/living-stand --device home-1/air-purifier
deploy/home/owner.sh always --none        # take it back
deploy/home/owner.sh protection           # says which devices are always allowed
```

- It covers On and Off for the named devices only (`--command` names others). Everything else
  still needs a control window.
- It is a standing choice, kept in `~/.config/porch-hub/always-allowed.json`. It is set only
  from this Mac's Terminal: no page, no PIN and no agent can add a device to it.
- Every hub page says so on its label, **FAMILY PROTECTION ON · 2 ALWAYS ALLOWED**, and the
  panel names the devices, so everyone at home can see what may switch by itself.
- The hub cannot tell who sends a command. An always-allowed device takes On and Off from any
  agent with the scope for it and from the hub's own pages. Choose devices that are safe to
  switch at any hour, with anyone in the room.

This sits under the gateway's rules, not in place of them. An agent still needs the scope, and
the gateway still asks you to approve what its rules hold for the owner.

## Your floor plan

The digital twin (http://127.0.0.1:8100/digital-twin) and the 3D home
(http://127.0.0.1:8100/home-3d) draw this home from one file,
`~/.config/porch-hub/home-layout.json`, named by `PORCH_HOME_LAYOUT` in `home.env`. Without it
both pages draw the demo home and list your devices beside it.

```bash
cp deploy/home-layout.example.json ~/.config/porch-hub/home-layout.json
```

The example is a two-floor townhome. Edit it to match your home:

- Lengths are in feet. Each floor has its own origin at its top-left corner: `x` runs right,
  `y` runs down the page.
- `rooms`: an `id`, a `name`, where it is and how big, and a `kind` that sets how it is drawn
  (`room`, `kitchen`, `bath`, `hall`, `store`, `outside`). `matches` lists the room names your
  devices carry in the hub that mean this room, for example Google Home's "Living Room".
- `fixtures`: `stairs`, `door`, `counter`, or a `label`.
- `placements`: where a device sits, by its Porch id: a room id and a spot inside the room as
  fractions of its width and depth (`"x": 0.5, "y": 0.5` is the middle). A device with no
  placement goes into the room that matches its room name. One that fits no room is listed
  beside the plan, never hidden.
- `extras`: things in the home the hub does not list, such as a dishwasher, drawn greyed with a note.

Reload the page after an edit; the hub reads the file each time. If the file has a mistake, the
pages draw the demo plan and say what is wrong. The file stays on this Mac, outside the
repository.

## Energy readings

The digital twin and the 3D home both show an energy panel: what each plug draws now, its
energy counter, and how its power moved since the hub started. It shows readings only.

- **Through Google Home a plug has no reading.** Google lists a Tapo P110M with on/off and
  nothing else, so the panel shows the plug as on or off and says there is no power reading.
- **Paired over Matter, the same plug reports watts and kWh, and volts and amps.** Do [step 2](#2-start-the-matter-server)
  and [step 3](#3-pair-each-matter-device-with-porch) for each plug, then run
  `deploy/home/discover.sh`: the plug's row shows the readings it gives. In `home.env`, turn on
  `MATTER_SERVER_URL` and give each plug's Matter binding the **same `porch_id`** as its Google
  Home binding. The hub then merges the two into one device, read and switched over Matter,
  with Google Home as the fallback.
- **Pairing also makes the plug switchable through Porch.** Family protection still applies, and
  the gateway treats a plug as an unknown load until you say what it powers
  ([step 5](#5-start-porch)). For the plug that powers the Wi-Fi router, choose in
  [step 4](#4-name-the-devices): read only, or switchable with the `network` guard.
- **Four identical plugs look alike over Matter.** Pair them one at a time in an order you note
  down, and give each binding the `"name"` you know it by.
- **The Matter Server has to be running** for the readings to arrive: start
  `deploy/home/matter_server.sh` in its own Terminal tab before `deploy/home/start.sh`. If it
  is not running, the hub still starts, keeps trying, and shows those plugs through Google
  Home meanwhile.
- The history is kept in the hub's memory for six hours and starts again after a restart.

## Everything a device reports

The hub's model of a device keeps what it acts on: on or off, the watts, the PM2.5. Each
platform knows more. Select a device on the digital twin or the 3D home and press
**Everything the platform reports**: the hub asks each platform that reaches the device for its
full record, now, and shows it by platform and by topic (`GET /devices/{id}/details`).

- **LG ThinQ:** the appliance's whole status, as LG words it (job mode, timers, filter life,
  each particle size).
- **Google Home:** every trait's state and the product's details (maker, model, versions); for
  the TV, its apps and remote keys too.
- **Matter:** what the node reports that a person can read: maker and versions, restarts and
  uptime, Wi-Fi channel and signal, what the plug does after a power cut, volts, amps, watts
  and kWh. Away from home it shows the last values heard, and says so.
- **Ring:** the doorbell's status, what it supports and its settings. Motion and privacy zones
  show as a count.

Serial numbers, unique ids, network names and addresses, fabrics, keys and tokens are never
shown, nor anything under them. It reads only: the panel has no command of its own. A platform
that does not answer in time gets a line saying so, and the others still show.

## 6. Check it end to end

1. Open http://127.0.0.1:8100/dashboard. Every device should have a green dot. Then
   http://127.0.0.1:8100/digital-twin: every device should sit in its room on your plan.
2. Open a control window for a bulb with the **Family protection** button
   ([Family protection](#family-protection)), then turn it on and off on the dashboard or by
   clicking it on the digital twin. Each command is confirmed by the device.
3. Check the whole path the way an agent uses it, with no AI needed. Switching a device needs a
   control window for it too:

   ```bash
   deploy/home/check.sh                           # reads only: devices, what each agent may do
   deploy/home/check.sh home-1/living-room-lamp   # also switches that one device and puts it back
   ```

   With a device id, an agent that may switch the device sends On and Off through the gateway.
   Each must come back allowed and verified by the device, and the device ends as it started. If
   no agent may switch it on its own (an unmapped plug), nothing is sent.
4. Read a plug's power: `curl -s http://127.0.0.1:8100/devices/home-1/desk-lamp-plug/state`.
   `ElectricalPowerMeasurement.ActivePower` is in milliwatts. If the plug has no such cluster, its
   firmware does not report energy over Matter yet.
5. Connect an agent to the gateway as in `porch_gateway/README.md`, for example Claude Code with
   the energy agent's key, and ask it to turn on the living room lamp. The gateway's terminal
   shows the decision, the command and the read-back.
6. The owner's side is `deploy/home/owner.sh`: `list`, `approve`, `decline`, `home`, `away`. To
   see an approval with only plugs in the home, add `"unknown_load"` to an agent's `scopes` in
   `porch_config.json` and restart: that agent's On for an unmapped plug then waits for
   `deploy/home/owner.sh approve`.

`porch_gateway/check_demo.py` and `owner.py reset`, `leak` and `dry` belong to the simulated demo:
they send the demo's commands by the demo's device ids, so they refuse to run when the config
says the hardware is not simulated.

The agent keys in `porch_config.json` are the demo's, fine on this Mac since the gateway listens
only on 127.0.0.1. Change them before the gateway is reachable from anywhere else.

The gateway on the `fix/gateway-review-followups` branch checks only the attributes a command is
meant to change. Merge it before relying on verification with energy plugs, whose power readings
change by themselves.

## Your own agents

A personal agent reaches every device in this home through Porch alone. It never signs in to LG,
Google Home, Ring or the Matter Server: the hub holds those connections, and the agent holds one
key, for Porch.

**Give an agent its own key and rights** (it is made on this Mac, kept in the gateway's settings
file and never printed):

```bash
deploy/home/agent.sh add dot --name Dot --acts-for Yoonchan
deploy/home/agent.sh list                 # the agents and what each may ask for; no keys
deploy/home/agent.sh remove dot           # take its key away
```

- Without `--scopes` the agent may ask for anything. The gateway still decides each request: a
  light or an appliance goes through, what the rules hold for you (a plug it cannot identify, a
  lock, the water valve) waits for `deploy/home/owner.sh approve`, and with family protection on
  nothing moves until you open a control window. `--scopes lighting sensor` narrows it.
- Restart Porch (`deploy/home/start.sh`) after `add` or `remove`: the gateway reads its settings
  when it starts.

**Connect a Claude on this Mac as that agent.** The app starts a small local program that passes
its calls to Porch with the agent's key. The key is read by that program, so the app's settings
name only the agent.

- Claude Code:

  ```bash
  claude mcp add porch-dot -- /full/path/to/device-hub/deploy/home/agent.sh connect dot
  ```

- Claude Desktop: say which agents live there, then let `desktop` add their connections:

  ```bash
  deploy/home/agent.sh set energy-agent --client "Claude Desktop"
  deploy/home/agent.sh desktop --check      # what it would change
  deploy/home/agent.sh desktop              # or double-click "Connect Agents to Claude Desktop.command"
  ```

  Claude Desktop reads its settings only when it starts, and writes its own copy back while it
  runs, so `desktop` asks you to quit it, adds one connection per agent (`porch-energy`
  for `energy-agent`, `porch-dot` for `dot`, ...), opens it again and waits until it has started them.
  It keeps a copy of the old settings beside them, and offers to remove a connection whose
  agent now lives in another app (`--client ChatGPT`). Each connection is this, with no key:

  ```json
  {"mcpServers": {"porch-energy": {
    "command": "/full/path/to/device-hub/porch_gateway/.venv/bin/python",
    "args": ["/full/path/to/device-hub/deploy/home/agents.py", "connect", "energy-agent"]}}}
  ```

One connection per agent, so each speaks to Porch under its own name. In each agent's chat,
switch on that agent's own connection in the chat's tools menu, and only that one. Connectors
added on claude.ai (`porch-concierge`, `porch-muse`) belong to your Claude account, so they show
up in Claude Desktop's chats too: a chat with two Porch connections on can read as one agent and
act as the other, and Porch then judges the request by the agent it came from. The app set
with `--client` is what the digital twin and the 3D home show beside each agent.

- **The connection tells the agent who it is.** When it connects, it gives the agent a short
  note: its name here, whose home this is, what each Porch tool does, and that it may say a
  device changed only when Porch answered that the action was executed and verified.
- **A Claude app asks a connection for its tools once, when the app starts.** The connection
  remembers the tools Porch offered last time, so an agent keeps them even if Porch was not
  running then. A call made while Porch is down answers that Porch is not running and that
  nothing was changed.
- **Check what really happened in Porch, not in the chat.** `deploy/home/owner.sh` lists what
  is waiting for you, and the gateway's window shows every call under the agent's name. If an
  agent says it switched something and Porch shows no request from it, it did not.

Then ask the agent what is in the home: it calls `describe_home` and gets
every device from every platform, with its state and what this agent may do. The gateway's
terminal shows each call under the agent's name, and its requests to act are written to the
audit log (`deploy/home/owner.sh` lists the ones waiting for you).

**Energy agents read, never control.** An agent set to read energy only sees the devices that
report power or energy, and only those readings; it has a `get_energy` tool for the whole home's
power and history, and every request to act is refused:

```bash
deploy/home/agent.sh set energy-agent --reads energy --scopes none
```

**The Energy agent itself** is two things, both reading through its `porch-energy` connection:

- **A chat you talk to.** In Claude Desktop, make a project named Energy agent, paste
  [`deploy/home/energy-agent-instructions.md`](../deploy/home/energy-agent-instructions.md) as its
  instructions (`pbcopy < deploy/home/energy-agent-instructions.md` puts it on the clipboard), and
  switch on `porch-energy` in its chats. Ask it what is using power, what was left on, how much a
  plug used today.
- **An hourly check that runs on its own:** a scheduled task in the Claude app ("Energy agent:
  hourly check", in the sidebar's Scheduled section). Each hour it reads the home's energy once,
  which the twins draw like any agent's read, and appends a short report to a dated file in an
  `Energy reports` folder beside the repository: each plug's watts and counter, what it used
  since the last entry, and flags (a stale or offline reading, a plug that is on but reads 0 W, a
  jump past twice its last reading, a counter that went back). It runs only while the Claude app
  is open; a run missed while it was closed runs when it opens. Change, pause or stop it in the
  app.

**Choose the devices each agent may control.** By default an agent may control any device its
rights allow. To give one only some devices, open **How Porch works**
(`http://127.0.0.1:8100/porch`), press **Choose devices** on its card, tick the devices, and save
with the owner PIN. Porch uses it from that agent's next request; no restart. Any other device
is then refused with the reason, for example "Muse may control only Living stand in this home, as
the owner chose." What the agent may read is not limited, and family protection still decides
whether a device it was given may move right now. From Terminal:

```bash
deploy/home/agent.sh devices muse home-1/living-stand   # only Living stand
deploy/home/agent.sh devices muse --any                 # any device its rights allow again
deploy/home/agent.sh list                               # shows "controls only ..." for each
```

The choice is kept in `~/.config/porch-hub/agent-devices.json`. If that file is damaged, Porch
controls nothing for anyone until it is fixed or removed.

## The household: whose room it is

Your agents act for you, your wife's agent for her. When someone else is in a room and the
agent's own person is not, the person in the room decides: Porch holds the request until they
say yes with their PIN. The rule runs both ways.

```bash
deploy/home/household.sh list                     # who lives here, where each is, who has a PIN
deploy/home/household.sh add wife --name "Wife"   # someone who lives here (rename any time)
deploy/home/household.sh set-pin wife             # she chooses her own PIN, typed hidden, twice
deploy/home/agent.sh set dot --person yoonchan    # whose agent each one is
```

- **Where people are is set by hand.** The home has no sensor that tells people apart. Open
  **How Porch works** (`http://127.0.0.1:8100/porch`) and use **Who is where**: a change needs
  that person's PIN, or the owner's. From Terminal, `deploy/home/household.sh where wife living`
  is the owner's own hand and needs none.
- **Unknown counts as away;** home with no room set counts as being in every room, so Porch asks.
- **Answering.** Every hub page shows **N REQUEST WAITING FOR A YES** in its top bar while Porch
  holds a request; click it to answer, with the PIN of the person asked (it also shows under
  **Waiting for an answer** on How Porch works). Only the person in the room can answer it; the
  owner's `owner.sh approve` cannot answer for them. An agent never can.
- **A yes is carried out at once.** Porch does the request itself, as the agent that asked, and
  the page says how it went. The agent learns it from `get_audit_log`, or from asking again.
- **The owner's yes passes family protection, once.** Your yes, with your owner PIN, lets that one
  command on that one device through, while Porch carries it out and never longer; no window is
  opened. Anyone else's yes does not: the request then waits for yours.
- **Family protection asks you, instead of only refusing.** A request Porch's rules allow, but that
  family protection stops, waits for your yes in the same place. Asking again does not add a
  second one. Decline it, or let it run out after 10 minutes, and nothing happens.
- Switching a heater off, locking a door and the emergency water shut-off are never held.

## Your agents in ChatGPT and on claude.ai

The connections above work only in apps on this Mac. ChatGPT, claude.ai and the Claude phone app
run on their makers' servers, so for them Porch has an **online door**: the only part of Porch
the internet reaches. It passes on requests to the agents' tools and nothing else, only with an
agent's **online key**, only while online access is on. Each request runs under the agent's own
Porch key, so the same rules apply. Requests are logged, without keys, to
`~/.config/porch-hub/logs/online-door.log`.

Neither app's connector form has a field for a key: ChatGPT offers OAuth or no authentication,
and claude.ai's request headers are a beta for some organizations only. So each agent gets its
own **connector address with its key in it**, `https://<this Mac>.<tailnet>.ts.net/k/<key>/mcp`,
and the connector is added with no sign-in. Keep that address private: whoever has it can ask
Porch for what that agent may ask for, under the same rules and family protection.
`agent.sh online-off <agent>` takes it back at once.

Once:

1. Install Tailscale on this Mac (https://tailscale.com/download/mac). Tailscale's pages disagree
   on which macOS version supports Funnel: start with the **Standalone** version, and if
   `online.sh on` fails, use the open-source one. Sign in with a free account.
2. Start Porch as usual (`Start Porch.command`): it also starts the door on this Mac.
3. Open the door. The first time, Tailscale prints a link to allow Funnel: open it, allow, run again.

   ```bash
   deploy/home/online.sh on
   ```

4. **Dot, in ChatGPT.** Put Dot's connector address on the clipboard (it is never shown):

   ```bash
   deploy/home/agent.sh online-key dot
   ```

   In ChatGPT on the web, turn on developer mode (Settings, Apps & Connectors, Advanced), then
   create a connector: name `Porch (Dot)`, the pasted address as its MCP server URL,
   authentication **No authentication**. In a chat, switch it on in the tools menu. ChatGPT asks
   before every `request_action`; the reading tools are marked read only. OpenAI's own pages
   differ on which plans may use tools that change things: if ChatGPT offers Dot only reading,
   that is the plan, not Porch.
5. **Muse, in the Claude phone app.** The same for Muse:

   ```bash
   deploy/home/agent.sh online-key muse
   ```

   In claude.ai: Settings, Connectors, **Add custom connector**. Name `porch-muse`, the pasted
   address as the URL, authentication **No sign-in**. Connectors belong to your Claude account,
   so the Claude phone app has it too. There, a project named Muse, with Muse's instructions, and
   `porch-muse` switched on in its chat make that chat Muse. An app that has no place to add a
   server (an App Store app of its own, for one) cannot reach Porch: its maker decides its tools.

Every day: Porch must be running on this Mac, awake, with the door open. `deploy/home/online.sh
off` closes it at once; `online.sh status` says whether it is open; `agent.sh online-off muse`
takes one agent's online key away (`online-key muse` makes a new address). An app that lets you
add a request header can use the door's plain address, `https://<this Mac>.<tailnet>.ts.net/mcp`,
with `Authorization: Bearer <key>` from `agent.sh online-key <agent> --header`.

## Demo Day: moving the home to the table

The run sheet for a table demo with two lamps reached through Google Home is
[DEMO_DAY.md](DEMO_DAY.md). The notes below are for devices paired over Matter.

- **Give the travel router the same Wi-Fi name, password and security type as home, on
  2.4 GHz.** The bulbs and plugs then join it on their own, and Porch's Matter pairing still
  works. A new Wi-Fi name means setting every device up again.
- Put the Mac on the same router. LG's API needs internet, so give the router a phone hotspot.
- Rehearse once at home (`home.env` sets `DIGITAL_COPY_PATH`). If the network fails, offline
  mode then runs from the real devices' last states, and every command answer says
  `simulated: true`.

## Not connected yet

- **Ring events, video and the Indoor Cam.** The doorbell is listed and read
  ([Ring doorbell](#ring-doorbell)), but a press or motion does not reach the hub. Ring sends
  `button_press` and `motion_detected` events by webhook, signed with HMAC-SHA256
  (https://developer.amazon.com/docs/ring/api-reference.html), so the hub would need an HTTPS
  address the webhook can reach. Linking a Ring account to a private Ring Partner app also has
  no tool in this repository yet.
- **Plugs through Google Home.** The hub only reads them on that path, and Google reports only
  whether each is on: no watts and no kWh ([Energy readings](#energy-readings)). Pair a plug
  over Matter to read its energy and to switch it.
- **Door lock.** There is none in this home, so the entry scenes (unlock held for the owner)
  need a Matter lock, or stay simulated and say so.
- **Setting bulb brightness and color.** Through Google Home the hub reads a bulb's brightness
  and white tone, and shows them. It cannot set them yet, and over Matter it maps only On/Off.
- **Other LG appliances.** The hub maps dryers, washers, robot and stick vacuums and air
  purifiers. LG also lists this home's dishwasher and two air conditioners. The dishwasher's
  profile is read only and would map like the stick vacuum; one air conditioner can be
  controlled through LG and the other LG does not offer at all ("not provided feature"). None
  has a profile in the hub yet, and `deploy/home/discover.sh` shows them as not used.

## When something breaks

| You see | Why | Do this |
| --- | --- | --- |
| `setup_mac.sh` stops at Node.js or uv | Missing or too old | `brew install node uv`, or `brew upgrade node` |
| Commissioning fails with `not-discovered` | The Mac and the device are on different networks, or the code expired | Same Wi-Fi for both; make a new code and paste it at once |
| Commissioning fails with `timeout` | The device was found but did not answer in time | Move the Mac closer, make a new code, try again |
| A bulb or plug missing from the hub | Not paired with Porch's Matter Server yet, or an endpoint type the hub does not map | Run `deploy/home/discover.sh`: it shows every endpoint and whether the hub uses it |
| Matter devices show offline | The Matter Server stopped, or the Mac slept | Run `deploy/home/matter_server.sh` again (same storage folder); keep the Mac awake |
| The plugs lose their watts and the energy panel says "No power readings"; the Matter Server's tab repeats `send EHOSTUNREACH 224.0.0.251:5353` | macOS is keeping the app that started the Matter Server off the home network. The internet routes (LG, Google, Ring) still work | System Settings > Privacy & Security > Local Network: switch on the app the Matter Server runs in (Terminal, or Claude). Then start the Matter Server again |
| A plug's watts are greyed with "No new reading since 20:05" | It draws 20 W or more and neither its power nor its counter has moved for 45 minutes: the hub is no longer hearing from it | Check the Matter Server's tab and the row above |
| After quitting the app the hub ran in, every page says the hub did not answer | Programs started in that app's terminal end with it | Start the Matter Server, then `deploy/home/start.sh`, again |
| `discover.sh` cannot reach the Matter Server | It is not running | Start it (step 2) |
| An agent is refused on a plug | The plug is not mapped in `porch_config.json` | Map it to what it powers (step 5) and restart `start.sh` |
| LG "API calls exceeded" | Too many calls in a short time | Wait, then start the hub once; it caches state and gets push updates |
| `PORCH_REAL_DEVICES_ONLY=1 needs one of MATTER_SERVER_URL, LG_THINQ_PAT, ...` | `home.env` sets no platform | Check `~/.config/porch-hub/home.env` |
| A command answers "Family protection is on" | No control window is open for that device | `deploy/home/owner.sh allow --device <id>` ([Family protection](#family-protection)) |
| LG answers "not allowed" (1305 or 1309) | The country does not match the LG account, or the token is wrong | Set `LG_THINQ_COUNTRY` to the account's country and paste the token once, whole |
| Google Home: "requires a Google Home Premium Advanced subscription" | Google's condition for its Home MCP server | Check the subscription on the account and home you signed in with |
| Google devices go offline and the hub logs "Google Home's device list could not be read" | The saved sign-in no longer works, or Google is unreachable. The hub asks again after 30 seconds, then less often | Sign in again: `deploy/home/connect.sh google` |
| `connect.sh google` says "Not ready" | A file or tool is missing | Follow the line it prints, then run it again |
| Ring: "Ring could not be read (HTTPStatusError, HTTP 401)" | The saved link no longer works, for example because another program used and replaced its token | Link again and run `deploy/home/connect.sh ring` |

Matter Server networking requirements (flat network, IPv6, multicast):
https://github.com/matter-js/matterjs-server/blob/main/docs/os_requirements.md
