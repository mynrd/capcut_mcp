"""A CapCut project on disk and the edits that can be made to it.

Times inside a draft are integer microseconds. Public methods take and return seconds.
"""

from __future__ import annotations

import contextlib
import contextvars
import copy
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import compat, schema
from .env import CapCutError, capcut_running, data_dir, drafts_dir, lock_file, unlock_file
from .media import MediaInfo, extract_frame, probe

CONTENT_FILE = "draft_content.json"
# Windows builds keep the timeline in draft_content.json; macOS builds use draft_info.json.
CONTENT_NAMES = (CONTENT_FILE, "draft_info.json")
META_FILE = "draft_meta_info.json"
MAX_BACKUPS = 100          # undo steps kept per project
MAX_REDO = 20
MAX_AUTO_CHECKPOINTS = 10  # copies kept automatically when an undo discards work done in CapCut
LAST_WRITTEN = "last-written.snapshot"
LOCK_WAIT_SECONDS = 30.0
CANVAS_RATIOS = {"16:9": 16 / 9, "9:16": 9 / 16, "1:1": 1.0, "4:3": 4 / 3, "3:4": 3 / 4,
                 "2:1": 2.0, "2.35:1": 2.35, "1.85:1": 1.85}
_INVALID_NAME = re.compile(r'[\\/:*?"<>|]')


# ------------------------------------------------------------------------- file IO

def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise CapCutError(
            f"{path} is not readable JSON. This CapCut version may encrypt its project files, "
            "which this server cannot edit."
        ) from None
    if not isinstance(data, dict):
        raise CapCutError(f"{path} does not look like a CapCut project file.")
    return data


def _encode(data: dict) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _write_json(path: Path, data: dict) -> None:
    _write_bytes(path, _encode(data))


def _write_bytes(path: Path, payload: bytes) -> None:
    """Write through a temporary file and swap it in, so a crash cannot leave half a file.

    The temporary name is unique to this process and thread. Windows refuses the swap while
    another program (CapCut, a virus scanner) has the file open, so it is retried briefly.
    """
    temp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.mcp-tmp")
    temp.write_bytes(payload)
    for attempt in range(6):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if attempt == 5:
                temp.unlink(missing_ok=True)
                raise CapCutError(
                    f"Could not write {path.name}: another program is holding the file open. "
                    "Try again in a moment.") from None
            time.sleep(0.15 * (attempt + 1))


def _now_us() -> int:
    return time.time_ns() // 1000


def _seconds(us: int | float) -> float:
    return round(us / 1e6, 3)


def _root_content(folder: Path) -> Path | None:
    """The timeline file in a project's own folder, under whichever name this build uses."""
    found = [folder / name for name in CONTENT_NAMES if (folder / name).is_file()]
    return max(found, key=lambda f: f.stat().st_mtime) if found else None


def _content_files(folder: Path) -> list[Path]:
    """Every copy of the timeline CapCut keeps for a project (root, plus Timelines/<id>/).

    Which copy CapCut reads differs between builds, so all of them are kept identical.
    """
    files = [folder / name for name in CONTENT_NAMES if (folder / name).is_file()] or [folder / CONTENT_FILE]
    index = folder / "Timelines" / "project.json"
    if index.is_file():
        try:
            main = json.loads(index.read_text(encoding="utf-8")).get("main_timeline_id")
        except ValueError:
            main = None
        nested = folder / "Timelines" / main if main else None
        if nested and nested.is_dir():
            present = [nested / name for name in CONTENT_NAMES if (nested / name).is_file()]
            files += present or [nested / file.name for file in files]
    return files


def _mirror_files(folder: Path, tag: str) -> list[Path]:
    """Other files in a project folder that hold an exact copy of this project's timeline.

    CapCut 8.3 writes template-2.tmp beside draft_content.json with the same bytes, and 9.8 does
    the same beside the copy under Timelines/<id>/. Which copy a build reads first is not known
    for every version, so a copy that exists is kept identical. Only a file that already holds
    this timeline (same id, plain JSON) is touched; nothing is created, and a file in another
    format is left alone.
    """
    found = []
    places = {file.parent for file in _content_files(folder)}
    for path in (p for place in sorted(places) for p in place.glob("template*.tmp")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and "tracks" in data and _id_tag(data) == tag:
            found.append(path)
    return found


def project_folders() -> list[Path]:
    root = drafts_dir()
    found = [(p, _root_content(p)) for p in root.iterdir() if p.is_dir()]
    return [p for p, content in sorted(
        ((p, c) for p, c in found if c), key=lambda item: item[1].stat().st_mtime, reverse=True)]


def _is_open(folder: Path) -> bool:
    return (folder / ".locked").exists() and capcut_running()


def list_projects() -> list[dict]:
    out = []
    for folder in project_folders():
        entry = {"name": folder.name, "open_in_capcut": _is_open(folder)}
        try:
            content = json.loads(_newest(_content_files(folder)).read_text(encoding="utf-8"))
            canvas = content.get("canvas_config", {})
            entry["duration"] = _seconds(content.get("duration", 0))
            entry["canvas"] = f"{canvas.get('width')}x{canvas.get('height')}"
            entry["segments"] = sum(len(t.get("segments", [])) for t in content.get("tracks", []))
            level, _ = compat.status(compat.app_version(content), compat.app_source(content))
            if level in ("unverified", "untested"):
                entry["capcut_version"] = f"{compat.app_version(content)} ({level})"
        except (ValueError, OSError, UnicodeDecodeError):
            entry["unreadable"] = True
        root_file = _root_content(folder)
        modified = root_file.stat().st_mtime if root_file else folder.stat().st_mtime
        entry["modified"] = datetime.fromtimestamp(modified).strftime("%Y-%m-%d %H:%M")
        out.append(entry)
    return out


def _newest(files: list[Path]) -> Path:
    existing = [f for f in files if f.is_file()]
    if not existing:
        raise CapCutError(f"No {CONTENT_FILE} found in {files[0].parent}")
    return max(existing, key=lambda f: f.stat().st_mtime)


def _find_folder(name: str) -> Path:
    folders = project_folders()
    for match in (lambda f: f.name == name, lambda f: f.name.lower() == name.strip().lower()):
        found = [f for f in folders if match(f)]
        if found:
            return found[0]
    available = ", ".join(f"'{f.name}'" for f in folders[:15]) or "none"
    raise CapCutError(f"No CapCut project named '{name}'. Available: {available}.")


def _check_name(name: str) -> str:
    name = name.strip()
    if not name or _INVALID_NAME.search(name) or name in (".", ".."):
        raise CapCutError('Project names cannot be empty or contain \\ / : * ? " < > |')
    if (drafts_dir() / name).exists():
        raise CapCutError(f"A project named '{name}' already exists.")
    return name


def _canvas_ratio(width: int, height: int) -> str:
    for label, value in CANVAS_RATIOS.items():
        if abs(width / height - value) < 0.005:
            return label
    return "original"


# ---------------------------------------------------- one edit of a project at a time

_SESSION: contextvars.ContextVar[Session | None] = contextvars.ContextVar(
    "capcut_mcp_session", default=None)
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class _ProjectLock:
    """Makes edits of one project take turns.

    Every edit reads the project, changes it and writes it back. Two of them running at once
    (tool calls sent in parallel, or two server processes) would each write over the other. A
    thread lock covers this process and a file lock covers other server processes. Neither is
    tied to a thread, so a batch can take the lock in one worker thread and release in another.
    """

    def __init__(self, folder: Path):
        self.name = folder.name
        key = os.path.normcase(str(folder))
        with _THREAD_LOCKS_GUARD:
            self._thread_lock = _THREAD_LOCKS.setdefault(key, threading.Lock())
        self._path = data_dir() / "locks" / (hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".lock")
        self._handle = None

    def acquire(self) -> None:
        busy = (f"Another edit of '{self.name}' is still running. Wait for it to finish, then "
                "try again. Send edits to one project one after another, or together in one batch call.")
        if not self._thread_lock.acquire(timeout=LOCK_WAIT_SECONDS):
            raise CapCutError(busy)
        try:
            self._handle = lock_file(self._path, LOCK_WAIT_SECONDS, busy)
        except BaseException:
            self._thread_lock.release()
            raise

    def release(self) -> None:
        if self._handle is not None:
            unlock_file(self._handle)
            self._handle = None
        self._thread_lock.release()


@contextlib.contextmanager
def editing(name: str):
    """Hold a project for the length of one edit. Inside a batch the batch already holds it."""
    folder = _find_folder(name)
    session = _SESSION.get()
    if session is not None and session.folder == folder:
        yield
        return
    lock = _ProjectLock(folder)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


class Session:
    """Several edits to one project, kept in memory and written to disk once.

    While a session is active in the current context, Project.open returns the session's copy
    and Project.save only marks it changed. commit() writes it: one save, one undo step. If any
    edit fails the caller calls abort() instead and nothing reaches the disk.
    """

    def __init__(self, name: str):
        self.folder = _find_folder(name)
        self._lock = _ProjectLock(self.folder)
        self._lock.acquire()
        try:
            self.project = Project.open(name)
            self.project._require_writable()
        except BaseException:
            self._lock.release()
            raise
        self.dirty = False

    def activate(self) -> contextvars.Token:
        return _SESSION.set(self)

    @staticmethod
    def deactivate(token: contextvars.Token) -> None:
        _SESSION.reset(token)

    def commit(self) -> bool:
        """Write the project if anything changed. Returns whether it was written."""
        try:
            if self.dirty:
                self.project._write()
            return self.dirty
        finally:
            self._lock.release()

    def abort(self) -> None:
        self._lock.release()


# ------------------------------------------------------------- stored versions (undo)

def _versions(folder: Path) -> list[Path]:
    """Versions stored in a folder, oldest first. File names begin with a timestamp."""
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and (p.name.endswith(".json.gz") or p.suffix == ".json"))


