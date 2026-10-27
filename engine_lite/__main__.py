"""Run the stand-in Engine, or answer an approval as the owner.

    python -m engine_lite                       # MCP over HTTP at http://127.0.0.1:8765/mcp
    python -m engine_lite --stdio               # MCP over stdio, for clients that start the server themselves
    python -m engine_lite approvals             # what is waiting for the owner
    python -m engine_lite approve <id>          # the owner says yes
    python -m engine_lite deny <id>             # the owner says no
    python -m engine_lite walkthrough           # a scripted agent runs the MVP scenes (no AI needed)
"""

import argparse
import json
import logging
import sys
import urllib.request
from pathlib import Path

import httpx
import uvicorn

from . import walkthrough
from .audit import Audit, StageFeed
from .engine import EngineLite
from .policy import SCOPES
from .server import build_http_app, build_server


def serve(args: argparse.Namespace) -> None:
    # The terminal is the audit trail: keep library request and session chatter out of it.
    for name in ("httpx", "httpcore", "mcp", "uvicorn"):
        logging.getLogger(name).setLevel(logging.WARNING)
    scope = SCOPES[args.scope]
    agent = {"id": args.agent.lower().replace(" ", "-"), "name": args.agent, "scope_label": scope.label,
             "scopes": [scope.name]}
    audit = Audit(file=Path(args.audit_file) if args.audit_file else None)
    stage = StageFeed(agent)
    hub = httpx.AsyncClient(base_url=args.hub, timeout=30)
    engine = EngineLite(hub, args.agent, scope, audit, stage)
    server = build_server(engine)

    audit.log("START", f"engine_lite: STAND-IN for the Porch Engine · agent {args.agent!r} · scope {scope.label} "
                       f"· hub {args.hub}")
    if args.stdio:
        server.run("stdio")
        return
    audit.log("START", f"MCP at http://{args.host}:{args.port}/mcp · stage feed ws://{args.host}:{args.port}/ws/stage "
                       f"· owner approvals http://{args.host}:{args.port}/approvals")
    app = build_http_app(server, stage.endpoint, args.host)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def owner(args: argparse.Namespace) -> None:
    base = f"http://{args.host}:{args.port}/approvals"
    url = base if args.action == "approvals" else f"{base}/{args.approval_id}/{args.action}"
    request = urllib.request.Request(url, method="GET" if args.action == "approvals" else "POST")
    with urllib.request.urlopen(request) as response:
        print(json.dumps(json.load(response), indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m engine_lite", description="Stand-in for Erik's Porch Engine (HW6).")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    sub = parser.add_subparsers(dest="action")
    for action in ("approve", "deny"):
        sub.add_parser(action, help=f"{action} a held action as the owner").add_argument("approval_id")
    sub.add_parser("approvals", help="list actions waiting for the owner")
    walk = sub.add_parser("walkthrough", help="a scripted agent runs the MVP scenes, no AI needed")
    walk.add_argument("--owner", choices=["ask", "approve", "deny"], default="ask",
                      help="how the owner answers the unlock request")
    walk.add_argument("--pause", type=float, default=1.5, help="seconds between scenes, to watch the pages")
    parser.add_argument("--hub", default="http://127.0.0.1:8000", help="Device Hub URL")
    parser.add_argument("--agent", default="Claude Code", help="the agent this server speaks for")
    parser.add_argument("--scope", choices=sorted(SCOPES), default="energy")
    parser.add_argument("--stdio", action="store_true", help="MCP over stdio instead of HTTP")
    parser.add_argument("--audit-file", help="also append audit lines as JSON to this file")
    args = parser.parse_args()
    if args.action == "walkthrough":
        walkthrough.run(args.host, args.port, args.owner, args.pause)
    elif args.action:
        owner(args)
    else:
        serve(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
