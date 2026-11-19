"""Builders for the pieces of a CapCut draft: materials, segments and tracks.

The shapes in templates.json were taken from drafts written by CapCut 8.3.0 and blanked
(tools/extract_templates.py rebuilds them). The styled part of a text item, the JSON string in
its "content" field, is assembled by hand in apply_text_style.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from importlib import resources
from pathlib import Path

from .env import CapCutError
from .media import MediaInfo

_TEMPLATES: dict = json.loads(
    resources.files(__package__).joinpath("templates.json").read_text(encoding="utf-8")
)

PHOTO_MATERIAL_DURATION_US = 10_800_000_000  # CapCut treats a still as a 3 hour source
TEXT_RENDER_INDEX = 14000

# Bits of a text material's check_flag that switch optional decorations on.
_FLAG_TEXT_BASE, _FLAG_STROKE, _FLAG_BACKGROUND, _FLAG_SHADOW = 7, 8, 16, 32


def template(name: str) -> dict:
    return copy.deepcopy(_TEMPLATES[name])


def new_id() -> str:
    """A GUID in the style CapCut writes: upper case, third group lower case."""
    parts = str(uuid.uuid4()).upper().split("-")
    parts[2] = parts[2].lower()
    return "-".join(parts)


def capcut_path(path: Path | str) -> str:
    return str(path).replace("\\", "/")


def _with_id(name: str) -> dict:
    item = template(name)
    item["id"] = new_id()
    return item


# --------------------------------------------------------------------------- tracks

OVERLAY_TRACK_FLAG = 2  # CapCut sets this on every video track above the main one


def track(kind: str, overlay: bool = False) -> dict:
    item = _with_id("track")
    item["type"] = kind
    if overlay:
        item["flag"] = OVERLAY_TRACK_FLAG
    return item


# ------------------------------------------------------------------ media materials

def video_material(info: MediaInfo) -> dict:
    item = _with_id("video_material")
    item.update(
        local_material_id=str(uuid.uuid4()),
        material_name=info.path.name,
        path=capcut_path(info.path),
        width=info.width,
        height=info.height,
    )
    if info.kind == "photo":
        item.update(type="photo", duration=PHOTO_MATERIAL_DURATION_US, has_audio=False)
    else:
        item.update(type="video", duration=info.duration_us, has_audio=info.has_audio)
    return item


def audio_material(info: MediaInfo) -> dict:
    item = _with_id("audio_material")
    item.update(
        local_material_id=str(uuid.uuid4()),
        name=info.path.stem,
        path=capcut_path(info.path),
        duration=info.duration_us,
    )
    if info.kind == "video":  # the sound of a video file used as an audio clip
        item.update(type="video_original_sound", category_name="")
    return item


def bin_entry(info: MediaInfo, local_material_id: str, now_us: int) -> dict:
    """An entry for the project's media bin (stored in draft_meta_info.json)."""
    item = template("meta_material")
    duration = 5_000_000 if info.kind == "photo" else info.duration_us // 1000 * 1000
    item.update(
        id=local_material_id,
        create_time=now_us // 1_000_000,
        import_time=now_us // 1_000_000,
        import_time_ms=now_us,
        duration=duration,
        extra_info=info.path.name,
        file_Path=capcut_path(info.path),
        width=info.width,
        height=info.height,
        metetype="photo" if info.kind == "photo" else "video",
    )
    item["roughcut_time_range"] = {"duration": duration, "start": 0}
    return item


# ------------------------------------------------------------------------ segments

def _timerange(start: int, duration: int) -> dict:
    return {"duration": int(duration), "start": int(start)}


def video_segment(materials: dict, material: dict, start: int, duration: int,
                  source_start: int, source_duration: int, speed: float, volume: float) -> dict:
    """A clip on a video track. Its helper materials are added to `materials`."""
    seg = _with_id("video_segment")
    helpers = [
        ("speeds", "speed"), ("placeholder_infos", "placeholder_info"), ("canvases", "canvas"),
        ("sound_channel_mappings", "sound_channel_mapping"), ("material_colors", "material_color"),
        ("loudnesses", "loudness"), ("vocal_separations", "vocal_separation"),
    ]
    seg["extra_material_refs"] = _add_helpers(materials, helpers, speed)
    seg.update(material_id=material["id"], speed=speed, volume=volume)
    seg["source_timerange"] = _timerange(source_start, source_duration)
    seg["target_timerange"] = _timerange(start, duration)
    return seg


