# Note for Erik: household rules in the gateway

Yoonchan asked for two rules for showing how Porch works in a real home, and agreed that I change
`porch_gateway.py` on the `devices/cloud-connectors` branch and leave you this note. Nothing here is
committed yet. Please look it over; anything you would rather do differently is easy to move.

## What the household asked for

1. **Energy agents read energy and control nothing.**
2. **The person in the room decides.** Yoonchan's agents (Dot, Muse, Instinct) control the
   devices, but if his wife is home in the living room and he is at school, her permission is
   needed. Her agent works the same way the other way round.

## What changed in `porch_gateway.py`

- **`household_rules.py` (new, pure functions)** holds the rules, so they are tested without a
  running gateway (`tests/test_household_rules.py`, 13 cases).
- **`decide_for()`** wraps your `decide()` and does not change it:
  - an agent with `"reads": "energy"` is refused every action;
  - after `decide()` says allow, the room rule may turn it into a hold for the person in the room.
    Your safe-direction commands and the leak exception are never held.
- **`household_context()`** reads who is where (`GET /household`) and which room each device is in
  (`GET /home/places`) from the hub. An older hub without these routes means no room rule. If the
  hub has them but cannot answer, the gateway refuses to decide rather than guess.
- **Approvals** now carry `approvers` and `approver_names`. A hold for a person is answered with
  that person's PIN through the new `POST /household/approvals/{id}/{approve|decline}`; the
  gateway asks the hub to check the PIN (`POST /household/verify`, with lockout after wrong
  tries). `owner.sh approve` (the owner key) is refused, with a 403, for a hold that waits for
  someone else, unless the owner is the one in the room.
- **Tools:** `describe_home` shows an energy agent only the meters and their readings, and other
  agents "needs Wife's approval" where the rule applies. `get_state` is limited the same way for
  energy agents. **`get_energy` is new:** the hub's `/home/energy` summary, for any agent.
- **New read-only routes for the hub's pages:** `GET /household/agents` (no keys) and
  `GET /household/approvals`.
- `request_action` answers `held_for_approval` with `waiting_for` for a person-held request, and
  `held_for_owner_approval` as before for the owner.

## Agent settings

Two optional fields per agent in the config: `"person"` (the household person id it acts for;
without it, `acts_for` is matched against the people's names) and `"reads": "energy"`.
`deploy/home/agent.sh set <id> --person ... --reads ...` writes them.

## How it was checked

- An end-to-end run on a stand-in home (simulated devices, separate ports), 21 checks. Among them:
  the energy agent sees meters only and is refused actions; Dot's request is held for Wife; the
  owner key cannot answer it; a wrong PIN is refused; Wife's PIN lets it run, verified by read-back;
  the approval works once; with Yoonchan in the room nothing is held; her agent is held for him.
- On the real home, read-only: the energy agent and Dot see what they should.
- The hub suite: 476 tests at the time of writing.

## Later: the router plug (`network`)

Yoonchan made all four of the home's plugs switchable through Porch (2026-11-16), the one that
powers the Wi-Fi router included. As an `unknown_load` its Off would count as the safe direction
and run without asking, and an Off takes the whole home offline, the plug included, until
someone at home presses the plug's button. So:

- `RISK_TIER` has a new capability, **`network`**, tier `never_autonomous`: On and Off are both
  held for the owner. `decide()` words the Off hold with that warning. Nothing else in
  `decide()` changed.
- The config maps the plug: `"device_capabilities": {"home-1/living-wifi": "network"}`, in both
  the everyday and the Demo Day config.
- An agent can ask for it only with `network` in its scopes (otherwise it is refused, as for any
  scope). `deploy/home/agent.sh` now includes `network` in "all", and Dot, Muse, Instinct and
  Her agent have it.
- Checked with the gateway's own Python (`decide_for` for both plugs, an agent with and one
  without the scope), and on the real home with Dot's `preview_action`: held, with the warning.
  The README's rules table has the new row.

## Later: each request, step by step, for the live view (`flow` messages)

For the HW9 demo video Yoonchan wanted a view of how a request moves through Porch. The hub's new
page, `/flow`, draws it from your stage feed. Everything is additive: no rule or answer changed.

- **`flow.py` (new, pure):** puts Porch's three checks into words (who is asking, what it may
  control, who is in the room), with a state for each: `ok`, `wait`, `stop` or `skip`. It is
  tested in `tests/test_flow.py` without a running gateway.
- **New stage messages, `{"type": "flow", "id", "phase", ...}`:** `ask` (the agent, the tool,
  the device, the command and the agent's reason), `rules` (the checks, the decision, who it
  waits for, the approval id), then `dry_run`, `sent` and `verified` from `execute_and_verify`,
  `read` for the read tools, `approval` when someone answers a held request, and `error`. The
  agent appears as id, name, acts_for, person and reads: never its key.
- **How it is wired:** `request_action`'s body moved into `_request_action` so the ask can be
  sent first and an error can be reported. `execute_and_verify` takes an optional `flow_id`.
  `preview_action` sends the same steps, marked as a preview. An approval keeps its request's
  flow id (`"flow"`), so the answer lands on the request that was held. The `safety` and `audit`
  messages your stage display uses are unchanged.
