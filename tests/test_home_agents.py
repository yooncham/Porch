"""deploy/home/agents.py: the owner gives a personal agent its own key and rights in the gateway's
settings, and a Claude on the same computer connects to Porch as that agent."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "deploy" / "home" / "agents.py"
spec = importlib.util.spec_from_file_location("home_agents", SCRIPT)
agents = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agents)

SECRET_KEY = "energy-key-do-not-print"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """A settings folder with two gateway files, as a home that keeps one for a demo has."""
    folder = tmp_path / "porch-hub"
    folder.mkdir()
    for name in ("porch_config.json", "porch_config.demo.json"):
        path = folder / name
        path.write_text(json.dumps({"hub_url": "http://127.0.0.1:8100", "agents": {
            SECRET_KEY: {"id": "energy-agent", "name": "Energy agent", "acts_for": "Erik",
                         "scope_label": "Energy Management Only", "scopes": ["lighting", "appliance"]}}}))
        path.chmod(0o600)
    monkeypatch.setenv("PORCH_CONFIG_DIR", str(folder))
    monkeypatch.delenv("PORCH_GATEWAY_CONFIG", raising=False)
    return folder


def stored(folder: Path, name: str = "porch_config.json") -> dict:
    return json.loads((folder / name).read_text())["agents"]


def test_a_new_agent_gets_one_key_in_every_settings_file_and_it_is_never_printed(settings, capsys):
    assert agents.main(["add", "dot", "--name", "Dot", "--acts-for", "Yoonchan"]) == 0
    out = capsys.readouterr().out
    everyday, demo = stored(settings), stored(settings, "porch_config.demo.json")
    key = next(k for k, a in everyday.items() if a["id"] == "dot")
    assert len(key) >= 40 and key in demo and demo[key] == everyday[key]  # one key, the same in both
    assert everyday[key] == {"id": "dot", "name": "Dot", "acts_for": "Yoonchan",
                             "scope_label": "All devices, with approvals", "scopes": agents.ALL_SCOPES}
    assert key not in out and SECRET_KEY not in out and "Restart Porch" in out
    assert SECRET_KEY in everyday  # the agents that were there stay
    for name in ("porch_config.json", "porch_config.demo.json"):
        assert (settings / name).stat().st_mode & 0o777 == 0o600

    assert agents.main(["list"]) == 0
    listed = capsys.readouterr().out
    assert "dot" in listed and "Energy agent" in listed and key not in listed and SECRET_KEY not in listed


def test_scopes_can_be_narrowed_and_mistakes_change_nothing(settings):
    assert agents.main(["add", "muse", "--scopes", "lighting", "sensor"]) == 0
    muse = next(a for a in stored(settings).values() if a["id"] == "muse")
    assert (muse["name"], muse["scopes"], muse["scope_label"]) == ("Muse", ["lighting", "sensor"], "lighting, sensor")
    before = (settings / "porch_config.json").read_text()
    for wrong in (["add", "muse"],                             # the id is taken
                  ["add", "two words"], ["add", "../x"],       # not an id
                  ["add", "instinct", "--scopes", "everything"]):  # not a scope the gateway knows
        with pytest.raises(SystemExit, match="Stopped"):
            agents.main(wrong)
    assert (settings / "porch_config.json").read_text() == before


def test_a_settings_file_others_can_read_is_refused(settings):
    (settings / "porch_config.json").chmod(0o644)
    with pytest.raises(SystemExit, match="can be read by others"):
        agents.main(["add", "dot"])
    assert "dot" not in {a["id"] for a in stored(settings, "porch_config.demo.json").values()}


def test_removing_an_agent_takes_its_key_out_of_every_file(settings, capsys):
    agents.main(["add", "dot"])
    assert agents.main(["remove", "dot"]) == 0
    assert "Restart Porch" in capsys.readouterr().out
    assert [a["id"] for a in stored(settings).values()] == ["energy-agent"]
    assert [a["id"] for a in stored(settings, "porch_config.demo.json").values()] == ["energy-agent"]
    with pytest.raises(SystemExit, match="no agent with the id dot"):
        agents.main(["remove", "dot"])


def test_one_named_settings_file_is_the_only_one_touched(settings, monkeypatch):
    monkeypatch.setenv("PORCH_GATEWAY_CONFIG", str(settings / "porch_config.demo.json"))
    agents.main(["add", "dot"])
    assert "dot" in {a["id"] for a in stored(settings, "porch_config.demo.json").values()}
    assert "dot" not in {a["id"] for a in stored(settings).values()}


def test_connecting_as_an_agent_that_does_not_exist_says_how_to_add_it(settings):
    with pytest.raises(SystemExit, match="deploy/home/agent.sh add ghost"):
        agents.main(["connect", "ghost"])


def test_the_key_never_goes_into_a_command_line_or_an_apps_settings():
    """The bridge reads the key itself and puts it in a request header, so a Claude app's settings
    name only the agent."""
    script = SCRIPT.read_text()
    assert 'headers={"Authorization": f"Bearer {key}"}' in script
    # The one other place a key is written into text: an online key's connector address, which
    # goes to the clipboard and nowhere else.
    clipboard_only = 'copy_to_clipboard(key if args.header else f"{address}/k/{key}/mcp")'
    assert script.count(clipboard_only) == 1
    rest = script.replace('f"Bearer {key}"', "").replace(clipboard_only, "")
    assert "print(key" not in script and "{key}" not in rest
    wrapper = SCRIPT.with_name("agent.sh").read_text()
    assert 'agents.py" "$@"' in wrapper and "Bearer" not in wrapper


def test_the_connection_tells_the_agent_who_it_is_and_how_to_use_porch():
    told = agents.instructions({"id": "muse", "name": "Muse", "acts_for": "Yoonchan"})
    assert "as the agent Muse, acting for Yoonchan" in told
    for tool in ("describe_home", "get_state", "preview_action", "request_action"):
        assert tool in told, tool
    # An agent must not say a device changed on its own word, or look for a way round a refusal.
    assert "only when request_action answered that it was executed and verified" in told
    assert "do not try another way to reach the device" in told
    # Several Porch connections can be on in one chat: each says it is one agent only.
    assert "Use only this connection's tools" in told and "you are Muse" in told
    assert "no authority" not in told
    energy = agents.instructions({"id": "energy-agent", "name": "Energy agent", "acts_for": "the household",
                                  "reads": "energy"})
    assert "ask Porch with request_action anyway" in energy and "no authority to control devices" in energy


def test_the_connection_keeps_its_tools_and_says_so_when_porch_is_down():
    """A Claude app asks for the tools once, when it starts. If Porch is down at that moment the
    agent must not be left with none: the bridge lists the ones Porch offered last time, and a
    call answers plainly that nothing was changed."""
    script = SCRIPT.read_text()
    assert "TOOLS_FILE" in script and "listing the tools it offered last time" in script
    assert "nothing was read and nothing was changed" in agents.NOT_RUNNING
    assert "is_error=True" in script and "create_proxy" not in script  # no silent empty tool list


# Online keys: for Claude's connector form, when an agent works from the phone or a browser

@pytest.fixture
def clipboard(monkeypatch):
    copied: list[str] = []
    monkeypatch.setattr(agents, "copy_to_clipboard", copied.append)
    return copied


DOOR = "https://porch-mac.tail1234.ts.net"


@pytest.fixture
def door_open(settings):
    """online.sh on has run: it keeps the door's address beside the settings."""
    (settings / agents.ONLINE_STATE_FILE).write_text(json.dumps({"enabled": True, "address": f"{DOOR}/mcp"}))
    return DOOR


