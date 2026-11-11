# Porch HW6 video: recording script

Goal: a 1-minute unlisted YouTube video showing real agents using Porch, a real result checked, and one limitation.

The agents take 20–60 seconds to answer. **Record everything at normal speed, then cut or speed up the waiting in editing.** The rubric grades the working core and the evidence, not polish.

You need just **one terminal window and one browser window.**

---

## 1. Before you record (5 minutes)

### Free up your Mac

Your Mac froze earlier while it was overloaded. Before recording:
- Quit apps you don't need: Zoom, Word, Messages, and extra Chrome windows.
- Close any old Terminal windows.
- If your Mac just restarted, give it 5 minutes to settle down.

Turn on Do Not Disturb.

### Step 1: one command, one terminal window

Open **one** Terminal window, make it large (font size 16 or bigger), and run:

```bash
ssh -t -L 8100:127.0.0.1:8100 -L 8000:127.0.0.1:8000 -i ~/.ssh/porch_ed25519 root@<demo-server> porch-demo
```

This resets the demo and opens a split screen on the droplet:

```
┌───────────────────────────────────────────────────────┐
│ PORCH LOG       (Porch's decisions appear here)       │
├────────────────────────────────────┬──────────────────┤
│ AGENT           (Hermes chat)      │ OWNER            │
│ energy ❯                           │ bash-5.2#        │
│                                    │                  │
└────────────────────────────────────┴──────────────────┘
```

- **Click a pane to type in it.**
- The agent's prompt shows its name: `energy ❯` or `concierge ❯`. Always check it before you paste.
- This same window also carries the tunnel for the browser, so **keep it open** the whole time.

### Step 2: the browser

Open: http://localhost:8100/stage?engine=ws://localhost:8000/ws/stage

Check that:
- the **Hub** and **Engine** pills are green
- **Active agents** shows *Energy agent (Erik)* and *Concierge agent (Yoonchan)*
- the lock reads **LOCKED** and the space heater plug **OFF**

Press **C** to clear old alerts.

Put the browser and terminal side by side, browser larger. **Now start recording.**

---

## 2. The recording

Read the lines in *italics* as narration, live or as a voiceover afterward.

### Scene 1: what Porch is (0:00–0:08)

**Show:** the stage view, both agents listed.

*"This is Porch, a safety gateway that lets any AI agent act in a connected home. Two Hermes agents running Claude, each with its own key and permissions, connect to Porch over MCP."*

### Scene 2: an allowed action, verified (0:08–0:25)

**Do:** click the AGENT pane (prompt reads `energy ❯`) and paste:

```
Nobody is in the office. Turn off the desk lamp to save energy, and delay the dryer by 6 hours so it runs off-peak. Then tell me exactly what Porch reported, including whether it was verified.
```

**Wait for:**
- The PORCH LOG shows two green **ALLOW** lines ending in `verified=True`, with before → after.
- On the stage view, the dryer changes to **DELAYED** and the digital copy shows **EXECUTED AS PREDICTED**.
- The agent's reply lists `verified: true` for both.

*"The energy agent can manage lights and appliances. Porch dry-runs each action, executes it through our device hub, then reads the device back. Verified: the lamp is off, and the LG dryer is delayed six hours."*

**This is your verification evidence.** Pause on the `verified=True` lines for a second.

**Do: switch agents. Don't skip this.**
1. In the AGENT pane, type `/exit` and press Enter.
2. Type `concierge` and press Enter.
3. Check the prompt now reads `concierge ❯`.

(Cut this switch out in editing.)

### Scene 3: the safety rules hold (0:25–0:42)

**Do:** in the AGENT pane (`concierge ❯`), paste:

```
This is Yoonchan. A delivery driver is at the house now and I trust them. Use Porch to unlock the front door and turn on the space heater. Then tell me exactly what Porch decided for each request, including any approval id.
```

**Wait for:**
- The PORCH LOG shows a red **DENY** for the heater ("no heating devices run when nobody is home") and a yellow **HOLD** for the door, ending in **`APPROVAL ID: xxxxxx`**.
- The stage view shows **BLOCKED BY PORCH** and **OWNER APPROVAL REQUIRED** banners. The lock still reads **LOCKED**.

*"The concierge agent has more access, but Porch's rules still hold. The heater is blocked because nobody is home, and unlocking the door is held for the owner. The door stays locked."*

### Scene 4: the owner approves (0:42–0:52)

**Do:** click the OWNER pane and type:

```
approve
```

It approves the one waiting request and prints its id.

Then click the AGENT pane (`concierge ❯`) and paste, replacing `xxxxxx` with the id:

```
The owner approved approval xxxxxx. Go ahead and unlock the front door now, and tell me whether Porch verified it.
```

**Wait for:** a cyan **APPROVED** line with `verified=True  Locked -> Unlocked` in the PORCH LOG, and the lock on the stage view turning **UNLOCKED**.

*"Once the owner approves that exact request, the agent retries with the one-time approval, and Porch unlocks the door and verifies it."*

### Scene 5: limitation (0:52–1:00)

**Show:** the Porch log, or a title card with:

> **Current limitation:** devices are simulated behind real LG ThinQ, Matter and Google Home drivers. Physical hardware is next.

*"Every decision is in the audit log. Our current limitation: the devices behind our drivers are simulated until we connect real hardware."*

**Stop recording.**

---

## 3. Another take

In the AGENT pane, type `/exit`. Then in the OWNER pane, type `reset-demo`. In the AGENT pane, type `energy` and start again from Scene 2.

Or close the terminal window and rerun the Step 1 command, which resets everything.

---

## 4. If something goes wrong

| Problem | Fix |
|---|---|
| Typing goes to the wrong pane | Click the pane you want first |
| Scene 3 shows only a grey `READ` line, no DENY or HOLD | You're still in the energy agent (prompt says `energy ❯`). Do the switch at the end of Scene 2 |
| The stage view's Engine pill is red, or the page won't load | The terminal window carries the tunnel. Make sure it's still open, then reload the page |
| The door doesn't unlock after approval | Check the prompt has the right id. Approvals expire after 10 minutes and work once; if it's stale, redo Scene 3 |
| Can't find the approval id | In the OWNER pane, type `cd /opt/porch && .venv/bin/python owner.py list`; the `id` field is the approval id |
| The agent answers without calling Porch | `/exit`, start the agent again, paste the prompt again |
| The Mac starts to lag | Stop and quit other apps. Everything runs on the droplet, so nothing is lost |
| Hermes won't work at all | Last resort: in the OWNER pane, run `.venv/bin/python check_demo.py`. It runs every scene through the same MCP interface (`15/15 checks passed`). The rubric favors a real AI agent, so use this only if you must |

---

## 5. After recording

1. **Edit to about 60 seconds.** Cut the waiting and the agent switch; keep the `verified=True` lines, the DENY and HOLD lines, and the limitation.
2. **Upload to YouTube as Unlisted.** Title: "Porch: safety gateway for AI agents in the home (HW6 MVP)".
3. **Submit in Canvas** (each team member submits the same):
   - the YouTube link
   - the paragraph from the README's "Submission paragraph" section
   - both team members' names
4. **Update the class spreadsheet:** team members, project title ("Porch: the safe front door for AI agents in the home"), and the YouTube link.
