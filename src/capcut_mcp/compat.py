"""Which CapCut versions this server has been checked against, and what to do about the rest.

The project format is undocumented and changes between releases. A project records the version
of CapCut that last saved it, so every write can be checked against what is known to work.
"""

from __future__ import annotations

import os

from .env import CapCutError

# Confirmed with the re-save loop described in docs/development.md.
VERIFIED_VERSIONS = ("8.3.0",)
# Other open-source tools report plain-JSON projects through the 9.x line. From 10 on, projects
# written by tools are reported to be rejected as corrupted and no working sample is published.
NEWEST_KNOWN_MAJOR = 9
ALLOW_ENV = "CAPCUT_MCP_ALLOW_UNTESTED"


def app_version(content: dict) -> str:
    """The CapCut version that last saved a project, as recorded in its timeline file."""
    for key in ("last_modified_platform", "platform"):
        block = content.get(key)
        if isinstance(block, dict) and block.get("app_version"):
            return str(block["app_version"]).strip()
    return ""


def app_source(content: dict) -> str:
    """"cc" for CapCut, "lv" for JianYing (the Chinese edition), "" when not recorded."""
    block = content.get("platform")
    return str(block.get("app_source") or "") if isinstance(block, dict) else ""


def _major(version: str) -> int | None:
    try:
        return int(version.split(".")[0])
    except ValueError:
        return None


def is_verified(version: str) -> bool:
    return any(version == known or version.startswith(known + ".") for known in VERIFIED_VERSIONS)


def status(version: str, source: str = "cc") -> tuple[str, str]:
    """How far a version can be trusted: (level, a sentence for the assistant to pass on).

    Levels: "verified", "unverified" (expected to work, not checked), "untested" (newer than
    anything known to work; writes are refused) and "unknown" (the project records no version).
    """
    verified = ", ".join(VERIFIED_VERSIONS)
    if source == "lv":
        return "unverified", (
            "This project was written by JianYing, not CapCut. The server was built for CapCut; "
            "basic edits may work, but library items use different ids and are likely to be missing.")
    if not version:
        return "unknown", ""
    if is_verified(version):
        return "verified", ""
    major = _major(version)
    if major is not None and major > NEWEST_KNOWN_MAJOR:
        return "untested", (
            f"This project was last saved by CapCut {version}. The server has been confirmed on "
            f"CapCut {verified} only, and CapCut {NEWEST_KNOWN_MAJOR + 1} and later are reported "
            "to reject projects edited by outside tools as corrupted.")
    return "unverified", (
        f"This project was last saved by CapCut {version}; the server has been confirmed on "
        f"CapCut {verified} only. Edits are expected to work, but the user should check the "
        "result in CapCut, and the checks in docs/development.md should be re-run for this version.")


def allow_untested() -> bool:
    return os.environ.get(ALLOW_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def require_supported(content: dict, name: str) -> None:
    """Refuse to write into a project from a CapCut version newer than anything known to work."""
    level, note = status(app_version(content), app_source(content))
    if level == "untested" and not allow_untested():
        raise CapCutError(
            f"'{name}' was not changed. {note} To try anyway, the user can set the environment "
            f"variable {ALLOW_ENV}=1 for the server and restart it; work on a duplicate of the "
            "project (duplicate_project) so the original stays safe.")
