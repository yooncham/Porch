from ...models import ControlPath, Device
from ..base import CommandRejected
from ..memory import InMemoryDriver

# Google Home MCP blocks sensitive actions such as unlocking doors.
BLOCKED_COMMANDS = frozenset({"UnlockDoor"})


class GoogleHomeDriver(InMemoryDriver):
    """Google Home for the simulated demo home: devices kept in memory.

    The live connector is GoogleHomeMcpDriver (driver.py), an MCP client of Google's Home MCP
    server (home.googleapis.com/mcp). This one applies the rule the demo needs from it: Google
    blocks unlocking.
    """

    control_path = ControlPath.GOOGLE_HOME

    def check_allowed(self, device: Device, command: str) -> None:
        if command in BLOCKED_COMMANDS:
            raise CommandRejected(
                f"Google Home blocks {command}; add {device.id} to Porch's Matter fabric to control it"
            )
