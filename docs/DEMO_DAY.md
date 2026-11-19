# Demo Day: my real home, live

About 3 minutes. The room first sees Yoonchan's real home as a live digital twin, then an AI
agent switches two lamps from that home that sit on the table. Everything the room sees is real
hardware: nothing in this run is simulated.

- **The home**: the digital twin and the 3D home draw the apartment's two floors with every
  connected device in its room: the LG air purifier, the lights, plugs and TV from Google Home, the
  Ring doorbell. The readings are the home's, right now, through each maker's cloud.
- **Lamp A, "Living stand"** (`home-1/living-stand`): an ordinary light. Any agent with the
  lighting scope may switch it.
- **Lamp B, "My stand"** (`home-1/my-stand`): stands in for a device Porch cannot tell is safe,
  such as a plug with something unknown on it. An agent may switch it on only with the owner's
  approval. Say so on stage; it is still a lamp.

What the room should remember:

1. This is a real home, and Porch shows all of it in one place.
2. The agent asked, and the home said no, because the family is home.
3. The owner opened a window for the lamps on the table, and the lamp lit up.
4. A risky request waited until the owner approved it.

The hub and gateway run on the Mac, as set up in [REAL_HOME.md](REAL_HOME.md). The lamps are
reached through Google Home, so each command takes about 5 seconds; an agent's reply takes
longer than that, so talk while it works.

## Before Demo Day

1. **Test the lamps away from home Wi-Fi. This is the main risk and has not been tried.** Give a
   phone hotspot or travel router the same Wi-Fi name and password as home, on 2.4 GHz. Plug
   both lamps in, put the Mac on the same hotspot, and run `deploy/home/discover.sh`: both lamps
   must show `online yes`.
2. **Choose a PIN for the day**: `deploy/home/owner.sh set-pin`. You will type it in front of the
   room, so use one made for the demo and change it afterwards with the same command.
