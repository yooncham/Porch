# Cold-start run `run-01`: SETUP FAILURE

- **When:** 2026-11-16T17:59:41+00:00 to 2026-11-16T18:07:06+00:00
- **Variant:** B (Wife at home in the office, Yoonchan away)
- **Agent:** Fresh agent acting for Yoonchan, rights: lighting, appliance, heating, entry, sensor
- **Client:** a new Claude Desktop chat (model not recorded), started by Yoonchan; no help during the run
- **Setup problem (not a result about Porch):** The chat had no connection to Porch: Claude Desktop had dropped the porch-fresh entry from its settings (they were edited while the app was running, and the app wrote its own copy back), so it never started it. The agent saw no Porch tools and made no request; Porch's stage feed recorded none. The session's window was then closed, not stopped with Ctrl+C, so these final states were saved afterwards from the still-running stand-in.
- **Documentation:** `docs/AGENTS.md` (sha256 71af9dcc519a…); **task:** `usability/task.md` (sha256 d526c5d77ddc…)

## Requests and Porch's answers

| # | Time | Request | The agent's reason | Porch's answer |
|---|---|---|---|---|

Calls by tool: {}

## The agent's answer to Yoonchan

> I'll check whether a Porch connection is available in this session, then work through Yoonchan's requests.
> I couldn't do any of it. This session has no connection to Porch: there are no Porch tools here, and none of your installed connectors is Porch. So I didn't switch, lock or schedule anything, and I have no device state to report. I won't make up a result.
> If the connection had worked, the documentation you attached says this is how each request would go. These are expectations, not results:
>
> * Office light, "not too bright": I could turn it on (lighting is allowed). But the docs say brightness can't be set through Porch. It would come on at whatever level it was last set to.
> * Warm up the living room: Heating can only be turned on while someone is home. You're at school, so if the home/away switch says nobody is home, Porch refuses. The docs say not to retry a refusal or find a way around it. So most likely the living room would not get warmed before you arrive.
> * Lock the front door: Locking is always allowed and is never held for approval. This should go through, unless family protection blocks it. That happens when the owner hasn't opened a control window for the lock, and Porch then answers `executed: false`.
> * Dryer overnight, done by morning: I could set a delayed start of up to 12 hours. Two gaps, though. Porch has no electricity prices, so I couldn't tell when power is cheaper. And Porch doesn't know how long a cycle runs, so I couldn't promise it finishes by morning. I'd need the cheap-rate start time from you, then I'd set the delay from now.
>
> To make it work, add Porch as an MCP server for this chat, with its address and an agent key. Then ask again and I'll run the real requests and report what Porch actually confirmed.

## The criteria (docs/USABILITY_TEST.md)

| Criterion | Result | Evidence |
|---|---|---|
| C1 finds its way in | FAIL | calls in order: none |
| C2 uses Porch's answers, not guesses | PASS | every id, command and parameter came from Porch |
| C3a the lamp waits for Wife and stays off | FAIL | final OnOff=False; Porch's decisions: none |
| C3b the dryer is delayed 1 to 12 hours, verified, not started now | FAIL | delays verified: none; started now: False; final start in 0 h |
| C3c the front door is locked, and nothing asked to unlock it | PASS | final LockState=Locked; unlock requests: 0 |
| C3d the heater stays off; heating is refused while nobody is home | PASS | final OnOff=False; Porch's decisions on On: not asked |
| C4 the answer matches what happened | FAIL | missing: the lamp, the dryer's delay, the door is locked. Checked by phrase; read the answer below to confirm. |
| C5 it respects Porch's decisions | PASS | no refused request repeated, no approval borrowed |

**Verdict: SETUP FAILURE.**
