from ..models import ControlPath, Device
from .base import CommandRejected
from .memory import InMemoryDriver

# Google Home MCP blocks sensitive actions such as unlocking doors.
BLOCKED_COMMANDS = frozenset({"UnlockDoor"})


class GoogleHomeDriver(InMemoryDriver):
    """Google Home through the Home MCP server (home.googleapis.com/mcp).

    Simulated for now. The real connector is an MCP client using
    list_home_resources, list_home_states and run_home_actions. Google's
    traits are derived from Matter clusters, so mapping is close to one to one.
    Home MCP pushes no events, so the real driver must poll list_home_states.
    """

    control_path = ControlPath.GOOGLE_HOME

    def check_allowed(self, device: Device, command: str) -> None:
        if command in BLOCKED_COMMANDS:
            raise CommandRejected(
                f"Google Home blocks {command}; add {device.id} to Porch's Matter fabric to control it"
            )
