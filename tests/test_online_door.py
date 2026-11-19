"""deploy/home/online.py: the door that lets an agent reach Porch from Claude's servers.

The door must pass on the agents' tools and nothing else, only with an agent's online key, only
while online access is on, and under that agent's own Porch key, so the gateway's rules are the
same as on the Mac. These tests put a stand-in gateway behind it.
"""

import hashlib
import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

SCRIPT = Path(__file__).parents[1] / "deploy" / "home" / "online.py"
spec = importlib.util.spec_from_file_location("home_online", SCRIPT)
online = importlib.util.module_from_spec(spec)
spec.loader.exec_module(online)

MUSE_PORCH_KEY = "muse-porch-key-kept-on-the-mac"
MUSE_ONLINE_KEY = "porch-online-muse-test-key"
CALL = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "request_action", "arguments": {"device_id": "home-1/living-stand", "command": "On",
                                                            "reason": "test"}}}


class Gateway:
    """A stand-in for Porch's gateway: it answers /mcp and remembers what reached it."""

    def __init__(self):
        self.seen: list[dict] = []
        self.down = False

        async def mcp(request: Request):
            self.seen.append({"method": request.method, "headers": dict(request.headers),
                              "raw_headers": list(request.headers.items()),
                              "body": (await request.body()).decode()})
            return JSONResponse({"jsonrpc": "2.0", "id": 3, "result": {"ok": True}},
                                headers={"mcp-session-id": "session-1", "x-gateway-only": "secret"})

        self.app = Starlette(routes=[Route("/mcp", mcp, methods=["GET", "POST", "DELETE"])])

    def transport(self):
        inner = httpx.ASGITransport(app=self.app)
        gateway = self

        class MaybeDown(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                if gateway.down:
                    raise httpx.ConnectError("refused")
                return await inner.handle_async_request(request)

        return MaybeDown()


@pytest.fixture
def home(tmp_path):
    settings = tmp_path / "porch-hub"
    settings.mkdir()
    (settings / "porch_config.json").write_text(json.dumps({"agents": {
        MUSE_PORCH_KEY: {"id": "muse", "name": "Muse"},
        "dot-porch-key": {"id": "dot", "name": "Dot"}}}))
    online.write_private(settings / online.KEYS_FILE, {"keys": {
        hashlib.sha256(MUSE_ONLINE_KEY.encode()).hexdigest(): {"agent": "muse"}}})
    online.write_private(settings / online.STATE_FILE, {"enabled": True})
    return settings


def door(settings, gateway, clock=None, lines=None):
    app = online.make_app(settings, gateway_mcp="http://gateway/mcp", config_path=settings / "porch_config.json",
                          transport=gateway.transport(), clock=clock or (lambda: 0.0),
                          echo=(lambda *a, **k: lines.append(" ".join(map(str, a)))) if lines is not None else (lambda *a, **k: None))
    return TestClient(app)


def ask(client, key=MUSE_ONLINE_KEY, body=CALL, **headers):
    if key is not None:
        headers.setdefault("authorization", f"Bearer {key}")
    return client.post("/mcp", json=body, headers={"accept": "application/json, text/event-stream", **headers})


def test_an_online_key_reaches_the_gateway_as_the_agents_own_porch_key(home):
    gateway = Gateway()
    with door(home, gateway) as client:
        answer = ask(client, **{"mcp-session-id": "session-1", "cookie": "sid=1", "origin": "https://evil.example",
                                "x-owner-key": "guess", "x-forwarded-for": "1.2.3.4"})
    assert answer.status_code == 200 and answer.json()["result"] == {"ok": True}
    assert answer.headers["mcp-session-id"] == "session-1" and "x-gateway-only" not in answer.headers
    reached = gateway.seen[0]["headers"]
    assert reached["authorization"] == f"Bearer {MUSE_PORCH_KEY}"  # swapped: the online key never reaches Porch
    assert reached["mcp-session-id"] == "session-1"
    for kept_back in ("cookie", "origin", "x-owner-key", "x-forwarded-for"):
        assert kept_back not in reached, kept_back
    assert json.loads(gateway.seen[0]["body"]) == CALL


def test_the_2026_revisions_routing_headers_reach_the_gateway(home):
    """ChatGPT speaks MCP's 2026-07-28 revision: every request carries Mcp-Method (and Mcp-Name,
    Mcp-Param-*), and the gateway routes by them. A header sent twice reaches it twice, so its own
    check can refuse the request rather than the door picking one copy."""
    gateway = Gateway()
    discover = {"jsonrpc": "2.0", "id": "d1", "method": "server/discover", "params": {}}
    with door(home, gateway) as client:
        assert in_address(client, body=discover, extra=[("mcp-protocol-version", "2026-07-28"),
                                                         ("mcp-method", "server/discover")]).status_code == 200
        assert in_address(client, extra=[("mcp-protocol-version", "2026-07-28"), ("mcp-method", "tools/call"),
                                         ("mcp-name", "request_action"), ("mcp-param-device-id", "home-1/living-stand"),
                                         ("mcp-other", "x"), ("x-forwarded-for", "1.2.3.4")]).status_code == 200
        assert in_address(client, extra=[("mcp-method", "tools/call"), ("mcp-method", "tools/list")]).status_code == 200
    first, second, third = (seen["headers"] for seen in gateway.seen)
    assert first["mcp-method"] == "server/discover" and first["mcp-protocol-version"] == "2026-07-28"
    assert second["mcp-name"] == "request_action" and second["mcp-param-device-id"] == "home-1/living-stand"
    assert "mcp-other" not in second and "x-forwarded-for" not in second
    assert [v for n, v in gateway.seen[2]["raw_headers"] if n == "mcp-method"] == ["tools/call", "tools/list"]
    assert third["authorization"] == f"Bearer {MUSE_PORCH_KEY}"


def test_an_api_key_header_works_as_well_as_bearer(home):
    gateway = Gateway()
    with door(home, gateway) as client:
        assert ask(client, key=None, **{"x-api-key": MUSE_ONLINE_KEY}).status_code == 200
    assert gateway.seen[0]["headers"]["authorization"] == f"Bearer {MUSE_PORCH_KEY}"


@pytest.mark.parametrize("key, status", [(None, 401), ("", 401), ("porch-online-wrong", 401),
                                         (MUSE_PORCH_KEY, 401)])  # a Mac key is not an online key
def test_without_a_valid_online_key_nothing_reaches_the_gateway(home, key, status):
    gateway = Gateway()
    with door(home, gateway) as client:
        assert ask(client, key=key).status_code == status
    assert gateway.seen == []


@pytest.mark.parametrize("path", ["/", "/owner/approvals", "/owner/audit", "/ws/stage", "/devices", "/dashboard",
                                  "/protection/window", "/.well-known/oauth-protected-resource", "/mcp/../owner/audit"])
def test_only_the_agents_tools_are_reachable(home, path):
    gateway = Gateway()
    with door(home, gateway) as client:
        for method in ("get", "post"):
            answer = getattr(client, method)(path, headers={"authorization": f"Bearer {MUSE_ONLINE_KEY}"})
            assert answer.status_code in (404, 405), (method, path, answer.status_code)
    assert gateway.seen == []


def test_off_means_off_even_for_a_valid_key(home):
    gateway = Gateway()
    online.write_private(home / online.STATE_FILE, {"enabled": False})
    with door(home, gateway) as client:
        answer = ask(client)
        assert answer.status_code == 503 and "online access is off" in answer.json()["error"]
        (home / online.STATE_FILE).write_text("{not json")  # a damaged switch file means off
        assert ask(client).status_code == 503
        (home / online.STATE_FILE).unlink()
        assert ask(client).status_code == 503
    assert gateway.seen == []


def test_an_agent_that_was_removed_is_refused(home):
    gateway = Gateway()
    (home / "porch_config.json").write_text(json.dumps({"agents": {"dot-porch-key": {"id": "dot"}}}))
    with door(home, gateway) as client:
        answer = ask(client)
    assert answer.status_code == 403 and "no longer exists" in answer.json()["error"] and gateway.seen == []


def test_too_many_requests_and_too_large_requests_are_refused(home):
    gateway = Gateway()
    now = [0.0]
    with door(home, gateway, clock=lambda: now[0]) as client:
        for _ in range(online.RATE[0]):
            assert ask(client).status_code == 200
        assert ask(client).status_code == 429
        now[0] += online.RATE[1] + 1  # a minute later
        assert ask(client).status_code == 200
        big = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"pad": "x" * (online.MAX_BODY + 10)}}
        assert ask(client, body=big).status_code == 413
    assert len(gateway.seen) == online.RATE[0] + 1


