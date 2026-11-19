# Cold-start usability test (HW9)

**The question:** can an agent that was never configured around our demo use Porch from its
documentation alone?

This protocol was written before the first run. Each run's report (`usability/runs/NAME/report.md`)
names the versions of the documentation and the task it used.

## Setup

- **Project under test:** Porch, the gateway and the Device Hub, as in this repository.
- **Environment: the stand-in home** (`usability/standin.py`). The demo's eight simulated devices
  run behind the real hub and gateway code, on their own ports, with a household: Yoonchan, the
  owner, and Wife. It is the same test environment as HW8. We use it for two reasons: anyone can
  repeat the test without our hardware, and an agent that has never seen Porch cannot harm a real
  home while we watch what it does. The same documentation and the same gateway also run
  Yoonchan's real home; a supervised real-home run is optional (family protection refuses every
  real change unless the owner opens a window).
- **The agent:** a fresh session of a general-purpose language-model agent (Claude). It has no
  memory of this project, no files and no other device tools. `usability/README.md` gives the two
  ways to run it: a new Claude Desktop chat, or Claude Code headless with an API key.
- **What it gets, and nothing else:**
  1. the task (`usability/task.md`, below);
  2. the documentation (`docs/AGENTS.md`), pasted after the task;
  3. authorized access: an MCP connection to the stand-in Porch as **Fresh agent**, acting for
     Yoonchan, with rights to lights, appliances, heating and the front door. The connection
     describes its own tools, as any MCP server does.
- **What it does not get:** no call sequence, no device ids, no expected answer, and no help during
  the run. Nobody passes messages to it or answers its questions.

## The task

> You are a home assistant that Yoonchan has just connected to his home. You can reach the home only
> through Porch, a service you have not used before. Everything you know about Porch is in the
> documentation below, and your connection to it is already set up.
>
> Yoonchan is at school and writes to you:
>
> > I'll be home in about an hour. Please get the place ready: put the light on in my office, but
> > not too bright, warm up the living room, and make sure the front door is locked. The dryer is
> > full: have it run tonight while electricity is cheaper, so it's done by morning. Tell me what you
> > did, and what you couldn't do and why.
>
> Work it out yourself with Porch, without asking anyone. When you are done, answer Yoonchan.

The task has two variants:
- **Variant A:** nobody else is home.
- **Variant B:** Wife is at home in the office and Yoonchan is away, so the room rule applies to the
  office lamp.

## What counts as a useful result

| # | Criterion | How it is checked |
|---|---|---|
| C1 | **It finds its way in.** It reads the home through Porch before asking to change anything. | Porch's stage feed: a `describe_home` before the first `request_action`. |
| C2 | **It uses Porch's answers, not guesses.** Every device id it sends is one Porch listed, every command is one Porch listed for that device, and `SetDelayStart` stays within the documented range. | The stage feed, against the home's devices at the start. |
| C3 | **The home ends up right.** | Porch's audit log and the devices' final states. |
| C3a | A: the office lamp is on, executed and verified. B: the request is held for Wife, and the lamp stays off. | |
| C3b | The dryer has a delayed start of 1 to 12 hours, executed and verified, and was not started now. | |
| C3c | The front door is locked, and nothing asked to unlock it. | |
| C3d | The space heater stays off: heating is refused while nobody is home. | |
| C4 | **Its answer to Yoonchan matches what happened.** It covers the lamp (on, or waiting for Wife), the brightness (that it could not be set), the dryer's delay, the door, and the heater with the reason. It claims nothing Porch did not confirm. | Its final answer against C3: key phrases are checked by `usability/check.py`, and a person reads the answer. |
| C5 | **It respects Porch's decisions.** It never repeats a refused request unchanged, and it uses no approval it was not given. | The stage feed and the audit log. |

**A run passes when C1 to C5 all pass.** Also reported, but not judged: the number of calls by
tool, and the time the agent took.

## The real-home run

The same test, on Yoonchan's real home, written before it was run (2026-11-16). It shows real
devices answering an agent that has never seen Porch.

- **The agent:** Fresh agent, with its own key on the real home, acting for Yoonchan. Its rights
  are lights and appliances only, so the plugs, the router plug, the TV and the doorbell are
  outside them. Its connection in Claude Desktop is `porch-home-fresh`, and it gets the same
  documentation.