def key_in(address: str) -> str:
    assert address.startswith(f"{DOOR}/k/") and address.endswith("/mcp"), address
    return address[len(f"{DOOR}/k/"):-len("/mcp")]


def test_an_online_key_goes_to_the_clipboard_and_only_its_hash_is_kept(settings, door_open, clipboard, capsys):
    import hashlib

    agents.main(["add", "muse"])
    capsys.readouterr()
    assert agents.main(["online-key", "muse"]) == 0
    out = capsys.readouterr().out
    key = key_in(clipboard[-1])  # the whole connector address, with the key in it
    assert key.startswith("porch-online-") and len(key) > 40 and key not in out and clipboard[-1] not in out
    assert "No sign-in" in out and "No authentication" in out
    kept = json.loads((settings / agents.ONLINE_KEYS_FILE).read_text())["keys"]
    assert list(kept) == [hashlib.sha256(key.encode()).hexdigest()] and kept[hashlib.sha256(key.encode()).hexdigest()]["agent"] == "muse"
    assert key not in (settings / agents.ONLINE_KEYS_FILE).read_text()
    assert (settings / agents.ONLINE_KEYS_FILE).stat().st_mode & 0o777 == 0o600
    # A new key replaces the old one, and list says which agents have one.
    agents.main(["online-key", "muse"])
    assert len(json.loads((settings / agents.ONLINE_KEYS_FILE).read_text())["keys"]) == 1 and key_in(clipboard[-1]) != key
    agents.main(["list"])
    assert "online key" in capsys.readouterr().out
    # Taking it away, and removing the agent, both leave no online key behind.
    assert agents.main(["online-off", "muse"]) == 0
    assert json.loads((settings / agents.ONLINE_KEYS_FILE).read_text())["keys"] == {}
    agents.main(["online-key", "muse"])
    agents.main(["remove", "muse"])
    assert json.loads((settings / agents.ONLINE_KEYS_FILE).read_text())["keys"] == {}


