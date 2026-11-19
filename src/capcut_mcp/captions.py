"""Reading and writing .srt subtitle files."""

from __future__ import annotations

import re
from pathlib import Path

from . import schema
from .draft import Project
from .env import CapCutError

_TIME = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def parse_srt(path: str) -> list[tuple[float, float, str]]:
    """(start seconds, end seconds, text) for every cue in an .srt file."""
    file = Path(path).expanduser()
    if not file.is_file():
        raise CapCutError(f"Subtitle file not found: {file}")
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", file.read_text(encoding="utf-8-sig", errors="replace")):
        lines = [line.strip() for line in block.strip().splitlines()]
        for number, line in enumerate(lines):
            match = _TIME.search(line)
            if match:
                h1, m1, s1, f1, h2, m2, s2, f2 = match.groups()
                begin = int(h1) * 3600 + int(m1) * 60 + int(s1) + int(f1) / 10 ** len(f1)
                end = int(h2) * 3600 + int(m2) * 60 + int(s2) + int(f2) / 10 ** len(f2)
                body = re.sub(r"<[^>]+>", "", "\n".join(lines[number + 1:])).strip()
                if body and end > begin:
                    cues.append((begin, end, body))
                break
    if not cues:
        raise CapCutError(f"No captions could be read from {file.name}. It should be an .srt file.")
    return cues


def _stamp(seconds: float) -> str:
    millis = round(seconds * 1000)
    return f"{millis // 3_600_000:02d}:{millis // 60_000 % 60:02d}:{millis // 1000 % 60:02d},{millis % 1000:03d}"


def export_srt(project: Project, path: str, track: int | None = None) -> int:
    """Write a project's text segments to an .srt file. Returns how many cues were written."""
    tracks = project.tracks("text") if track is None else [project.track("text", track)]
    cues = []
    for text_track in tracks:
        for seg in text_track["segments"]:
            material = project._main_material(text_track, seg)
            wording = schema.text_of(material) if material else ""
            if wording.strip():
                cues.append((project._start(seg) / 1e6, project._end(seg) / 1e6, wording.strip()))
    if not cues:
        raise CapCutError("There is no text on the timeline to export.")
    cues.sort()
    out = Path(path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "\n".join(f"{i}\n{_stamp(a)} --> {_stamp(b)}\n{text}\n" for i, (a, b, text) in enumerate(cues, 1)),
        encoding="utf-8")
    return len(cues)
