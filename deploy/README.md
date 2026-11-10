# Deploying the Porch demo server

The demo server is a DigitalOcean droplet (Ubuntu 24.04, 2 GB) running:

| Service | What it runs | Port |
|---|---|---|
| `porch-hub` | Device Hub (`porch_hub.main:app`, simulated devices) | 127.0.0.1:8100 |
| `porch-gateway` | Porch gateway (`porch_gateway/porch_gateway.py`) | 127.0.0.1:8000 |
| `porch-direct` | Benchmark baseline, direct hub MCP (`porch_gateway/bench/hub_direct_mcp.py`) | 127.0.0.1:8200 |

It also runs Hermes Agent with six profiles (`deploy/hermes_profiles.sh`), and the demo shortcuts in `deploy/bin/`. Nothing listens on a public port.

## Workflow

1. Work on a branch and open a PR. Run everything locally first (see `porch_gateway/README.md`).
2. Merge to `main`.
3. Whoever has the server key runs, from their clone:

```bash
deploy/deploy.sh
```

This copies `origin/main` to `/opt/porch-app` (with `git archive` and `rsync`, so the server needs no GitHub access), reinstalls dependencies, restarts the services, and runs the end-to-end check. Expect `15/15 checks passed`. To try a branch before merging, run `REF=origin/my-branch deploy/deploy.sh`.

## Fresh server

```bash
SETUP_ARGS=--with-hermes PORCH_HOST=root@<ip> PORCH_KEY=~/.ssh/<key> deploy/deploy.sh
```

Then on the server:
1. Run `set-anthropic-key`. It asks for the key at a hidden prompt; never put keys in the repo.
2. Run `/opt/porch-app/deploy/hermes_profiles.sh` to create the six agent profiles.
3. Run `porch-demo` to test.

## Demo

From your laptop: one command opens the dashboards' tunnel and the split-screen demo:

```bash
ssh -t -L 8100:127.0.0.1:8100 -L 8000:127.0.0.1:8000 -i ~/.ssh/porch_ed25519 root@<demo-server> porch-demo
```

Then open http://localhost:8100/stage?engine=ws://localhost:8000/ws/stage. The full script is in `porch_gateway/docs/CLASS_DEMO.md`.

Shortcuts on the server: `energy`, `concierge` (start an agent), `approve`, `reset-demo`, `occupied` / `vacant`, `leak-on` / `leak-off`, `set-anthropic-key`.

## Access

You don't need server access to contribute: PRs are enough. Server access is only for deploying, running the LLM benchmark on the server, or presenting the live demo. To get it, send your **public** SSH key to whoever manages the server and they'll add it to `/root/.ssh/authorized_keys`. Never share private keys.

## Automating deploys (optional, later)

A GitHub Action can run `deploy/deploy.sh` on every merge to `main`. A repo admin would add the server's SSH private key and host as repository secrets.
