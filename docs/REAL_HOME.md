# Connecting a real home

How to put real devices behind the Porch Device Hub, written for Yoonchan's home first. The same
steps work for any home with LG ThinQ appliances and Matter devices. Four scripts in
`deploy/home/` do the Mac side.

**Contents:** [What connects how](#what-connects-how) · [0. Set up the Mac](#0-set-up-the-mac) ·
[1. LG appliances](#1-lg-appliances) · [2. Matter Server](#2-start-the-matter-server) ·
[3. Pair the Matter devices](#3-pair-each-matter-device-with-porch) ·
[4. Name the devices](#4-name-the-devices) · [5. Start Porch](#5-start-porch) ·
[6. Check it](#6-check-it-end-to-end) · [Demo Day](#demo-day-moving-the-home-to-the-table) ·
[Not connected yet](#not-connected-yet) · [When something breaks](#when-something-breaks)

## What connects how

| Device | Path into the hub | What agents can do through Porch |
| --- | --- | --- |
| LG PuriCare 360 | LG's official ThinQ Connect API (cloud, with push updates) | On, Off. Read air quality, PM2.5, fan mode |
| Tapo L535E bulbs | Matter over Wi-Fi, through Porch's own Matter Server on the Mac | On, Off, Toggle. Brightness and color are not mapped yet |
| Tapo P110M plugs | Matter over Wi-Fi, same Matter Server | On, Off, Toggle. Read power (mW) and energy (mWh) when the plug reports them over Matter |
| LG cordless stick vacuum | LG lists it, but the hub has no profile for it (LG's API has no controls for it) | Nothing yet |
| Ring doorbell and Indoor Cam | No driver yet; see [Not connected yet](#not-connected-yet) | Nothing yet |
| Google TV Streamer | Not a Porch device. It is Google Home's hub | |

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

## 4. Name the devices

Matter has no rooms, and LG's ids say nothing about where a device is. Bindings give each device
a Porch id and a room. List what the platforms report (this only reads):

```bash
deploy/home/discover.sh
```

It prints a table for LG (each appliance's full device id and the id the hub gives it) and one
for the Matter Server (every endpoint, which Porch device it becomes, and which plugs report
power and energy), then a binding line for each. On a first run it looks like this:

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
- Run `discover.sh` again whenever you add a device. It keeps your bindings as they are and
  suggests ids only for the new ones.

## 5. Start Porch

Tell the gateway what each plug powers, in `~/.config/porch-hub/porch_config.json`. Its rules
are per capability: lights are `lighting` on their own and the purifier is an `appliance`, but a
plug that is not mapped is an `unknown_load`, and no agent may control it until you map it:

```json
"device_capabilities": {
  "home-1/desk-lamp-plug": "lighting"
}
```

The energy agent may use `lighting` and `appliance`; the concierge agent `lighting`, `heating`
and `entry`. A `heating` plug turns on only when someone is home.

Then, in another Terminal tab (the Matter Server keeps running in its own):

```bash
deploy/home/start.sh
```

It loads `home.env`, points the gateway at your `porch_config.json`, and starts the hub on port
8100 and the gateway on 8000. `home.env` sets `PORCH_REAL_DEVICES_ONLY=1`, so the hub lists only
your LG and Matter devices, with none of the simulated demo devices mixed in.

## 6. Check it end to end

1. Open http://127.0.0.1:8100/dashboard. Every device should have a green dot.
2. On the dashboard, turn a bulb on and off. The latency log shows each command confirmed by the
   device.
3. Read a plug's power: `curl -s http://127.0.0.1:8100/devices/home-1/desk-lamp-plug/state`.
   `ElectricalPowerMeasurement.ActivePower` is in milliwatts. If the plug has no such cluster, its
   firmware does not report energy over Matter yet.
4. Connect an agent to the gateway as in `porch_gateway/README.md`, for example Claude Code with
   the energy agent's key, and ask it to turn on the living room lamp. The gateway's terminal
   shows the decision, the command and the read-back.

The agent keys in `porch_config.json` are the demo's, fine on this Mac since the gateway listens
only on 127.0.0.1. Change them before the gateway is reachable from anywhere else.

The gateway on the `fix/gateway-review-followups` branch checks only the attributes a command is
meant to change. Merge it before relying on verification with energy plugs, whose power readings
change by themselves.

## Demo Day: moving the home to the table

- **Give the travel router the same Wi-Fi name, password and security type as home, on
  2.4 GHz.** The bulbs and plugs then join it on their own, and Porch's Matter pairing still
  works. A new Wi-Fi name means setting every device up again.
- Put the Mac on the same router. LG's API needs internet, so give the router a phone hotspot.
- Rehearse once at home (`home.env` sets `DIGITAL_COPY_PATH`). If the network fails, offline
  mode then runs from the real devices' last states, and every command answer says
  `simulated: true`.

## Not connected yet

- **Ring doorbell and Indoor Cam.** Ring has an official developer API
  (https://developer.amazon.com/docs/ring/get-started.html). Registering includes an ID check,
  and an app can be built and tested with your own Ring account before Ring's review. It sends
  `button_press` and `motion_detected` events by webhook, signed with HMAC-SHA256
  (https://developer.amazon.com/docs/ring/api-reference.html), so the hub would need an HTTPS
  address the webhook can reach. No driver exists yet.
- **Door lock.** There is none in this home, so the entry scenes (unlock held for the owner)
  need a Matter lock, or stay simulated and say so.
- **Bulb brightness and color.** The bulbs report Level Control and Color Control, but the hub
  maps only On/Off so far.
- **Stick vacuum.** LG's SDK has no commands for stick cleaners. A read-only profile (battery,
  status) would let agents see it and be told it cannot be started.

## When something breaks

| You see | Why | Do this |
| --- | --- | --- |
| `setup_mac.sh` stops at Node.js or uv | Missing or too old | `brew install node uv`, or `brew upgrade node` |
| Commissioning fails with `not-discovered` | The Mac and the device are on different networks, or the code expired | Same Wi-Fi for both; make a new code and paste it at once |
| Commissioning fails with `timeout` | The device was found but did not answer in time | Move the Mac closer, make a new code, try again |
| A bulb or plug missing from the hub | Not paired with Porch's Matter Server yet, or an endpoint type the hub does not map | Run `deploy/home/discover.sh`: it shows every endpoint and whether the hub uses it |
| Matter devices show offline | The Matter Server stopped, or the Mac slept | Run `deploy/home/matter_server.sh` again (same storage folder); keep the Mac awake |
| `discover.sh` cannot reach the Matter Server | It is not running | Start it (step 2) |
| An agent is refused on a plug | The plug is not mapped in `porch_config.json` | Map it to what it powers (step 5) and restart `start.sh` |
| LG "API calls exceeded" | Too many calls in a short time | Wait, then start the hub once; it caches state and gets push updates |
| `PORCH_REAL_DEVICES_ONLY=1 needs MATTER_SERVER_URL or LG_THINQ_PAT` | `home.env` has neither set | Check `~/.config/porch-hub/home.env` |

Matter Server networking requirements (flat network, IPv6, multicast):
https://github.com/matter-js/matterjs-server/blob/main/docs/os_requirements.md
