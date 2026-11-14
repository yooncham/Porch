"""App factory. Run the demo hub with: uvicorn porch_hub.main:app --reload"""

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
from .registry import DeviceRegistry
from .router import CommandRouter
from .seed import demo_home


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


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


def default_drivers(real_devices_only: bool = False, env: Mapping[str, str] = os.environ) -> list[BaseDriver]:
    """The demo home, with live platforms in place of simulated ones when they are configured.

    MATTER_SERVER_URL set: the local Matter Server. LG_THINQ_PAT set: the live LG account.
    real_devices_only: leave every simulated platform out, Google Home's included.
    """
    home = demo_home()
    matter: BaseDriver | None = None if real_devices_only else home.matter
    thinq: BaseDriver | None = None if real_devices_only else home.thinq
    google: BaseDriver | None = None if real_devices_only else home.google
    if env.get("MATTER_SERVER_URL"):
        matter = matter_driver_from_env(env)
    if env.get("LG_THINQ_PAT"):
        from .drivers.thinq.connect import thinq_driver_from_env

        thinq = thinq_driver_from_env(env)
    drivers = [d for d in (matter, thinq, google) if d is not None]
    if not drivers:
        raise RuntimeError("PORCH_REAL_DEVICES_ONLY=1 needs MATTER_SERVER_URL or LG_THINQ_PAT")
    return drivers


def create_app(drivers: Sequence[BaseDriver] | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    drivers = list(drivers) if drivers is not None else default_drivers(settings.real_devices_only)
    registry = DeviceRegistry(drivers)
    registry.driver_timeout_s = settings.driver_timeout_s
    digital_copy = DigitalCopy(registry, drivers, offline=settings.demo_offline_mode,
                               snapshot_path=settings.digital_copy_path,
                               demo_fallback=not settings.real_devices_only)
    hub = api.Hub(
        registry=registry,
        commands=CommandRouter(
            registry,
            IdempotencyStore(settings.idempotency_ttl_s),
            confirm_timeout_s=settings.confirm_timeout_s,
            driver_timeout_s=settings.driver_timeout_s,
            poll_interval_s=settings.poll_interval_s,
            digital_copy=digital_copy,
        ),
        digital_copy=digital_copy,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await digital_copy.start()
        yield
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
    app.add_exception_handler(UnsupportedCommand, detail(422))
    app.add_exception_handler(InvalidParams, invalid_params)
    # Only reads reach these; command failures come back as status "failed".
    app.add_exception_handler(DeviceOffline, detail(503))
    app.add_exception_handler(DriverError, detail(502))


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
app = create_app()