def test_a_gateway_that_is_down_is_said_plainly(home):
    gateway = Gateway()
    gateway.down = True
    with door(home, gateway) as client:
        answer = ask(client)
    assert answer.status_code == 502 and "Is Porch running?" in answer.json()["error"]


def test_every_request_is_logged_by_agent_and_what_it_asked_without_keys(home):
    gateway = Gateway()
    lines: list[str] = []
    with door(home, gateway, lines=lines) as client:
        ask(client)
        ask(client, key="porch-online-wrong")
    log = (home / online.LOG_FILE).read_text()
    entries = [json.loads(line) for line in log.splitlines()]
    assert entries[0]["agent"] == "muse" and entries[0]["status"] == 200
    assert entries[0]["asks"] == "tools/call request_action home-1/living-stand On"
    assert entries[1]["status"] == 401 and entries[1]["agent"] is None
    for secret in (MUSE_ONLINE_KEY, MUSE_PORCH_KEY, "porch-online-wrong"):
        assert secret not in log and all(secret not in line for line in lines)
    assert (home / online.LOG_FILE).stat().st_mode & 0o777 == 0o600
    assert any("[PORCH ONLINE] muse" in line for line in lines)


def in_address(client, key=MUSE_ONLINE_KEY, body=CALL, method="post", extra=()):
    """The form for connector forms with no field for a key (ChatGPT, claude.ai's No sign-in)."""
    headers = [("accept", "application/json, text/event-stream"), *extra]
    if method != "post":
        return getattr(client, method)(f"/k/{key}/mcp", headers=headers)
    return client.post(f"/k/{key}/mcp", content=json.dumps(body), headers=[("content-type", "application/json"), *headers])


