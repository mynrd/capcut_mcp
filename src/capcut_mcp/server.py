"""The MCP server: the tools an assistant calls to edit a CapCut project."""

from __future__ import annotations

import functools
import os
import re
import time
from pathlib import Path
from typing import Any, Literal

import anyio
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import analysis, assist, draft, library, lint, schema, speech
from . import captions as srt
from . import doctor as health
from .draft import Project, Session, editing
from .draft import list_projects as _list_projects
from .env import CapCutError, data_dir
from .media import extract_frame, probe
from .render import render_preview as _render_preview
from .sheet import contact_sheet

INSTRUCTIONS = """\
Edits CapCut desktop projects by rewriting their project files. CapCut itself is not controlled.

How to work:
- All times are seconds. Timeline times are positions in the edited video; source times are
  positions inside a media file.
- Call get_timeline to see tracks and segment ids before editing, and again when unsure.
  Segments are addressed by the short id it shows. On a long project narrow it with type,
  track, start and end; give `segment` to see one segment in full.
- Video track 0 is the main track. Higher video tracks are overlays drawn on top of it.
- Segments on one track cannot overlap. Edits with ripple=true shift the later segments on the
  same track; other tracks stay where they are, so check text and audio still line up.
- Send edits to one project one after another, not in parallel. For many edits at once (a
  transition on every cut, twenty trims) use batch: one save, one undo step, all or nothing.
- Positions (x, y) run from -1 to 1 with 0,0 at the centre of the frame; x=1 is the right edge
  and y=1 the top edge.

Seeing and hearing:
- You cannot watch or hear media directly. probe_media, extract_frames, preview_timeline and
  render_preview show pictures. analyze_media reports silences, scene cuts, loudness and a
  level graph. transcribe_media turns speech into timed text when the optional speech
  package is installed.
- cut_silence removes the pauses from a talking clip, auto_captions writes subtitles from the
  speech, duck_audio lowers music while a voice plays.
- add_voiceover speaks text, or reads a project's captions aloud in time with them, using the
  voices built into Windows (list_voices names them). They sound synthetic; for a natural
  voice the user makes the file elsewhere and add_audio places it.
- Before telling the user an edit is ready, run check_project and fix what it reports.

Safety:
- Every change is kept: undo steps back, redo steps forward again. Before a large or risky
  change save a checkpoint, which stays until the user removes it.
- A project has to be closed in CapCut while it is edited; tools refuse otherwise. After edits
  the user opens the project from CapCut's home screen to see them.
- If something behaves oddly (edits not showing, projects unreadable, tools refusing), run
  doctor: it reports the CapCut version, FFmpeg and what is missing.

Library items (transitions, animations, effects, filters, fonts, blend modes, voice effects,
stickers, music):
- Two sources. Items the user has already used in one of their projects are copied exactly and
  are the most reliable. A built-in catalogue adds thousands of transitions, filters,
  animations, fonts and voice effects by name. Tools look in the user's projects first.
- Find names with list_library and its `search` parameter before applying anything. Most
  catalogue names are Chinese. Common English words are translated for you ("dissolve",
  "glitch", "typewriter", "zoom"), and several words can be tried at once, separated by commas.
- The catalogue is a snapshot. An item CapCut no longer offers shows up as missing when the
  project is opened; tell the user this can happen and offer another item.
- Items marked pro need a CapCut Pro account to export.
- Video effects, stickers, music and stock clips cannot come from the catalogue. They can only
  be reused from a project where the user already placed them. If the user asks for an effect
  that is not listed, ask them to add it once in CapCut.
- Masks, chroma key and voice effects are written the way other open tools write them but are
  not yet confirmed in CapCut by this server. After using one, ask the user to check it.

What the user must do in CapCut:
- Exporting the final video. render_preview only makes a rough low-resolution preview.
"""

mcp = MCPServer("capcut", instructions=INSTRUCTIONS)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
EDIT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

Align = Literal["left", "center", "right"]
TrackKind = Literal["video", "audio", "text", "effect", "filter", "sticker", "adjust"]
SoundTrack = Literal["video", "audio"]

# Tools that may be used as a step of `batch`.
BATCHABLE: set[str] = set()


def edit_tool(fn=None, *, batchable: bool = True):
    """Register a tool that changes a project.

    The project is held for as long as the tool runs, so two edits of it never interleave: each
    reads the project, changes it and writes it back, and without this the second would write
    over the first. Every such tool takes the project's name as `project`.
    """
    def register(function):
        @functools.wraps(function)
        def held(*args, **kwargs):
            with editing(kwargs["project"]):
                return function(*args, **kwargs)
        mcp.tool(annotations=EDIT)(held)
        if batchable:
            BATCHABLE.add(function.__name__)
        return held
    return register(fn) if fn is not None else register


class Clip(BaseModel):
    path: str = Field(description="Full path to a video or image file.")
    source_start: float = Field(0.0, description="Where in the file the clip begins, in seconds.")
    source_end: float | None = Field(None, description="Where in the file the clip ends. Default: end of file.")
    duration: float | None = Field(
        None, description="Length on the timeline. For images this is how long they show (default 5).")
    speed: float = Field(1.0, description="Playback speed. 2 is twice as fast, 0.5 is slow motion.")
    volume: float = Field(1.0, description="0 is silent, 1 is the original level.")


class Caption(BaseModel):
    text: str
    start: float = Field(description="Timeline time the caption appears, in seconds.")
    end: float = Field(description="Timeline time the caption disappears, in seconds.")


class Keyframe(BaseModel):
    time: float = Field(description="Seconds from the start of the segment (not the timeline).")
    value: float


class BatchEdit(BaseModel):
    tool: str = Field(description="Name of an editing tool, for example add_transition.")
    args: dict[str, Any] = Field(
        default_factory=dict, description="That tool's parameters, without `project`.")


def _saved(project: Project, *segments: dict, **extra) -> dict:
    project.save()
    out: dict = {"project": project.name, "duration": round(project.duration_us() / 1e6, 3)}
    described = []
    for seg in segments:
        track, _ = project.find(seg["id"])
        kind, index = project._track_position(track)
        described.append({"type": kind, "track": index, **project._describe_segment(track, seg)})
    if described:
        out["segments"] = described
    out.update(extra)
    return out


def _style(**values) -> dict:
    return {key: value for key, value in values.items() if value is not None}


def _overview(project: Project) -> dict:
    counts: dict[str, list[int]] = {}
    for track in project.content["tracks"]:
        counts.setdefault(track.get("type", ""), []).append(len(track.get("segments", [])))
    return {"duration": round(project.duration_us() / 1e6, 3), "segments_per_track": counts}


def _highlighted(project: Project, seg: dict) -> int:
    found = project.find_material(seg["material_id"])
    return len(schema.text_style_of(found[1]).get("other_styles", [])) if found else 0


# ----------------------------------------------------------------------- projects

@mcp.tool(annotations=READ_ONLY)
def list_projects() -> list[dict]:
    """List the CapCut projects on this computer, most recently changed first."""
    return _list_projects()


