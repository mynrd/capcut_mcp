"""A health check of the setup: CapCut, its version, the projects folder, FFmpeg, optional parts.

Most trouble with this server comes from outside it: CapCut updated itself to a version that
stores projects differently, FFmpeg is missing, the catalogue package is not installed. This
gathers what can be known about all of that in one place, with what to do about each finding.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from . import __version__, analysis, catalogue, compat, speech
from .draft import _content_files, _newest, project_folders
from .env import CapCutError, capcut_running, data_dir, drafts_dir
from .media import run_tool

_VERSION_FOLDER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:\.\d+)?$")


def _installed_versions(drafts: Path | None) -> list[str]:
    """CapCut versions present on disk (Windows keeps one folder per version under Apps)."""
    roots = []
    if os.environ.get("LOCALAPPDATA"):
        roots.append(Path(os.environ["LOCALAPPDATA"]) / "CapCut" / "Apps")
    if drafts is not None and len(drafts.parents) > 2:
        roots.append(drafts.parents[2] / "Apps")  # <CapCut>/User Data/Projects/com.lveditor.draft
    for root in roots:
        try:
            found = [p.name for p in root.iterdir() if p.is_dir() and _VERSION_FOLDER.match(p.name)]
        except OSError:
            continue
        if found:
            return sorted(found, key=lambda v: [int(part) for part in v.split(".")])
    return []


def _triple(version: str) -> list[int]:
    return [int(part) for part in version.split(".")[:3] if part.isdigit()]


def _tool_version(name: str) -> str | None:
    path = os.environ.get(f"{name.upper()}_PATH") or shutil.which(name)
    if not path:
        return None
    try:
        first = run_tool([path, "-version"], timeout=15, what=f"Asking {name} for its version").stdout
    except (CapCutError, OSError):
        return "found, but it did not answer"
    match = re.search(r"version\s+(\S+)", first)
    return match.group(1) if match else "found"


def _megabytes(folder: Path) -> float:
    total = 0
    try:
        for path in folder.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
    except OSError:
        pass
    return round(total / 1e6, 1)


def report() -> dict:
    problems: list[str] = []
    warnings: list[str] = []
    out: dict = {"server_version": __version__,
                 "confirmed_on_capcut": list(compat.VERIFIED_VERSIONS)}

    drafts: Path | None = None
    try:
        drafts = drafts_dir()
        out["projects_folder"] = str(drafts)
    except CapCutError as error:
        problems.append(str(error))

    last_saved_by = ""
    if drafts is not None:
        folders = project_folders()
        unreadable, versions = [], {}
        for folder in folders:
            try:
                content = json.loads(_newest(_content_files(folder)).read_text(encoding="utf-8"))
                version = compat.app_version(content) or "not recorded"
                versions[version] = versions.get(version, 0) + 1
                last_saved_by = last_saved_by or compat.app_version(content)
            except (ValueError, OSError, UnicodeDecodeError, CapCutError):
                unreadable.append(folder.name)
        out["projects"] = {"count": len(folders), "saved_by_capcut_version": versions}
        if unreadable:
            out["projects"]["unreadable"] = unreadable[:20]
            warnings.append(
                f"{len(unreadable)} project(s) are not readable JSON and cannot be edited. This "
                "is what an encrypted project looks like; some CapCut and JianYing versions "
                "encrypt their project files.")
        if not folders:
            warnings.append("No projects were found in the projects folder yet.")

    installed = _installed_versions(drafts)
    capcut: dict = {"running": capcut_running()}
    if installed:
        capcut["versions_on_disk"] = installed
    if last_saved_by:
        capcut["newest_project_saved_by"] = last_saved_by
        level, note = compat.status(last_saved_by)
        capcut["status"] = level
        if level == "untested":
            problems.append(note + " Edits are refused for such projects.")
        elif level == "unverified":
            warnings.append(note)
    if installed and last_saved_by:
        newest = installed[-1]
        if _triple(newest) > _triple(last_saved_by):
            level, _ = compat.status(".".join(newest.split(".")[:3]))
            warnings.append(
                f"CapCut {newest} is on this computer but projects were last saved by "
                f"{last_saved_by}. CapCut may switch to the newer version by itself"
                + (", and that version is newer than anything this server is known to work "
                   "with." if level == "untested" else "; the server has not been confirmed on it.")
                + " After it does, check one edit in CapCut before relying on the server.")
    out["capcut"] = capcut

    tools = {name: _tool_version(name) for name in ("ffmpeg", "ffprobe")}
    out["ffmpeg"] = tools
    if not all(tools.values()):
        problems.append("FFmpeg was not found (both ffmpeg and ffprobe are needed). Install it and "
                        "put it on PATH, or set FFMPEG_PATH and FFPROBE_PATH. Without it no media "
                        "can be added or looked at.")

    if catalogue.available():
        out["catalogue"] = {"available": True, "items": sum(catalogue.counts().values())}
    else:
        out["catalogue"] = {"available": False}
        warnings.append("The item catalogue is not available (the pycapcut package is missing). "
                        "Run 'uv sync' in the server's folder. Items from the user's own projects "
                        "still work.")
    out["transcription"] = {"available": analysis.transcription_available()}
    if not out["transcription"]["available"]:
        out["transcription"]["how_to_enable"] = (
            "Optional. Run 'uv sync --extra transcribe' in the server's folder to let the server "
            "turn speech into text (auto_captions, transcribe_media).")

    voice: dict = {"available": speech.available()}
    if voice["available"]:
        try:
            found = speech.voices()
            voice["voices"] = [v["name"] for v in found["voices"]]
            voice["default"] = found["default"]
            if not found["voices"]:
                warnings.append("Windows has no speech voices enabled, so add_voiceover cannot "
                                "speak. Voices come with Windows' speech and language settings.")
        except CapCutError as error:
            voice["available"] = False
            warnings.append(f"Text to speech (add_voiceover) does not work here: {error}")
    else:
        voice["note"] = ("add_voiceover uses the voices built into Windows and is not available "
                         "on this system. Voice-overs made elsewhere can be placed with add_audio.")
    out["text_to_speech"] = voice

    home = data_dir()
    out["storage"] = {"folder": str(home), "megabytes": {
        name: _megabytes(home / name)
        for name in ("backups", "previews", "frames", "stills", "transcripts", "voiceovers")
        if (home / name).is_dir()}}

    if problems:
        out["problems"] = problems
    if warnings:
        out["warnings"] = warnings
    out["ok"] = not problems
    return out
