"""App factory. Run the demo hub with: uvicorn porch_hub.main:app --reload"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__, api
from .commands import InvalidParams, UnsupportedCommand
from .digital_copy import DigitalCopy, env_flag
from .drivers import BaseDriver, DeviceNotFound, DeviceOffline, DriverError
from .drivers.matter import matter_driver_from_env
from .idempotency import IdempotencyConflict, IdempotencyStore
from .household import Household, household_file
from .layout import layout_file
from .protection import FamilyProtection, OwnerPin, PhysicalCommandBlocked, owner_pin_file, protection_file
from .registry import DeviceRegistry
from .router import CommandRouter
from .seed import demo_home

log = logging.getLogger(__name__)


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def _family_protection_default() -> bool:
    """On for a hub that runs real devices only, unless PORCH_FAMILY_PROTECTION says otherwise."""
    if os.environ.get("PORCH_FAMILY_PROTECTION", "").strip():
        return env_flag("PORCH_FAMILY_PROTECTION")
    return env_flag("PORCH_REAL_DEVICES_ONLY")


@dataclass(frozen=True)
class Settings:
    confirm_timeout_s: float = 5.0
    poll_interval_s: float = 0.2
    # The longest any one driver call (a read or a send) may take before the hub gives up on it.
    driver_timeout_s: float = 10.0
    idempotency_ttl_s: float = 24 * 3600
    # DEMO_OFFLINE_MODE=1: start with every command answered by the digital copy, no network needed.
    # It can also be switched at runtime with PUT /demo/offline-mode.
    demo_offline_mode: bool = field(default_factory=lambda: env_flag("DEMO_OFFLINE_MODE"))
    # DIGITAL_COPY_PATH: where the digital copy is saved, so an offline start has the rehearsal's devices.
    digital_copy_path: Path | None = field(default_factory=lambda: _env_path("DIGITAL_COPY_PATH"))
    # PORCH_REAL_DEVICES_ONLY=1: only the live platforms that are configured, and no simulated devices.
    real_devices_only: bool = field(default_factory=lambda: env_flag("PORCH_REAL_DEVICES_ONLY"))
    # PORCH_FAMILY_PROTECTION: real devices take commands only inside a window the owner opens
    # (see protection.py). On by default with PORCH_REAL_DEVICES_ONLY=1; set it to 0 to turn it off.
    family_protection: bool = field(default_factory=_family_protection_default)
    # PORCH_PROTECTION_FILE: the owner's control window. Default: permission.json in the settings folder.
    protection_path: Path = field(default_factory=protection_file)
    # PORCH_OWNER_PIN_FILE: the hash of the PIN that lets the owner open a window from a page.
    owner_pin_path: Path = field(default_factory=owner_pin_file)
    # PORCH_HOME_LAYOUT: the home's floor plan for the twin pages (see layout.py). Unset: the demo home's.
    home_layout_path: Path | None = field(default_factory=layout_file)
    # PORCH_HOUSEHOLD_FILE: who lives here and where each person is (household.py).
    household_path: Path = field(default_factory=household_file)
    # PORCH_GATEWAY_BASE_URL: the gateway the pages ask about agents and approvals.
    gateway_url: str = field(default_factory=lambda: os.environ.get("PORCH_GATEWAY_BASE_URL", "http://127.0.0.1:8000"))


LIVE_PLATFORM_SETTINGS = ("MATTER_SERVER_URL", "LG_THINQ_PAT", "GOOGLE_HOME_DIRECTORY", "RING_AUTHORIZATION_FILE")


def default_drivers(real_devices_only: bool = False, env: Mapping[str, str] = os.environ) -> list[BaseDriver]:
    """The demo home, with live platforms in place of simulated ones when they are configured.

    MATTER_SERVER_URL set: the local Matter Server. LG_THINQ_PAT set: the live LG account.
    GOOGLE_HOME_DIRECTORY set: the Google account signed in there, through Google Home MCP.
    RING_AUTHORIZATION_FILE set: the Ring account linked there (doorbells, read only).
    real_devices_only: leave every simulated platform out, Google Home's included.
    """
    home = demo_home()
    matter: BaseDriver | None = None if real_devices_only else home.matter
    thinq: BaseDriver | None = None if real_devices_only else home.thinq
    google: BaseDriver | None = None if real_devices_only else home.google
    ring: BaseDriver | None = None
    if env.get("MATTER_SERVER_URL"):
        matter = matter_driver_from_env(env)
    if env.get("LG_THINQ_PAT"):
        from .drivers.thinq.connect import thinq_driver_from_env

        thinq = thinq_driver_from_env(env)
    if env.get("GOOGLE_HOME_DIRECTORY"):
        from .drivers.google_home.driver import google_home_driver_from_env

        google = google_home_driver_from_env(env)
    if env.get("RING_AUTHORIZATION_FILE"):
        from .drivers.ring import ring_driver_from_env

        ring = ring_driver_from_env(env)
    drivers = [d for d in (matter, thinq, google, ring) if d is not None]
    if not drivers:
        raise RuntimeError(f"PORCH_REAL_DEVICES_ONLY=1 needs one of {', '.join(LIVE_PLATFORM_SETTINGS)}")
    return drivers


def create_app(drivers: Sequence[BaseDriver] | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    drivers = list(drivers) if drivers is not None else default_drivers(settings.real_devices_only)
    registry = DeviceRegistry(drivers)
    registry.driver_timeout_s = settings.driver_timeout_s
    owner_pin = OwnerPin(settings.owner_pin_path)  # one object: family protection and the household share its lock
    protection = (FamilyProtection(settings.protection_path, owner_pin) if settings.family_protection else None)
    if protection is not None:
        log.warning("family protection is on: real commands need the owner's control window (%s)",
                    settings.protection_path)
    digital_copy = DigitalCopy(registry, drivers, offline=settings.demo_offline_mode,
                               snapshot_path=settings.digital_copy_path,
                               demo_fallback=not settings.real_devices_only, protection=protection)
    hub = api.Hub(
        registry=registry,
        commands=CommandRouter(
            registry,
            IdempotencyStore(settings.idempotency_ttl_s),
            confirm_timeout_s=settings.confirm_timeout_s,
            driver_timeout_s=settings.driver_timeout_s,
            poll_interval_s=settings.poll_interval_s,
            digital_copy=digital_copy,
            protection=protection,
        ),
        digital_copy=digital_copy,
        protection=protection,
        home_layout_path=settings.home_layout_path,
        household=Household(settings.household_path, owner_pin),
        gateway_url=settings.gateway_url,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await digital_copy.start()
        # Power readings are kept from the moment the hub starts, whether or not a page is open.
        energy = asyncio.create_task(hub.energy.follow(registry.events))
        yield
        energy.cancel()
        await hub.commands.drain()  # commands still running finish before their drivers stop
        await digital_copy.stop()

    app = FastAPI(title="Porch Device Hub", version=__version__, lifespan=lifespan)
    app.state.hub = hub
    app.include_router(api.router)
    _add_error_handlers(app)
    return app


def _add_error_handlers(app: FastAPI) -> None:
    def detail(status_code: int):
        async def handler(_: Request, exc: Exception) -> JSONResponse:
            return JSONResponse(status_code=status_code, content={"detail": str(exc)})
        return handler

    async def invalid_params(_: Request, exc: InvalidParams) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": exc.errors})

    app.add_exception_handler(DeviceNotFound, detail(404))
    app.add_exception_handler(IdempotencyConflict, detail(409))
    # Family protection: nothing was sent, and the same request can run once a window is open.
    app.add_exception_handler(PhysicalCommandBlocked, detail(403))
    app.add_exception_handler(UnsupportedCommand, detail(422))
    app.add_exception_handler(InvalidParams, invalid_params)
    # Only reads reach these; command failures come back as status "failed".
    app.add_exception_handler(DeviceOffline, detail(503))
    app.add_exception_handler(DriverError, detail(502))


class QuietPolls(logging.Filter):
    """Keeps the pages' family-protection polls out of the access log.

    Every open page asks GET /protection every few seconds. Left in, those lines bury the
    commands and decisions the log is read for.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return '"GET /protection ' not in record.getMessage()


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("uvicorn.access").addFilter(QuietPolls())
app = create_app()