@mcp.tool(annotations=EDIT)
def create_project(name: str, width: int = 1920, height: int = 1080, fps: float = 30.0) -> dict:
    """Create a new, empty CapCut project.

    The canvas takes the size of the first clip added unless set_canvas is called, so width and
    height only matter for projects that start with text or images. Common sizes: 1920x1080
    (landscape), 1080x1920 (vertical, for Reels/TikTok/Shorts), 1080x1080 (square).
    """
    project = Project.create(name, width, height, fps)
    return {"project": project.name, "folder": str(project.folder),
            "note": "It appears on CapCut's home screen on its own within a few seconds. If it "
                    "does not, restarting CapCut makes it look for new projects."}


@edit_tool(batchable=False)
def duplicate_project(project: str, new_name: str) -> dict:
    """Copy a project under a new name, for example before a risky edit or to make a variant.

    This is also how a project the user designed in CapCut is used as a template: duplicate
    it, then swap its footage with replace_media and its wording with update_text.
    """
    copy = Project.duplicate(project, new_name)
    return {"project": copy.name, "folder": str(copy.folder)}


@mcp.tool(annotations=READ_ONLY)
def get_timeline(project: str, type: TrackKind | None = None, track: int | None = None,
                 start: float | None = None, end: float | None = None,
                 segment: str | None = None) -> dict:
    """Show a project's tracks and the segments on them, with the segment ids used by other tools.

    With nothing else given it lists everything. On a long project narrow it down: `type` and
    `track` pick tracks, and `start`/`end` (timeline seconds) keep only the segments that
    overlap that stretch. Give `segment` instead to see one segment in full: keyframe points,
    animation timing, text style, effect settings. Also reports media files that can no longer
    be found (see relink_media).
    """
    target = Project.open(project)
    if segment:
        return {"project": target.name, "segment": target.detail(segment)}
    return target.describe(type, track, start, end)


@edit_tool
def set_canvas(project: str, width: int, height: int) -> dict:
    """Change the project's frame size in pixels, e.g. 1080x1920 for vertical video."""
    target = Project.open(project)
    target.set_canvas(width, height)
    return _saved(target, canvas=f"{width}x{height}")


@edit_tool
def relink_media(project: str, old_path: str, new_path: str) -> dict:
    """Fix clips whose media was moved or renamed.

    old_path and new_path are either two folders (every file under the old folder is looked up
    at the same relative place under the new one) or two files. A clip is only repointed when
    the file exists at the new location. CapCut's own media list is repointed as well, which
    stops its "Link media" prompt for files that were moved.
    """
    target = Project.open(project)
    result = target.relink(old_path, new_path)
    if draft.capcut_running() and (result["relinked"] or result.get("media_list_entries_fixed")):
        # Seen on CapCut 9.8: it keeps the media list it read when it first listed the project
        # and writes that back when the project is opened, undoing a change made meanwhile.
        result["note"] = (
            "CapCut is running. The clips are repointed, but CapCut may put its own older media "
            "list back and still ask the user to link the moved files. If it does, the user "
            "links them once in that dialog, or closes CapCut completely before this is run again.")
    if not result["relinked"] and not result.get("media_list_entries_fixed"):
        return {"project": target.name, **result, "still_missing": target.missing_media()}
    return _saved(target, **result, still_missing=target.missing_media())


@mcp.tool(annotations=READ_ONLY)
def check_project(project: str) -> dict:
    """Look a project over for mistakes before the user opens it in CapCut.

    Reports problems (broken references, missing media, overlaps), warnings (a transition with
    no clip after it, text outside the frame, gaps on the main track, animations that no longer
    fit their segment) and notes (audio running past the picture, captions that are hard to
    read in time, library items still to be downloaded). Run it after a round of edits.
    """
    return lint.check(Project.open(project))


@mcp.tool(annotations=READ_ONLY)
def doctor() -> dict:
    """Check the setup: CapCut and its version, the projects folder, FFmpeg, optional parts.

    Use it when something behaves oddly, when a tool refuses because of the CapCut version, or
    when the user asks whether everything is installed. It says what is wrong and what to do.
    """
    return health.report()


# ----------------------------------------------------------- undo, redo, checkpoints

@edit_tool(batchable=False)
def undo(project: str) -> dict:
    """Restore the project to how it was before the last change made through this server.

    Can be called repeatedly to step further back; a batch counts as one change. redo reverses
    it. If the user also edited the project in CapCut since that change, undo takes those edits
    back as well: the result says so, and the state before the undo is kept as a checkpoint.
    """
    target = Project.open(project)
    result = target.undo()
    return {"project": target.name, **result, **_overview(target)}


@edit_tool(batchable=False)
def redo(project: str) -> dict:
    """Bring back what the last undo took away. Works until another change is made."""
    target = Project.open(project)
    result = target.redo()
    return {"project": target.name, **result, **_overview(target)}


@edit_tool(batchable=False)
def checkpoint(project: str, action: Literal["save", "restore", "list"] = "save",
               name: str | None = None) -> dict:
    """Keep a named copy of the project's timeline, or go back to one.

    "save" stores the project as it is now under `name` (the same name again replaces it).
    "restore" puts the project back to that copy; the state it replaces can still be reached
    with undo. "list" shows the checkpoints. Unlike undo steps, checkpoints are never dropped,
    so save one before a large change ("before captions") and the user can always return.
    """
    target = Project.open(project)
    if action == "list":
        return {"project": target.name, "checkpoints": target.checkpoints()}
    if not name:
        raise CapCutError("Give the checkpoint a name.")
    if action == "save":
        return {"project": target.name, **target.save_checkpoint(name)}
    result = target.restore_checkpoint(name)
    return {"project": target.name, **result, **_overview(target)}


@mcp.tool(annotations=EDIT)
async def batch(project: str, edits: list[BatchEdit], ctx: Context) -> dict:
    """Make several edits to one project in one go: one save, one undo step, all or nothing.

    Each edit names an editing tool and gives its parameters without `project`, for example
    {"tool": "add_transition", "args": {"segment": "7E5CA538", "name": "叠化"}}. They run in
    order on the same copy of the project, so a later edit sees what an earlier one did. If any
    edit fails, nothing is saved and the error says which one. Use it for repeated edits such
    as a transition on every cut, many trims or many titles. Segment ids that an earlier step
    creates are not known in advance; put edits that need them in a second batch.
    """
    if not edits:
        raise CapCutError("Give at least one edit.")
    if len(edits) > 300:
        raise CapCutError("A batch takes at most 300 edits; split it up.")
    unknown = sorted({edit.tool for edit in edits} - BATCHABLE)
    if unknown:
        raise CapCutError(f"Not usable in a batch: {', '.join(unknown)}. A batch can hold: "
                          + ", ".join(sorted(BATCHABLE)) + ".")
    session = await anyio.to_thread.run_sync(Session, project)
    token = session.activate()
    steps = []
    try:
        for number, edit in enumerate(edits, 1):
            try:
                # The tool manager validates the arguments exactly as for a direct call.
                result = await mcp._tool_manager.call_tool(
                    edit.tool, {**edit.args, "project": project}, ctx, convert_result=False)
            except ToolError as error:
                reason = str(error).removeprefix(f"Error executing tool {edit.tool}: ")
                raise CapCutError(
                    f"Edit {number} of {len(edits)} ({edit.tool}) failed, so nothing was saved: {reason}"
                ) from None
            brief = {k: v for k, v in result.items() if k not in ("project", "duration")} \
                if isinstance(result, dict) else {}
            steps.append({"tool": edit.tool, **brief})
    except BaseException:
        Session.deactivate(token)
        session.abort()
        raise
    Session.deactivate(token)
    saved = await anyio.to_thread.run_sync(session.commit)
    return {"project": session.project.name, "edits": len(steps), "saved": saved,
            "duration": round(session.project.duration_us() / 1e6, 3), "steps": steps}


