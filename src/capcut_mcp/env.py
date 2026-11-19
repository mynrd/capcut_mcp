"""Where things live on this machine, and whether CapCut is running."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import BinaryIO

from mcp.server.mcpserver.exceptions import ToolError


class CapCutError(ToolError):
    """A problem the caller can act on.

    Being a ToolError is what gets the message to the assistant; the SDK hides the text of any
    other exception behind a generic "Error executing tool".
    """


def drafts_dir() -> Path:
    """The folder CapCut keeps its projects in."""
    override = os.environ.get("CAPCUT_DRAFTS_DIR")
    if override:
        path = Path(override).expanduser()
        if not path.is_dir():
            raise CapCutError(f"CAPCUT_DRAFTS_DIR points to a folder that does not exist: {path}")
        return path

    candidates: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft")
    candidates.append(Path.home() / "Movies" / "CapCut" / "User Data" / "Projects" / "com.lveditor.draft")
    for path in candidates:
        if path.is_dir():
            return path
    raise CapCutError(
        "Could not find CapCut's projects folder. Set the CAPCUT_DRAFTS_DIR environment variable "
        "to it (CapCut shows the location under Settings > Project > Save to)."
    )


def capcut_user_data() -> Path | None:
    """CapCut's own "User Data" folder, which holds its cache and bundled resources."""
    try:
        folder = drafts_dir().parent.parent
    except CapCutError:
        return None
    return folder if (folder / "Resources").is_dir() else None


def data_dir() -> Path:
    """Our own folder for backups and extracted frames. Never inside CapCut's folders."""
    override = os.environ.get("CAPCUT_MCP_HOME")
    if override:
        path = Path(override).expanduser()
    elif os.environ.get("LOCALAPPDATA"):
        path = Path(os.environ["LOCALAPPDATA"]) / "capcut-mcp"
    else:
        path = Path.home() / ".capcut-mcp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def capcut_running() -> bool:
    """True when a CapCut process exists. Errs toward True if we cannot tell."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq CapCut.exe", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
            return "capcut.exe" in out.lower()
        # Match the process name exactly. Matching the whole command line (-f) would also find
        # this server, whose own command line contains "capcut".
        out = subprocess.run(["pgrep", "-ix", "capcut"], capture_output=True, text=True, timeout=10,
                             stdin=subprocess.DEVNULL)
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return True


def lock_file(path: Path, wait: float, busy: str) -> BinaryIO:
    """Take an exclusive lock that other server processes respect. Returns the open handle.

    The operating system drops the lock when the process ends, so a crash cannot leave one
    behind. Waits up to `wait` seconds, then raises CapCutError with the `busy` message.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    deadline = time.monotonic() + wait
    while True:
        try:
            if sys.platform == "win32":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            if time.monotonic() >= deadline:
                handle.close()
                raise CapCutError(busy) from None
            time.sleep(0.05)


def unlock_file(handle: BinaryIO) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        handle.close()
