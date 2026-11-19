"""The scripts in deploy/home that start and manage a real home. They are not run here (they
would start servers); they are checked for syntax and for the promises their comments make."""

import subprocess
from pathlib import Path

import pytest

HOME_SCRIPTS = Path(__file__).parents[1] / "deploy" / "home"


@pytest.mark.parametrize("script", sorted(p.name for p in HOME_SCRIPTS.glob("*.sh")))
def test_every_home_script_is_valid_shell_and_can_be_run(script):
    path = HOME_SCRIPTS / script
    assert subprocess.run(["bash", "-n", str(path)], capture_output=True).returncode == 0
    assert path.stat().st_mode & 0o111, f"{script} is not executable"


def test_the_one_window_start_does_not_fight_a_running_porch_and_cleans_up():
    script = (HOME_SCRIPTS / "run_all.sh").read_text()
    # It refuses while anything already listens on the Matter Server's, the hub's or the gateway's port.
    assert "for port in 5580 8100 8000 8090" in script and "another Porch is running" in script
    # The Matter Server it started stops with it, and only a home that uses one gets one.
    assert "trap stop_matter EXIT" in script and "grep -q '^MATTER_SERVER_URL='" in script
    # It says so when macOS keeps the terminal off the local network, and keeps the Mac awake.
    assert "EHOSTUNREACH" in script and "Local Network" in script and "caffeinate -i" in script
    assert script.index('"$HERE/matter_server.sh"') < script.index('"$HERE/start.sh"')  # the Matter Server first


def test_the_one_window_start_runs_the_online_door_and_stops_it():
    script = (HOME_SCRIPTS / "run_all.sh").read_text()
    assert '"$HERE/online.sh" serve &' in script and "trap stop_all EXIT" in script
    assert script.index('"$HERE/online.sh" serve') < script.index('"$HERE/start.sh"')
