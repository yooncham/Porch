from ...models import ControlPath
from ..memory import InMemoryDriver


class SimulatedMatterDriver(InMemoryDriver):
    """Matter devices on the in-memory digital copy, for tests and the offline demo.

    MatterDriver (driver.py) is the live path through the Matter Server.
    """

    control_path = ControlPath.MATTER
