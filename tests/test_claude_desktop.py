"""deploy/home/claude_desktop.py and `agent.sh desktop`: one Claude Desktop connection for each agent
whose app is Claude Desktop, changed only while the app is closed, never with a key in it."""

import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest

HOME_SCRIPTS = Path(__file__).parents[1] / "deploy" / "home"
sys.path.insert(0, str(HOME_SCRIPTS))
import claude_desktop  # the module agents.py imports, so monkeypatching reaches it

spec = importlib.util.spec_from_file_location("home_agents_for_desktop", HOME_SCRIPTS / "agents.py")
agents = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agents)

SECRET_KEY = "claude-key-do-not-print"
CLIENTS = {"claude": "Claude Desktop", "instinct": "Claude Desktop", "energy-agent": "Claude Desktop",
           "dot": "ChatGPT", "muse": "Claude on the web", "fresh-agent": None, "wife-agent": None}


def entry(agent_id: str) -> dict:
    return claude_desktop.entry_for(agent_id)


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    """Claude Desktop's settings and log, as they are before the change, and Porch's settings folder."""
    config = tmp_path / "claude_desktop_config.json"
    log = tmp_path / "mcp.log"
    log.write_text("")
    config.write_text(json.dumps({"preferences": {"theme": "dark"}, "mcpServers": {
        "porch-instinct": entry("instinct"), "porch-dot": entry("dot"), "porch-muse": entry("muse"),
        "porch-home-fresh": entry("fresh-agent"),
        "porch-fresh": {"command": "python", "args": [str(HOME_SCRIPTS / "agents.py"), "connect", "fresh-agent",
                                                       "--url", "http://127.0.0.1:8002/mcp"],
                        "env": {"PORCH_CONFIG_DIR": str(tmp_path / "standin")}},
        "notes": {"command": "npx", "args": ["some-other-server"]}}}))
    config.chmod(0o600)
    settings = tmp_path / "porch-hub"
    settings.mkdir()
    agents_by_key = {f"{SECRET_KEY}-{agent_id}": {"id": agent_id, "name": agent_id.capitalize(), "scopes": [],
                                                  **({"client": client} if client else {})}
                     for agent_id, client in CLIENTS.items()}
    (settings / "porch_config.json").write_text(json.dumps({"agents": agents_by_key}))
    (settings / "porch_config.json").chmod(0o600)  # agents.py refuses settings others can read
    monkeypatch.setenv("CLAUDE_DESKTOP_CONFIG", str(config))
    monkeypatch.setenv("CLAUDE_DESKTOP_LOG", str(log))
    monkeypatch.setenv("PORCH_CONFIG_DIR", str(settings))
    monkeypatch.delenv("PORCH_GATEWAY_CONFIG", raising=False)
    return {"config": config, "log": log, "settings": settings}


def test_each_agent_in_claude_desktop_gets_one_connection_and_others_are_only_offered_for_removal(desktop):
    config = json.loads(desktop["config"].read_text())
    plan = claude_desktop.plan(CLIENTS, config, desktop["settings"])
    assert plan["add"] == {"porch-claude": entry("claude"), "porch-energy": entry("energy-agent")}
    assert plan["keep"] == {"porch-instinct": "instinct"}
    # Dot lives in ChatGPT and Muse on the web now; the Fresh agent has no app set, the stand-in's
    # connection speaks to another Porch, and the other server is not Porch's: those stay.
    assert plan["drop"] == {"porch-dot": "dot", "porch-muse": "muse"}


def test_a_connection_for_an_agent_no_longer_in_porch_is_offered_for_removal(desktop):
    config = {"mcpServers": {"porch-old": entry("gone")}}
    assert claude_desktop.plan(CLIENTS, config, desktop["settings"])["drop"] == {"porch-old": "gone"}


def test_a_name_already_taken_by_another_server_gets_a_number(desktop):
    config = {"mcpServers": {"porch-claude": {"command": "npx", "args": ["not-porch"]}}}
    assert list(claude_desktop.plan({"claude": "Claude Desktop"}, config, desktop["settings"])["add"]) == ["porch-claude-2"]