def test_a_key_in_the_address_reaches_the_gateway_as_the_agents_own_porch_key(home):
    gateway = Gateway()
    with door(home, gateway) as client:
        answer = in_address(client)
        assert in_address(client, method="get").status_code == 200  # the stream an MCP client may open
        assert in_address(client, method="delete").status_code == 200  # and the end of its session
    assert answer.status_code == 200 and answer.json()["result"] == {"ok": True}
    assert [seen["method"] for seen in gateway.seen] == ["POST", "GET", "DELETE"]
    assert all(seen["headers"]["authorization"] == f"Bearer {MUSE_PORCH_KEY}" for seen in gateway.seen)
    assert json.loads(gateway.seen[0]["body"]) == CALL


@pytest.mark.parametrize("key", ["porch-online-wrong", MUSE_PORCH_KEY, "x"])  # a Mac key is not an online key
def test_an_address_with_no_online_key_is_not_found_and_starts_no_sign_in(home, key):
    gateway = Gateway()
    with door(home, gateway) as client:
        answer = in_address(client, key=key)
    assert answer.status_code == 404 and "www-authenticate" not in answer.headers
    assert gateway.seen == []


def test_a_key_in_the_address_follows_the_same_switch_and_limits(home):
    gateway = Gateway()
    now = [0.0]
    with door(home, gateway, clock=lambda: now[0]) as client:
        online.write_private(home / online.STATE_FILE, {"enabled": False})
        assert in_address(client).status_code == 503
        online.write_private(home / online.STATE_FILE, {"enabled": True})
        for _ in range(online.RATE[0]):
            assert in_address(client).status_code == 200
        assert in_address(client).status_code == 429  # one budget per agent, whichever way the key came
        assert ask(client).status_code == 429
    assert len(gateway.seen) == online.RATE[0]


def test_an_address_with_a_key_is_never_logged(home):
    gateway = Gateway()
    lines: list[str] = []
    with door(home, gateway, lines=lines) as client:
        in_address(client)
        in_address(client, key="porch-online-wrong")
    log = (home / online.LOG_FILE).read_text()
    entries = [json.loads(line) for line in log.splitlines()]
    assert entries[0]["agent"] == "muse" and entries[0]["status"] == 200
    assert entries[1]["status"] == 404 and entries[1]["agent"] is None
    for secret in (MUSE_ONLINE_KEY, MUSE_PORCH_KEY, "porch-online-wrong", "/k/"):
        assert secret not in log and all(secret not in line for line in lines)


def test_the_switch_needs_tailscale_and_a_running_door_and_off_works_without_either(home, monkeypatch, capsys):
    monkeypatch.setenv("PORCH_CONFIG_DIR", str(home))
    monkeypatch.setattr(online, "tailscale", lambda: None)
    assert online.main(["on"]) == 1 and "Tailscale is not installed" in capsys.readouterr().err
    monkeypatch.setattr(online, "tailscale", lambda: "/usr/local/bin/tailscale")
    monkeypatch.setattr(online, "door_listening", lambda: False)
    assert online.main(["on"]) == 1 and "the door is not running" in capsys.readouterr().err
    monkeypatch.setattr(online, "tailscale", lambda: None)
    assert online.main(["off"]) == 0
    assert online.online_state(home) == {"enabled": False}
    assert "refuses every request" in capsys.readouterr().out
    assert online.main(["status"]) == 0 and "Online access is OFF" in capsys.readouterr().out
