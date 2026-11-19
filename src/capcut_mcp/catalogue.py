"""A catalogue of CapCut library items, by name, from the open-source `pycapcut` package.

CapCut identifies every transition, effect, filter, animation and font by ids it does not
publish. `pycapcut` ships lists of those ids. An item written into a project with its ids and no
local file path is fetched by CapCut itself when the project is opened.

This module is the only place that touches `pycapcut`. It is imported lazily, so the server
still starts, and everything else still works, if the package is missing. The catalogue is a
snapshot (its own note dates it August 2025): some entries may no longer exist in CapCut, and
items added to CapCut since then are not in it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from . import schema
from .env import CapCutError

# kind -> ((enum name, group), ...). A group says what the item is for.
_SOURCES = {
    "transition": (("TransitionType", "transition"),),
    "effect": (("VideoSceneEffectType", "scene"), ("VideoCharacterEffectType", "face")),
    "filter": (("FilterType", "filter"),),
    "animation": (("IntroType", "clip-in"), ("OutroType", "clip-out"),
                  ("GroupAnimationType", "clip-combo"), ("TextIntro", "text-in"),
                  ("TextOutro", "text-out"), ("TextLoopAnim", "text-loop")),
    "font": (("FontType", "font"),),
    "blend": (("MixModeType", "blend"),),
    "audio_effect": (("ToneEffectType", "voice"), ("AudioSceneEffectType", "scene")),
}
KINDS = tuple(_SOURCES)

BLEND_NAMES = schema.BLEND_NAMES

# English search words -> the Chinese words CapCut's item names use for them. Most catalogue
# names are Chinese, so a search for "dissolve" would otherwise find nothing. This is a plain
# glossary written for this server; the names it helps to find stay in the pycapcut package.
GLOSSARY: dict[str, tuple[str, ...]] = {
    "dissolve": ("叠化", "溶解"), "crossfade": ("叠化",), "fade": ("渐显", "渐隐", "褪色", "淡"),
    "fade in": ("渐显",), "fade out": ("渐隐",), "blur": ("模糊",), "flash": ("闪光", "闪白", "闪黑", "爆闪", "闪现"),
    "white flash": ("闪白",), "black flash": ("闪黑",), "flicker": ("闪烁", "闪动", "频闪"),
    "strobe": ("频闪", "爆闪"), "glitch": ("故障", "乱码", "错误"), "push": ("推",), "pull": ("拉",),
    "slide": ("滑动", "滑入", "滑出", "上滑", "下滑"), "zoom": ("缩放", "放大", "缩小", "变焦"),
    "zoom in": ("放大",), "zoom out": ("缩小", "拉远"), "rotate": ("旋转",), "spin": ("旋转", "旋入"),
    "flip": ("翻转", "翻页"), "page turn": ("翻页",), "shake": ("抖动", "震动", "摇晃", "晃动"),
    "wobble": ("摇摆", "晃动"), "bounce": ("弹跳", "弹动", "弹入", "弹出"), "pop": ("弹出", "弹入"),
    "typewriter": ("打字机", "打字", "逐字"), "wave": ("波浪", "波动"), "ripple": ("波纹", "涟漪", "水波"),
    "wipe": ("擦除", "擦拭"), "split": ("分割", "分屏"), "switch": ("切换",), "stretch": ("拉伸",),
    "distort": ("扭曲", "变形"), "warp": ("扭曲",), "swirl": ("漩涡",), "vortex": ("漩涡",),
    "retro": ("复古",), "vintage": ("复古", "胶片"), "film": ("胶片", "胶卷", "电影"), "vhs": ("录像带", "录像"),
    "tv": ("电视",), "negative": ("负片",), "black and white": ("黑白", "单色"), "monochrome": ("黑白", "单色"),
    "glow": ("发光", "辉光", "荧光", "泛光"), "neon": ("霓虹",), "light leak": ("漏光",), "flare": ("光晕", "光斑"),
    "bokeh": ("光斑",), "sparkle": ("闪闪", "星光", "金粉"), "star": ("星星", "星光"), "heart": ("爱心",),
    "fire": ("火焰", "燃烧", "火光"), "smoke": ("烟雾",), "snow": ("雪花",), "rain": ("雨",),
    "lightning": ("闪电", "电光"), "firework": ("烟花",), "particle": ("粒子",), "magic": ("魔法",),
    "rainbow": ("彩虹",), "prism": ("棱镜",), "chromatic": ("色差", "色散"), "rgb": ("色差", "色散"),
    "pixel": ("像素",), "mosaic": ("马赛克",), "noise": ("噪点",), "grain": ("噪点",),
    "scan": ("扫描", "扫光"), "shutter": ("快门",), "camera": ("相机", "镜头", "运镜"), "lens": ("镜头", "透镜"),
    "fisheye": ("鱼眼",), "focus": ("聚焦",), "mirror": ("镜像",), "clone": ("分身", "克隆", "复制"),
    "ghost": ("幻影", "重影", "叠影", "幽灵"), "trail": ("拖尾", "拖影"), "shatter": ("碎片", "破碎"),
    "tear": ("撕裂", "撕纸"), "paper": ("纸张", "纸质", "撕纸"), "glass": ("玻璃",), "cube": ("立方体",),
    "3d": ("立体",), "collage": ("拼贴", "拼图"), "border": ("边框", "图框"), "frame": ("边框", "图框"),
    "outline": ("描边", "轮廓"), "doodle": ("涂鸦",), "comic": ("漫画",), "anime": ("动漫",),
    "open": ("开幕", "展开"), "close": ("闭幕",), "curtain": ("开幕", "闭幕"), "blinds": ("百叶窗",),
    "scroll": ("滚动",), "float": ("漂浮",), "fall": ("飘落", "下降"), "rise": ("上升",),
    "left": ("向左", "左移"), "right": ("向右",), "up": ("向上", "上滑"), "down": ("向下", "下滑"),
    "explode": ("爆炸", "炸开"), "shockwave": ("冲击波", "震波"), "pulse": ("脉冲", "心动"),
    "countdown": ("倒计时",), "freeze": ("定格",), "slideshow": ("幻灯片",), "polaroid": ("拍立得",),
    "dream": ("梦境", "迷幻"), "horror": ("恐怖", "暗夜"), "cyber": ("赛博",), "christmas": ("圣诞",),
    "smooth": ("丝滑",), "fast": ("快速",), "random": ("随机",), "gradient": ("渐变",),
    "laser": ("激光",), "thermal": ("热成像",), "sharpen": ("锐化",), "exposure": ("曝光",),
    "butterfly": ("蝴蝶",), "bubble": ("泡泡", "气泡"), "tunnel": ("隧道",), "grid": ("网格",),
    "gold": ("金色", "金粉"), "red": ("红色",), "blue": ("蓝色",), "pink": ("粉色",), "white": ("白色", "闪白"),
    "black": ("黑色", "闪黑"), "robot": ("机器人",), "monster": ("怪物",), "chipmunk": ("花栗鼠",),
}


@dataclass(frozen=True)
class Entry:
    kind: str
    group: str
    name: str
    pro: bool
    member: Any  # the pycapcut enum member

    def summary(self) -> dict:
        out: dict = {"kind": self.kind, "name": self.name, "source": "catalogue"}
        if self.pro:
            out["pro"] = True
        if self.kind == "animation":
            use, _, when = self.group.partition("-")
            out["for"] = "text" if use == "text" else "clips"
            out["type"] = when
        if self.kind == "audio_effect":
            out["type"] = self.group
        if self.kind in ("effect", "audio_effect"):
            params = [p.name.removeprefix("effects_adjust_") for p in self.member.value.params]
            if params:
                out["settings"] = params
        return out


def _key(text: str) -> str:
    return text.casefold().replace(" ", "").replace("_", "").replace("-", "")


@lru_cache(maxsize=1)
def _entries() -> tuple[Entry, ...]:
    try:
        from pycapcut import metadata
    except Exception as error:  # a missing or broken optional package must not stop the server
        raise CapCutError(
            "The item catalogue is not available because the 'pycapcut' package could not be "
            f"loaded ({type(error).__name__}). Run 'uv sync' in the server's folder. Items already "
            "used in the user's own projects still work."
        ) from None
    found = []
    for kind, sources in _SOURCES.items():
        for enum_name, group in sources:
            for member in getattr(metadata, enum_name):
                meta = member.value
                name = (getattr(meta, "name", None) or getattr(meta, "title", "") or member.name).strip()
                found.append(Entry(kind, group, name, bool(getattr(meta, "is_vip", False)), member))
    return tuple(found)


def available() -> bool:
    try:
        _entries()
    except CapCutError:
        return False
    return True


def counts() -> dict[str, int]:
    totals: dict[str, int] = {}
    for entry in _entries():
        totals[entry.kind] = totals.get(entry.kind, 0) + 1
    return totals


def search_terms(query: str) -> list[str]:
    """The words a search looks for: the query's own, plus the Chinese words for English ones.

    Several alternatives can be given at once, separated by commas, e.g. "dissolve, 叠化, fade".
    """
    own = _typed_terms(query)
    translated = [_key(term) for part in own for term in GLOSSARY.get(part.lower(), ())]
    return list(dict.fromkeys(term for term in [*map(_key, own), *translated] if term))


def _typed_terms(query: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,;|/，、]", query) if part.strip()]


def search(kind: str | None = None, query: str = "", limit: int = 60,
           groups: tuple[str, ...] | None = None) -> list[Entry]:
    """Entries whose name contains any of the search words (case and spacing ignored).

    Best matches first: a name that is exactly what was typed, then an exact translated name,
    then names starting with a word; free items before Pro.
    """
    terms = search_terms(query)
    typed = {_key(part) for part in _typed_terms(query)}
    pool = [e for e in _entries()
            if (kind is None or e.kind == kind) and (groups is None or e.group in groups)]
    if not terms:
        return sorted(pool, key=lambda e: (e.pro, len(e.name)))[:limit]
    scored = []
    for entry in pool:
        name = _key(entry.name)
        hits = [term for term in terms if term in name]
        if hits:
            scored.append((name not in typed, name not in hits,
                           not any(name.startswith(term) for term in hits),
                           entry.pro, len(entry.name), entry))
    scored.sort(key=lambda item: item[:5])
    return [item[5] for item in scored[:limit]]


def find(kind: str, name: str, groups: tuple[str, ...] | None = None) -> Entry | None:
    """The entry with exactly this name, or the only one containing it. None if not present."""
    wanted = _key(BLEND_NAMES.get(name.strip().lower(), name) if kind == "blend" else name)
    pool = [e for e in _entries() if e.kind == kind and (groups is None or e.group in groups)]
    exact = [e for e in pool if _key(e.name) == wanted or _key(e.member.name) == wanted]
    if exact:
        return exact[0]
    partial = [e for e in pool if wanted and wanted in _key(e.name)]
    names = sorted({e.name for e in partial})
    if len(names) == 1:  # the same item can be listed in more than one group
        return partial[0]
    # An English word for which CapCut has an item of exactly that meaning: "dissolve" is 叠化.
    for term in GLOSSARY.get(name.strip().lower(), ()):
        translated = [e for e in pool if _key(e.name) == _key(term)]
        if translated:
            return translated[0]
    if names:
        raise CapCutError(f"'{name}' matches several {kind}s: {', '.join(names[:15])}. Use the full name.")
    return None


# ------------------------------------------------------------------ building materials

def _stamp(material: dict) -> dict:
    material["id"] = schema.new_id()
    return material


def transition(entry: Entry, duration_us: int | None) -> dict:
    from pycapcut.video_segment import Transition
    return _stamp(Transition(entry.member, duration_us).export_json())


def effect(entry: Entry, params: dict[str, float] | None) -> dict:
    """A video effect. `params` maps the names shown by search to values from 0 to 100."""
    from pycapcut.video_segment import VideoEffect
    names = [p.name.removeprefix("effects_adjust_") for p in entry.member.value.params]
    values: list[float | None] = [None] * len(names)
    for key, value in (params or {}).items():
        short = key.removeprefix("effects_adjust_")
        if short not in names:
            raise CapCutError(
                f"'{entry.name}' has no setting '{key}'. Its settings: {', '.join(names) or 'none'}.")
        if not 0 <= value <= 100:
            raise CapCutError("Effect settings run from 0 to 100.")
        values[names.index(short)] = float(value)
    # 2 = the effect covers everything under it for its time span, as an effect track does
    return _stamp(VideoEffect(entry.member, values, apply_target_type=2).export_json())


def filter_(entry: Entry, intensity: float) -> dict:
    from pycapcut.video_segment import Filter
    if not 0 <= intensity <= 100:
        raise CapCutError("Filter intensity runs from 0 to 100.")
    # 0 is what CapCut itself keeps for a filter on a filter track; it changed 2 back to 0.
    return _stamp(Filter(entry.member.value, intensity / 100.0, apply_target_type=0).export_json())


def blend(entry: Entry) -> dict:
    from pycapcut.video_segment import MixMode
    return _stamp(MixMode(entry.member.value).export_json())


def audio_effect(entry: Entry, params: dict[str, float] | None) -> dict:
    """A voice changer or sound-scene effect for an audio clip. `params` run from 0 to 100."""
    from pycapcut.audio_segment import AudioEffect
    names = [p.name.removeprefix("effects_adjust_") for p in entry.member.value.params]
    values: list[float | None] = [None] * len(names)
    for key, value in (params or {}).items():
        short = key.removeprefix("effects_adjust_")
        if short not in names:
            raise CapCutError(
                f"'{entry.name}' has no setting '{key}'. Its settings: {', '.join(names) or 'none'}.")
        if not 0 <= value <= 100:
            raise CapCutError("Effect settings run from 0 to 100.")
        values[names.index(short)] = float(value)
    return _stamp(AudioEffect(entry.member, values).export_json())


def mask(shape: str, *, x: float, y: float, size: float, width: float | None, rotation: float,
         feather: float, invert: bool, round_corner: float, media_width: int,
         media_height: int) -> tuple[dict, dict]:
    """A mask in both forms CapCut has used: (the older `masks` entry, the `common_mask` entry).

    They share one id. `size` is the mask's main dimension as a share of the picture's height
    (a circle's diameter, a heart's height, the visible band of a mirror mask).
    """
    try:
        from pycapcut import MaskType
        from pycapcut.video_segment import Mask
    except Exception as error:
        raise CapCutError(
            f"Masks need the 'pycapcut' package, which could not be loaded ({type(error).__name__}). "
            "Run 'uv sync' in the server's folder.") from None
    meta = MaskType[schema.MASK_SHAPES[shape]].value
    if width is None:
        # Keep the shape's own proportions in pixels, whatever the picture's aspect ratio.
        width = size if shape == "rectangle" else (
            size * media_height * meta.default_aspect_ratio / max(media_width, 1))
    built = Mask(meta, x, y, w=width, h=size, ratio=meta.default_aspect_ratio, rot=rotation,
                 inv=invert, feather=feather / 100.0, round_corner=round_corner / 100.0)
    old, new = built.export_json(), built.export_common_json()
    old["id"] = new["id"] = schema.new_id()
    return old, new


def chroma(color_rgba: str, intensity: float, shadow: float, edge_smooth: float, spill: float) -> dict:
    """A chroma key (green screen) setting. The four amounts run from 0 to 100 like CapCut's sliders."""
    try:
        from pycapcut.video_segment import Chroma
    except Exception as error:
        raise CapCutError(
            f"Chroma key needs the 'pycapcut' package, which could not be loaded "
            f"({type(error).__name__}). Run 'uv sync' in the server's folder.") from None
    return _stamp(Chroma(color_rgba, intensity / 100.0, shadow / 100.0, edge_smooth / 100.0,
                         spill / 100.0).export_json())


def animation(entry: Entry, start_us: int, duration_us: int) -> dict:
    from pycapcut.animation import Text_animation, VideoAnimation
    maker = Text_animation if entry.group.startswith("text") else VideoAnimation
    return maker(entry.member, start_us, duration_us).export_json()


def animation_default_duration(entry: Entry) -> int:
    return int(entry.member.value.duration)


def font_reference(entry: Entry) -> dict:
    """What goes in a text style's "font" slot. The path is a placeholder CapCut replaces."""
    return {"id": entry.member.value.resource_id, "path": "D:"}