def test_only_connections_to_this_homes_porch_count(desktop):
    here = {**entry("dot"), "env": {"PORCH_CONFIG_DIR": str(desktop["settings"])}}
    elsewhere = {**entry("dot"), "env": {"PORCH_CONFIG_DIR": str(desktop["settings"].parent / "standin")}}
    other_gateway = {**entry("dot"), "env": {"PORCH_GATEWAY_URL": "http://127.0.0.1:8002/mcp"}}
    assert claude_desktop.agent_of(entry("dot"), desktop["settings"]) == "dot"
    assert claude_desktop.agent_of(here, desktop["settings"]) == "dot"
    assert claude_desktop.agent_of(elsewhere, desktop["settings"]) is None
    assert claude_desktop.agent_of(other_gateway, desktop["settings"]) is None
    assert claude_desktop.agent_of({"command": "npx", "args": ["x"]}, desktop["settings"]) is None


def test_a_change_keeps_everything_else_and_a_copy_of_the_old_settings(desktop):
    before = desktop["config"].read_text()
    backup = claude_desktop.change({"porch-claude": entry("claude")}, ["porch-dot"])
    after = json.loads(desktop["config"].read_text())
    assert backup is not None and backup.read_text() == before
    assert after["preferences"] == {"theme": "dark"}
    assert "porch-dot" not in after["mcpServers"] and after["mcpServers"]["porch-claude"] == entry("claude")
    assert after["mcpServers"]["notes"] == {"command": "npx", "args": ["some-other-server"]}
    assert stat.S_IMODE(desktop["config"].stat().st_mode) == 0o600


def test_nothing_changes_while_claude_desktop_stays_open(desktop, monkeypatch):
    monkeypatch.setattr(claude_desktop, "running", lambda: True)
    before = desktop["config"].read_text()
    asked, said = [], []
    done = claude_desktop.apply({"porch-claude": entry("claude")}, [], ask=asked.append, wait=lambda _: None,
                                say=said.append, reopen=lambda: pytest.fail("reopened an app that never closed"))
    assert done is False and desktop["config"].read_text() == before
    assert len(asked) == 1 and "Quit Claude Desktop" in asked[0]
    assert "nothing was changed" in said[-1]


def test_once_closed_it_changes_reopens_and_waits_for_each_new_connection(desktop, monkeypatch):
    states = iter([True, True, False])  # open, still closing, closed
    monkeypatch.setattr(claude_desktop, "running", lambda: next(states, False))

    def reopen() -> None:  # Claude Desktop starts the connection and logs its answer
        desktop["log"].write_text(f"{claude_desktop.now()[:19]}.120Z [info] [porch-claude] Message from server: "
                                  "id=1 result\n")

    said = []
    done = claude_desktop.apply({"porch-claude": entry("claude")}, [], ask=lambda _: "", wait=lambda _: None,
                                say=said.append, reopen=reopen)
    assert done is True
    assert "porch-claude" in json.loads(desktop["config"].read_text())["mcpServers"]
    assert "  porch-claude: started" in said


def test_check_names_the_connections_and_never_prints_a_key_or_changes_anything(desktop, capsys):
    before = desktop["config"].read_text()
    assert agents.main(["desktop", "--check"]) == 0
    out = capsys.readouterr().out
    assert "porch-claude" in out and "porch-energy" in out and "to add" in out
    assert "porch-instinct" in out and "already there" in out
    assert "porch-dot" in out and "lives in ChatGPT" in out
    assert SECRET_KEY not in out
    assert desktop["config"].read_text() == before


@pytest.mark.parametrize(("answer", "dropped"), [("", []), ("n", []), ("y", ["porch-dot", "porch-muse"])])
def test_connections_elsewhere_are_removed_only_when_the_owner_says_yes(desktop, monkeypatch, capsys, answer, dropped):
    calls = []
    monkeypatch.setattr("builtins.input", lambda _prompt: answer)
    monkeypatch.setattr(claude_desktop, "apply", lambda add, drop: calls.append((sorted(add), sorted(drop))) or True)
    assert agents.main(["desktop"]) == 0
    assert calls == [(["porch-claude", "porch-energy"], dropped)]
    assert SECRET_KEY not in capsys.readouterr().out
