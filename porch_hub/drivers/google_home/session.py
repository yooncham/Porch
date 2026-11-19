"""A session with Google's Home MCP server, signed in as the home's owner.

Google Home's API for other programs is an MCP server at home.googleapis.com/mcp, behind Google
sign-in. This module opens a session to it through mcp-remote, a small Node.js bridge that does
the OAuth sign-in and keeps the tokens.

Everything a session needs is in one folder only the owner can read (GOOGLE_HOME_DIRECTORY,
normally ~/.config/porch-hub/google-home):

    google-home-client.json   the OAuth client (client_id, client_secret) from Google Cloud
    google-home-auth/         the bridge's token cache
    google-home-auth.log      what the bridge wrote to stderr

The bridge is given file paths, never a secret, and none of the hub's other credentials. A
session can list tools and call the three read tools. It can call the action tool only when it
was opened with allow_actions, and never the tool that writes automations.
"""

import asyncio
import importlib.util
import json
import os
import shutil
import stat
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from ...private_files import private_write
from ..base import DriverError

HOME_MCP_URL = "https://home.googleapis.com/mcp"
HOME_SCOPE = "https://www.googleapis.com/auth/home.platform.v2"
# Pinned: this exact version did the sign-in and the calls this driver was checked against.
BRIDGE_PACKAGE = "mcp-remote@0.16.6"
BRIDGE_CALLBACK_PORT = "8989"
INVENTORY_TOOLS = frozenset({"list_homes", "list_home_resources"})
READ_TOOLS = INVENTORY_TOOLS | {"list_home_states"}
ACTION_TOOL = "run_home_actions"
AUTOMATION_TOOL = "save_automation"

CLIENT_FILE = "google-home-client.json"
METADATA_FILE = "google-home-metadata.json"
CACHE_DIR = "google-home-auth"
LOG_FILE = "google-home-auth.log"
LOG_MAX_BYTES = 1_000_000
# Starting the bridge and the MCP handshake. Sign-in itself happens on the first tool listing.
OPEN_TIMEOUT_S = 60.0


class SetupIncomplete(DriverError):
    """The folder is not ready for a session: a file, the SDK or Node.js is missing."""


def validate_credentials(path: Path) -> None:
    """Raise unless path is a private file holding a Google OAuth client id and secret."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise PermissionError(f"{path.name} must be a regular file that only you can read (chmod 600)")
    value = json.loads(path.read_text())
    client_id = value.get("client_id") if isinstance(value, dict) else None
    secret = value.get("client_secret") if isinstance(value, dict) else None
    if (not isinstance(client_id, str) or not client_id.endswith(".apps.googleusercontent.com")
            or not isinstance(secret, str) or not secret.strip()):
        raise ValueError(f"{path.name} does not hold a Google OAuth client_id and client_secret")


def setup_problems(directory: Path) -> list[str]:
    """What is still missing before a session can open. Checks this computer only; contacts nobody."""
    problems = []
    try:
        validate_credentials(directory / CLIENT_FILE)
    except FileNotFoundError:
        problems.append(f"{CLIENT_FILE} is missing from {directory}")
    except (PermissionError, ValueError) as exc:  # these messages name the file, never its contents
        problems.append(str(exc))
    except OSError:
        problems.append(f"{CLIENT_FILE} in {directory} cannot be read")
    if importlib.util.find_spec("mcp") is None:
        problems.append('the MCP SDK is not installed: uv pip install --python .venv/bin/python -e ".[google]"')
    if shutil.which("npx") is None:
        problems.append("Node.js (npx) is not installed: brew install node")
    return problems


def bridge_arguments(directory: Path, auth_timeout: int, *, allow_actions: bool = False) -> list[str]:
    """Only file paths appear in the bridge's arguments, never a credential value."""
    args = [
        "-y", BRIDGE_PACKAGE, HOME_MCP_URL, BRIDGE_CALLBACK_PORT,
        "--host", "127.0.0.1",
        "--bind-host", "127.0.0.1",
        "--transport", "http-only",
        "--auth-timeout", str(auth_timeout),
        "--authorize-param", "access_type=offline",
        "--authorize-param", "prompt=consent",
        "--static-oauth-client-info", "@" + str(directory / CLIENT_FILE),
        "--static-oauth-client-metadata", "@" + str(directory / METADATA_FILE),
        "--ignore-tool", AUTOMATION_TOOL,
    ]
    if not allow_actions:
        args.extend(["--ignore-tool", ACTION_TOOL])
    return args


def bridge_environment(directory: Path) -> dict[str, str]:
    """The bridge's environment: enough to run Node.js, and none of the hub's own credentials."""
    env = {key: os.environ[key]
           for key in ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
           if key in os.environ}
    env["MCP_REMOTE_CONFIG_DIR"] = str(directory / CACHE_DIR)
    env["NODE_TLS_REJECT_UNAUTHORIZED"] = "1"
    return env


class SessionAdapter:
    """The session as plain JSON, limited to the tools this driver is meant to call."""

    def __init__(self, session: Any, *, allow_actions: bool = False) -> None:
        self.session = session
        self.allow_actions = allow_actions

    async def list_tools(self, cursor: str | None) -> dict[str, Any]:
        from mcp.types import PaginatedRequestParams

        params = PaginatedRequestParams(cursor=cursor) if cursor else None
        result = await self.session.list_tools(params=params)
        return result.model_dump(mode="json", by_alias=True, exclude_none=True)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name not in READ_TOOLS and not (self.allow_actions and name == ACTION_TOOL):
            raise ValueError(f"{name} is not allowed for this session")
        result = await self.session.call_tool(name, arguments=arguments)
        return result.model_dump(mode="json", by_alias=True, exclude_none=True)


def _open_log(directory: Path):
    """The bridge's stderr, kept privately. Nothing from it is echoed anywhere else."""
    path = directory / LOG_FILE
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
    try:
        if path.lstat().st_size > LOG_MAX_BYTES:
            flags |= os.O_TRUNC
    except OSError:
        pass
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "a")


@asynccontextmanager
async def google_session(directory: Path, *, auth_timeout: int = 600,
                         allow_actions: bool = False) -> AsyncIterator[SessionAdapter]:
    """Open a session. With no saved sign-in, the bridge opens a browser for the owner to sign in.

    auth_timeout is how long that sign-in may take, in seconds. The session must be closed by
    the task that opened it (the MCP SDK requires it).
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    if problems := setup_problems(directory):
        raise SetupIncomplete("Google Home is not set up: " + "; ".join(problems))
    cache = directory / CACHE_DIR
    if cache.is_symlink():
        raise SetupIncomplete(f"{CACHE_DIR} is a symlink; it must be a real folder")
    cache.mkdir(mode=0o700, exist_ok=True)
    cache.chmod(0o700)
    private_write(directory / METADATA_FILE,
                  json.dumps({"scope": HOME_SCOPE, "token_endpoint_auth_method": "client_secret_post"}))
    params = StdioServerParameters(
        command=shutil.which("npx"),
        args=bridge_arguments(directory, auth_timeout, allow_actions=allow_actions),
        env=bridge_environment(directory),
    )
    with _open_log(directory) as log:
        async with stdio_client(params, errlog=log) as (read, write):
            async with ClientSession(read, write, read_timeout_seconds=auth_timeout + 60) as session:
                async with asyncio.timeout(OPEN_TIMEOUT_S):
                    await session.initialize()
                yield SessionAdapter(session, allow_actions=allow_actions)