- **Checked:** 14 end-to-end checks on a stand-in home (a read, an energy agent's refusal, an
  action read back, a hold for Wife, her PIN, the retry, the router's preview), the household run
  above again (21 checks), and on the real home with reads and previews only.

## Later: the HW9 cold-start test

- **`PORCH_AUDIT_FILE` (new, optional)** sets where the audit log is written. A stand-in or test
  run now keeps its own log. Until this change every gateway, test runs included, wrote to
  `porch_gateway/audit.jsonl`. So the household-rules check, the flow check and earlier stand-in
  runs added entries to the real home's log: 40 entries as of 2026-11-16, 9 of them marked
  `"devices": "simulated by the Device Hub"`. Nothing was removed; tell Yoonchan if you want them
  cleaned.
- **A request for a device that does not exist** (`request_action`, `preview_action`) and a failed
  `get_state` are now sent on the stage feed as an `ask` and an `error`. That way the live view
  and the usability test see an agent's mistakes, not only its successes. The answers to the
  agent are unchanged.
- **`docs/AGENTS.md`** is the agent-facing documentation, and `docs/USABILITY_TEST.md` is the
  test. The test runs on a stand-in with its own ports and settings (`usability/standin.py`).

## Later: what each device's commands take (`command_params`)

The cold-start test's run-02 showed a fresh agent trusting `describe_home`'s home-wide hint. That
hint said `SetDelayStart` takes hours and minutes, but the dryer takes whole hours only.
- `command_params.py` (new, pure) builds each device's own `command_params` from what the hub
  reports.
- The hub now says, per laundry appliance, whether minutes can be set:
  `PorchLaundryExtension.DelayMinutesSettable`, read from the appliance's LG profile.
- `describe_home` adds `command_params` to each device that has parameters. The home-wide
  `command_params` now says minutes count only where a device lists them.
- `decide()` is unchanged, and so is the 12-hour household limit.

## Later: agents on the twins, and agents in ChatGPT and on claude.ai

Yoonchan's agents now live in different apps: the energy agent in Claude Desktop, Dot in
ChatGPT, Muse in the Claude phone app and the concierge agent (every scope, for the household) on
claude.ai. Claude and Instinct were removed on 2026-11-16, at Yoonchan's request. The digital twin and the 3D home show each one and
draw its requests through Porch.
- **`client` (new, optional agent field)**: the app an agent lives in, set with
  `deploy/home/agent.sh set <id> --client "Claude Desktop"`. It is only shown, never checked.
  `AGENT_FIELDS` includes it.
- **`last_seen`** in `GET /household/agents`: when each agent last called Porch, any tool. It is
  kept in memory (`LAST_SEEN`, set in `current_agent()`), so it starts empty after a restart.
  Still no key in that answer.
- **Read tools are marked read only**: `describe_home`, `get_state`, `get_energy`,
  `preview_action` and `get_audit_log` carry `annotations=READ_ONLY` (`readOnlyHint`). ChatGPT
  asks the person before every tool without it; with it, it asks only before `request_action`.
  `request_action` is unchanged.
- **The online door** (`deploy/home/online.py`, not your code) also takes the agent's online key
  inside the address, `/k/<key>/mcp`, because the connector forms of ChatGPT and claude.ai have
  no field for a key. The gateway sees the agent's own key either way.
- `decide()`, the rules and the stage messages are unchanged.

## Later: the devices each agent may control (`device_grants.py`)

Yoonchan asked to give an agent only some devices: "if I give Muse the living stand control,
then Muse can control it, not others."
- **`device_grants.py` (new, pure)** reads `agent-devices.json` beside the gateway's settings
  (`PORCH_AGENT_DEVICES_FILE` to name another). No entry for an agent: no limit, as before.
- **`decide()`** checks it right after the scope check and before the safe-direction rule:
  another device is denied with "Permission: Muse may control only Living stand in this home, as
  the owner chose." The water main's emergency close during a leak still comes first. Read on
  every call, so the owner's change applies at once. A damaged file denies every action.
- **`GET /household/agents`** adds `devices` (the list, or null). The hub's page writes the file
  with the owner PIN (`POST /household/agents/{id}/devices`), and so does `agent.sh devices`.
- **`flow.agent_card`** adds `client`, so the live views say which app an agent lives in.

## Later: a yes is carried out at once (`carry_out`)

Yoonchan found no way to let a held request run: after a yes the agent had to call
`request_action` again with the approval id, which a chat agent rarely does, and family protection
then stopped it anyway.
- **`carry_out(a)` (new):** both answer routes (`/household/approvals/...` and
  `/owner/approvals/...`) now carry an approved request out at once, as the agent that asked, by
  calling `_request_action` with the approval id. The same checks run, and the approval is used up.
- **An agent's retry** with that approval id answers `already_carried_out` with the outcome, and
  never runs it twice, even if the rules would now allow it without the approval.
- **The answer to an approval** now includes `outcome`, the result of carrying it out.
- **Family protection:** the hub (not the gateway) lets the one command through, and only for the
  owner's own yes with the owner PIN, only while this answer is carried out. `decide()` and the
  rules are unchanged.
- **Family protection asks the owner:** when Porch's rules allow a request but the hub's dry run
  says family protection stops it, `_hold_for_protection` holds it for the owner (approvers empty,
  `"family_protection": true`) instead of returning `executed: false`. That approval counts only
  while `decide()` still allows the request. Not after the owner's own yes (`owner_answered`), so
  it never loops. `_pending_same` makes an agent's repeated ask reuse the request already waiting.
- **Wording:** a request held for a household agent (no `person`, like the concierge) now says "…
  and Concierge agent acts for the whole household, so Yoonchan's permission is needed".

## Open points for you

- `audit()` records a person's approval with `acts_for` set to that person's name. Say if you want
  a separate field.
- The Demo Day config (`porch_config.demo.json`) still lets the energy agent switch lamp A, as the
  run sheet needs. The everyday config has the energy agent as read-only.
- `import asyncio` at the top of `porch_gateway.py` was unused before this change; I left it.
