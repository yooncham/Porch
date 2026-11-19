"""Files only the owner may read: tokens, sign-in caches, and the owner's control window.

They live in the hub's settings folder, outside the repository: PORCH_CONFIG_DIR, or
~/.config/porch-hub when that is not set.
"""

import os
import stat
import tempfile
from collections.abc import Mapping
from pathlib import Path


def config_dir(env: Mapping[str, str] = os.environ) -> Path:
    return Path(env.get("PORCH_CONFIG_DIR") or Path.home() / ".config" / "porch-hub").expanduser()


def private_write(path: Path, text: str) -> None:
    """Replace path with text in one step, readable and writable by the owner only."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError(f"{path.name} is a symlink; refusing to write through it")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def read_private(path: Path) -> str:
    """The text of a regular file that belongs to this user and that nobody else can read.

    Raises PermissionError when the file is a symlink, belongs to someone else, or is readable
    or writable by group or others, and OSError when it cannot be opened.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        if path.is_symlink():
            raise PermissionError(f"{path.name} is a symlink; it must be a regular file") from exc
        raise
    with os.fdopen(descriptor, "r") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError(f"{path.name} must be a regular file that only you can read (chmod 600)")
        return stream.read()
