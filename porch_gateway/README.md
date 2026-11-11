# Porch gateway

The safe front door for AI agents in the connected home. Agents connect over MCP with their own key. Porch checks every request against that agent's permissions and the home's safety rules, dry-runs it on the hub's digital copy, executes it through the Device Hub, and verifies the result twice: the hub's read-back, then Porch's own read of the device compared with the dry run's prediction. Every decision goes to the audit log and to the hub's stage display.

```
Agents (Hermes + Claude, one key each)
   │  MCP (streamable HTTP)   :8000/mcp
   ▼
Porch gateway   identity · capability → risk tier → allow/deny/hold · approvals · dry run → execute → verify · audit
   │  HTTP, Interface v0      :8100
   ▼
Porch Device Hub (repo root)  →  LG ThinQ · Matter · Google Home drivers (simulated devices by default)
```

There is no LLM inside the gateway: every decision is a deterministic rule.

## Run it locally (about 2 minutes, no API key)

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/). From the repo root:

```bash
porch_gateway/start.sh
```

This creates both virtual environments the first time, starts the hub on port 8100, then starts Porch on 8000. In a second terminal:

```bash
porch_gateway/.venv/bin/python porch_gateway/check_demo.py
```

That runs every demo scene through real MCP calls and checks each result against the hub. Expect `15/15 checks passed`. Open the dashboards while it runs:
- http://127.0.0.1:8100/stage?engine=ws://127.0.0.1:8000/ws/stage
- http://127.0.0.1:8100/digital-twin

### With a real AI agent (optional, needs your own Anthropic API key)

Any MCP client works. Each agent needs its key in the `Authorization` header; the keys are in `porch_config.json` and are demo values. For Claude Code:

```bash
claude mcp add --transport http porch http://127.0.0.1:8000/mcp --header "Authorization: Bearer porch_demo_key_energy"
```

For Hermes, add an `mcp_servers` entry, as `deploy/hermes_profiles.sh` does.

## Files

| File | What it is |
|---|---|
| `porch_gateway.py` | The gateway. MCP tools for agents (`describe_home`, `get_state`, `preview_action`, `request_action`, `get_audit_log`), owner HTTP routes (`/owner/*`, owner key), and the stage feed WebSocket (`/ws/stage`) |
| `porch_config.json` | Agents with their keys and scopes, what each plug powers, the owner key and household limits. **Demo values only.** |
| `owner.py` | The owner's side: `list`, `approve [id]`, `decline`, `home` / `away`, `leak` / `dry`, `reset` |
| `check_demo.py` | End-to-end check, 15 checks, no LLM needed |
| `start.sh` | Runs the hub and the gateway locally |
| `requirements.txt` | Gateway dependencies |
| `bench/` | The HW7 benchmark the team submitted (below) |
| `docs/CLASS_DEMO.md` | Live demo script for class |
| `docs/RECORDING_SCRIPT.md` | HW6 video recording script |

## Rules (in `decide()`)

| Capability | Example device | Rule |
|---|---|---|
| lighting | desk lamp plug | Allowed within scope |
| appliance | LG dryer, vacuum, purifier | Allowed within household limits (delayed start ≤ 12 h) |
| heating | space heater plug | Off always allowed. On allowed if someone is home, denied if not |
| unknown_load | any plug not mapped in the config | Off always allowed; on held for the owner |
| entry | front door lock | Locking always allowed; unlocking held for the owner |
| water_shutoff | main water valve | Held for the owner, except closing it while a leak is detected (any agent) |

An owner approval covers one exact action by one agent, can be used once, and expires after 10 minutes. The agent retries with the approval id.

## HW7 benchmark (`bench/`)

The report is `bench/report/HW7_Porch_Benchmark.pdf`. `bench/README.md` explains the claim, conditions and how to rerun it. Every raw run is in `bench/results/`. Regrade any table without running anything:

```bash
python3 porch_gateway/bench/grade.py porch_gateway/bench/results/tuning.jsonl
```

Running the benchmark itself needs the Hermes profiles and services from `deploy/`, so it runs on the demo server.

This is a different benchmark from the one in `bench/` at the repo root, which tests `engine_lite` under injected faults.

## Who built what

- **Erik:** the Porch gateway, the HW7 benchmark in this folder, and the deploy scripts.
- **Yoonchan:** the Porch Device Hub, its dashboards, `engine_lite` and the root `bench/`.
