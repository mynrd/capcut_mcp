"""Reading media files with ffprobe and pulling still frames with ffmpeg."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .env import CapCutError

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".heic"}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    kind: str  # "video", "photo" or "audio"
    duration_us: int  # 0 for photos
    width: int
    height: int
    fps: float
    has_audio: bool

    def summary(self) -> dict:
        out: dict = {"path": str(self.path), "kind": self.kind}
        if self.kind != "photo":
            out["duration"] = round(self.duration_us / 1e6, 3)
        if self.kind != "audio":
            out["width"], out["height"] = self.width, self.height
        if self.kind == "video":
            out["fps"] = round(self.fps, 3)
            out["has_audio"] = self.has_audio
        return out


PROBE_TIMEOUT = 60.0
FRAME_TIMEOUT = 120.0


def run_tool(command: list[str], *, timeout: float, what: str) -> subprocess.CompletedProcess:
    """Run ffprobe or ffmpeg and capture what it prints.

    stdin is closed on purpose. This server talks to its client over its own stdin, and a child
    process that inherits it (FFmpeg reads keypresses from stdin) could swallow protocol bytes.
    """
    try:
        return subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise CapCutError(
            f"{what} took longer than {int(timeout)} seconds and was stopped. The file may be "
            "damaged or on a drive that is not responding.") from None


def _tool(name: str) -> str:
    found = os.environ.get(f"{name.upper()}_PATH") or shutil.which(name)
    if not found:
        raise CapCutError(
            f"{name} was not found. Install FFmpeg and make sure {name} is on PATH, "
            f"or set {name.upper()}_PATH to its full path."
        )
    return found


def probe(path: str | os.PathLike) -> MediaInfo:
    """Describe a media file. Raises CapCutError if it is missing or unreadable."""
    file = Path(path).expanduser()
    if not file.is_file():
        raise CapCutError(f"File not found: {file}")
    file = file.resolve()

    result = run_tool(
        [_tool("ffprobe"), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(file)],
        timeout=PROBE_TIMEOUT, what=f"Reading {file.name}")
    if result.returncode != 0:
        raise CapCutError(f"ffprobe could not read {file.name}: {result.stderr.strip()[:300]}")
    data = json.loads(result.stdout or "{}")
    streams = data.get("streams", [])

    # Cover art embedded in an audio file is reported as a video stream; ignore it.
    video = next(
        (s for s in streams
         if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")),
        None,
    )
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    duration_us = _duration_us(video, data.get("format", {}))

    if video is None:
        if not has_audio:
            raise CapCutError(f"{file.name} has no video or audio in it.")
        return MediaInfo(file, "audio", duration_us, 0, 0, 0.0, True)

    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    if _rotation(video) in (90, 270):
        width, height = height, width

    if file.suffix.lower() in IMAGE_EXTENSIONS:
        return MediaInfo(file, "photo", 0, width, height, 0.0, False)

    fps = 0.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            fps = float(Fraction(video.get(key) or "0"))
        except (ValueError, ZeroDivisionError):
            fps = 0.0
        if fps > 0:
            break
    if duration_us <= 0:
        raise CapCutError(f"Could not determine the duration of {file.name}.")
    return MediaInfo(file, "video", duration_us, width, height, fps, has_audio)


def _duration_us(video: dict | None, container: dict) -> int:
    """Length in whole microseconds, cut off rather than rounded, which is what CapCut stores.

    ffprobe prints the container length rounded to six decimals (6.166667), while CapCut derives
    6166666 from the exact frame count. The video stream's own tick count gives the exact value.
    """
    if video:
        try:
            exact = Fraction(int(video["duration_ts"])) * Fraction(video["time_base"])
            if exact > 0:
                return int(exact * 1_000_000)
        except (KeyError, ValueError, ZeroDivisionError):
            pass
    try:
        return int(Fraction(str(container.get("duration") or "0")) * 1_000_000)
    except ValueError:
        return 0


def _rotation(stream: dict) -> int:
    for side in stream.get("side_data_list", []):
        if "rotation" in side:
            return abs(int(float(side["rotation"]))) % 360
    try:
        return abs(int(float(stream.get("tags", {}).get("rotate", 0)))) % 360
    except ValueError:
        return 0


def extract_frame(path: str | os.PathLike, seconds: float, out: Path,
                  width: int | None = 480) -> Path:
    """Save one frame of a video (or a copy of a photo) as an image.

    `width` scales the frame; None keeps the original size. The format follows `out`'s extension.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    command = [_tool("ffmpeg"), "-nostdin", "-v", "error", "-y"]
    if Path(path).suffix.lower() not in IMAGE_EXTENSIONS:
        command += ["-ss", f"{max(seconds, 0):.3f}"]
    command += ["-i", str(path), "-frames:v", "1"]
    if width:
        command += ["-vf", f"scale={int(width)}:-2"]
    if out.suffix.lower() in (".jpg", ".jpeg"):
        command += ["-q:v", "5"]
    command.append(str(out))
    result = run_tool(command, timeout=FRAME_TIMEOUT, what=f"Reading a frame of {Path(path).name}")
    if result.returncode != 0 or not out.is_file():
        raise CapCutError(
            f"Could not extract a frame from {Path(path).name} at {seconds:.2f}s: "
            f"{result.stderr.strip()[:300]}"
        )
    return out
