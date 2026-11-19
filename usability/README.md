# The cold-start usability test

Can an agent that has never seen Porch use it from its documentation alone? The protocol, with the
success criteria decided before any run, is in [docs/USABILITY_TEST.md](../docs/USABILITY_TEST.md).
The documentation the agent gets is [docs/AGENTS.md](../docs/AGENTS.md).

| File | What it does |
|---|---|
| `task.md` | The task, word for word |
| `prompt.py` | Builds the exact prompt: the task, then the documentation |
| `standin.py` | A stand-in Porch: the demo's eight simulated devices behind the real hub and gateway, on ports 8102 and 8002, with a household and a test agent, **Fresh agent**. It records everything Porch does into `runs/NAME/` |
| `check.py` | Checks a run against the criteria and writes `runs/NAME/report.md` |
| `run_headless.sh` | The whole run with a headless Claude, for someone with an Anthropic API key |

## Run it with a Claude Desktop chat

1. **The connection, `porch-fresh`.** The launcher (next step) checks Claude Desktop's settings
   first. If `porch-fresh` is missing, it asks you to quit Claude Desktop, adds the entry (with a
   backup), opens the app again, and waits until the app has started the connection. Claude
   Desktop must be closed for this: it writes its own copy of its settings while it runs, so an
   entry added then is lost. By hand, the entry is printed by
   `porch_gateway/.venv/bin/python usability/standin.py desktop`.
2. **Start a run.** Double-click **Start Cold-start Test (HW9).command**, next to the repository, or
   run:

   ```bash
   porch_gateway/.venv/bin/python usability/standin.py session
   ```

   It names the run (`run-01`, `run-02`, ...) and asks for the variant and the chat's model. Then
   it puts the prompt on the clipboard, opens the live view of the agent's requests (which you
   can record), and starts the stand-in.
3. **In Claude Desktop, open a new chat**, outside any project. If Claude's memory or search of
   past chats is on, use an incognito chat or turn them off for the test: the agent must not
   remember Porch.
4. **Switch on porch-fresh only** in the chat's tools menu. Switch off `porch-dot`, `porch-muse`,
   `porch-instinct` and every other connector: the first three reach the real home.
5. **Paste the prompt, send it, and type nothing else** until the agent has answered. If Claude asks
   to allow porch-fresh's tools, choose Always allow: that is the access the test gives, not help.
6. **Copy its whole answer** (the Copy button under it), go back to the Terminal window, and press
   **Ctrl+C**. Closing the window instead also saves the run, and a stand-in left running by an
   earlier run is stopped by the next start, or by `usability/standin.py stop`. The session saves the final states and the audit log, takes the answer from the
   clipboard, checks the run and opens `runs/NAME/report.md`. Save the whole chat too, exported or
   as screenshots, for the PDF.

## Run it on the real home

The same test on Yoonchan's real home ("The real-home run" in docs/USABILITY_TEST.md): real devices
answering an agent that has never seen Porch. Fresh agent has its own key there, with lights and
appliances only. Only Living stand and Airpuri may move.

1. **Start Porch** (`Start Porch.command`) at home, so every platform answers.
2. **Double-click "Start Cold-start Test (HW9, Real Home).command".** It walks you through four
   things, waiting at each one:
   - adding the `porch-home-fresh` connection (quit Claude Desktop when it asks);
   - checking that Porch is up and knows Fresh agent (restart Porch if it was started before the
     agent was added);
   - asking you to open a control window for exactly Living stand and Airpuri, with the owner PIN;
   - showing who is home.
3. **Run the chat as above,** with **porch-home-fresh** switched on and nothing else. Film the live
   view (`http://127.0.0.1:8100/flow`) and the living room lamp.
4. **Copy the answer and press Ctrl+C.** The run is saved in `runs/home-NN/`, with energy readings
   every 5 seconds and Porch's decisions for Fresh agent, and checked against R1 to R5.

The script never sends a command and never opens a window: the agent asks Porch, and the owner
decides what may move.

## Run it headless

With your own Anthropic API key in `~/.config/porch-standin/anthropic-key` (chmod 600):

```bash
usability/run_headless.sh run-01                     # variant A
usability/run_headless.sh run-02 --wife-in office    # variant B
```

It starts the stand-in, runs a fresh Claude Code session in `--bare` mode, and checks the run. That
session has no settings, no memory, no files and no tools of its own, only the connection to the
stand-in. The full transcript is kept in `runs/NAME/transcript.jsonl`.

## The rehearsals in `runs/`

`rehearsal-scripted-good`, `rehearsal-scripted-goodB`, `rehearsal-scripted-bad`,
`rehearsal-session-scripted` and `rehearsal-session-closed-window` are **not** cold-start runs. A short script, not an agent, played Fresh agent, to check the harness itself:
- a correct run passes, in both variants;
- a run with typical mistakes fails on exactly those mistakes: an invented device id, a command
  Porch never offered, a refused request repeated, and claims Porch did not confirm.
- the guided session works end to end, through the same bridge Claude Desktop starts
  (`rehearsal-session-scripted`), and saves the run when its window is closed instead of stopped
  with Ctrl+C (`rehearsal-session-closed-window`).

`rehearsal-realhome-mechanics` checked the real-home script's recording and checks against the
stand-in, not the real home. Its FAIL is expected: the stand-in has no Living stand, and the
script's client switched the heater off on purpose, to show that R3c catches it.

`run-01` was a real attempt that never reached Porch: the chat had no `porch-fresh` connection,
because Claude Desktop had dropped it from its settings. Its report says **SETUP FAILURE** and why.

`tests/test_usability_check.py` checks the same judgments on made-up runs.