# ------------------------------------------------------------------ looking at media

@mcp.tool(annotations=READ_ONLY)
def probe_media(paths: list[str]) -> list[dict]:
    """Report the type, duration, frame size and frame rate of media files."""
    out = []
    for path in paths:
        try:
            out.append(probe(path).summary())
        except CapCutError as error:
            out.append({"path": path, "error": str(error)})
    return out


def _frames_dir() -> Path:
    folder = data_dir() / "frames"
    folder.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 24 * 3600
    for old in folder.glob("*.jpg"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
        except OSError:
            pass
    return folder


def _frame_file(path: str, seconds: float, width: int) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(path).stem)[:40]
    out = _frames_dir() / f"{safe}-{int(seconds * 1000):08d}-{width}.jpg"
    extract_frame(path, seconds, out, width)
    return out


def _frame(path: str, seconds: float, width: int, label: str) -> tuple[str, Image]:
    out = _frame_file(path, seconds, width)
    return f"{label} -> {out}", Image(path=str(out))


def _sheet(frames: list[tuple[Path, str]], name: str, notes: list[str]) -> list:
    """One labelled grid picture from (image file, label) pairs, with a line saying what is where."""
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name)[:40]
    out = _frames_dir() / f"sheet-{safe}-{int(time.time() * 1000)}.jpg"
    contact_sheet([(str(file), label) for file, label in frames], out)
    order = ", ".join(label for _, label in frames)
    return [*notes, f"Contact sheet of {name}, left to right then top to bottom: {order} -> {out}",
            Image(path=str(out))]


@mcp.tool(annotations=READ_ONLY)
def extract_frames(path: str, times: list[float] | None = None, count: int = 4, width: int = 480,
                   sheet: bool = False) -> list:
    """Return still frames from a video file so its content can be seen.

    Give `times` (seconds into the file) for specific moments, or leave it out to get `count`
    frames spread evenly across the file. Returns the images and where each was saved. With
    sheet=true the frames come back as one grid picture with the time written on each cell,
    which costs less to look at and allows up to 24 frames instead of 12: good for skimming a
    long clip before choosing moments to look at closely.
    """
    info = probe(path)
    limit = 24 if sheet else 12
    if info.kind == "audio":
        raise CapCutError(f"{info.path.name} is an audio file; it has no frames.")
    if info.kind == "photo":
        moments = [0.0]
    elif times:
        moments = [min(max(t, 0.0), info.duration_us / 1e6 - 0.05) for t in times[:limit]]
    else:
        count = max(1, min(count, limit))
        moments = [info.duration_us / 1e6 * (i + 0.5) / count for i in range(count)]
    width = max(120, min(width, 1280))
    if sheet:
        cells = [(_frame_file(str(info.path), moment, min(width, 480)), f"{moment:.2f}s")
                 for moment in moments]
        return _sheet(cells, info.path.name, [])
    out: list = []
    for moment in moments:
        out.extend(_frame(str(info.path), moment, width, f"{info.path.name} at {moment:.2f}s"))
    return out


@mcp.tool(annotations=READ_ONLY)
def preview_timeline(project: str, times: list[float] | None = None, count: int = 6, width: int = 360,
                     sheet: bool = False) -> list:
    """Return frames of the footage playing at points on the project's timeline.

    Fast, but shows only the lowest video clip at each time, without text or overlays. Use
    render_preview to see the composed picture. Give `times` (timeline seconds) or get `count`
    evenly spaced frames. With sheet=true they come back as one labelled grid picture (up to
    24 frames), a quick storyboard of the whole edit.
    """
    target = Project.open(project)
    total = target.duration_us() / 1e6
    if total <= 0:
        raise CapCutError(f"'{target.name}' has nothing on its timeline yet.")
    limit = 24 if sheet else 12
    if times:
        moments = times[:limit]
    else:
        count = max(1, min(count, limit))
        moments = [total * (i + 0.5) / count for i in range(count)]
    width = max(120, min(width, 1280))
    out: list = []
    cells: list[tuple[Path, str]] = []
    for moment in moments:
        source = target.frame_source(moment)
        if source is None:
            out.append(f"timeline {moment:.2f}s -> no video at this time")
        elif sheet:
            cells.append((_frame_file(source[0], source[1], min(width, 480)), f"{moment:.2f}s"))
        else:
            out.extend(_frame(source[0], source[1], width,
                              f"timeline {moment:.2f}s ({Path(source[0]).name} at {source[1]:.2f}s)"))
    if sheet and cells:
        return _sheet(cells, target.name, out)
    return out


@mcp.tool(annotations=READ_ONLY)
def render_preview(project: str, output_path: str | None = None, height: int = 640,
                   start: float | None = None, end: float | None = None,
                   include_text: bool = True, frames: int = 0) -> list:
    """Render a rough low-resolution video of the project with FFmpeg, to check the edit.

    Draws every video track with position, scale, rotation, crop, opacity, fades, keyframed
    movement and blurred or coloured backgrounds, burns in text, and mixes all audio with
    volumes and fades. It is an approximation, not CapCut's output: transitions, effects,
    filters, stickers, masks and library animations are not drawn, and text size and font only
    roughly match. `start`/`end` limit it to part of the timeline, which is much faster on a
    long project. Set `frames` to also get that many stills from the result back as images.
    The video file is saved to output_path (default: this server's data folder) for the user
    to play.
    """
    target = Project.open(project)
    if output_path:
        out = Path(output_path).expanduser()
    else:
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", target.name)[:50]
        out = data_dir() / "previews" / f"{safe}.mp4"
    report = _render_preview(target, out, height=height, start=start, end=end,
                             include_text=include_text)
    result: list = [report]
    frames = max(0, min(frames, 12))
    for i in range(frames):
        moment = report["duration"] * (i + 0.5) / frames
        result.extend(_frame(str(out), moment, min(480, int(report["size"].split("x")[0])),
                             f"preview at {(start or 0) + moment:.2f}s"))
    return result