def _read_version(path: Path) -> dict:
    try:
        raw = path.read_bytes()
        data = json.loads((gzip.decompress(raw) if path.name.endswith(".gz") else raw).decode("utf-8"))
    except (OSError, ValueError, EOFError, UnicodeDecodeError):
        raise CapCutError(f"The stored version {path.name} cannot be read.") from None
    if not isinstance(data, dict) or "tracks" not in data:
        raise CapCutError(f"The stored version {path.name} is not a project timeline.")
    return data


def _id_tag(content: dict) -> str:
    """A short form of a timeline's id, used to tell one project's stored versions from another's."""
    return re.sub(r"[^0-9A-Za-z]", "", str(content.get("id") or ""))[:8].upper() or "NOID"


def _label_of(path: Path) -> str:
    """The part of a stored version's file name before its id tag and extension."""
    name = path.name
    for ending in (".json.gz", ".json"):
        if name.endswith(ending):
            name = name[:-len(ending)]
            break
    return name.rsplit(".", 1)[0] if "." in name else name


# ------------------------------------------------------------------------- project

class Project:
    def __init__(self, folder: Path, content: dict):
        self.folder = folder
        self.content = content
        self._new_bin_entries: list[dict] = []
        self._relinked: dict[str, str] = {}  # old media path -> new, for the media bin

    # -- opening and creating ------------------------------------------------------

    @classmethod
    def open(cls, name: str) -> Project:
        folder = _find_folder(name)
        session = _SESSION.get()
        if session is not None and session.folder == folder and getattr(session, "project", None):
            return session.project  # a batch is running: keep working on its in-memory copy
        content = _read_json(_newest(_content_files(folder)))
        if "tracks" not in content or "materials" not in content:
            raise CapCutError(f"'{folder.name}' is not in a project format this server understands.")
        return cls(folder, content)

    @classmethod
    def create(cls, name: str, width: int, height: int, fps: float) -> Project:
        name = _check_name(name)
        if width < 16 or height < 16:
            raise CapCutError("Canvas width and height must be at least 16 pixels.")
        if not 1 <= fps <= 120:
            raise CapCutError("fps must be between 1 and 120.")
        root = drafts_dir()
        folder = root / name
        now = _now_us()
        timeline_id, draft_id = schema.new_id(), schema.new_id()

        content = schema.template("empty_draft")
        content["id"] = timeline_id
        content["fps"] = float(fps)
        content["canvas_config"].update(width=int(width), height=int(height), ratio="original")
        for key, value in _local_platform(root).items():
            content[key] = value

        meta = schema.template("meta_info")
        meta.update(draft_fold_path=schema.capcut_path(folder), draft_id=draft_id, draft_name=name,
                    draft_root_path=str(root), tm_draft_create=now, tm_draft_modified=now)

        index = schema.template("timelines_project")
        index.update(id=schema.new_id(), main_timeline_id=timeline_id, create_time=now, update_time=now)
        index["timelines"][0].update(id=timeline_id, create_time=now, update_time=now)

        layout = schema.template("timeline_layout")
        layout["dockItems"][0]["timelineIds"] = [timeline_id]

        timeline_dir = folder / "Timelines" / timeline_id
        timeline_dir.mkdir(parents=True)
        # macOS builds read draft_info.json. Not checked on a Mac; both names are written there.
        for file_name in (CONTENT_NAMES if sys.platform == "darwin" else (CONTENT_FILE,)):
            _write_json(folder / file_name, content)
            _write_json(timeline_dir / file_name, content)
        _write_json(folder / META_FILE, meta)
        _write_json(folder / "Timelines" / "project.json", index)
        _write_json(folder / "timeline_layout.json", layout)
        _write_json(folder / "draft_agency_config.json", schema.template("agency_config"))
        (folder / "draft_biz_config.json").write_bytes(b"")
        seconds = now // 1_000_000
        (folder / "draft_settings").write_text(
            f"[General]\ndraft_create_time={seconds}\ndraft_last_edit_time={seconds}\n"
            "real_edit_seconds=0\nreal_edit_keys=1\n", encoding="utf-8")
        return cls(folder, content)

    @classmethod
    def duplicate(cls, source: str, new_name: str) -> Project:
        origin = _find_folder(source)
        new_name = _check_name(new_name)
        folder = drafts_dir() / new_name
        shutil.copytree(origin, folder, ignore=shutil.ignore_patterns(".locked", "*.mcp-tmp"))
        meta_path = folder / META_FILE
        if meta_path.is_file():
            meta = _read_json(meta_path)
            now = _now_us()
            meta.update(draft_fold_path=schema.capcut_path(folder), draft_id=schema.new_id(),
                        draft_name=new_name, tm_draft_create=now, tm_draft_modified=now)
            _write_json(meta_path, meta)
        _renew_timeline_id(folder)
        return cls.open(new_name)

    # -- basics --------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self.folder.name

    @property
    def materials(self) -> dict:
        return self.content["materials"]

    @property
    def fps(self) -> float:
        return float(self.content.get("fps") or 30.0)

    def us(self, seconds: float) -> int:
        """Seconds -> microseconds, snapped to the project's frame grid."""
        if seconds < 0:
            raise CapCutError("Times cannot be negative.")
        frames = round(float(seconds) * self.fps)
        return int(frames * 1_000_000 / self.fps + 1e-6)

    def is_open(self) -> bool:
        return _is_open(self.folder)

    def tracks(self, kind: str) -> list[dict]:
        return [t for t in self.content["tracks"] if t.get("type") == kind]

    def track(self, kind: str, index: int, create: bool = False) -> dict:
        if index < 0:
            raise CapCutError("Track numbers start at 0.")
        existing = self.tracks(kind)
        if index < len(existing):
            return existing[index]
        if not create:
            raise CapCutError(f"There is no {kind} track {index} (the project has {len(existing)}).")
        if index > len(existing):
            raise CapCutError(f"Use {kind} track {len(existing)} next; tracks are created in order.")
        new = schema.track(kind, overlay=(kind == "video" and index > 0))
        self.content["tracks"].append(new)
        return new

    def _track_position(self, track: dict) -> tuple[str, int]:
        kind = track.get("type", "")
        return kind, self.tracks(kind).index(track)

    def find(self, ref: str) -> tuple[dict, dict]:
        """Resolve a segment id (or its first characters) to (track, segment)."""
        wanted = ref.strip().upper()
        if len(wanted) < 4:
            raise CapCutError("Segment ids need at least 4 characters.")
        matches = [(t, s) for t in self.content["tracks"] for s in t.get("segments", [])
                   if s.get("id", "").upper().startswith(wanted)]
        if not matches:
            raise CapCutError(f"No segment with id '{ref}'. Call get_timeline for current ids.")
        if len(matches) > 1:
            raise CapCutError(f"Segment id '{ref}' is ambiguous; use more characters.")
        return matches[0]

    def _material(self, group: str, material_id: str) -> dict | None:
        return next((m for m in self.materials.get(group, []) if m.get("id") == material_id), None)

    def _main_material(self, track: dict, seg: dict) -> dict | None:
        group = {"video": "videos", "audio": "audios", "text": "texts"}.get(track.get("type"))
        return self._material(group, seg.get("material_id")) if group else None

    def _helper(self, seg: dict, group: str) -> dict | None:
        refs = set(seg.get("extra_material_refs") or [])
        return next((m for m in self.materials.get(group, []) if m.get("id") in refs), None)

    def find_material(self, material_id: str) -> tuple[str, dict] | None:
        """Look a material up in every group. Returns (group name, material)."""
        for name, group in self.materials.items():
            if isinstance(group, list):
                for item in group:
                    if isinstance(item, dict) and item.get("id") == material_id:
                        return name, item
        return None

    def _users(self, material_id: str) -> int:
        return sum(1 for t in self.content["tracks"] for s in t.get("segments", [])
                   if s.get("material_id") == material_id)

    def _speed_length(self, source_length: int, speed: float) -> int:
        """Timeline length of `source_length` played at `speed`.

        CapCut rounds a sped-up clip up to a whole frame; a length that is not on the frame grid
        gets changed when CapCut opens the project, which then shifts every later clip.
        """
        if speed == 1.0:
            return source_length
        frames = max(1, math.ceil(source_length / speed * self.fps / 1_000_000 - 1e-6))
        return int(frames * 1_000_000 / self.fps + 1e-6)

    # -- timeline arithmetic ---------------------------------------------------------

    @staticmethod
    def _start(seg: dict) -> int:
        return seg["target_timerange"]["start"]

    @staticmethod
    def _end(seg: dict) -> int:
        return seg["target_timerange"]["start"] + seg["target_timerange"]["duration"]

    def _track_end(self, track: dict) -> int:
        return max((self._end(s) for s in track["segments"]), default=0)

    def _label(self, seg: dict) -> str:
        return f"{seg['id'][:8]} ({_seconds(self._start(seg))}s-{_seconds(self._end(seg))}s)"

    def _require_free(self, track: dict, start: int, duration: int, ignore: dict | None = None) -> None:
        for seg in track["segments"]:
            if seg is not ignore and start < self._end(seg) and self._start(seg) < start + duration:
                kind, index = self._track_position(track)
                raise CapCutError(
                    f"That would overlap segment {self._label(seg)} on {kind} track {index}. "
                    "Pick another time or another track."
                )

    def _make_room(self, track: dict, begin: int, length: int, ripple: bool) -> None:
        """Check the space for a new segment, or with ripple push later segments along."""
        if not ripple:
            self._require_free(track, begin, length)
            return
        inside = next((s for s in track["segments"] if self._start(s) < begin < self._end(s)), None)
        if inside:
            raise CapCutError(
                f"{_seconds(begin)}s falls inside segment {self._label(inside)}. Split it there "
                "first, or insert at the start or end of a segment.")
        self._shift(track, begin, length)

    def _shift(self, track: dict, from_us: int, delta: int, ignore: dict | None = None) -> None:
        if delta:
            for seg in track["segments"]:
                if seg is not ignore and self._start(seg) >= from_us:
                    seg["target_timerange"]["start"] += delta

    def _place(self, track: dict, seg: dict) -> None:
        kind, index = self._track_position(track)
        seg["track_render_index"] = self.content["tracks"].index(track)
        if kind == "video":
            seg["render_index"] = index
        elif kind == "text":
            seg["render_index"] = schema.TEXT_RENDER_INDEX + index
        if seg not in track["segments"]:
            track["segments"].append(seg)

    @staticmethod
    def _check_speed(speed: float) -> None:
        if not 0.1 <= speed <= 100:
            raise CapCutError("Speed must be between 0.1 and 100.")

    @staticmethod
    def _check_volume(volume: float) -> None:
        if not 0 <= volume <= 10:
            raise CapCutError("Volume must be between 0 (silent) and 10. 1 is the original level.")

    def _source_window(self, total_us: int, source_start: float, source_end: float | None,
                       duration: float | None, speed: float) -> tuple[int, int]:
        """(source start, source length) in microseconds for a clip of a media file."""
        begin = self.us(source_start)
        end = total_us if source_end is None else min(self.us(source_end), total_us)
        if duration is not None:
            end = min(end, begin + round(self.us(duration) * speed))
        if end - begin < 1_000_000 / self.fps / 2:
            raise CapCutError(
                f"Nothing left to use: the file is {_seconds(total_us)}s long and the requested "
                f"part runs from {_seconds(begin)}s to {_seconds(end)}s."
            )
        return begin, end - begin

    # -- adding media ----------------------------------------------------------------

    def _media_material(self, group: str, info: MediaInfo) -> dict:
        path = schema.capcut_path(info.path)
        for existing in self.materials.get(group, []):
            if existing.get("path") == path and _uncropped(existing):
                return existing
        material = schema.video_material(info) if group == "videos" else schema.audio_material(info)
        self.materials.setdefault(group, []).append(material)
        if group == "videos":
            # Only pictures go in the media bin; CapCut removed audio entries written there.
            self._new_bin_entries.append(
                schema.bin_entry(info, material["local_material_id"], _now_us()))
        return material

    def add_clip(self, path: str, *, track: int = 0, start: float | None = None,
                 start_us: int | None = None, ripple: bool = False,
                 source_start: float = 0.0, source_end: float | None = None,
                 duration: float | None = None, speed: float = 1.0, volume: float = 1.0) -> dict:
        info = probe(path)
        if info.kind == "audio":
            raise CapCutError(f"{info.path.name} is an audio file. Use add_audio for it.")
        self._check_volume(volume)
        target = self.track("video", track, create=True)
        first_visual = not any(t["segments"] for t in self.tracks("video"))

        if info.kind == "photo":
            speed, source_begin = 1.0, 0
            source_length = self.us(5.0 if duration is None else duration)
            if source_length <= 0:
                raise CapCutError("A photo needs a duration greater than 0.")
        else:
            self._check_speed(speed)
            source_begin, source_length = self._source_window(
                info.duration_us, source_start, source_end, duration, speed)
        length = self._speed_length(source_length, speed)
        if start_us is not None:
            begin = start_us  # exact microseconds, used to chain clips without frame rounding
        else:
            begin = self._track_end(target) if start is None else self.us(start)
        self._make_room(target, begin, length, ripple)

        material = self._media_material("videos", info)
        seg = schema.video_segment(self.materials, material, begin, length,
                                   source_begin, source_length, speed, volume)
        self._place(target, seg)

        canvas = self.content["canvas_config"]
        if first_visual and canvas.get("ratio") == "original" and info.width and info.height:
            canvas.update(width=info.width, height=info.height)
        return seg

    def add_audio(self, path: str, *, track: int = 0, start: float | None = None,
                  source_start: float = 0.0, source_end: float | None = None,
                  duration: float | None = None, speed: float = 1.0, volume: float = 1.0,
                  fade_in: float = 0.0, fade_out: float = 0.0) -> dict:
        info = probe(path)
        if info.kind == "photo" or not info.has_audio:
            raise CapCutError(f"{info.path.name} has no audio in it.")
        self._check_speed(speed)
        self._check_volume(volume)
        target = self.track("audio", track, create=True)
        source_begin, source_length = self._source_window(
            info.duration_us, source_start, source_end, duration, speed)
        length = self._speed_length(source_length, speed)
        begin = self._track_end(target) if start is None else self.us(start)
        self._require_free(target, begin, length)

        material = self._media_material("audios", info)
        seg = schema.audio_segment(self.materials, material, begin, length,
                                   source_begin, source_length, speed, volume)
        self._place(target, seg)
        if fade_in or fade_out:
            self._set_fade(seg, fade_in, fade_out)
        return seg

    def _set_fade(self, seg: dict, fade_in: float | None, fade_out: float | None) -> None:
        fade = self._helper(seg, "audio_fades")
        if fade is None:
            fade = schema.audio_fade(0, 0)
            self.materials.setdefault("audio_fades", []).append(fade)
            refs = seg.setdefault("extra_material_refs", [])
            refs.insert(min(2, len(refs)), fade["id"])  # where CapCut itself puts it
        length = seg["target_timerange"]["duration"]
        for key, value in (("fade_in_duration", fade_in), ("fade_out_duration", fade_out)):
            if value is not None:
                fade[key] = min(self.us(value), length)

    # -- text ------------------------------------------------------------------------

    def add_text(self, text: str, *, start: float, duration: float, track: int = 0,
                 x: float = 0.0, y: float = 0.0, scale: float = 1.0, rotation: float = 0.0,
                 opacity: float = 1.0, kind: str = "text", **style) -> dict:
        target = self.track("text", track, create=True)
        begin, length = self.us(start), self.us(duration)
        if length <= 0:
            raise CapCutError("Text needs a duration greater than 0.")
        self._require_free(target, begin, length)
        material = schema.text_material(kind)
        schema.apply_text_style(material, text=text, **style)
        self.materials.setdefault("texts", []).append(material)
        seg = schema.text_segment(self.materials, material, begin, length)
        self._place(target, seg)
        self._apply_transform(seg, x=x, y=y, scale=scale, rotation=rotation, opacity=opacity)
        return seg

    def update_text(self, ref: str, **style) -> dict:
        track, seg = self.find(ref)
        material = self._main_material(track, seg)
        if track.get("type") != "text" or material is None:
            raise CapCutError(f"Segment {seg['id'][:8]} is not a text segment.")
        schema.apply_text_style(material, **style)
        return seg

    # -- editing ---------------------------------------------------------------------

    def _is_still(self, track: dict, seg: dict) -> bool:
        """Text and photos have no source to run out of; only their duration matters."""
        if track.get("type") == "text":
            return True
        material = self._main_material(track, seg)
        return track.get("type") == "video" and bool(material) and material.get("type") == "photo"

    def trim(self, ref: str, *, source_start: float | None = None, source_end: float | None = None,
             duration: float | None = None, ripple: bool = True) -> dict:
        track, seg = self.find(ref)
        if source_start is None and source_end is None and duration is None:
            raise CapCutError("Give source_start, source_end or duration.")
        old_length, old_end = seg["target_timerange"]["duration"], self._end(seg)

        if self._is_still(track, seg) or not seg.get("source_timerange"):
            if duration is None:
                raise CapCutError("This segment has no source footage; set its duration instead.")
            length = self.us(duration)
            if length <= 0:
                raise CapCutError("Duration must be greater than 0.")
            if seg.get("source_timerange"):
                seg["source_timerange"]["duration"] = length
        else:
            material = self._main_material(track, seg)
            total = int(material["duration"]) if material else 0
            speed = float(seg.get("speed") or 1.0)
            source = seg["source_timerange"]
            begin = source["start"] if source_start is None else self.us(source_start)
            end = source["start"] + source["duration"]
            if source_end is not None:
                end = self.us(source_end)
            elif duration is not None:
                end = begin + round(self.us(duration) * speed)
            if total:
                end = min(end, total)
            if end - begin < 1_000_000 / self.fps / 2:
                raise CapCutError(
                    f"The trim leaves nothing: source {_seconds(begin)}s to {_seconds(end)}s"
                    + (f" of a {_seconds(total)}s file." if total else "."))
            head_cut = round((begin - source["start"]) / speed)
            seg["source_timerange"] = {"duration": end - begin, "start": begin}
            length = self._speed_length(end - begin, speed)
            self._rebase_keyframes(seg, head_cut, length)

        seg["target_timerange"]["duration"] = length
        self._rebase_keyframes(seg, 0, length)
        self._refit_animations(seg, old_length)
        if ripple:
            self._shift(track, old_end, length - old_length, ignore=seg)
        else:
            self._require_free(track, self._start(seg), length, ignore=seg)
        return seg

    def split(self, ref: str, at: float) -> tuple[dict, dict]:
        track, seg = self.find(ref)
        cut = self.us(at)
        if not self._start(seg) < cut < self._end(seg):
            raise CapCutError(
                f"{at}s is not inside segment {self._label(seg)}; split points must fall within it.")
        return self._split_at(track, seg, cut)

    def _split_at(self, track: dict, seg: dict, cut: int) -> tuple[dict, dict]:
        left_length = cut - self._start(seg)
        right_length = seg["target_timerange"]["duration"] - left_length

        right = copy.deepcopy(seg)
        right["id"] = schema.new_id()
        right["extra_material_refs"] = self._clone_materials(seg.get("extra_material_refs") or [])
        if track.get("type") not in ("video", "audio"):
            # Text, stickers and effects own their material; media clips share the file's.
            right["material_id"] = self._clone_materials([seg["material_id"]])[0]
        if (self._helper(seg, "transitions")):
            # A transition sits at the end of a clip, so it stays with the right-hand half.
            transition = self._helper(seg, "transitions")
            seg["extra_material_refs"].remove(transition["id"])
            self.materials["transitions"].remove(transition)
        for prop in right.get("common_keyframes") or []:
            prop["id"] = schema.new_id()
            for frame in prop.get("keyframe_list", []):
                frame["id"] = schema.new_id()

        if seg.get("source_timerange"):
            speed = float(seg.get("speed") or 1.0)
            source = seg["source_timerange"]
            used = left_length if speed == 1.0 else round(left_length * speed)
            used = min(used, source["duration"])
            right["source_timerange"] = {"start": source["start"] + used,
                                         "duration": source["duration"] - used}
            source["duration"] = used
        old_length = left_length + right_length
        seg["target_timerange"]["duration"] = left_length
        right["target_timerange"] = {"start": cut, "duration": right_length}
        self._rebase_keyframes(seg, 0, left_length)
        self._rebase_keyframes(right, left_length, right_length)
        # The entrance stays with the first part and the exit goes with the second.
        self._refit_animations(seg, old_length, keep=("in", "loop", "group"))
        self._refit_animations(right, old_length, keep=("out", "loop", "group"))

        # A fade belongs to one end of the clip, so each half keeps only its own.
        left_fade, right_fade = self._helper(seg, "audio_fades"), self._helper(right, "audio_fades")
        if left_fade and right_fade:
            left_fade["fade_out_duration"] = 0
            right_fade["fade_in_duration"] = 0

        track["segments"].append(right)
        return seg, right

    def _clone_materials(self, ids: list[str]) -> list[str]:
        new_ids = []
        for old in ids:
            clone_id = None
            # A mask can sit in two lists under one id, so every list holding the id is copied.
            for group in self.materials.values():
                if not isinstance(group, list):
                    continue
                found = next((m for m in group if isinstance(m, dict) and m.get("id") == old), None)
                if found:
                    clone = copy.deepcopy(found)
                    clone["id"] = clone_id = clone_id or schema.new_id()
                    group.append(clone)
            new_ids.append(clone_id or old)
        return new_ids

    def move(self, ref: str, *, start: float | None = None, track: int | None = None) -> dict:
        origin, seg = self.find(ref)
        if start is None and track is None:
            raise CapCutError("Give a new start time, a new track, or both.")
        kind = origin.get("type")
        target = origin if track is None else self.track(kind, track, create=True)
        begin = self._start(seg) if start is None else self.us(start)
        self._require_free(target, begin, seg["target_timerange"]["duration"], ignore=seg)
        if target is not origin:
            origin["segments"].remove(seg)
        seg["target_timerange"]["start"] = begin
        self._place(target, seg)
        return seg

    def delete(self, refs: list[str], ripple: bool = True) -> list[str]:
        found = [self.find(ref) for ref in refs]
        removed = []
        for track, seg in found:
            if seg not in track["segments"]:
                continue  # the same segment was listed twice
            track["segments"].remove(seg)
            if ripple:
                self._shift(track, self._end(seg), -seg["target_timerange"]["duration"])
            self._drop_unused_materials(seg)
            removed.append(seg["id"][:8])
        return removed

    def _drop_unused_materials(self, seg: dict) -> None:
        candidates = {seg.get("material_id"), *(seg.get("extra_material_refs") or [])}
        in_use = set()
        for track in self.content["tracks"]:
            for other in track.get("segments", []):
                in_use.add(other.get("material_id"))
                in_use.update(other.get("extra_material_refs") or [])
        drop = candidates - in_use - {None, ""}
        for name, group in self.materials.items():
            if isinstance(group, list):
                self.materials[name] = [
                    m for m in group if not (isinstance(m, dict) and m.get("id") in drop)]

    def arrange(self, kind: str, track: int, order: list[str] | None = None,
                start: float | None = None, gap: float = 0.0) -> dict:
        target = self.track(kind, track)
        segments = sorted(target["segments"], key=self._start)
        if not segments:
            raise CapCutError(f"{kind} track {track} is empty.")
        if order:
            chosen = []
            for ref in order:
                owner, seg = self.find(ref)
                if owner is not target:
                    raise CapCutError(f"Segment {seg['id'][:8]} is not on {kind} track {track}.")
                if seg not in chosen:
                    chosen.append(seg)
            segments = chosen + [s for s in segments if s not in chosen]
        cursor = self._start(min(target["segments"], key=self._start)) if start is None else self.us(start)
        spacing = self.us(gap)
        for seg in segments:
            seg["target_timerange"]["start"] = cursor
            cursor += seg["target_timerange"]["duration"] + spacing
        target["segments"] = segments
        return target

    def set_speed(self, ref: str, speed: float, ripple: bool = True) -> dict:
        track, seg = self.find(ref)
        self._check_speed(speed)
        if self._is_still(track, seg) or not seg.get("source_timerange") \
                or track.get("type") not in ("video", "audio"):
            raise CapCutError("Speed applies to video and audio clips only.")
        old_length, old_end = seg["target_timerange"]["duration"], self._end(seg)
        length = self._speed_length(seg["source_timerange"]["duration"], float(speed))
        for prop in seg.get("common_keyframes") or []:
            for frame in prop.get("keyframe_list", []):
                frame["time_offset"] = round(frame["time_offset"] * length / old_length)
        seg["speed"] = float(speed)
        helper = self._helper(seg, "speeds")
        if helper:
            helper["speed"] = float(speed)
        seg["target_timerange"]["duration"] = length
        self._refit_animations(seg, old_length)
        if ripple:
            self._shift(track, old_end, length - old_length, ignore=seg)
        else:
            self._require_free(track, self._start(seg), length, ignore=seg)
        return seg

    def set_volume(self, ref: str, volume: float | None = None, fade_in: float | None = None,
                   fade_out: float | None = None) -> dict:
        track, seg = self.find(ref)
        if track.get("type") not in ("video", "audio") or self._is_still(track, seg):
            raise CapCutError("Volume applies to video and audio clips only.")
        if volume is None and fade_in is None and fade_out is None:
            raise CapCutError("Give a volume, a fade_in or a fade_out.")
        if volume is not None:
            self._check_volume(volume)
            seg["volume"] = float(volume)
        if fade_in is not None or fade_out is not None:
            self._set_fade(seg, fade_in, fade_out)
        return seg

    def set_transform(self, ref: str, **changes) -> dict:
        track, seg = self.find(ref)
        if track.get("type") not in ("video", "text", "sticker") or not isinstance(seg.get("clip"), dict):
            raise CapCutError("Only video, photo and text segments can be positioned.")
        self._apply_transform(seg, **changes)
        return seg

    @staticmethod
    def _apply_transform(seg: dict, *, x: float | None = None, y: float | None = None,
                         scale: float | None = None, rotation: float | None = None,
                         opacity: float | None = None, flip_horizontal: bool | None = None,
                         flip_vertical: bool | None = None) -> None:
        clip = seg["clip"]
        if x is not None:
            clip["transform"]["x"] = float(x)
        if y is not None:
            clip["transform"]["y"] = float(y)
        if scale is not None:
            if scale <= 0:
                raise CapCutError("Scale must be greater than 0. 1 is the original size.")
            clip["scale"] = {"x": float(scale), "y": float(scale)}
        if rotation is not None:
            clip["rotation"] = float(rotation)
        if opacity is not None:
            if not 0 <= opacity <= 1:
                raise CapCutError("Opacity must be between 0 and 1.")
            clip["alpha"] = float(opacity)
        if flip_horizontal is not None:
            clip["flip"]["horizontal"] = bool(flip_horizontal)
        if flip_vertical is not None:
            clip["flip"]["vertical"] = bool(flip_vertical)

    def set_canvas(self, width: int, height: int) -> None:
        if width < 16 or height < 16:
            raise CapCutError("Canvas width and height must be at least 16 pixels.")
        self.content["canvas_config"].update(
            width=int(width), height=int(height), ratio=_canvas_ratio(width, height))

    def set_track_volume(self, kind: str, track: int, volume: float) -> list[dict]:
        self._check_volume(volume)
        if kind not in ("video", "audio"):
            raise CapCutError("Only video and audio tracks have a volume.")
        target = self.track(kind, track)
        changed = [s for s in target["segments"] if not self._is_still(target, s)]
        for seg in changed:
            seg["volume"] = float(volume)
        return changed

    # -- cutting and replacing ---------------------------------------------------------

    def cut_range(self, start: float, end: float, kind: str = "video", track: int = 0,
                  all_tracks: bool = False, ripple: bool = True) -> list[str]:
        """Remove everything between two timeline times, splitting segments that straddle them."""
        begin, finish = self.us(start), self.us(end)
        if finish <= begin:
            raise CapCutError("The range must end after it starts.")
        targets = list(self.content["tracks"]) if all_tracks else [self.track(kind, track)]
        removed = []
        for target in targets:
            for edge in (begin, finish):
                for seg in list(target["segments"]):
                    if self._start(seg) < edge < self._end(seg):
                        self._split_at(target, seg, edge)
            for seg in list(target["segments"]):
                if self._start(seg) >= begin and self._end(seg) <= finish:
                    target["segments"].remove(seg)
                    self._drop_unused_materials(seg)
                    removed.append(seg["id"][:8])
            if ripple:
                self._shift(target, finish, begin - finish)
        return removed

    def replace_media(self, ref: str, path: str) -> dict:
        """Point a clip at another file, keeping its place, speed, volume and transform."""
        track, seg = self.find(ref)
        kind = track.get("type")
        if kind not in ("video", "audio"):
            raise CapCutError("Only video, image and audio clips have a media file to replace.")
        info = probe(path)
        if kind == "video" and info.kind == "audio":
            raise CapCutError(f"{info.path.name} is an audio file; a video clip needs video or an image.")
        if kind == "audio" and (info.kind == "photo" or not info.has_audio):
            raise CapCutError(f"{info.path.name} has no audio in it.")

        old_id = seg["material_id"]
        old_length, old_end = seg["target_timerange"]["duration"], self._end(seg)
        material = self._media_material("videos" if kind == "video" else "audios", info)
        seg["material_id"] = material["id"]

        if info.kind == "photo":
            seg["speed"] = 1.0
            helper = self._helper(seg, "speeds")
            if helper:
                helper["speed"] = 1.0
            seg["source_timerange"] = {"start": 0, "duration": old_length}
            length = old_length
        else:
            speed = float(seg.get("speed") or 1.0)
            source = seg.get("source_timerange") or {"start": 0, "duration": old_length}
            begin = source["start"] if source["start"] < info.duration_us else 0
            wanted = round(old_length * speed)
            available = min(wanted, info.duration_us - begin)
            seg["source_timerange"] = {"start": begin, "duration": available}
            length = old_length if available == wanted else self._speed_length(available, speed)
        seg["target_timerange"]["duration"] = length
        self._shift(track, old_end, length - old_length, ignore=seg)
        self._rebase_keyframes(seg, 0, length)
        self._refit_animations(seg, old_length)

        if not self._users(old_id):
            for group in ("videos", "audios"):
                self.materials[group] = [m for m in self.materials.get(group, []) if m.get("id") != old_id]
        return seg

    def freeze_frame(self, ref: str, at: float, duration: float = 3.0) -> dict:
        """Hold the frame showing at a timeline time for `duration` seconds, pushing later clips on."""
        track, seg = self.find(ref)
        material = self._main_material(track, seg)
        if track.get("type") != "video" or not material or material.get("type") == "photo":
            raise CapCutError("A freeze frame can only be taken from a video clip.")
        cut = self.us(at)
        if not self._start(seg) <= cut <= self._end(seg):
            raise CapCutError(f"{at}s is not within segment {self._label(seg)}.")
        length = self.us(duration)
        if length <= 0:
            raise CapCutError("The freeze needs a duration greater than 0.")

        source = seg["source_timerange"]
        moment = (source["start"] + (cut - self._start(seg)) * float(seg.get("speed") or 1.0)) / 1e6
        moment = min(moment, max(material.get("duration", 0) / 1e6 - 0.05, 0))
        still = (data_dir() / "stills" / self.name
                 / f"{Path(material['path']).stem}-{int(moment * 1000):08d}.png")
        extract_frame(material["path"], moment, still, width=None)

        if self._start(seg) < cut < self._end(seg):
            self._split_at(track, seg, cut)
        _, index = self._track_position(track)
        return self.add_clip(str(still), track=index, start_us=cut, duration=duration, ripple=True)

    def relink(self, old: str, new: str) -> dict:
        """Repoint media whose path starts with `old` (a folder or a file) to `new`.

        CapCut's media list (the bin in draft_meta_info.json) is repointed too, including
        entries no clip uses any more: CapCut asks the user to link every listed file it
        cannot find, whether or not the timeline needs it.
        """
        old_norm = schema.capcut_path(Path(old)).rstrip("/")
        new_norm = schema.capcut_path(Path(new)).rstrip("/")

        def moved(path: str) -> str | None:
            if path.lower() == old_norm.lower():
                return new_norm
            if path.lower().startswith(old_norm.lower() + "/"):
                return new_norm + path[len(old_norm):]
            return None

        updated, not_found = [], []
        for group in ("videos", "audios"):
            for material in self.materials.get(group, []):
                path = material.get("path", "")
                candidate = moved(path)
                if candidate is None:
                    continue
                if Path(candidate).is_file():
                    self._relinked[path.lower()] = candidate
                    material["path"] = candidate
                    updated.append(candidate)
                else:
                    not_found.append(candidate)

        listed = 0
        try:
            meta = _read_json(self.folder / META_FILE)
        except (CapCutError, OSError):
            meta = {}
        for media_bin in meta.get("draft_materials") or []:
            for entry in media_bin.get("value") or []:
                path = str(entry.get("file_Path", ""))
                candidate = moved(path) if path else None
                if candidate and path.lower() not in self._relinked and Path(candidate).is_file():
                    self._relinked[path.lower()] = candidate
                    listed += 1
        out: dict = {"relinked": updated, "not_found_at_new_location": not_found}
        if listed:
            out["media_list_entries_fixed"] = listed
        return out

    def missing_media(self) -> list[str]:
        paths = {m.get("path", "") for group in ("videos", "audios")
                 for m in self.materials.get(group, [])}
        return sorted(p for p in paths if p and not p.startswith("##") and not Path(p).is_file())

    # -- animation, crop, background ---------------------------------------------------

    def _rebase_keyframes(self, seg: dict, head_cut: int, length: int) -> None:
        """Keep a segment's animation looking the same after its head or tail was cut.

        Keyframes outside the part that remains are dropped. Where the cut falls between two
        keyframes, a keyframe with the in-between value is added at the cut, so a clip split in
        the middle of a fade carries on from the opacity it had reached instead of jumping. When
        every keyframe ends up on one side, the property simply keeps the value it had there.
        """
        props = seg.get("common_keyframes") or []
        tail_cut = head_cut + length
        for prop in props:
            frames = sorted(prop.get("keyframe_list", []), key=lambda f: f["time_offset"])
            if not frames:
                continue
            kept = [f for f in frames if head_cut <= f["time_offset"] <= tail_cut]
            dropped = len(kept) != len(frames)
            # A keyframe at the cut is only needed where the value is still changing there.
            if frames[0]["time_offset"] < head_cut and (not kept or kept[0]["time_offset"] > head_cut):
                edge = _keyframe_at(frames, head_cut)
                if not kept or abs(edge["values"][0] - kept[0]["values"][0]) > 1e-9:
                    kept.insert(0, edge)
            if frames[-1]["time_offset"] > tail_cut and (not kept or kept[-1]["time_offset"] < tail_cut):
                edge = _keyframe_at(frames, tail_cut)
                if not kept or abs(edge["values"][0] - kept[-1]["values"][0]) > 1e-9:
                    kept.append(edge)
            for frame in kept:
                frame["time_offset"] -= head_cut
            if dropped and len(kept) == 1 and _set_static(seg, prop.get("property_type"), kept[0]["values"][0]):
                kept = []  # one keyframe is a constant: store it as the plain value instead
            prop["keyframe_list"] = kept
        if props:
            seg["common_keyframes"] = [p for p in props if p.get("keyframe_list")]

    def _refit_animations(self, seg: dict, old_length: int | None = None,
                          keep: tuple[str, ...] | None = None) -> None:
        """Fit a segment's in, out, loop and combo animations to its current length.

        An exit animation is stored with its start time, so it has to move when the segment
        gets shorter or longer. `keep` limits which kinds stay (a split gives the entrance to
        the first part and the exit to the second).
        """
        holder = self._helper(seg, "material_animations")
        if not holder or not holder.get("animations"):
            return
        length = seg["target_timerange"]["duration"]
        animations = [a for a in holder["animations"] if keep is None or a.get("type") in keep]
        lead = next((a for a in animations if a.get("type") == "in"), None)
        tail = next((a for a in animations if a.get("type") == "out"), None)
        lead_span = min(int(lead.get("duration", 0)), length) if lead else 0
        tail_span = min(int(tail.get("duration", 0)), length) if tail else 0
        if lead_span + tail_span > length:  # share a segment too short for both
            lead_span = length * lead_span // (lead_span + tail_span)
            tail_span = length - lead_span
        if lead:
            lead.update(start=0, duration=max(1, lead_span))
        if tail:
            tail.update(start=length - tail_span, duration=max(1, tail_span))
        for animation in animations:
            if animation.get("type") == "loop":  # fills whatever entrance and exit leave free
                animation.update(start=lead_span, duration=max(1, length - lead_span - tail_span))
            elif animation.get("type") == "group":
                whole = old_length is not None and int(animation.get("duration", 0)) >= old_length
                animation.update(start=0, duration=length if whole
                                 else max(1, min(int(animation.get("duration", 0)), length)))
        holder["animations"] = animations

    def set_keyframes(self, ref: str, prop: str, points: list[tuple[float, float]]) -> dict:
        """Animate one property of a segment. `points` are (seconds from its start, value)."""
        track, seg = self.find(ref)
        if prop not in schema.KEYFRAME_PROPERTIES:
            raise CapCutError("property must be one of: " + ", ".join(schema.KEYFRAME_PROPERTIES))
        kind = track.get("type")
        if prop == "volume":
            if kind not in ("video", "audio") or self._is_still(track, seg):
                raise CapCutError("Volume can only be animated on video and audio clips.")
        elif not isinstance(seg.get("clip"), dict):
            raise CapCutError("Only video, image and text segments can be animated this way.")

        length = seg["target_timerange"]["duration"]
        converted = []
        for moment, value in points:
            offset = self.us(moment)
            if offset > length + 1_000_000 / self.fps:
                raise CapCutError(
                    f"Keyframe at {moment}s is past the end of the segment ({_seconds(length)}s long). "
                    "Keyframe times count from the segment's own start.")
            if prop == "opacity" and not 0 <= value <= 1:
                raise CapCutError("Opacity values must be between 0 and 1.")
            if prop == "scale" and value <= 0:
                raise CapCutError("Scale values must be greater than 0.")
            if prop == "volume":
                self._check_volume(value)
            converted.append((min(offset, length), value))
        if len({offset for offset, _ in converted}) != len(converted):
            raise CapCutError("Two keyframes fall on the same frame.")

        name = schema.KEYFRAME_PROPERTIES[prop]
        seg["common_keyframes"] = [k for k in (seg.get("common_keyframes") or [])
                                   if k.get("property_type") != name]
        if converted:
            seg["common_keyframes"].append(schema.keyframes(name, converted))
            if prop == "scale":
                seg["uniform_scale"] = {"on": True, "value": 1.0}
        return seg

    def fade(self, ref: str, fade_in: float = 0.0, fade_out: float = 0.0, audio: bool = True) -> dict:
        """Fade a segment in from and out to transparent; audio clips fade their sound instead."""
        track, seg = self.find(ref)
        if track.get("type") == "audio":
            self._set_fade(seg, fade_in, fade_out)
            return seg
        if not isinstance(seg.get("clip"), dict):
            raise CapCutError("This segment cannot be faded.")
        length = seg["target_timerange"]["duration"]
        lead, tail = self.us(fade_in), self.us(fade_out)
        if lead + tail > length:
            raise CapCutError(
                f"The fades add up to more than the segment's length ({_seconds(length)}s).")
        level = float(seg["clip"].get("alpha", 1.0))
        points = []
        if lead:
            points += [(0.0, 0.0), (lead / 1e6, level)]
        if tail:
            points += [((length - tail) / 1e6, level), (length / 1e6, 0.0)]
        self.set_keyframes(ref, "opacity", points)
        material = self._main_material(track, seg)
        if audio and track.get("type") == "video" and material and material.get("has_audio"):
            self._set_fade(seg, fade_in, fade_out)
        return seg

    def crop(self, ref: str, left: float = 0.0, top: float = 0.0, right: float = 0.0,
             bottom: float = 0.0) -> dict:
        """Cut a fraction (0 to 1) off each side of a video or image clip."""
        track, seg = self.find(ref)
        material = self._main_material(track, seg)
        if track.get("type") != "video" or not material:
            raise CapCutError("Only video and image clips can be cropped.")
        if min(left, top, right, bottom) < 0 or left + right >= 1 or top + bottom >= 1:
            raise CapCutError("Crop amounts are fractions of the picture: each 0 or more, and "
                              "left+right and top+bottom each less than 1.")
        if self._users(material["id"]) > 1:
            # The crop lives on the media entry, which clips of the same file share.
            material = copy.deepcopy(material)
            material["id"] = schema.new_id()
            self.materials["videos"].append(material)
            seg["material_id"] = material["id"]
        material["crop"] = {
            "upper_left_x": left, "upper_left_y": top,
            "upper_right_x": 1 - right, "upper_right_y": top,
            "lower_left_x": left, "lower_left_y": 1 - bottom,
            "lower_right_x": 1 - right, "lower_right_y": 1 - bottom,
        }
        material["crop_ratio"] = "free"
        material["crop_scale"] = 1.0
        return seg

    def set_background(self, ref: str, blur: float | None = None, color: str | None = None) -> dict:
        """Fill the area a clip leaves empty with a blurred copy of itself or a colour."""
        track, seg = self.find(ref)
        if track.get("type") != "video":
            raise CapCutError("Only video and image clips have a background.")
        if blur is not None and color is not None:
            raise CapCutError("Give a blur or a colour, not both.")
        canvas = self._helper(seg, "canvases")
        if canvas is None:
            canvas = schema.template("canvas")
            canvas["id"] = schema.new_id()
            self.materials.setdefault("canvases", []).append(canvas)
            seg.setdefault("extra_material_refs", []).append(canvas["id"])
        if blur is not None:
            if not 0 < blur <= 1:
                raise CapCutError("Blur runs from just above 0 to 1. CapCut's four presets are "
                                  "0.0625, 0.375, 0.75 and 1.")
            canvas.update(type="canvas_blur", blur=float(blur), color="")
        elif color is not None:
            hex_color, _ = schema.parse_color(color)
            canvas.update(type="canvas_color", blur=0.0, color=hex_color + "FF")
        else:
            canvas.update(type="canvas_color", blur=0.0, color="")
        return seg

    # -- reading ---------------------------------------------------------------------

    def duration_us(self) -> int:
        return max((self._end(s) for t in self.content["tracks"] for s in t.get("segments", [])),
                   default=0)

    def describe(self, kind: str | None = None, track: int | None = None,
                 start: float | None = None, end: float | None = None) -> dict:
        """The timeline as the assistant sees it, optionally narrowed to a track type, one track
        and a stretch of time (segments that overlap `start`..`end`)."""
        canvas = self.content["canvas_config"]
        begin = None if start is None else int(start * 1e6)
        finish = None if end is None else int(end * 1e6)
        narrowed = kind is not None or track is not None or begin is not None or finish is not None
        counters: dict[str, int] = {}
        counts: dict[str, list[int]] = {}
        tracks = []
        for item in self.content["tracks"]:
            item_kind = item.get("type", "")
            index = counters.get(item_kind, 0)
            counters[item_kind] = index + 1
            segments = sorted(item.get("segments", []), key=self._start)
            counts.setdefault(item_kind, []).append(len(segments))
            if (kind is not None and item_kind != kind) or (track is not None and index != track):
                continue
            if begin is not None:
                segments = [s for s in segments if self._end(s) > begin]
            if finish is not None:
                segments = [s for s in segments if self._start(s) < finish]
            tracks.append({"type": item_kind, "track": index,
                           "segments": [self._describe_segment(item, s) for s in segments]})
        out: dict = {
            "project": self.name,
            "canvas": f"{canvas.get('width')}x{canvas.get('height')}",
            "fps": self.fps,
            "duration": _seconds(self.duration_us()),
            "open_in_capcut": self.is_open(),
            "tracks": tracks,
        }
        if narrowed:
            # Segment counts of every track, so a narrowed view is not mistaken for the whole project.
            out["segments_per_track"] = counts
        missing = self.missing_media()
        if missing:
            out["missing_media"] = missing
        note = self.version_note()
        if note:
            out["capcut_version_note"] = note
        return out

    def detail(self, ref: str) -> dict:
        """Everything about one segment: keyframe points, animation timing, text style, settings."""
        track, seg = self.find(ref)
        kind, index = self._track_position(track)
        out: dict = {"type": kind, "track": index, **self._describe_segment(track, seg),
                     "full_id": seg["id"]}
        known = {stored: name for name, stored in schema.KEYFRAME_PROPERTIES.items()}
        points: dict[str, list] = {}
        for prop in seg.get("common_keyframes") or []:
            name = known.get(prop.get("property_type"), prop.get("property_type") or "?")
            points[name] = [{"time": _seconds(f["time_offset"]), "value": round(f["values"][0], 4)}
                            for f in sorted(prop.get("keyframe_list", []), key=lambda f: f["time_offset"])]
        if points:
            out["keyframes"] = points
        holder = self._helper(seg, "material_animations")
        if holder and holder.get("animations"):
            out["animations"] = [
                {"type": "combo" if a.get("type") == "group" else a.get("type", ""),
                 "name": a.get("name", ""), "start": _seconds(a.get("start", 0)),
                 "duration": _seconds(a.get("duration", 0))} for a in holder["animations"]]
        transition = self._helper(seg, "transitions")
        if transition:
            out["transition_after"] = {"name": transition.get("name", ""),
                                       "duration": _seconds(transition.get("duration", 0))}
        material = self._main_material(track, seg)
        if kind == "text" and material:
            out["style"] = schema.text_style_of(material)
        elif kind in ("video", "audio") and material:
            out["media"] = {key: material[key] for key in ("type", "width", "height", "has_audio")
                            if key in material}
        else:
            found = self.find_material(seg.get("material_id", ""))
            if found:
                group, item = found
                if item.get("adjust_params"):
                    out["settings"] = {
                        p.get("name", "").removeprefix("effects_adjust_"): round(
                            100 * (float(p.get("value", 0)) - float(p.get("min_value", 0.0)))
                            / ((float(p.get("max_value", 1.0)) - float(p.get("min_value", 0.0))) or 1.0))
                        for p in item["adjust_params"]}
                if group == "effects" and item.get("type") == "filter":
                    out["intensity"] = round(float(item.get("value", 1.0)) * 100)
        mask = self._helper(seg, "common_mask") or self._helper(seg, "masks")
        if mask:
            config = mask.get("config") or {}
            out["mask"] = {"shape": schema.mask_shape_of(mask),
                           **{key: round(float(config[stored]), 4) for key, stored in (
                               ("x", "centerX"), ("y", "centerY"), ("width", "width"),
                               ("height", "height"), ("rotation", "rotation"),
                               ("feather", "feather")) if stored in config},
                           "invert": bool(config.get("invert"))}
        attached = sorted({found[0] for found in map(self.find_material, seg.get("extra_material_refs") or [])
                           if found})
        if attached:
            out["attached"] = attached
        return out

    def _describe_segment(self, track: dict, seg: dict) -> dict:
        out: dict = {"id": seg["id"][:8], "start": _seconds(self._start(seg)),
                     "end": _seconds(self._end(seg)),
                     "duration": _seconds(seg["target_timerange"]["duration"])}
        kind = track.get("type")
        material = self._main_material(track, seg)
        if kind == "text" and material:
            out["text"] = schema.text_of(material)
        elif kind in ("video", "audio") and material:
            still = material.get("type") == "photo"
            out["file"] = material.get("path", "")
            if still:
                out["photo"] = True
            elif seg.get("source_timerange"):
                source = seg["source_timerange"]
                out["source_start"] = _seconds(source["start"])
                out["source_end"] = _seconds(source["start"] + source["duration"])
                out["source_length"] = _seconds(material.get("duration", 0))
            if float(seg.get("speed") or 1.0) != 1.0:
                out["speed"] = seg["speed"]
            if not still and float(seg.get("volume", 1.0)) != 1.0:
                out["volume"] = seg["volume"]
            fade = self._helper(seg, "audio_fades")
            if fade and (fade.get("fade_in_duration") or fade.get("fade_out_duration")):
                out["fade_in"] = _seconds(fade.get("fade_in_duration", 0))
                out["fade_out"] = _seconds(fade.get("fade_out_duration", 0))
        elif material is None:
            found = self.find_material(seg.get("material_id", ""))
            if found:
                label = found[1].get("name") or found[1].get("material_name")
                if label:
                    out["name"] = label
        if kind == "video" and material and not _uncropped(material):
            crop = material["crop"]
            out["crop"] = {"left": round(crop["upper_left_x"], 3), "top": round(crop["upper_left_y"], 3),
                           "right": round(1 - crop["lower_right_x"], 3),
                           "bottom": round(1 - crop["lower_right_y"], 3)}
        canvas = self._helper(seg, "canvases")
        if canvas and canvas.get("type") == "canvas_blur":
            out["background"] = f"blur {canvas.get('blur')}"
        elif canvas and canvas.get("color"):
            out["background"] = canvas["color"]
        transition = self._helper(seg, "transitions")
        if transition:
            out["transition_after"] = transition.get("name", "")
        refs = set(seg.get("extra_material_refs") or [])
        blend = next((m for m in self.materials.get("effects", [])
                      if m.get("id") in refs and m.get("type") == "mix_mode"), None)
        if blend:
            english = {chinese: name for name, chinese in schema.BLEND_NAMES.items()}
            out["blend"] = english.get(blend.get("name", ""), blend.get("name", ""))
        mask = self._helper(seg, "common_mask") or self._helper(seg, "masks")
        if mask:
            out["mask"] = schema.mask_shape_of(mask)
        chroma = self._helper(seg, "chromas")
        if chroma:
            out["chroma_key"] = str(chroma.get("color", ""))[:7]
        sound = [m.get("name", "") for m in self.materials.get("audio_effects", []) if m.get("id") in refs]
        if sound:
            out["audio_effects"] = sound
        animation = self._helper(seg, "material_animations")
        if animation and animation.get("animations"):
            out["animations"] = [
                f"{'combo' if a.get('type') == 'group' else a.get('type', '')}: {a.get('name', '')}"
                for a in animation["animations"]]
        animated = [name for name, kf in schema.KEYFRAME_PROPERTIES.items()
                    if any(k.get("property_type") == kf for k in seg.get("common_keyframes") or [])]
        if animated:
            out["keyframes"] = animated
        clip = seg.get("clip")
        if isinstance(clip, dict):
            placement = {
                "x": clip.get("transform", {}).get("x", 0.0),
                "y": clip.get("transform", {}).get("y", 0.0),
                "scale": clip.get("scale", {}).get("x", 1.0),
                "rotation": clip.get("rotation", 0.0),
                "opacity": clip.get("alpha", 1.0),
            }
            defaults = {"x": 0.0, "y": 0.0, "scale": 1.0, "rotation": 0.0, "opacity": 1.0}
            out.update({k: round(v, 3) for k, v in placement.items() if v != defaults[k]})
        return out

    def frame_source(self, seconds: float) -> tuple[str, float] | None:
        """The media file and the moment in it shown on the main video track at a timeline time."""
        moment = int(seconds * 1e6)
        for track in self.tracks("video"):
            for seg in track.get("segments", []):
                if self._start(seg) <= moment < self._end(seg):
                    material = self._main_material(track, seg)
                    if not material or not material.get("path"):
                        continue
                    source = seg.get("source_timerange") or {"start": 0}
                    offset = (moment - self._start(seg)) * float(seg.get("speed") or 1.0)
                    return material["path"], (source["start"] + offset) / 1e6
        return None

    # -- saving ----------------------------------------------------------------------

    def _backup_dir(self) -> Path:
        return data_dir() / "backups" / self.name

    def _require_closed(self) -> None:
        if self.is_open():
            raise CapCutError(
                f"'{self.name}' is open in CapCut right now. Close it there (go back to CapCut's "
                "home screen), then try again. Changes made while it is open would be overwritten."
            )

    def _require_writable(self) -> None:
        self._require_closed()
        compat.require_supported(self.content, self.name)

    def version_note(self) -> str:
        """A warning about the CapCut version that last saved this project, or ''."""
        return compat.status(compat.app_version(self.content), compat.app_source(self.content))[1]

    def _normalise(self) -> None:
        for track in self.content["tracks"]:
            track.get("segments", []).sort(key=self._start)
        self.content["duration"] = self.duration_us()

    def save(self) -> None:
        """Write the project, keeping the version it replaces for undo.

        Inside a batch nothing is written yet: the batch writes once when all its edits are done.
        """
        self._normalise()
        session = _SESSION.get()
        if session is not None and session.project is self:
            session.dirty = True
            return
        self._write()

    def _write(self) -> None:
        self._require_writable()
        self._normalise()
        self._keep_version(self._backup_dir(), MAX_BACKUPS)
        for stale in _versions(self._backup_dir() / "redo"):
            stale.unlink(missing_ok=True)  # a new edit ends the chance to redo
        self._write_content()
        self._ensure_cover()

    def _write_content(self) -> None:
        payload = _encode(self.content)
        mirrors = _mirror_files(self.folder, _id_tag(self.content))
        for file in _content_files(self.folder):
            file.parent.mkdir(parents=True, exist_ok=True)
            _write_bytes(file, payload)
        for file in mirrors:
            _write_bytes(file, payload)
        # What was last written, kept to tell later whether CapCut changed the project since
        # (undo warns about it, and tools/diff_resave.py lists the differences).
        self._backup_dir().mkdir(parents=True, exist_ok=True)
        _write_bytes(self._backup_dir() / LAST_WRITTEN, payload)
        self._update_meta()

    # -- stored versions: undo, redo, checkpoints ------------------------------------

    def _keep_version(self, folder: Path, limit: int | None = None, label: str = "") -> Path:
        """Store the timeline as it is on disk right now. Call before replacing self.content."""
        current = _newest(_content_files(self.folder))
        folder.mkdir(parents=True, exist_ok=True)
        stem = label or f"{datetime.now():%Y%m%d-%H%M%S-%f}"
        target = folder / f"{stem}.{_id_tag(self.content)}.json.gz"
        target.write_bytes(gzip.compress(current.read_bytes(), compresslevel=5))
        if limit:
            for old in _versions(folder)[:-limit]:
                old.unlink(missing_ok=True)
        return target

    def _is_mine(self, path: Path) -> bool:
        """Whether a stored version is of this project.

        Versions are filed by project name, and a name can be reused after a project is deleted.
        Each version carries the timeline's id, so another project's history is never restored.
        """
        mine = _id_tag(self.content)
        name = path.name[:-len(".json.gz")] if path.name.endswith(".json.gz") else None
        if name is not None and "." in name:
            return name.rsplit(".", 1)[1] == mine
        try:  # versions written before ids were put in the file name
            return _id_tag(_read_version(path)) == mine
        except CapCutError:
            return False

    def _mine(self, folder: Path) -> list[Path]:
        return [path for path in _versions(folder) if self._is_mine(path)]

    def _outline(self) -> list:
        """What is on the timeline, in a form that ignores how CapCut happens to store it."""
        return [(track.get("type"), [self._describe_segment(track, s)
                                     for s in sorted(track["segments"], key=self._start)])
                for track in self.content["tracks"] if track.get("segments")]

    def changed_outside(self) -> bool:
        """True when the timeline differs from what this server last wrote: edited in CapCut since."""
        snapshot = self._backup_dir() / LAST_WRITTEN
        if not snapshot.is_file():
            return False
        try:
            before = Project(self.folder, _read_json(snapshot))
            return _id_tag(before.content) == _id_tag(self.content) and before._outline() != self._outline()
        except (CapCutError, KeyError, TypeError, ValueError):
            return False

    def undo(self) -> dict:
        """Go back to the version before the last change made through this server."""
        self._require_writable()
        history = self._backup_dir()
        stored = self._mine(history)
        if not stored:
            raise CapCutError(f"No earlier version of '{self.name}' is stored.")
        latest = stored[-1]
        restored = _read_version(latest)
        result: dict = {"restored_version": _label_of(latest)}
        outside = self.changed_outside()
        self._keep_version(history / "redo", MAX_REDO)
        if outside:
            # The project was also edited in CapCut after the server's last change. Undo takes
            # that work back too, so keep a copy that a later edit cannot push out of reach.
            name = f"auto before-undo {datetime.now():%Y%m%d-%H%M%S}"
            self._keep_version(history / "checkpoints", label=name)
            automatic = [p for p in self._mine(history / "checkpoints") if _label_of(p).startswith("auto ")]
            for old in sorted(automatic, key=lambda p: p.stat().st_mtime)[:-MAX_AUTO_CHECKPOINTS]:
                old.unlink(missing_ok=True)
            result.update(changed_in_capcut=True, checkpoint=name, note=(
                "The project had also been changed in CapCut since the server's last edit, and "
                "those changes were taken back too. redo brings everything back; the state just "
                f"before this undo is also kept as checkpoint '{name}'."))
        self.content = restored
        self._write_content()
        latest.unlink(missing_ok=True)
        return result

    def redo(self) -> dict:
        """Bring back what the last undo took away."""
        self._require_writable()
        history = self._backup_dir()
        stored = self._mine(history / "redo")
        if not stored:
            raise CapCutError(
                f"There is nothing to redo for '{self.name}'. Redo is only possible right after "
                "an undo, before any other change.")
        latest = stored[-1]
        restored = _read_version(latest)
        self._keep_version(history, MAX_BACKUPS)
        self.content = restored
        self._write_content()
        latest.unlink(missing_ok=True)
        return {"redone_version": _label_of(latest)}

    @staticmethod
    def _checkpoint_label(name: str) -> str:
        label = re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|.]', " ", name)).strip()
        if not label:
            raise CapCutError("Give the checkpoint a name, for example 'before captions'.")
        return label[:60]

    def checkpoints(self) -> list[dict]:
        found = sorted(self._mine(self._backup_dir() / "checkpoints"), key=lambda p: p.stat().st_mtime)
        return [{"name": _label_of(p),
                 "saved": datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")}
                for p in found]

    def save_checkpoint(self, name: str) -> dict:
        """Keep the project as it is now under a name. Checkpoints are never rotated away."""
        label = self._checkpoint_label(name)
        folder = self._backup_dir() / "checkpoints"
        for existing in self._mine(folder):
            if _label_of(existing).lower() == label.lower():
                existing.unlink(missing_ok=True)  # same name: the newer state replaces it
        self._keep_version(folder, label=label)
        return {"checkpoint": label, "duration": _seconds(self.duration_us())}

    def restore_checkpoint(self, name: str) -> dict:
        """Put the project back to a named checkpoint. The current state stays reachable by undo."""
        self._require_writable()
        wanted = self._checkpoint_label(name).lower()
        match = next((p for p in self._mine(self._backup_dir() / "checkpoints")
                      if _label_of(p).lower() == wanted), None)
        if match is None:
            names = ", ".join(f"'{c['name']}'" for c in self.checkpoints()) or "none"
            raise CapCutError(f"'{self.name}' has no checkpoint named '{name}'. It has: {names}.")
        restored = _read_version(match)
        self._keep_version(self._backup_dir(), MAX_BACKUPS)
        for stale in _versions(self._backup_dir() / "redo"):
            stale.unlink(missing_ok=True)
        self.content = restored
        self._write_content()
        return {"restored_checkpoint": _label_of(match)}

    def _update_meta(self) -> None:
        now = _now_us()
        meta_path = self.folder / META_FILE
        if meta_path.is_file():
            try:
                meta = _read_json(meta_path)
            except CapCutError:
                meta = None
            if meta is not None:
                meta["tm_draft_modified"] = now
                meta["tm_duration"] = self.content.get("duration", 0)
                meta["draft_timeline_materials_size_"] = self._media_bytes()
                if self._new_bin_entries:
                    bins = meta.setdefault("draft_materials", [])
                    imported = next((b for b in bins if b.get("type") == 0), None)
                    if imported is None:
                        imported = {"type": 0, "value": []}
                        bins.insert(0, imported)
                    known = {e.get("file_Path") for e in imported["value"]}
                    imported["value"] += [e for e in self._new_bin_entries
                                          if e["file_Path"] not in known]
                if self._relinked:  # keep the media bin pointing at the files the clips now use
                    for media_bin in meta.get("draft_materials") or []:
                        kept, seen = [], set()
                        for entry in media_bin.get("value") or []:
                            moved = self._relinked.get(str(entry.get("file_Path", "")).lower())
                            if moved:
                                entry["file_Path"] = moved
                            # Two entries for one file would only be listed twice in CapCut.
                            key = str(entry.get("file_Path", "")).lower()
                            if moved and key in seen:
                                continue
                            seen.add(key)
                            kept.append(entry)
                        if isinstance(media_bin.get("value"), list):
                            media_bin["value"] = kept
                _write_json(meta_path, meta)
        self._new_bin_entries = []
        self._relinked = {}

        index_path = self.folder / "Timelines" / "project.json"
        if index_path.is_file():
            try:
                index = json.loads(index_path.read_text(encoding="utf-8"))
                index["update_time"] = now
                for timeline in index.get("timelines", []):
                    if timeline.get("id") == index.get("main_timeline_id"):
                        timeline["update_time"] = now
                _write_json(index_path, index)
            except (ValueError, OSError):
                pass

    def _media_bytes(self) -> int:
        total = 0
        for group in ("videos", "audios"):
            for material in self.materials.get(group, []):
                try:
                    total += Path(material.get("path", "")).stat().st_size
                except OSError:
                    pass
        return total

    def _ensure_cover(self) -> None:
        cover = self.folder / "draft_cover.jpg"
        if cover.exists():
            return
        source = self.frame_source(0.0)
        if source:
            try:
                extract_frame(source[0], source[1], cover, width=540)
            except CapCutError:
                pass


def _keyframe_at(frames: list[dict], offset: int) -> dict:
    """A new keyframe at `offset` with the value the animation has there (straight-line between
    its neighbours, holding the first or last value outside them). `frames` is sorted by time."""
    before = [f for f in frames if f["time_offset"] <= offset]
    after = [f for f in frames if f["time_offset"] >= offset]
    if not before:
        value = after[0]["values"][0]
    elif not after:
        value = before[-1]["values"][0]
    else:
        left, right = before[-1], after[0]
        span = right["time_offset"] - left["time_offset"]
        share = (offset - left["time_offset"]) / span if span else 0.0
        value = left["values"][0] + (right["values"][0] - left["values"][0]) * share
    frame = copy.deepcopy((before or after)[-1 if before else 0])
    frame.update(id=schema.new_id(), time_offset=int(offset), values=[float(value)])
    return frame


def _set_static(seg: dict, property_type: str | None, value: float) -> bool:
    """Store a keyframe property's value as the segment's plain setting. False if it has none."""
    clip = seg.get("clip") if isinstance(seg.get("clip"), dict) else None
    if property_type == "KFTypeVolume":
        seg["volume"] = float(value)
    elif clip is None:
        return False
    elif property_type == "KFTypeAlpha":
        clip["alpha"] = float(value)
    elif property_type == "KFTypePositionX":
        clip["transform"]["x"] = float(value)
    elif property_type == "KFTypePositionY":
        clip["transform"]["y"] = float(value)
    elif property_type == "KFTypeRotation":
        clip["rotation"] = float(value)
    elif property_type == "KFTypeScaleX" and (seg.get("uniform_scale") or {}).get("on", True):
        clip["scale"] = {"x": float(value), "y": float(value)}
    else:
        return False
    return True


_FULL_FRAME = {"upper_left_x": 0.0, "upper_left_y": 0.0, "upper_right_x": 1.0, "upper_right_y": 0.0,
               "lower_left_x": 0.0, "lower_left_y": 1.0, "lower_right_x": 1.0, "lower_right_y": 1.0}


def _uncropped(material: dict) -> bool:
    crop = material.get("crop")
    return not isinstance(crop, dict) or all(
        abs(float(crop.get(key, value)) - value) < 1e-9 for key, value in _FULL_FRAME.items())


def _renew_timeline_id(folder: Path) -> None:
    """Give a copied project its own timeline id, so it shares no identifier with the original."""
    index_path = folder / "Timelines" / "project.json"
    if not index_path.is_file():
        return
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except ValueError:
        return
    old = index.get("main_timeline_id")
    if not old or not (folder / "Timelines" / old).is_dir():
        return
    new = schema.new_id()
    (folder / "Timelines" / old).rename(folder / "Timelines" / new)
    index.update(id=schema.new_id(), main_timeline_id=new)
    for timeline in index.get("timelines", []):
        if timeline.get("id") == old:
            timeline["id"] = new
    _write_json(index_path, index)

    for file in _content_files(folder):
        content = _read_json(file)
        if content.get("id") == old:
            content["id"] = new
            _write_json(file, content)
    layout_path = folder / "timeline_layout.json"
    if layout_path.is_file():
        layout_path.write_text(layout_path.read_text(encoding="utf-8").replace(old, new),
                               encoding="utf-8")


def _local_platform(root: Path) -> dict:
    """Reuse the app/device block from a project CapCut itself wrote on this machine."""
    for folder in project_folders():
        try:
            other = json.loads(_root_content(folder).read_text(encoding="utf-8"))
        except (ValueError, OSError, UnicodeDecodeError, AttributeError):
            continue
        found = {k: other[k] for k in ("platform", "last_modified_platform", "version", "new_version")
                 if k in other}
        if "platform" in found:
            return found
    return {}