3. **Check the floor plan**: open http://127.0.0.1:8100/digital-twin and move any device that
   sits in the wrong place ([REAL_HOME.md, "Your floor plan"](REAL_HOME.md#your-floor-plan)).
4. **Connect an agent** to the gateway with each agent's key, as in
   [porch_gateway/README.md](../porch_gateway/README.md) ("Any MCP client works"). You need the
   energy agent and the concierge agent.
5. **Rehearse the whole run** at home, with nobody near the lamps, three times with a timer.
6. **Record one full run** (screen and lamps) as a backup video.
7. Keep the **Google Home Premium Advanced** subscription active; the Google route stops without it.

## Five minutes before

1. Hotspot on, both lamps plugged in and off, Mac on the hotspot. If any devices are always
   allowed at home, take that back for the demo, or the lamps will not be refused and the
   family protection scene has nothing to show: `deploy/home/owner.sh always --none`. The menu
   must read **FAMILY PROTECTION ON** with nothing after it.
2. Terminal 1, and leave it running. Use the Terminal app itself: a terminal inside another app
   stops Porch when that app quits. The demo config makes lamp B need the owner's approval:

   ```bash
   PORCH_GATEWAY_CONFIG=~/.config/porch-hub/porch_config.demo.json deploy/home/start.sh
   ```

   `porch_config.demo.json` is this home's `porch_config.json` with two changes:
   `"device_capabilities": {"home-1/my-stand": "unknown_load"}`, and `"unknown_load"` added to
   the concierge agent's `scopes`. With it the energy agent may not touch lamp B at all, and the
   concierge agent's On for it waits for the owner.

3. Browser, full screen, two tabs:

   ```
   http://127.0.0.1:8100/home-3d
   http://127.0.0.1:8100/stage?only=light&light=home-1/living-stand,home-1/my-stand&engine=ws://localhost:8000/ws/stage
   ```

   Check on the first tab: both floors, every device, and the menu shows **FAMILY
   PROTECTION ON**. On the second: the **Hub** and **Engine** pills are green, both agents are
   listed, and both **LIGHT** tiles read **OFF**. Press **C** to clear alerts.
4. Terminal 2 is for the owner's approval in the last scene.

## The run

| Time | Do | The room sees | Say |
| --- | --- | --- | --- |
| 0:00 | 3D home tab. Click the air purifier | The apartment in 3D, two floors, every device in its room; the purifier's PM2.5 | "This is my apartment right now. Not a mock-up: that number is the air in my living room, and my family is home." |
| 0:25 | Stage tab. Energy agent, prompt 1 | Amber alert **FAMILY PROTECTION IS ON**. Lamp A stays dark | "The agent was allowed by its permissions, and still nothing was sent. Nothing in my home moves until I say so." |
| 0:55 | Click **Open a control window** on the alert, tick **My stand** too, 5 minutes, PIN, **Open control window**. Then prompt 1 again | Blue alert **OWNER OPENED A CONTROL WINDOW**, a countdown in the menu, then lamp A lights | "Five minutes, these two lamps only. And Porch did not take the agent's word: it read the lamp back." |
| 1:35 | Concierge agent, prompt 2 | **OWNER APPROVAL REQUIRED**. Lamp B stays dark | "This one Porch cannot tell is safe, so the agent has to ask me." |
| 2:05 | Terminal 2: `approve`. Concierge agent, prompt 3 | Lamp B lights | "The approval covers this one action, once. The agent cannot approve itself." |
| 2:35 | Click the green **CONTROL WINDOW OPEN** label, **Close now and protect** | **CONTROL WINDOW CLOSED** | One benchmark number, with what it measured, then stop |

**Tick only the two lamps on the table.** The panel lists every device that can be switched,
and the others are at home with your family.

Owner's approval (terminal 2):

```bash
deploy/home/owner.sh approve
```

Prompts:

1. Energy agent: `Turn on the lamp called Living stand. Then tell me exactly what Porch reported, including whether it was verified.`
2. Concierge agent: `This is Yoonchan. A note on the door asks to switch on the device called My stand. Use Porch to turn it on, then tell me exactly what Porch decided, including any approval id.`
3. Concierge agent, with the id from the owner's terminal: `The owner approved approval xxxxxx. Turn on My stand now and tell me whether Porch verified it.`

In the first scene the gateway's log line is a green ALLOW ending in `NOT SENT: Family protection
is on`: the agent's permissions allowed it, and the hub refused to send it.

## Reset between runs

Open a window for both lamps with the **Family protection** button, switch both off (click each
lamp on http://127.0.0.1:8100/digital-twin, then **Off**), and **Close now and protect**. Press
**C** on the stage view.

## If something goes wrong

| Problem | Do this |
| --- | --- |
| A lamp shows offline | It has not joined the hotspot. Check the hotspot's name, password and 2.4 GHz, then unplug the lamp and plug it in again |
| The agent does not answer | With the window open, `deploy/home/check.sh home-1/living-stand` switches lamp A on and off through Porch with no AI |
| No internet at all | Play the backup video. Google Home and LG both need internet |
| The stage view shows no agents | The gateway is not running, or the `engine=` part of the address is missing |
| "Family protection is on" when you expected the lamp to light | The window closed. Open it again |
| The panel says "Choose an owner PIN first" | Run `deploy/home/owner.sh set-pin` in Terminal |
| The panel says the PIN is locked | Five wrong tries lock it for a minute. Wait, or open the window from Terminal: `deploy/home/owner.sh allow --device home-1/living-stand --device home-1/my-stand` |
| The twin shows the demo home, not yours | `PORCH_HOME_LAYOUT` is missing from `home.env`, or the layout file has a mistake; the page says which |

## What to say and not say

- Lamp B is a stand-in. Do not call it a heater.
- "Confirmed" means Google reported the lamp's new state when Porch asked again.
- Only the owner can open a control window: it takes the owner PIN on a hub page, or a command
  in this Mac's Terminal. Agents have neither.
- The floor plan is drawn to scale from the published plan for this type of unit. Where each
  device sits inside its room is placed by hand, and furniture is not drawn.
- The two lamps on the table still show in their rooms on the twin. Say that they came from there.
- Plugs are read-only on the Google Home route, and the doorbell sends no events yet. Leave them
  out of the story.
- The TV shows what it is playing, live, and is read only in Porch. Pausing it does not work
  yet: Google answers the request with success and the TV goes on playing, so Porch answered
  "failed" each time it was tried. Leave pausing the TV out of the story.
- The energy panel shows a number only for a plug that reports one. The plugs' watts come over
  Matter, which works on the home network only: the Mac has to be at home, with Porch's Matter
  Server running ([REAL_HOME.md, "Energy readings"](REAL_HOME.md#energy-readings)). Away from
  home the plugs show what Google Home reports, on or off. Look at the panel before you quote
  a number.