@mcp.tool(annotations=READ_ONLY)
def analyze_media(path: str, what: list[Literal["silence", "scenes", "loudness", "levels"]],
                  start: float = 0.0, end: float | None = None, silence_db: float = -35.0,
                  min_silence: float = 0.4, scene_threshold: float = 0.3,
                  level_step: float = 0.5) -> dict:
    """Measure a media file's sound and picture, since they cannot be heard or watched directly.

    `what` lists the measurements wanted. All times are seconds inside the file.
    - "silence": the silent stretches and, between them, the stretches with speech or sound.
      A stretch counts as silent below `silence_db` for at least `min_silence` seconds. Use it
      to find where someone talks, or pauses worth cutting (cut_silence does the cutting).
    - "scenes": the moments the picture changes abruptly, i.e. cuts in the footage. Lower
      `scene_threshold` (0.2) to catch cuts between similar-looking shots.
    - "loudness": overall loudness in LUFS and the true peak, plus the `volume` setting that
      would bring the clip to -14 LUFS, a common level for online video.
    - "levels": loudness over time, one number in dB every `level_step` seconds; -90 is
      silence. Shows where the loud and quiet parts are, for example to place a music drop.
    `start` and `end` limit the measurement to part of the file.
    """
    if not what:
        raise CapCutError('Say what to measure: "silence", "scenes", "loudness" and/or "levels".')
    out: dict = {"file": str(Path(path).expanduser())}
    if "silence" in what:
        found = analysis.detect_silence(path, start=start, end=end, noise_db=silence_db,
                                        min_silence=min_silence)
        out["analysed"] = found["analysed"]
        out["silence"] = {key: found[key] for key in ("silences", "sounds", "silent_seconds")}
    if "scenes" in what:
        found = analysis.detect_scenes(path, start=start, end=end, threshold=scene_threshold)
        out["analysed"] = found["analysed"]
        out["scenes"] = {key: found[key] for key in ("cuts", "truncated") if key in found}
    if "loudness" in what:
        found = analysis.measure_loudness(path, start=start, end=end)
        out["analysed"] = found["analysed"]
        loudness = {key: value for key, value in found.items() if key not in ("file", "analysed")}
        if loudness.get("integrated_lufs") is not None:
            loudness["volume_for_minus_14_lufs"] = round(
                10 ** ((-14.0 - loudness["integrated_lufs"]) / 20), 2)
        out["loudness"] = loudness
    if "levels" in what:
        found = analysis.audio_levels(path, start=start, end=end, step=level_step)
        out["levels"] = {key: found[key] for key in ("start", "step", "db")}
    return out


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
def transcribe_media(path: str, language: str | None = None, model: str = "base",
                     start: float = 0.0, end: float | None = None, words: bool = False) -> dict:
    """Turn the speech in a video or audio file into text with timestamps.

    Returns sentences with their start and end in the file; words=true adds every single word
    with its timing, for cutting on exact words. Leave `language` out to detect it, or give a
    code such as "en" or "tl". `model` trades speed for accuracy: "tiny", "base", "small",
    "medium", "large-v3". The first use of a model downloads it. Results are remembered, so
    asking again for the same file is instant. Needs the optional speech package; if it is
    missing the error says how to install it. To subtitle a project, auto_captions is simpler.
    """
    found = analysis.transcribe(path, language=language, model=model, start=start, end=end)
    out = {key: found[key] for key in ("file", "language", "model", "cached", "segments")}
    if words:
        out["words"] = found["words"]
    return out


