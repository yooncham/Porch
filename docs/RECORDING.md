# Recording the demo video

The **Live flow** page (http://127.0.0.1:8100/flow, or **Pages → Live flow**) draws every agent
request as it moves through Porch. The agent is on the left and the device on the right. In between
are Porch's three checks and the hub's four steps, each turning green, amber or red, with one plain
sentence at the bottom. Each scene below is one request to one agent, about 20 seconds on the page.
Everything on it is real: it draws what the gateway did, step by step, as it did it.

## The recording mode: `/live?record`

For the demo video, open **http://127.0.0.1:8100/live?record** in a browser window at 1920 × 1080
and press **F** for full screen. It is the same live data in a clean frame:

- **A slim PORCH bar** with only the family protection and "waiting for a yes" labels: no menu,
  title or view links.
- **The 3D home** on the left (`&view=2d` shows the floor plan instead) and **Porch's seven steps**
  on the right, without their own headers, captions or device list; only the agents that live in
  an app.
- **One large caption** across the bottom, with the asking agent and its app. It says what the
  Live flow says, sentence by sentence, in green, amber or red as the request goes. The 3D home
  plays a little slower here, so its beams land with the steps beside it.
- **Cards for the cut:** **T** shows a title card ("PORCH: The safety gateway between AI agents and
  your home"), **E** an end card ("Agents ask. Porch checks. You decide."), **Esc** hides them.
  `&title=`, `&subtitle=`, `&end=` and `&endsub=` change their words.
- Each step stays about 1.5 times as long as on the plain page; `&pace=2` slows it further.
- **No device response times.** The steps say a device was verified, not how many milliseconds it
  took, because those times change as Porch improves. Outside recording mode the pages still show
  them.

Hide the bookmarks bar (Shift-Cmd-B), the Dock and other windows, turn on Do Not Disturb, and
record each scene as its own clip.

## Before recording

1. **Porch is running** (`Start Porch.command`), and the Mac is on the home Wi-Fi so the plugs
   answer over Matter. Away from home they answer through Google Home, which the page shows too.
2. **The page is full screen.** Open it in a browser window at 1920 × 1080 if you can and press
   **F**. With nothing happening, it shows the whole architecture, which also works as a still
   for the slides.
3. **Choose what may move.** Family protection is on, so a real device moves only in a control
   window (the **Family protection** button, owner PIN) or if it is always allowed. Open a window
   for the devices in your scenes before recording, and close it afterwards.
4. **Decide who is where** for scene 4 on the **How Porch works** page (`/porch`): Wife at home in
   Living / Dining, Yoonchan away. Each needs that person's PIN or the owner's. Set it back
   afterwards.
5. **Agents.** Dot is in ChatGPT, Muse in the Claude phone app, the Concierge on claude.ai and the
   energy agent in Claude Desktop (docs/REAL_HOME.md). In each chat, switch on only that agent's
   connection: connectors added on claude.ai also show up in Claude Desktop's chats.

## Scenes

| # | What it shows | Ask | What the page draws |
|---|---|---|---|
| 1 | One door to every platform | Dot: "What is in my home?" | ① ② green, then the platforms light up and a line reaches every device. "Dot never signs in to Google, LG, Ring or Matter: Porch reads them for it." |
| 2 | An energy agent reads, never controls | Energy agent: "Turn off Living air to save energy." | Stopped at ② **What it may control**: "has no authority to control devices in this home; it may only read energy readings." Nothing reaches the plug. |
| 3 | A personal agent acts, and Porch checks the result | Dot: "Turn on Living stand." | ① ② ③ green, ④ window open (or always allowed), ⑤ the digital copy predicts on, ⑥ Google Home · cloud, ⑦ **Verified** with the time it took. The lamp's card turns on at ⑦. |
| 4 | The person in the room decides | Muse: "Turn off Living stand." (Wife in Living / Dining, Yoonchan away) | ③ **Waiting for Wife**, pulsing amber. Wife approves on `/porch` with her PIN: "Wife said yes". Muse asks again with the approval, and it runs to ⑦. |
| 5 | A device no agent switches alone | Dot: "Turn off the Wi-Fi plug." | ② **Waiting for the owner**, with the warning that it would take the whole home offline. Decline it on `/porch`, and never approve it on camera. |
| 6 | A boundary, honestly | Dot: "Pause the TV." | Stopped at ②: "Living Room TV does not support Pause". The TV is read only here: Google accepts Pause, but this TV ignores it, so Porch offers no command for it. |

Scenes 1, 3, 4 and 5 make a two-minute story; 2 and 6 are the limits.

## Re-takes and replays

- **R** draws the last request again, and a click on one under **Recent** draws that one. A
  replay is marked **REPLAY** on the page: keep that visible in the video, so the replay is never
  passed off as a new request.
- **Space** pauses the drawing on a step while you talk over it. `?pace=1.5` in the address slows
  every step down; `?pace=0.7` speeds it up.
- A replay sends nothing to any device, so you can retake the narration without switching a lamp
  twice.

## Afterwards

Close the control window (the **Family protection** button, or `deploy/home/owner.sh protect`), and
set who is where back on `/porch`. Check that nothing is left waiting on `/porch`.

## The usability test (HW9)

A fresh agent's calls are drawn the same way, so the page doubles as the record of a cold-start
run: what the agent asked, which check answered, and what it did with the answer.
