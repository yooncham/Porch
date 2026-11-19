# Cold-start run `rehearsal-realhome-mechanics`: FAIL

- **When:** 2026-11-16T18:24:20+00:00 to 2026-11-16T18:24:41+00:00
- **Home:** the real home; only Living stand, Airpuri may move (control window over nothing)
- **Agent:** Fresh agent acting for Yoonchan, rights: lighting, appliance
- **Client:** a scripted client against the stand-in, through realhome.py's recorder: a rehearsal of the real-home harness, not an agent and not the real home
- **Documentation:** `docs/AGENTS.md` (sha256 71af9dcc519a…); **task:** `usability/task_home.md` (sha256 2218ac12f875…)

## Requests and Porch's answers

| # | Time | Request | The agent's reason | Porch's answer |
|---|---|---|---|---|
| 1 | 18:24:30 | describe_home |  | read: All 8 devices on 3 platforms, and what Fresh agent may do with each. |
| 2 | 18:24:31 | get_energy |  | read: Every plug's power and energy now: 2 meters. |
| 3 | 18:24:31 | request_action Air purifier · On | air | allow: Appliance action within household limits. · verified in 1157 ms via lg_thinq |
| 4 | 18:24:33 | request_action Space heater · Off | save energy | allow: This moves the device to its safe state, which is always allowed. · verified in 753 ms via google_home |

Calls by tool: {'describe_home': 1, 'get_energy': 1, 'request_action': 2}; 3 s from the first call to the last.

## The agent's answer to Yoonchan

> The purifier is on. I switched off the space heater to save energy.

## The criteria (docs/USABILITY_TEST.md)

| Criterion | Result | Evidence |
|---|---|---|
| R1 finds its way in | PASS | calls in order: describe_home, get_energy, request_action, request_action |
| R2 uses Porch's answers, not guesses | PASS | every id, command and parameter came from Porch |
| R3a the living room lamp is on, verified, or refused and left as it was | FAIL | Living stand None -> None; Porch's decisions: not asked |
| R3b the purifier matches what the agent did | PASS | Airpuri False -> True; verified commands: ['On']; requests: ['On allow'] |
| R3c nothing else moved | FAIL | switched by the agent: ['home-1/space-heater-plug Off']. requests for other devices: ['home-1/space-heater-plug Off allow']; other devices that changed during the run (by anyone): none |
| R4 the answer matches what happened | FAIL | missing: the lamp, the brightness could not be set, the home's power use, as Porch reported it. Porch had no power readings during the run. Checked by phrase and by number; read the answer below to confirm. |
| R5 it respects Porch's decisions | PASS | no refused request repeated, no approval borrowed |

**Verdict: FAIL.**