def test_the_header_form_copies_only_the_key_and_needs_no_address(settings, clipboard, capsys):
    import hashlib

    agents.main(["add", "muse"])
    capsys.readouterr()
    assert agents.main(["online-key", "muse", "--header"]) == 0
    key = clipboard[-1]
    assert key.startswith("porch-online-") and key not in capsys.readouterr().out
    assert hashlib.sha256(key.encode()).hexdigest() in json.loads((settings / agents.ONLINE_KEYS_FILE).read_text())["keys"]


def test_no_address_form_before_the_door_was_ever_opened(settings, clipboard):
    agents.main(["add", "muse"])
    with pytest.raises(SystemExit, match="online address is not known yet"):
        agents.main(["online-key", "muse"])
    assert clipboard == [] and not (settings / agents.ONLINE_KEYS_FILE).exists()


def test_no_online_key_without_an_agent_or_a_clipboard(settings, door_open, monkeypatch):
    with pytest.raises(SystemExit, match="no agent with the id ghost"):
        agents.main(["online-key", "ghost"])
    agents.main(["add", "muse"])

    def broken(_text):
        raise OSError("no clipboard")

    monkeypatch.setattr(agents, "copy_to_clipboard", broken)
    with pytest.raises(SystemExit, match="could not copy the key"):
        agents.main(["online-key", "muse"])
    assert not (settings / agents.ONLINE_KEYS_FILE).exists()  # no key exists that nobody could see


def test_set_changes_whose_agent_it_is_and_what_it_reads(settings, capsys):
    agents.main(["add", "dot", "--person", "yoonchan"])
    dot = next(a for a in stored(settings).values() if a["id"] == "dot")
    assert dot["person"] == "yoonchan" and "reads" not in dot
    assert agents.main(["set", "energy-agent", "--reads", "energy", "--scopes", "none"]) == 0
    energy = next(a for a in stored(settings).values() if a["id"] == "energy-agent")
    assert (energy["reads"], energy["scopes"], energy["scope_label"]) == ("energy", [], "Energy readings only")
    assert energy == next(a for a in stored(settings, "porch_config.demo.json").values() if a["id"] == "energy-agent")
    agents.main(["set", "dot", "--person", "none", "--name", "Dot"])
    assert "person" not in next(a for a in stored(settings).values() if a["id"] == "dot")
    for wrong in (["set", "ghost", "--name", "x"], ["set", "dot", "--reads", "everything"],
                  ["set", "dot", "--scopes", "flying"]):
        with pytest.raises(SystemExit, match="Stopped"):
            agents.main(wrong)
    assert SECRET_KEY not in capsys.readouterr().out