def audio_segment(materials: dict, material: dict, start: int, duration: int,
                  source_start: int, source_duration: int, speed: float, volume: float) -> dict:
    seg = _with_id("audio_segment")
    helpers = [
        ("speeds", "speed"), ("placeholder_infos", "placeholder_info"), ("beats", "beats"),
        ("sound_channel_mappings", "sound_channel_mapping"),
        ("vocal_separations", "vocal_separation"),
    ]
    seg["extra_material_refs"] = _add_helpers(materials, helpers, speed)
    seg.update(material_id=material["id"], speed=speed, volume=volume)
    seg["source_timerange"] = _timerange(source_start, source_duration)
    seg["target_timerange"] = _timerange(start, duration)
    return seg


def _add_helpers(materials: dict, helpers: list[tuple[str, str]], speed: float) -> list[str]:
    refs = []
    for group, name in helpers:
        item = _with_id(name)
        if name == "speed":
            item["speed"] = speed
        materials.setdefault(group, []).append(item)
        refs.append(item["id"])
    return refs


def audio_fade(fade_in: int, fade_out: int) -> dict:
    return {"fade_in_duration": int(fade_in), "fade_out_duration": int(fade_out),
            "fade_type": 0, "id": new_id(), "type": "audio_fade"}


def item_segment(material_id: str, start: int, duration: int, render_index: int) -> dict:
    """A segment on an effect or filter track: a time span that points at one material."""
    seg = _with_id("video_segment")
    seg.update(
        material_id=material_id,
        extra_material_refs=[],
        source_timerange=None,
        target_timerange=_timerange(start, duration),
        clip=None,
        hdr_settings=None,
        uniform_scale=None,
        enable_adjust=False,
        enable_lut=False,
        render_index=render_index,
    )
    return seg


# CapCut's blend modes are named in Chinese in its files; these are the usual English names.
BLEND_NAMES = {
    "multiply": "正片叠底", "color dodge": "颜色减淡", "color burn": "颜色加深",
    "linear burn": "线性加深", "soft light": "柔光", "hard light": "强光", "screen": "滤色",
    "overlay": "叠加", "lighten": "变亮", "darken": "变暗",
}


# Keyframe property names CapCut uses, by the name the tools expose.
KEYFRAME_PROPERTIES = {
    "x": "KFTypePositionX",
    "y": "KFTypePositionY",
    "scale": "KFTypeScaleX",  # with uniform scaling on, X drives both axes
    "rotation": "KFTypeRotation",
    "opacity": "KFTypeAlpha",
    "volume": "KFTypeVolume",
}


def keyframes(property_type: str, points: list[tuple[int, float]]) -> dict:
    """One animated property: (microseconds from the segment's start, value) pairs."""
    return {
        "id": new_id(),
        "keyframe_list": [{
            "curveType": "Line",
            "graphID": "",
            "id": new_id(),
            "left_control": {"x": 0.0, "y": 0.0},
            "right_control": {"x": 0.0, "y": 0.0},
            "string_value": "",
            "time_offset": int(offset),
            "values": [float(value)],
        } for offset, value in sorted(points)],
        "material_id": "",
        "property_type": property_type,
    }


def text_segment(materials: dict, material: dict, start: int, duration: int) -> dict:
    seg = _with_id("video_segment")
    animation = {"animations": [], "id": new_id(), "multi_language_current": "none",
                 "type": "sticker_animation"}
    materials.setdefault("material_animations", []).append(animation)
    seg.update(
        material_id=material["id"],
        extra_material_refs=[animation["id"]],
        source_timerange=None,
        target_timerange=_timerange(start, duration),
        enable_adjust=False,
        enable_lut=False,
        hdr_settings=None,
        render_index=TEXT_RENDER_INDEX,
    )
    return seg