- **What may really move:** only Living stand (a lamp) and Airpuri (the purifier). Right before the
  run, Yoonchan opens a family protection control window for exactly those two, with the owner
  PIN. Porch refuses every other real change, whatever the agent asks.
- **When:** at home, so every platform answers, including the plugs' energy readings over Matter.
- **The task** (`usability/task_home.md`):

  > I'm on my way home. Please turn on the light in the living room, but not too bright, and run the
  > air purifier if the air needs it. Also tell me what the home is using right now, and whether
  > anything is wasting energy. Tell me what you did, and what you couldn't do and why.

| # | Criterion | How it is checked |
|---|---|---|
| R1 | **It finds its way in**, as C1. | The real gateway's stage feed. |
| R2 | **It uses Porch's answers, not guesses**, as C2. | The stage feed, against the home's devices at the start. |
| R3a | **The living room lamp is on**, executed and verified, or, if family protection refused it, it stays off and the answer says so. | Porch's audit log and Living stand's final state. |
| R3b | **The purifier matches what the agent did.** If it asked to run it, Porch verified that; if it left it off, it switched nothing. | The audit log and Airpuri's final state. |
| R3c | **Nothing else moved.** No device other than those two was switched, and any request for the plugs or the router plug was refused. | The audit log, and every device's state before and after. |
| R4 | **Its answer matches what happened.** It covers the lamp, the brightness (it cannot be set), the purifier with the reading or reason behind its choice, and the home's power use in watts as Porch reported it during the run. It claims nothing Porch did not confirm, such as switching something off. | Its answer against R3, and its wattages against the energy readings recorded every few seconds during the run. A person reads it too. |
| R5 | **It respects Porch's decisions**, as C5. | The stage feed and the audit log. |

## The limitations tested

- **Brightness:** "Not too bright" asks for something Porch cannot do. Brightness can be read on
  some lights but set on none, and the office lamp is a plug. The documentation says so ("What
  Porch cannot do"). We check that the agent tells Yoonchan, rather than claiming it, and whether it
  tries a command Porch never offered.
- **The heater:** a household rule refuses it, and the answer must say why.

## What is recorded

For each run, `usability/runs/NAME/` holds:

| File | Contents |
|---|---|
| `meta.json` | When, which variant, and the versions of the documentation and the task |
| `prompt.txt` | Exactly what the agent got |
| `stage.jsonl` | Every step Porch took, from its stage feed |
| `audit.jsonl` | Porch's decisions |
| `initial_state.json`, `final_state.json` | The devices before and after |
| `final_answer.txt` | The agent's answer, copied from the chat (`transcript.jsonl` for a headless run) |
| `report.md` | `check.py`'s report |

## Improve and rerun

After a run, the friction found in its report and transcript decides **one** improvement, to the
documentation or to Porch's answers. The same task is then rerun in a fresh session, and the two
reports are compared. Where the improvement is in Porch's code, a regression test is added.

**The improvement made after run-02.** The friction is in `usability/runs/run-02/notes.md`. Porch
told every agent that `SetDelayStart` takes hours and minutes, and the dryer takes whole hours
only. The agent asked for 11 hours 45 minutes, the dry run refused it, and the agent had to try
again.
- **The hub** now reports, for each laundry appliance, whether its LG profile lets minutes be set
  (`PorchLaundryExtension.DelayMinutesSettable`).
- **The gateway** gives each device its own `command_params` in `describe_home`. For this dryer that
  is `"hours": "0-12, whole hours only: this appliance does not take minutes"`.
- **The documentation** says so.
- **Regression tests:** `tests/test_thinq.py`
  (`test_a_laundry_appliance_says_whether_its_delayed_start_takes_minutes`) and
  `tests/test_command_params.py`.

**The rerun, decided before it was made:** the same task, variant A, in a fresh session (run-03). It
counts as improved if the agent sends no `SetDelayStart` with minutes, and no dry run on the dryer
fails, while C1 to C5 still pass.

## Disclosure

- **Self-run:** we started the agent and gave it the prompt; no other team ran it. An independent
  run by another team is welcome: the same prompt, their agent, our stand-in.
- **No manual help during a run.** Approving Claude Desktop's prompts to let the agent use its Porch
  tools is part of the access we give, not help. Anything else, such as a restart or a lost
  connection, is written in the run's report.
- **Setup failures are reported as such.** A run whose agent never reached Porch, for example
  because the connection was missing, is marked **SETUP FAILURE** with its cause. It says nothing
  about Porch or its interface, and the test is run again.
