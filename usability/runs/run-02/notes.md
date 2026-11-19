# run-02: what a person reading it noticed

The checker's verdict is PASS (report.md). Read alongside Porch's records, these are the points
that matter for the documentation and the interface.

1. **It checked before acting.** It sent `preview_action` before both dryer changes. The first
   preview, 11 hours 45 minutes (to start at 1:00 AM), would have failed: the hub's dry run said
   "Dryer does not accept timer.relativeMinuteToStart remotely". It then previewed 12 hours, and
   only after that sent the real request. Nothing failed on a device.
2. **Porch told it something untrue about the dryer (the improvement to make).** `docs/AGENTS.md`
   and `describe_home`'s `command_params` both say `SetDelayStart` takes
   `{"hours": 0-12, "minutes": 0-59}` for every appliance. This dryer accepts whole hours only.
   The agent recovered thanks to the dry run, at the cost of an extra call and a start time it did
   not want. It reported the limit honestly: "The dryer doesn't accept minutes remotely, only whole
   hours."
3. **The heater was never requested.** `describe_home` already showed On for the heater as
   `blocked: Safety rule: no heating devices run when nobody is home`, so the agent sent no
   request. Porch's audit has no entry for it. Its answer says "Porch refused the request". The
   substance is right (Porch would refuse, for the reason given), but strictly nothing was
   requested.
4. **Limits it named, which Porch cannot answer today:**
   - no electricity rates or off-peak hours, so "while electricity is cheaper" was its own guess
     (late night, the longest delay allowed);
   - no cycle duration, so it could not promise the dryer finishes by morning.

   It said both plainly instead of guessing silently.
5. **It claimed nothing Porch did not confirm.** "Porch confirmed it by reading the plug back" and
   "Porch confirmed the dryer accepted the setting" match the verified read-backs in the audit
   log. It also ended with "Nothing is waiting on anyone's approval", which is true.

Setup: a new Claude Desktop chat (Opus 5.5) with only the porch-fresh connection, the prompt
pasted as the first message, no other input. Tool permission prompts were approved, as the
protocol allows.