# ---------------------------------------------------------------------------- text

def text_material(kind: str = "text") -> dict:
    """An unstyled text material. Call apply_text_style to give it content."""
    item = _with_id("text_material")
    item["type"] = kind
    item["content"] = json.dumps({
        "styles": [{
            "fill": {"alpha": 1.0, "content": {"render_type": "solid",
                                               "solid": {"alpha": 1.0, "color": [1.0, 1.0, 1.0]}}},
            "range": [0, 0],
            "size": 15.0,
        }],
        "text": "",
    }, ensure_ascii=False, separators=(",", ":"))
    return item


def parse_color(value: str) -> tuple[str, list[float]]:
    """'#RRGGBB' (or 'RRGGBB') -> ('#RRGGBB', [r, g, b] as 0..1 floats)."""
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    try:
        if len(text) != 6:
            raise ValueError
        rgb = [int(text[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    except ValueError:
        raise CapCutError(f"'{value}' is not a colour. Use hex like #FFD400.") from None
    return "#" + text.upper(), rgb


def text_of(material: dict) -> str:
    try:
        return json.loads(material.get("content") or "{}").get("text", "")
    except ValueError:
        return ""


_ALIGNMENTS = {"left": 0, "center": 1, "right": 2}
DEFAULT_HIGHLIGHT = "#FFD400"

# Mask shapes by the name the tools use. CapCut's own files name them in Chinese.
MASK_SHAPES = {"linear": "线性", "mirror": "镜面", "circle": "圆形", "rectangle": "矩形",
               "heart": "爱心", "star": "星形"}


def mask_shape_of(material: dict) -> str:
    english = {chinese: name for name, chinese in MASK_SHAPES.items()}
    return english.get(material.get("name", ""), material.get("name", "") or material.get("resource_type", ""))


def _utf16(text: str) -> int:
    """Length in UTF-16 units, which is how CapCut counts characters (an emoji counts as two)."""
    return len(text.encode("utf-16-le")) // 2


def _solid(rgb: list[float]) -> dict:
    return {"alpha": 1.0, "content": {"render_type": "solid", "solid": {"alpha": 1.0, "color": rgb}}}


def _span(style: dict) -> int:
    begin, end = (style.get("range") or [0, 0])[:2]
    return end - begin


def _hex(rgb: list[float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(float(c) * 255))):02X}" for c in rgb[:3])


def _fill_hex(style: dict) -> str:
    return _hex(style.get("fill", {}).get("content", {}).get("solid", {}).get("color", [1.0, 1.0, 1.0]))


def _highlight_runs(text: str, phrases: list[str]) -> list[tuple[int, int]]:
    """Where the given words or phrases occur in `text`, as merged (start, end) character spans.

    Matching ignores case. A phrase of plain letters and digits only matches whole words, so
    "art" does not light up inside "start"; anything else (Chinese, punctuation) matches anywhere.
    """
    spans = []
    for phrase in phrases:
        phrase = phrase.strip()
        if not phrase:
            continue
        pattern = re.escape(phrase)
        if re.fullmatch(r"[A-Za-z0-9' -]+", phrase):
            pattern = rf"(?<![A-Za-z0-9]){pattern}(?![A-Za-z0-9])"
        spans += [match.span() for match in re.finditer(pattern, text, re.IGNORECASE)]
    merged: list[tuple[int, int]] = []
    for begin, end in sorted(spans):
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((begin, end))
    return merged


def text_style_of(material: dict) -> dict:
    """A text material's look, in the terms the text tools take."""
    try:
        content = json.loads(material.get("content") or "{}")
    except ValueError:
        content = {}
    styles = [s for s in content.get("styles") or [] if isinstance(s, dict)] or [{}]
    base = max(styles, key=_span)
    out: dict = {"size": base.get("size", material.get("font_size")), "color": _fill_hex(base)}
    out.update({key: True for key in ("bold", "italic", "underline") if base.get(key)})
    align = {number: name for name, number in _ALIGNMENTS.items()}.get(material.get("alignment"))
    if align:
        out["align"] = align
    strokes = base.get("strokes") or []
    if strokes:
        out["stroke_color"] = _hex(strokes[0].get("content", {}).get("solid", {}).get("color", [0, 0, 0]))
        out["stroke_width"] = strokes[0].get("width")
    if material.get("background_style") and material.get("background_color"):
        out["background_color"] = material["background_color"]
        out["background_opacity"] = material.get("background_alpha")
    if base.get("shadows"):
        out["shadow"] = True
    font = base.get("font") or {}
    if font.get("id"):
        out["font"] = f"library font {font['id']}"
    elif font.get("path") or material.get("font_path"):
        out["font_path"] = font.get("path") or material.get("font_path")
    if material.get("letter_spacing"):
        out["letter_spacing"] = round(float(material["letter_spacing"]) / 0.05, 2)
    if material.get("line_max_width") is not None:
        out["wrap_width"] = material["line_max_width"]
    if len(styles) > 1:
        raw = content.get("text", "").encode("utf-16-le")
        out["other_styles"] = [
            {"text": raw[2 * s["range"][0]:2 * s["range"][1]].decode("utf-16-le", errors="replace"),
             "color": _fill_hex(s)}
            for s in styles if s is not base and len(s.get("range") or []) >= 2 and _fill_hex(s) != _fill_hex(base)]
    return out


def apply_text_style(material: dict, *, text: str | None = None, size: float | None = None,
                     color: str | None = None, bold: bool | None = None,
                     italic: bool | None = None, underline: bool | None = None,
                     align: str | None = None, stroke_color: str | None = None,
                     stroke_width: float | None = None, background_color: str | None = None,
                     background_opacity: float | None = None, shadow: bool | None = None,
                     font_path: str | None = None, letter_spacing: float | None = None,
                     line_spacing: float | None = None, wrap_width: float | None = None,
                     font_ref: dict | None = None, highlight: list[str] | None = None,
                     highlight_color: str | None = None) -> int:
    """Change the given properties of a text material and leave the others alone.

    `highlight` colours the given words or phrases differently from the rest; an empty list
    removes that again. Returns how many stretches of text were highlighted.

    A text can hold several styles, each covering a range of characters (highlighted words, or
    styling done in CapCut). A change of look is applied to every range. New wording or a new
    highlight makes the old ranges meaningless, so the text then goes back to one style, taken
    from the one that covered the most characters.
    """
    try:
        content = json.loads(material.get("content") or "{}")
    except ValueError:
        content = {}
    old_text = content.get("text", "")
    styles = [s for s in content.get("styles") or [] if isinstance(s, dict)] or [{}]
    if text is None:
        text = old_text
    if not text.strip():
        raise CapCutError("Text cannot be empty.")
    if highlight_color is not None and highlight is None:
        raise CapCutError("highlight_color needs `highlight`: the words or phrases to colour.")
    rebuild = text != old_text or highlight is not None or len(styles) == 1
    targets = [max(styles, key=_span)] if rebuild else styles

    if size is not None:
        if size <= 0:
            raise CapCutError("Text size must be greater than 0.")
        material["font_size"] = float(size)
    fill = None
    if color is not None:
        hex_color, rgb = parse_color(color)
        fill = _solid(rgb)
        material["text_color"] = hex_color
    if underline is not None:
        material["underline"] = bool(underline)
    if italic is not None:
        material["italic_degree"] = 10 if italic else 0

    if align is not None:
        if align not in _ALIGNMENTS:
            raise CapCutError("align must be 'left', 'center' or 'right'.")
        material["alignment"] = _ALIGNMENTS[align]

    flag = int(material.get("check_flag") or _FLAG_TEXT_BASE)

    strokes: list | None = None  # None: leave alone, []: remove, otherwise the outline to set
    if stroke_color is not None or stroke_width is not None:
        width = float(stroke_width) if stroke_width is not None else float(material.get("border_width") or 0.08)
        if width <= 0:
            strokes = []
            material["border_color"] = ""
            flag &= ~_FLAG_STROKE
        else:
            hex_color, rgb = parse_color(stroke_color or material.get("border_color") or "#000000")
            strokes = [{**_solid(rgb), "width": width}]
            material.update(border_color=hex_color, border_width=width, border_alpha=1.0)
            flag |= _FLAG_STROKE

    for style in targets:
        if size is not None:
            style["size"] = float(size)
        style.setdefault("size", float(material.get("font_size") or 15.0))
        if fill is not None:
            style["fill"] = copy.deepcopy(fill)
        style.setdefault("fill", _solid([1.0, 1.0, 1.0]))
        for key, value in (("bold", bold), ("italic", italic), ("underline", underline)):
            if value is True:
                style[key] = True
            elif value is False:
                style.pop(key, None)
        if strokes == []:
            style.pop("strokes", None)
        elif strokes:
            style["strokes"] = copy.deepcopy(strokes)

    if background_color is not None or background_opacity is not None:
        opacity = float(background_opacity) if background_opacity is not None else float(material.get("background_alpha") or 1.0)
        if background_color == "" or opacity <= 0:
            material.update(background_style=0, background_color="")
            flag &= ~_FLAG_BACKGROUND
        else:
            hex_color, _ = parse_color(background_color or material.get("background_color") or "#000000")
            material.update(background_style=1, background_color=hex_color,
                            background_alpha=min(opacity, 1.0), background_round_radius=0.2)
            flag |= _FLAG_BACKGROUND

    if shadow is not None:
        material["has_shadow"] = bool(shadow)
        if shadow:
            material["shadow_color"] = "#000000"
            flag |= _FLAG_SHADOW
        else:
            flag &= ~_FLAG_SHADOW
        for style in targets:
            if shadow:
                style["shadows"] = [{"alpha": 0.9, "angle": -45.0, "diffuse": 0.0833, "distance": 5.0,
                                     "content": {"render_type": "solid",
                                                 "solid": {"alpha": 1.0, "color": [0.0, 0.0, 0.0]}}}]
            else:
                style.pop("shadows", None)

    if font_ref is not None and font_path:
        raise CapCutError("Give a font name or a font file, not both.")
    font: dict | None = None  # None: leave alone, {}: back to the default, otherwise the font to set
    if font_ref is not None:
        # A font from CapCut's library, named by id. CapCut downloads it when the project opens.
        font = dict(font_ref)
        material["font_path"] = ""
    if font_path is not None:
        if font_path == "":
            font = {}
            material["font_path"] = ""
        else:
            file = Path(font_path).expanduser()
            if not file.is_file():
                raise CapCutError(f"Font file not found: {file}")
            font = {"id": "", "path": capcut_path(file.resolve())}
            material["font_path"] = capcut_path(file.resolve())
    for style in targets:
        if font == {}:
            style.pop("font", None)
        elif font:
            style["font"] = dict(font)

    # CapCut's panel shows spacing as whole numbers; the file stores them scaled by 0.05,
    # and line spacing sits on top of a 0.02 base.
    if letter_spacing is not None:
        material["letter_spacing"] = float(letter_spacing) * 0.05
    if line_spacing is not None:
        material["line_spacing"] = 0.02 + float(line_spacing) * 0.05
    if wrap_width is not None:
        if not 0.05 <= wrap_width <= 1:
            raise CapCutError("wrap_width is a fraction of the frame width, between 0.05 and 1.")
        material["line_max_width"] = float(wrap_width)

    highlighted = 0
    if rebuild:
        base = targets[0]
        base["range"] = [0, _utf16(text)]
        styles = [base]
        runs = _highlight_runs(text, highlight) if highlight else []
        if runs:
            _, rgb = parse_color(highlight_color or DEFAULT_HIGHLIGHT)
            styles, cursor = [], 0
            for begin, end in [*runs, (len(text), len(text))]:
                for first, last, lit in ((cursor, begin, False), (begin, end, True)):
                    if last > first:
                        piece = copy.deepcopy(base)
                        piece["range"] = [_utf16(text[:first]), _utf16(text[:last])]
                        if lit:
                            piece["fill"] = _solid(rgb)
                        styles.append(piece)
                cursor = end
            highlighted = len(runs)

    material["check_flag"] = flag
    content["styles"] = styles
    content["text"] = text
    material["content"] = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    return highlighted
