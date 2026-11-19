"""A rough preview of a project, rendered with FFmpeg instead of CapCut.

It exists so the assistant (and the user) can check timing, order, framing, motion and the audio
mix without opening CapCut. It is not CapCut's renderer. It draws clips with their crop, flip,
position, scale, rotation and opacity, linear keyframes for position, scale and rotation, fades,
background fills of main-track clips, and text with its position keyframes and opacity; see
NOT_DRAWN for what it leaves out. Text is only an approximation of CapCut's size and font.

Timing: the filter graph runs on a clock that starts where the rendered range starts, and only
segments inside the range are read, so a part of the timeline costs about as much as its length.
Each clip is first put on its own clock (seconds since the segment started), snapped to the
project's frame grid there, then moved by a whole number of frames. A frame at a timeline time
therefore looks the same in a partial render as in a full one.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

from . import schema
from .draft import Project, _uncropped
from .env import CapCutError
from .media import _NO_WINDOW, _tool

NOT_DRAWN = ("transitions", "effects", "filters", "stickers", "animations", "blend modes",
             "masks", "chroma key", "highlighted words in text",
             "opacity keyframes other than fades on video and image clips",
             "text rotation", "text scale keyframes", "text shadows", "volume keyframes")

# CapCut text size -> pixels, as a share of the frame's shorter side. Estimated, not measured.
_TEXT_SCALE = 0.0046
_FONTS = {False: ("C:/Windows/Fonts/arial.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf",
                  "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
          True: ("C:/Windows/Fonts/arialbd.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                 "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")}
_PREROLL = 1.0  # source seconds read before a range that starts inside a clip
_TAIL = 0.5     # source seconds read past the range, so its last frame is chosen as in a full render
_LEAD = 0.3     # frames: a source frame counts from this much before its time (see _Graph._video)
_MAX_COMMAND = 30_000  # Windows refuses command lines over 32,767 characters


def _quote(path: str | Path) -> str:
    """A path as a filter option value: forward slashes, drive colon escaped, single-quoted.

    Two parsers read it. The graph parser drops the quotes, which protect spaces, commas and
    brackets; the option parser then turns \\: into ':'. A quote in the path is closed, escaped
    for both parsers (\\\\\\') and reopened.
    """
    text = str(path).replace("\\", "/").replace(":", "\\:")
    return "'" + text.replace("'", "'\\\\\\''") + "'"


def _font(material: dict, bold: bool) -> str | None:
    custom = material.get("font_path") or ""
    # CapCut's own default (SystemFont/en.ttf) covers few characters, so prefer a system font.
    if custom and "/SystemFont/" not in custom and Path(custom).is_file():
        return custom
    return system_font(bold)


def system_font(bold: bool = False) -> str | None:
    """The first of the usual Windows, macOS and Linux fonts that is installed, or None."""
    return next((f for f in _FONTS[bold] if Path(f).is_file()), None)


def _fades(seg: dict) -> tuple[float, float]:
    """Fade-in and fade-out lengths in seconds, read back from opacity keyframes."""
    name = schema.KEYFRAME_PROPERTIES["opacity"]
    prop = next((k for k in seg.get("common_keyframes") or [] if k.get("property_type") == name), None)
    if not prop:
        return 0.0, 0.0
    points = sorted((f["time_offset"], f["values"][0]) for f in prop.get("keyframe_list", []))
    length = seg["target_timerange"]["duration"]
    lead = points[1][0] / 1e6 if len(points) >= 2 and points[0][1] == 0 else 0.0
    tail = (length - points[-2][0]) / 1e6 if len(points) >= 2 and points[-1][1] == 0 else 0.0
    return lead, tail


def _keyframes(seg: dict) -> dict[str, list[tuple[float, float]]]:
    """Animated properties: tool name (x, y, scale, ...) -> [(seconds into the segment, value)]."""
    names = {kind: name for name, kind in schema.KEYFRAME_PROPERTIES.items()}
    found = {}
    for prop in seg.get("common_keyframes") or []:
        points = sorted((f["time_offset"] / 1e6, float(f["values"][0]))
                        for f in prop.get("keyframe_list") or [] if f.get("values"))
        if prop.get("property_type") in names and points:
            found[names[prop["property_type"]]] = points
    return found


def _ramp(points: list[tuple[float, float]], shift: float = 0.0) -> str:
    """A piecewise-linear expression in t through (time, value) points, CapCut's "Line" curve.

    `shift` is where the segment starts on t's clock. The value holds at the first point before
    it and at the last one after it: a sum of clipped ramps, one per change. The result contains
    commas, so it must sit inside single quotes in the filter graph.
    """
    expr = f"{points[0][1]:.6g}"
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if v1 == v0:
            continue
        if t1 - t0 < 1e-6:
            expr += f"{v1 - v0:+.6g}*gte(t,{t0 + shift:.6f})"
        else:
            expr += f"{v1 - v0:+.6g}*clip((t{-(t0 + shift):+.6f})/{t1 - t0:.6f},0,1)"
    return f"({expr})"


def _value(keys: dict, name: str, static: float, shift: float = 0.0) -> str:
    """A property as an expression: its keyframes when it has them, else the static value."""
    return _ramp(keys[name], shift) if name in keys else f"({static:.6g})"


def _atempo(speed: float) -> list[str]:
    """atempo only accepts 0.5 to 2 per step, so larger changes are chained."""
    steps = []
    while speed > 2.0:
        steps.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5:
        steps.append("atempo=0.5")
        speed /= 0.5
    steps.append(f"atempo={speed:.6f}")
    return steps


def render_preview(project: Project, out: Path, height: int = 640, start: float | None = None,
                   end: float | None = None, include_text: bool = True) -> dict:
    total = project.duration_us() / 1e6
    if total <= 0:
        raise CapCutError(f"'{project.name}' has nothing on its timeline yet.")
    canvas = project.content["canvas_config"]
    height = max(144, min(int(height), 1080)) // 2 * 2
    width = max(2, round(canvas["width"] * height / canvas["height"] / 2) * 2)
    rate = f"{project.fps:.6g}"  # one spelling for every filter, so all share one frame grid
    fps = float(rate)
    # The range starts on a frame of the full render, so the two show the same moments.
    first = round(max(start or 0.0, 0.0) * fps)
    w0 = first / fps
    w1 = min(end, total) if end is not None else total
    span = w1 - w0
    if span <= 0:
        raise CapCutError("The preview range is empty.")
    frames = max(1, math.ceil(span * fps - 1e-6))  # output frames: those starting before w1

    def slot(seconds: float) -> int:
        """The output frame a timeline time falls on (it may lie outside the range)."""
        return round(seconds * fps) - first

    def shown(lo: int, hi: int) -> str:
        # Half a frame of margin either side, so rounding never moves a cut by a frame.
        return f"between(t,{(lo - 0.5) / fps:.6f},{(hi - 0.5) / fps:.6f})"

    workdir = Path(tempfile.mkdtemp(prefix="capcut-mcp-render-"))
    try:
        state = _Graph(project, workdir, width, height, rate, fps, frames, slot, shown)
        state.graph.append(f"color=c=black:s={width}x{height}:r={rate}:d={span:.6f}[base]")
        state.pictures()
        if include_text:
            state.texts()
        state.sounds(w0, w1)
        return _run(state, out, span, width, height)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


class _Graph:
    """The inputs and filter graph of one render, built segment by segment."""

    def __init__(self, project, workdir, width, height, rate, fps, frames, slot, shown):
        self.project, self.workdir = project, workdir
        self.width, self.height, self.rate, self.fps, self.frames = width, height, rate, fps, frames
        self.slot, self.shown = slot, shown
        self.inputs: list[str] = []
        self.graph: list[str] = []
        self.count = 0  # inputs so far; input n is [n:v] / [n:a]
        self.last = "base"
        self.voices: list[str] = []
        self.skipped: list[str] = []

    def _window(self, seg: dict) -> tuple[int, int] | None:
        """The segment's first and end frame on the output clock, or None if it is not drawn."""
        lo, hi = self.slot(self.project._start(seg) / 1e6), self.slot(self.project._end(seg) / 1e6)
        return (lo, hi) if hi > max(lo, 0) and lo < self.frames else None

    # ---- picture: every video track, main track first so overlays land on top
    def pictures(self) -> None:
        project = self.project
        for index, track in enumerate(project.tracks("video")):
            for seg in sorted(track["segments"], key=project._start):
                window = self._window(seg)
                if not window:
                    continue
                material = project._main_material(track, seg)
                path = (material or {}).get("path", "")
                if not material or not Path(path).is_file():
                    self.skipped.append(path or seg["id"][:8])
                    continue
                canvas = project._helper(seg, "canvases") if index == 0 else None
                self._video(seg, material, window, canvas)

    def _video(self, seg: dict, material: dict, window: tuple[int, int], canvas: dict | None) -> None:
        fps, width, height, n = self.fps, self.width, self.height, self.count
        lo, hi = window
        head = max(-lo, 0) / fps                      # segment time already past at the range start
        until = (min(hi, self.frames) - lo) / fps     # segment time where drawing stops
        length = seg["target_timerange"]["duration"] / 1e6
        speed = float(seg.get("speed") or 1.0)
        if material.get("type") == "photo":
            self.inputs += ["-loop", "1", "-framerate", self.rate, "-t", f"{until - head + 2 / fps:.6f}",
                            "-i", material["path"]]
            clock = f"PTS*TB+{head:.6f}"
        else:
            source = seg.get("source_timerange") or {"start": 0, "duration": round(length * speed * 1e6)}
            # Start a little before the range so the frame showing at its first moment is decoded.
            skip = max(head * speed - _PREROLL, 0.0)
            take = min(source["duration"] / 1e6, until * speed + _TAIL) - skip
            self.inputs += ["-ss", f"{source['start'] / 1e6 + skip:.6f}", "-t", f"{take:.6f}",
                            "-i", material["path"]]
            clock = f"(PTS*TB+{skip:.6f})/{speed:.6f}"
        self.count += 1

        # Segment clock first: frames get their time since the segment started, in microseconds.
        # A frame counts from _LEAD of a frame before its time and fps rounds up, so a source frame
        # that lands on a frame of the grid (the usual case) cannot slip to the next one by a
        # rounding error. start_time drops what lies before the range but keeps the frame showing
        # at its start, and pads the first frame back to the segment start if the file begins late.
        steps = ["settb=AVTB", f"setpts=({clock}-{_LEAD / fps:.6f})/TB",
                 f"fps=fps={self.rate}:round=up:start_time={head - 0.5 / fps:.6f}"]
        if not _uncropped(material):
            crop = material["crop"]
            left, top = crop["upper_left_x"], crop["upper_left_y"]
            steps.append(f"crop=iw*{crop['lower_right_x'] - left:.4f}:ih*{crop['lower_right_y'] - top:.4f}"
                         f":iw*{left:.4f}:ih*{top:.4f}")
        clip = seg.get("clip") or {}
        if clip.get("flip", {}).get("horizontal"):
            steps.append("hflip")
        if clip.get("flip", {}).get("vertical"):
            steps.append("vflip")
        keys = _keyframes(seg)
        scale = float(clip.get("scale", {}).get("x", 1.0))
        # Animated scale: draw at the largest size (up to 2x the frame) and shrink per frame below.
        base = min(max(v for _, v in keys["scale"]), 2.0) if "scale" in keys else scale
        steps += [f"scale=w={max(2, round(width * base))}:h={max(2, round(height * base))}"
                  ":force_original_aspect_ratio=decrease", "setsar=1"]

        look = ["format=rgba"]
        rotation = float(clip.get("rotation", 0.0))
        if "rotation" in keys or rotation:
            # Clear the corners to transparent: c=none leaves the previous frame's pixels there.
            look.append(f"rotate=a='{_value(keys, 'rotation', rotation)}*PI/180'"
                        ":ow='hypot(iw,ih)':oh=ow:c=black@0")
        opacity = float(clip.get("alpha", 1.0))
        if opacity < 1:
            look.append(f"colorchannelmixer=aa={opacity:.3f}")
        lead, tail = _fades(seg)
        if lead:
            look.append(f"fade=t=in:st=0:d={lead:.6f}:alpha=1")
        if tail:
            look.append(f"fade=t=out:st={length - tail:.6f}:d={tail:.6f}:alpha=1")
        if "scale" in keys:
            # After rotate, which needs a fixed frame size; overlay takes each frame's own size.
            size = f"{_ramp(keys['scale'])}/{base:.6g}"
            look.append(f"scale=w='max(2,iw*{size})':h='max(2,ih*{size})':eval=frame")
        # Onto the output clock by whole frames (the time base is one frame after fps).
        look.append(f"setpts=PTS{lo:+d}")

        shown = self.shown(lo, hi)
        fill = self._background(canvas, shown)
        if fill:
            graph_input = f"[{n}:v]{','.join(steps)},split[c{n}][k{n}]"
            self.graph += [graph_input, f"[k{n}]{fill},setpts=PTS{lo:+d}[b{n}]",
                           f"[{self.last}][b{n}]overlay=x=0:y=0:enable='{shown}':eof_action=pass[u{n}]"]
            self.graph.append(f"[c{n}]{','.join(look)}[c{n}x]")
            self.last, picture = f"u{n}", f"c{n}x"
        else:
            self.graph.append(f"[{n}:v]{','.join(steps + look)}[c{n}]")
            picture = f"c{n}"
        shift = lo / fps
        x = _value(keys, "x", float(clip.get("transform", {}).get("x", 0.0)), shift)
        y = _value(keys, "y", float(clip.get("transform", {}).get("y", 0.0)), shift)
        self.graph.append(f"[{self.last}][{picture}]overlay=x='(W-w)/2+{x}*W/2':y='(H-h)/2-{y}*H/2'"
                          f":enable='{shown}':eof_action=pass[o{n}]")
        self.last = f"o{n}"

    def _background(self, canvas: dict | None, shown: str) -> str | None:
        """Fill behind a main-track clip: a flat colour drawn now, or the blur chain to add."""
        if not canvas:
            return None
        if canvas.get("type") == "canvas_color" and canvas.get("color"):
            colour = "0x" + canvas["color"].lstrip("#")
            self.graph.append(f"[{self.last}]drawbox=x=0:y=0:w=iw:h=ih:color={colour}:t=fill"
                              f":enable='{shown}'[f{self.count}]")
            self.last = f"f{self.count}"
            return None
        strength = float(canvas.get("blur") or 0.0)
        if canvas.get("type") != "canvas_blur" or strength <= 0:
            return None
        # A copy of the clip covering the frame. CapCut's presets run from 0.0625 (light) to 1.
        sigma = min(self.width, self.height) * (0.01 + 0.07 * min(strength, 1.0))
        return (f"scale={self.width}:{self.height}:force_original_aspect_ratio=increase,"
                f"crop={self.width}:{self.height},gblur=sigma={sigma:.2f}")

    # ---- text
    def texts(self) -> None:
        project, width, height = self.project, self.width, self.height
        number = 0
        for track in project.tracks("text"):
            for seg in sorted(track["segments"], key=project._start):
                window = self._window(seg)
                material = project._main_material(track, seg)
                if not window or not material:
                    continue
                try:
                    content = json.loads(material.get("content") or "{}")
                except ValueError:
                    continue
                style = (content.get("styles") or [{}])[0]
                wording = content.get("text", "")
                if not wording.strip():
                    continue
                clip = seg.get("clip") or {}
                size = float(style.get("size") or material.get("font_size") or 15.0)
                pixels = max(8, round(size * min(width, height) * _TEXT_SCALE
                                      * float(clip.get("scale", {}).get("x", 1.0))))
                per_line = max(4, int(width * float(material.get("line_max_width") or 0.82)
                                      / (pixels * 0.55)))
                wrapped = "\n".join(
                    "\n".join(textwrap.wrap(line, per_line) or [""]) for line in wording.split("\n"))
                text_file = self.workdir / f"text{number}.txt"
                text_file.write_text(wrapped, encoding="utf-8")
                number += 1

                keys = _keyframes(seg)
                shift = window[0] / self.fps
                x = _value(keys, "x", float(clip.get("transform", {}).get("x", 0.0)), shift)
                y = _value(keys, "y", float(clip.get("transform", {}).get("y", 0.0)), shift)
                rgb = style.get("fill", {}).get("content", {}).get("solid", {}).get("color", [1, 1, 1])
                colour = "0x" + "".join(f"{round(c * 255):02X}" for c in rgb)
                # expansion=none: the words are drawn as written; '%' would otherwise start a function.
                options = [f"textfile={_quote(text_file)}", "expansion=none", f"fontsize={pixels}",
                           "text_align=C", f"x='(w-text_w)/2+{x}*w/2'", f"y='(h-text_h)/2-{y}*h/2'"]
                if "opacity" in keys:  # keyframes replace the static opacity; alpha also fades the box
                    options += [f"fontcolor={colour}", f"alpha='clip({_ramp(keys['opacity'], shift)},0,1)'"]
                else:
                    options.append(f"fontcolor={colour}@{float(clip.get('alpha', 1.0)):.2f}")
                font = _font(material, bool(style.get("bold")))
                if font:
                    options.append(f"fontfile={_quote(font)}")
                strokes = style.get("strokes") or []
                if strokes:
                    stroke_rgb = strokes[0].get("content", {}).get("solid", {}).get("color", [0, 0, 0])
                    options += [f"borderw={max(1, round(pixels * float(strokes[0].get('width', 0.08)) * 0.6))}",
                                "bordercolor=0x" + "".join(f"{round(c * 255):02X}" for c in stroke_rgb)]
                if material.get("background_style") and material.get("background_color"):
                    options += ["box=1", f"boxborderw={max(2, pixels // 4)}",
                                f"boxcolor=0x{material['background_color'].lstrip('#')}"
                                f"@{float(material.get('background_alpha', 1.0)):.2f}"]
                options.append(f"enable='{self.shown(*window)}'")
                self.graph.append(f"[{self.last}]drawtext={':'.join(options)}[t{number}]")
                self.last = f"t{number}"

    # ---- sound: audio tracks plus the sound of video clips
    def sounds(self, w0: float, w1: float) -> None:
        project = self.project
        for track in project.content["tracks"]:
            if track.get("type") not in ("video", "audio"):
                continue
            for seg in track.get("segments", []):
                material = project._main_material(track, seg)
                if not material or not seg.get("source_timerange") or float(seg.get("volume", 1.0)) <= 0:
                    continue
                if track["type"] == "video" and (material.get("type") == "photo" or not material.get("has_audio")):
                    continue
                begin, finish = project._start(seg) / 1e6, project._end(seg) / 1e6
                if finish <= w0 + 1e-6 or begin >= w1 - 1e-6:
                    continue
                path = material.get("path", "")
                if not Path(path).is_file():
                    if path not in self.skipped:
                        self.skipped.append(path)
                    continue
                speed = float(seg.get("speed") or 1.0)
                head = max(w0 - begin, 0.0)  # segment time already past at the range start
                source = seg["source_timerange"]
                take = min(source["duration"] / 1e6 - head * speed,
                           (min(finish, w1) - begin - head) * speed + 0.1)
                if take <= 0.001:
                    continue
                self.inputs += ["-ss", f"{source['start'] / 1e6 + head * speed:.6f}",
                                "-t", f"{take:.6f}", "-i", path]
                length = finish - begin
                steps = ["aresample=44100", "aformat=channel_layouts=stereo",
                         *_atempo(speed), f"volume={float(seg.get('volume', 1.0)):.3f}"]
                # Fades are timed on the segment's clock: start it at `head` when the range cuts in.
                steps.append(f"asetpts=(N/SR+{head:.6f})/TB")
                fade = project._helper(seg, "audio_fades") or {}
                fade_in = fade.get("fade_in_duration", 0) / 1e6
                fade_out = fade.get("fade_out_duration", 0) / 1e6
                if fade_in > head:
                    steps.append(f"afade=t=in:st=0:d={fade_in:.6f}")
                if fade_out:
                    steps.append(f"afade=t=out:st={max(length - fade_out, 0):.6f}:d={fade_out:.6f}")
                steps += ["asetpts=N/SR/TB", f"adelay={round(max(begin - w0, 0) * 44100)}S:all=1"]
                self.graph.append(f"[{self.count}:a]{','.join(steps)}[a{self.count}]")
                self.voices.append(f"[a{self.count}]")
                self.count += 1
        if len(self.voices) > 1:
            self.graph.append(f"{''.join(self.voices)}amix=inputs={len(self.voices)}"
                              ":normalize=0:dropout_transition=0[aout]")
        elif self.voices:
            self.graph.append(f"{self.voices[0]}anull[aout]")


def _run(state: _Graph, out: Path, span: float, width: int, height: int) -> dict:
    script = state.workdir / "graph.txt"
    script.write_text(";\n".join(state.graph), encoding="utf-8")
    out.parent.mkdir(parents=True, exist_ok=True)
    tail_args = ["-map", f"[{state.last}]", *(["-map", "[aout]"] if state.voices else []),
                 "-t", f"{span:.6f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "27",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)]
    limit = min(3600.0, max(300.0, span * 60))

    result = None
    for graph_option in ("-/filter_complex", "-filter_complex_script"):  # FFmpeg 7+, then older
        # -nostdin and no stdin: the MCP protocol arrives on this process's stdin, and FFmpeg
        # would otherwise read it looking for keypresses.
        command = [_tool("ffmpeg"), "-nostdin", "-v", "error", "-y", *state.inputs,
                   graph_option, str(script), *tail_args]
        if len(subprocess.list2cmdline(command)) > _MAX_COMMAND:
            raise CapCutError(
                f"This part of the timeline uses too many media files for one FFmpeg command "
                f"({state.inputs.count('-i')} inputs). Render a shorter part with start and end.")
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", creationflags=_NO_WINDOW,
                                    timeout=limit)
        except subprocess.TimeoutExpired:  # subprocess.run has already killed FFmpeg
            raise CapCutError(
                f"The preview took longer than {limit / 60:.0f} minutes and was stopped. Render a "
                "shorter part with start and end, or use a smaller height.") from None
        if result.returncode == 0 and out.is_file():
            break
    if result is None or result.returncode != 0 or not out.is_file():
        raise CapCutError(f"FFmpeg could not render the preview: {(result.stderr if result else '').strip()[-500:]}")

    report = {"file": str(out), "size": f"{width}x{height}", "duration": round(span, 3),
              "megabytes": round(out.stat().st_size / 1e6, 2),
              "note": "Approximate preview. Not drawn: " + ", ".join(NOT_DRAWN) + "."}
    if state.skipped:
        report["missing_media_skipped"] = state.skipped
    return report