@mcp.tool(annotations=READ_ONLY)
def list_fonts(search: str = "") -> list[str]:
    """List font files on this computer that can be given as font_path, optionally filtered by name."""
    folders = [Path("C:/Windows/Fonts"), Path.home() / "Library" / "Fonts", Path("/Library/Fonts"),
               Path("/System/Library/Fonts")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        folders.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
        apps = Path(local) / "CapCut" / "Apps"
        if apps.is_dir():
            folders += sorted(apps.glob("*/Resources/Font/SystemFont"), reverse=True)[:1]
    wanted = search.strip().lower()
    found = []
    for folder in folders:
        if folder.is_dir():
            found += [str(f) for f in sorted(folder.iterdir())
                      if f.suffix.lower() in (".ttf", ".otf", ".ttc") and wanted in f.name.lower()]
    if not found:
        raise CapCutError(f"No font files matching '{search}' were found.")
    return found[:80]


# -------------------------------------------------------------------- adding things

@edit_tool
def add_clips(project: str, clips: list[Clip], track: int = 0, start: float | None = None,
              ripple: bool = False) -> dict:
    """Put video or image files on a video track, one after another in the order given.

    By default they are appended after whatever is already on the track. Give `start` to place
    the first one at a specific timeline time: the space must be free, or set ripple=true to
    insert there and push the later clips along (start must then be where a clip begins or
    ends). Use track 1 or higher for overlays such as picture-in-picture or a logo.
    """
    if not clips:
        raise CapCutError("Give at least one clip.")
    target = Project.open(project)
    added = []
    cursor = None  # each clip after the first starts exactly where the previous one ends
    for position, clip in enumerate(clips):
        seg = target.add_clip(
            clip.path, track=track, start=start if position == 0 else None, start_us=cursor,
            ripple=ripple and (start is not None),
            source_start=clip.source_start, source_end=clip.source_end, duration=clip.duration,
            speed=clip.speed, volume=clip.volume)
        cursor = seg["target_timerange"]["start"] + seg["target_timerange"]["duration"]
        added.append(seg)
    return _saved(target, *added)


@edit_tool
def add_audio(project: str, path: str, start: float | None = None, source_start: float = 0.0,
              source_end: float | None = None, duration: float | None = None, track: int = 0,
              volume: float = 1.0, fade_in: float = 0.0, fade_out: float = 0.0,
              speed: float = 1.0) -> dict:
    """Put music, a voice-over or a sound effect on an audio track.

    `start` is the timeline time it begins (default: after the last audio on that track).
    `source_start`/`source_end` pick the part of the file to use. `duration` caps the length on
    the timeline, e.g. the video's duration so music ends with the picture. Fades are in seconds.
    A video file can be given to use only its sound. Sounds that overlap in time go on
    different tracks.
    """
    target = Project.open(project)
    seg = target.add_audio(path, track=track, start=start, source_start=source_start,
                           source_end=source_end, duration=duration, speed=speed, volume=volume,
                           fade_in=fade_in, fade_out=fade_out)
    return _saved(target, seg)


@mcp.tool(annotations=READ_ONLY)
def list_voices() -> dict:
    """List the computer voices add_voiceover can speak with, and which one is the default.

    These are the speech voices built into Windows, with their language and gender. More,
    including other languages, come with Windows' own speech and language settings.
    """
    return speech.voices()


@edit_tool
def add_voiceover(project: str, text: str | None = None, from_text_track: int | None = None,
                  voice: str | None = None, rate: int = 0, start: float | None = None,
                  track: int | None = None, volume: float = 1.0, fit: bool = True,
                  folder: str | None = None) -> dict:
    """Make a voice-over from text with a computer voice (text to speech) and put it on the timeline.

    Give `text` to have it spoken as one clip beginning at `start` (default: the start of the
    video, or after the last audio on `track` when one is given). Or give `from_text_track` to read the captions on that text track
    aloud: they are joined into sentences, and each sentence is spoken as its own clip starting
    when its first caption appears, so voice and captions stay together. With fit=true (the
    default) a sentence that would run into the next one is spoken faster until it fits; one
    that cannot fit even at the fastest rate starts late, and the result says so.

    `voice` is a name or part of one from list_voices (default: the computer's default voice).
    `rate` is the speaking pace from -10 (slowest) to 10 (fastest), 0 being the voice's normal
    pace. The voice goes on a new audio track unless `track` is given. The audio files are kept
    in this server's data folder, or in `folder`; the project plays them from there, so they
    must not be deleted. Same text, voice and rate reuse the same file. Uses the voices built
    into Windows: free and offline, but synthetic-sounding.
    """
    target = Project.open(project)
    result = assist.voiceover(target, text=text, text_track=from_text_track, voice=voice,
                              rate=rate, start=start, track=track, volume=volume, fit=fit,
                              folder=folder)
    return _saved(target, **result)


@edit_tool
def add_text(project: str, text: str, start: float, duration: float, track: int = 0,
             x: float = 0.0, y: float = 0.0, size: float = 15.0, color: str = "#FFFFFF",
             bold: bool | None = None, italic: bool | None = None, underline: bool | None = None,
             align: Align | None = None, stroke_color: str | None = None,
             stroke_width: float | None = None, background_color: str | None = None,
             background_opacity: float | None = None, shadow: bool | None = None,
             font: str | None = None, font_path: str | None = None,
             letter_spacing: float | None = None,
             line_spacing: float | None = None, wrap_width: float | None = None,
             highlight: list[str] | None = None, highlight_color: str | None = None,
             scale: float = 1.0, rotation: float = 0.0, opacity: float = 1.0) -> dict:
    """Add a title or other text overlay.

    y=-0.75 sits near the bottom of the frame, y=0.75 near the top. Colours are hex like
    #FFD400. stroke_width around 0.08 gives a normal outline. `font` is the name of a font
    from CapCut's library (find one with list_library kind="font"); `font_path` is a .ttf/.otf
    file on this computer instead (see list_fonts). Leave both out for CapCut's default font.
    letter_spacing and line_spacing use the same numbers as CapCut's text panel. wrap_width is
    the share of the frame width a line may fill before wrapping (default 0.82). `highlight`
    lists words or phrases to show in `highlight_color` (default yellow) while the rest keeps
    `color`. Text that must overlap in time goes on different tracks.
    """
    target = Project.open(project)
    seg = target.add_text(
        text, start=start, duration=duration, track=track, x=x, y=y, scale=scale,
        rotation=rotation, opacity=opacity,
        **_style(size=size, color=color, bold=bold, italic=italic, underline=underline, align=align,
                 stroke_color=stroke_color, stroke_width=stroke_width,
                 background_color=background_color, background_opacity=background_opacity,
                 shadow=shadow, font_path=font_path, letter_spacing=letter_spacing,
                 line_spacing=line_spacing, wrap_width=wrap_width, highlight=highlight,
                 highlight_color=highlight_color,
                 font_ref=library.font_reference(font) if font else None))
    if highlight:
        return _saved(target, seg, highlighted=_highlighted(target, seg))
    return _saved(target, seg)


@edit_tool
def add_captions(project: str, captions: list[Caption] | None = None, srt_path: str | None = None,
                 track: int = 0, y: float = -0.75, size: float = 8.0, color: str = "#FFFFFF",
                 bold: bool | None = None, stroke_color: str | None = "#000000",
                 stroke_width: float | None = 0.08, background_color: str | None = None,
                 background_opacity: float | None = None, font: str | None = None,
                 font_path: str | None = None, highlight: list[str] | None = None,
                 highlight_color: str | None = None, offset: float = 0.0) -> dict:
    """Add subtitles: many timed lines of text that share one style.

    Give either `captions` (text with start and end timeline times) or `srt_path` (an .srt
    file). `offset` shifts every caption by that many seconds, for a subtitle file timed
    against footage that starts later on the timeline. They are placed low in the frame with a
    black outline by default. `highlight` lists words or phrases to show in `highlight_color`
    (default yellow) wherever they occur. Captions must not overlap each other in time. To
    write captions from what is said in the footage, use auto_captions.
    """
    if bool(captions) == bool(srt_path):
        raise CapCutError("Give either captions or srt_path, not both and not neither.")
    cues = ([(c.start, c.end, c.text) for c in captions] if captions else srt.parse_srt(srt_path))
    target = Project.open(project)
    style = _style(size=size, color=color, bold=bold, stroke_color=stroke_color,
                   stroke_width=stroke_width, background_color=background_color,
                   background_opacity=background_opacity, font_path=font_path,
                   highlight=highlight, highlight_color=highlight_color,
                   font_ref=library.font_reference(font) if font else None)
    added = []
    for begin, end, text in sorted(cues):
        if end <= begin:
            raise CapCutError(f"Caption '{text[:30]}' ends before it starts.")
        if begin + offset < 0:
            raise CapCutError(f"With offset {offset}, caption '{text[:30]}' would start before 0.")
        added.append(target.add_text(text, start=begin + offset, duration=end - begin,
                                     track=track, y=y, kind="subtitle", **style))
    target.save()
    out = {"project": target.name, "captions_added": len(added),
           "first_id": added[0]["id"][:8], "last_id": added[-1]["id"][:8],
           "duration": round(target.duration_us() / 1e6, 3)}
    if highlight:
        out["captions_with_highlights"] = sum(1 for seg in added if _highlighted(target, seg))
    return out


@edit_tool
def auto_captions(project: str, type: SoundTrack = "video", track: int = 0,
                  language: str | None = None, model: str = "base", max_chars: int = 32,
                  max_duration: float = 4.0, text_track: int | None = None, y: float = -0.75,
                  size: float = 8.0, color: str = "#FFFFFF", bold: bool | None = None,
                  stroke_color: str | None = "#000000", stroke_width: float | None = 0.08,
                  font: str | None = None, font_path: str | None = None) -> dict:
    """Write subtitles from what is said on a track, timed to the speech.

    Listens to every clip on the given video or audio track (default: the main video track),
    follows each clip's trimming and speed, and adds short caption lines where the words are
    spoken. `max_chars` and `max_duration` limit how much goes into one line; short lines of
    about 20 characters suit vertical video. The captions go on a new text track unless
    `text_track` is given. `language` and `model` work as in transcribe_media. Needs the
    optional speech package; if it is missing the error says how to install it. Run it after
    cutting, so the captions match the final timing.
    """
    target = Project.open(project)
    result = assist.auto_captions(
        target, kind=type, track=track, language=language, model=model, max_chars=max_chars,
        max_duration=max_duration, text_track=text_track, y=y,
        **_style(size=size, color=color, bold=bold, stroke_color=stroke_color,
                 stroke_width=stroke_width, font_path=font_path,
                 font_ref=library.font_reference(font) if font else None))
    return _saved(target, **result)


@edit_tool
def update_text(project: str, segment: str, text: str | None = None, size: float | None = None,
                color: str | None = None, bold: bool | None = None, italic: bool | None = None,
                underline: bool | None = None, align: Align | None = None,
                stroke_color: str | None = None, stroke_width: float | None = None,
                background_color: str | None = None, background_opacity: float | None = None,
                shadow: bool | None = None, font: str | None = None,
                font_path: str | None = None,
                letter_spacing: float | None = None, line_spacing: float | None = None,
                wrap_width: float | None = None, highlight: list[str] | None = None,
                highlight_color: str | None = None) -> dict:
    """Change the wording or style of an existing text segment. Only what is given changes.

    stroke_width=0 removes the outline; background_opacity=0 removes the background.
    `highlight` lists words or phrases to show in `highlight_color`; an empty list removes the
    highlighting. New wording puts the whole text back in one style, so pass `highlight` again
    with it. Use set_transform to move or resize text, and trim_segment or move_segment for
    its timing.
    """
    target = Project.open(project)
    style = _style(text=text, size=size, color=color, bold=bold, italic=italic, underline=underline,
                   align=align, stroke_color=stroke_color, stroke_width=stroke_width,
                   background_color=background_color, background_opacity=background_opacity,
                   shadow=shadow, font_path=font_path, letter_spacing=letter_spacing,
                   line_spacing=line_spacing, wrap_width=wrap_width, highlight=highlight,
                   highlight_color=highlight_color,
                   font_ref=library.font_reference(font) if font else None)
    if not style:
        raise CapCutError("Nothing to change was given.")
    seg = target.update_text(segment, **style)
    if highlight:
        return _saved(target, seg, highlighted=_highlighted(target, seg))
    return _saved(target, seg)


@mcp.tool(annotations=READ_ONLY)
def export_captions(project: str, srt_path: str, track: int | None = None) -> dict:
    """Write the project's text segments to an .srt subtitle file (all text tracks, or one)."""
    target = Project.open(project)
    written = srt.export_srt(target, srt_path, track)
    return {"file": str(Path(srt_path).expanduser()), "captions": written}


# ------------------------------------------------------------------------- editing

@edit_tool
def trim_segment(project: str, segment: str, source_start: float | None = None,
                 source_end: float | None = None, duration: float | None = None,
                 ripple: bool = True) -> dict:
    """Change which part of a clip plays, or how long text or an image stays on screen.

    For video and audio, `source_start` and `source_end` are positions in the media file: raise
    source_start to cut the beginning off, lower source_end to cut the end off. `duration` sets
    the length on the timeline instead and is the only option for text and images. The segment
    keeps its timeline start. With ripple, later segments on the track move to stay adjacent.
    """
    target = Project.open(project)
    seg = target.trim(segment, source_start=source_start, source_end=source_end,
                      duration=duration, ripple=ripple)
    return _saved(target, seg)


@edit_tool
def split_segment(project: str, segment: str, at: float) -> dict:
    """Cut one segment into two at a timeline time, so each part can be edited or removed alone."""
    target = Project.open(project)
    left, right = target.split(segment, at)
    return _saved(target, left, right)


@edit_tool
def cut_range(project: str, start: float, end: float, type: TrackKind = "video", track: int = 0,
              all_tracks: bool = False, ripple: bool = True) -> dict:
    """Remove everything between two timeline times, e.g. a mistake from 12s to 15.5s.

    Segments reaching into the range are split at its edges and the part inside is removed. By
    default this affects one track; all_tracks=true cuts the same range out of every track so
    picture, sound and text stay in step. With ripple, what follows moves earlier to close the gap.
    """
    target = Project.open(project)
    removed = target.cut_range(start, end, kind=type, track=track, all_tracks=all_tracks, ripple=ripple)
    return _saved(target, deleted=removed)


@edit_tool
def cut_silence(project: str, segment: str, silence_db: float = -35.0, min_silence: float = 0.5,
                keep: float = 0.12, all_tracks: bool = False, dry_run: bool = False) -> dict:
    """Remove the pauses from a talking clip, closing the gaps (jump cuts).

    Listens to the part of the file the clip plays and cuts every stretch quieter than
    `silence_db` that lasts at least `min_silence` seconds, leaving `keep` seconds of air on
    each side of the speech. The clip becomes several clips. With all_tracks=true the same
    stretches are cut from every track, which keeps captions and overlays in step but also
    cuts music; add music afterwards instead. dry_run=true only reports what would be removed.
    For a noisy recording raise silence_db (-30); if speech gets clipped, lower it (-40).
    """
    target = Project.open(project)
    result = assist.cut_silence(target, segment, noise_db=silence_db, min_silence=min_silence,
                                keep=keep, all_tracks=all_tracks, dry_run=dry_run)
    if dry_run or not result.get("pieces_left"):
        return {"project": target.name, **result}
    return _saved(target, **result)


@edit_tool
def move_segment(project: str, segment: str, start: float | None = None,
                 track: int | None = None) -> dict:
    """Move a segment to another timeline time and/or another track of the same type.

    The destination must be free. To change the order of clips on a track, arrange_track is easier.
    """
    target = Project.open(project)
    return _saved(target, target.move(segment, start=start, track=track))


@edit_tool
def delete_segments(project: str, segments: list[str], ripple: bool = True) -> dict:
    """Remove segments from the timeline. With ripple, later segments on the same track close the gap."""
    target = Project.open(project)
    removed = target.delete(segments, ripple=ripple)
    return _saved(target, deleted=removed)


@edit_tool
def arrange_track(project: str, type: TrackKind = "video", track: int = 0,
                  order: list[str] | None = None, start: float | None = None,
                  gap: float = 0.0) -> dict:
    """Line a track's segments up back to back, optionally in a new order.

    `order` lists segment ids in the wanted sequence; segments left out follow in their current
    order. `start` is where the first one begins (default: where the track currently begins).
    Use this to reorder clips or to close every gap on a track.
    """
    target = Project.open(project)
    arranged = target.arrange(type, track, order=order, start=start, gap=gap)
    return _saved(target, *arranged["segments"])


@edit_tool
def replace_media(project: str, segment: str, path: str) -> dict:
    """Swap the file a clip plays for another one, keeping its position, speed, volume and framing.

    If the new file is shorter than the part the clip was using, the clip is shortened and the
    later clips on its track move up.
    """
    target = Project.open(project)
    return _saved(target, target.replace_media(segment, path))


@edit_tool
def freeze_frame(project: str, segment: str, at: float, duration: float = 3.0) -> dict:
    """Hold the picture showing at a timeline time for `duration` seconds.

    The clip is split at that time and a still of the frame is inserted, pushing the rest of
    the track later. The still image is saved in this server's data folder.
    """
    target = Project.open(project)
    return _saved(target, target.freeze_frame(segment, at, duration))


@edit_tool
def set_speed(project: str, segment: str, speed: float, ripple: bool = True) -> dict:
    """Speed a video or audio clip up or slow it down. 2 plays twice as fast, 0.5 at half speed.

    The clip's length on the timeline changes; with ripple the later segments on its track follow.
    """
    target = Project.open(project)
    return _saved(target, target.set_speed(segment, speed, ripple=ripple))


@edit_tool
def set_volume(project: str, segment: str, volume: float | None = None,
               fade_in: float | None = None, fade_out: float | None = None) -> dict:
    """Set a clip's loudness and audio fades. volume 0 mutes, 1 is original, 2 is twice as loud.

    fade_in and fade_out are lengths in seconds; 0 removes a fade.
    """
    target = Project.open(project)
    return _saved(target, target.set_volume(segment, volume, fade_in, fade_out))


@edit_tool
def set_track_volume(project: str, type: SoundTrack, track: int, volume: float) -> dict:
    """Set the loudness of every clip on a track at once, e.g. 0 to silence all the footage's own sound."""
    target = Project.open(project)
    changed = target.set_track_volume(type, track, volume)
    return _saved(target, clips_changed=len(changed))


@edit_tool
def duck_audio(project: str, segment: str, under_type: SoundTrack = "audio", under_track: int = 0,
               level: float = 0.25, fade: float = 0.3, speech_only: bool = False) -> dict:
    """Lower a clip's volume whenever the clips on another track play: music under a voice.

    `segment` is the clip to lower, usually the music. `under_type` and `under_track` say where
    the voice is (an audio track, or a video track for the footage's own sound). While those
    clips play the volume drops to `level` times its normal value, gliding down and back up
    over `fade` seconds. speech_only=true listens to the voice clips and ducks only while
    someone is actually speaking, not during their pauses. This replaces any volume keyframes
    on the clip.
    """
    target = Project.open(project)
    result = assist.duck(target, segment, under_type=under_type, under_track=under_track,
                         level=level, fade=fade, speech_only=speech_only)
    _, seg = target.find(segment)
    return _saved(target, seg, **{k: v for k, v in result.items() if k != "segment"})


@edit_tool
def set_audio_effect(project: str, segment: str, name: str,
                     settings: dict[str, float] | None = None) -> dict:
    """Put a voice changer or a sound-scene effect on an audio clip.

    `name` comes from list_library kind="audio_effect": type "voice" changes how a voice sounds
    (robot, chipmunk, monster), type "scene" makes it sound as if played somewhere. A clip
    holds one of each type; a new one replaces the old one of its type. `settings` adjusts the
    effect by setting name, each from 0 to 100. Applies to clips on audio tracks. Remove with
    clear_segment what=["audio_effects"].
    """
    target = Project.open(project)
    return _saved(target, library.set_audio_effect(target, segment, name, settings))


@edit_tool
def set_transform(project: str, segment: str, x: float | None = None, y: float | None = None,
                  scale: float | None = None, rotation: float | None = None,
                  opacity: float | None = None, flip_horizontal: bool | None = None,
                  flip_vertical: bool | None = None) -> dict:
    """Move, resize, rotate, fade or mirror a video, image or text segment.

    scale 1 is the fitted size, 0.5 half of it. rotation is in degrees, clockwise. opacity runs
    0 to 1.
    """
    target = Project.open(project)
    seg = target.set_transform(segment, x=x, y=y, scale=scale, rotation=rotation, opacity=opacity,
                               flip_horizontal=flip_horizontal, flip_vertical=flip_vertical)
    return _saved(target, seg)


@edit_tool
def crop_segment(project: str, segment: str, left: float = 0.0, top: float = 0.0,
                 right: float = 0.0, bottom: float = 0.0) -> dict:
    """Crop a video or image clip by cutting a fraction off each side.

    Each value is the share of the picture removed from that side: left=0.1 removes the left
    tenth. All zeros removes the crop. The cropped picture is then fitted to the frame again.
    """
    target = Project.open(project)
    return _saved(target, target.crop(segment, left, top, right, bottom))


@edit_tool
def set_background(project: str, segment: str, blur: float | None = None,
                   color: str | None = None) -> dict:
    """Fill the bars around a clip that does not cover the whole frame.

    Typical for landscape footage in a vertical project. Give `blur` (0.0625 light, 0.375,
    0.75 or 1 heavy) for a blurred copy of the clip, or `color` as hex for a flat colour. Give
    neither to go back to black. Applies to clips on the main video track.
    """
    target = Project.open(project)
    return _saved(target, target.set_background(segment, blur, color))


@edit_tool
def fade_segment(project: str, segment: str, fade_in: float = 0.0, fade_out: float = 0.0,
                 include_audio: bool = True) -> dict:
    """Fade a clip, image or text in from and out to transparent. Lengths are in seconds.

    On the main video track this is a fade from or to black. The clip's own sound fades with it
    unless include_audio is false. On an audio clip only the sound fades. This is the way to
    soften a cut when no library transition is available. Zeros remove the fade.
    """
    target = Project.open(project)
    return _saved(target, target.fade(segment, fade_in, fade_out, audio=include_audio))


@edit_tool
def set_keyframes(project: str, segment: str,
                  property: Literal["x", "y", "scale", "rotation", "opacity", "volume"],
                  keyframes: list[Keyframe]) -> dict:
    """Animate one property of a segment over time; CapCut moves smoothly between the points.

    Each keyframe is a time counted from the segment's own start and the value at that time.
    Examples: a slow zoom on an image is scale 1 at 0s and 1.2 at its end; a slide in from the
    left is x -1.5 at 0s and 0 at 0.5s. Values use the same units as set_transform (and
    set_volume for volume). This replaces any earlier keyframes of that property; an empty list
    removes them.
    """
    target = Project.open(project)
    seg = target.set_keyframes(segment, property, [(k.time, k.value) for k in keyframes])
    return _saved(target, seg)


@edit_tool
def set_mask(project: str, segment: str,
             shape: Literal["circle", "rectangle", "linear", "mirror", "heart", "star"],
             x: float = 0.0, y: float = 0.0, size: float = 0.5, width: float | None = None,
             rotation: float = 0.0, feather: float = 0.0, invert: bool = False,
             round_corner: float = 0.0) -> dict:
    """Show only part of a video or image clip, through a shape.

    "circle", "rectangle", "heart" and "star" keep what is inside the shape; "linear" keeps one
    side of a line through the centre; "mirror" keeps a band. x and y place the shape's centre
    inside the clip, from -1 to 1 with 0,0 at the clip's centre. `size` is the shape's height as
    a share of the clip's height (0.5 is half); `width`, for the rectangle only, is its share
    of the clip's width. `rotation` is in degrees, `feather` softens the edge (0 to 100),
    `round_corner` rounds a rectangle (0 to 100), invert=true keeps the outside instead.
    Typical use: a round picture-in-picture, by putting a circle on an overlay clip. A clip has
    one mask; a new one replaces it. Remove with clear_segment what=["mask"].
    """
    target = Project.open(project)
    seg = library.set_mask(target, segment, shape, x, y, size, width, rotation, feather, invert,
                           round_corner)
    return _saved(target, seg)


@edit_tool
def set_chroma_key(project: str, segment: str, color: str = "#00FF00", intensity: float = 20.0,
                   shadow: float = 0.0, edge_smooth: float = 0.0, spill: float = 0.0) -> dict:
    """Make one colour of a clip transparent, as for green-screen footage.

    `color` is the colour to remove, as hex; look at a frame with extract_frames to pick the
    actual shade of the background. `intensity` (0 to 100) is how much around that colour goes;
    raise it if green remains, lower it if the subject starts to vanish. `edge_smooth` softens
    the cut-out's edge and `spill` removes colour cast on it. Use it on an overlay clip (video
    track 1 or higher) so the picture under it shows through. Remove with clear_segment
    what=["chroma"].
    """
    target = Project.open(project)
    return _saved(target, library.set_chroma_key(target, segment, color, intensity, shadow,
                                                 edge_smooth, spill))


@edit_tool
def clear_segment(project: str, segment: str,
                  what: list[Literal["transition", "animations", "keyframes", "fade", "crop",
                                     "background", "blend", "mask", "chroma", "audio_effects"]]) -> dict:
    """Remove settings from a segment: transition, animations, keyframes, fade, crop, background,
    blend mode, mask, chroma key, audio effects."""
    target = Project.open(project)
    result = library.clear(target, segment, list(what))
    return _saved(target, result["segment"], cleared=result["cleared"])


# ------------------------------------------------------------------------- library

LibraryKind = Literal["transition", "animation", "effect", "filter", "font", "blend", "audio_effect",
                      "sticker", "adjust"]


@mcp.tool(annotations=READ_ONLY)
def list_library(kind: LibraryKind | None = None, search: str = "",
                 source: Literal["all", "projects", "catalogue"] = "all",
                 project: str | None = None) -> dict:
    """Find CapCut library items that can be applied, and their exact names.

    Two sources are searched. `from_your_projects` lists items the user has already used in a
    CapCut project; these are copied exactly and always work. `catalogue` is a built-in list of
    thousands of transitions, filters, animations, fonts and voice effects by name; it is too
    large to list, so give `search` with part of a name. Most catalogue names are Chinese.
    Common English words are translated automatically (search="dissolve" also finds 叠化), and
    several words can be tried at once, separated by commas: search="glitch, flash, 闪".

    Apply a transition with add_transition, an animation with add_animation, an effect with
    add_effect, a filter with add_filter, a font through the `font` parameter of add_text, a
    blend mode with set_blend_mode and a voice or sound effect with set_audio_effect. Video
    effects, stickers, music and stock clips exist only in `from_your_projects`; effects are
    applied with add_effect and the rest with copy_segment.
    """
    return library.listing(kind, project, search, source)


@edit_tool
def add_transition(project: str, segment: str, name: str, duration: float | None = None,
                   from_project: str | None = None) -> dict:
    """Put a transition between a clip and the clip right after it on the same track.

    `segment` is the first of the two clips. `name` comes from list_library. `duration` is the
    transition's length in seconds (default: the item's own). It cannot be longer than either
    clip. Give from_project to take the user's own copy from one particular project. For a
    transition on every cut, send them together with batch.
    """
    target = Project.open(project)
    return _saved(target, library.add_transition(target, segment, name, duration, from_project))


@edit_tool
def add_animation(project: str, segment: str, name: str, duration: float | None = None,
                  type: Literal["in", "out", "loop", "combo"] | None = None,
                  from_project: str | None = None) -> dict:
    """Give a clip, image or text an animation from list_library.

    "in" plays at the segment's start and "out" at its end, for `duration` seconds. "loop"
    (text only) repeats between them. "combo" (clips only) is one animation covering the whole
    clip and replaces any entrance and exit. Animations for text and for clips are separate
    sets. Give `type` when the same name exists as more than one kind.
    """
    target = Project.open(project)
    return _saved(target, library.add_animation(target, segment, name, duration, type, from_project))


@edit_tool
def add_effect(project: str, name: str, start: float, duration: float, track: int = 0,
               settings: dict[str, float] | None = None, from_project: str | None = None) -> dict:
    """Lay a video effect over a stretch of the timeline. It affects everything under it.

    Only effects the user has already used in one of their CapCut projects can be applied;
    list_library kind="effect" shows them. `settings` adjusts the effect by setting name, each
    from 0 to 100 (e.g. {"speed": 40}); leave it out to keep the user's values. Effects that
    overlap in time go on different effect tracks.
    """
    target = Project.open(project)
    seg = library.add_effect(target, name, start, duration, track, settings, from_project)
    return _saved(target, seg)


@edit_tool
def add_filter(project: str, name: str, start: float, duration: float, track: int = 0,
               intensity: float | None = None, from_project: str | None = None) -> dict:
    """Lay a colour filter over a stretch of the timeline. It affects everything under it.

    `name` comes from list_library kind="filter". `intensity` runs from 0 to 100 (default: full
    strength, or the strength the user set when the filter is copied from their project).
    Filters that overlap in time go on different filter tracks.
    """
    target = Project.open(project)
    seg = library.add_filter(target, name, start, duration, track, intensity, from_project)
    return _saved(target, seg)


@edit_tool
def set_blend_mode(project: str, segment: str,
                   mode: Literal["normal", "multiply", "screen", "overlay", "soft light",
                                 "hard light", "lighten", "darken", "color dodge", "color burn",
                                 "linear burn"]) -> dict:
    """Choose how an overlay clip mixes with the picture under it.

    "screen" drops black areas (light leaks, sparks, smoke on black), "multiply" drops white
    areas, "overlay" and "soft light" add texture. "normal" removes the blend mode. Use it on
    clips on video track 1 or higher.
    """
    target = Project.open(project)
    return _saved(target, library.set_blend_mode(target, segment, mode))


@edit_tool
def copy_segment(project: str, from_project: str, segment: str, start: float | None = None,
                 duration: float | None = None, track: int = 0, text: str | None = None) -> dict:
    """Copy a segment from another project into this one, with everything attached to it.

    This is how stickers, music from CapCut's library, stock clips and adjustment layers are
    applied, and how a text style designed in CapCut is reused: copy the segment, then set
    `start`, `duration` and, for text, new wording in `text`. `segment` is an id from
    get_timeline or list_library of from_project. Without `start` it lands at the same time it
    had in the source project.
    """
    target = Project.open(project)
    seg = library.copy_segment(target, from_project, segment, start, duration, track, text)
    return _saved(target, seg)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
