"""Applying CapCut library items: transitions, animations, effects, filters, blend modes.

An item can come from two places, tried in this order:

1. **The user's own projects.** Anything they have used in CapCut is copied over exactly as
   CapCut wrote it, including music, stickers and stock clips that no catalogue covers.
2. **The catalogue** (see catalogue.py): thousands of items by name, written with their ids only.
   CapCut fetches the asset when it opens the project.
"""

from __future__ import annotations

import copy
import json

from . import catalogue, schema
from .draft import Project, _content_files, _newest, _uncropped, project_folders
from .env import CapCutError, capcut_user_data

# Track types whose segments are library items in their own right.
ITEM_TRACKS = ("effect", "filter", "sticker", "adjust")
PROJECT_KINDS = ("transition", "animation", *ITEM_TRACKS)
EFFECT_RENDER_INDEX, FILTER_RENDER_INDEX = 10000, 11000

# Where CapCut keeps the reference to a transition or blend mode among a clip's helpers:
# third, after speed and placeholder. Written elsewhere, CapCut moves it there on save.
ATTACHMENT_SLOT = 2

# Video effects written by id alone were not fetched by CapCut 8.3.0 when tested (the two
# effects in the test project came back with an empty path and nothing in the cache, while
# transitions, filters, animations and the font written the same way were all downloaded).
# Until a way that works is found, effects can only be copied from the user's own projects.
CATALOGUE_EFFECTS_ENABLED = False


# ------------------------------------------------------------------- what is available

# Reading every project is the slow part of a library lookup, and several lookups happen in a
# row (one per transition added). A project's items are remembered until its file changes.
_SCAN_CACHE: dict[str, tuple[tuple, list[tuple[dict, tuple]]]] = {}


def _project_items(folder) -> list[tuple[dict, tuple]]:
    """(entry, the key that identifies repeats) for every library item used in one project."""
    try:
        file = _newest(_content_files(folder))
        stat = file.stat()
    except (OSError, CapCutError):
        return []
    stamp = (str(file), stat.st_mtime_ns, stat.st_size)
    cached = _SCAN_CACHE.get(str(folder))
    if cached and cached[0] == stamp:
        return cached[1]
    found: list[tuple[dict, tuple]] = []
    try:
        content = json.loads(file.read_text(encoding="utf-8"))
        materials = content["materials"]
        tracks = content["tracks"]
    except (ValueError, KeyError, OSError, UnicodeDecodeError):
        _SCAN_CACHE[str(folder)] = (stamp, found)
        return found

    for item in materials.get("transitions", []):
        name = item.get("name", "")
        if name:
            found.append(({"kind": "transition", "name": name, "project": folder.name,
                           "duration": round(item.get("duration", 0) / 1e6, 3)},
                          ("transition", name.lower())))

    for holder in materials.get("material_animations", []):
        for entry in holder.get("animations", []) or []:
            name = entry.get("name", "")
            if name:
                found.append(({"kind": "animation", "name": name, "type": _shown_type(entry.get("type", "")),
                               "for": "text" if entry.get("material_type") == "sticker" else "clips",
                               "project": folder.name,
                               "duration": round(entry.get("duration", 0) / 1e6, 3)},
                              ("animation", name.lower(), entry.get("type"), entry.get("material_type"))))

    by_id = {m["id"]: m for group in materials.values() if isinstance(group, list)
             for m in group if isinstance(m, dict) and "id" in m}
    for track in tracks:
        if track.get("type") not in ITEM_TRACKS:
            continue
        for seg in track.get("segments", []):
            material = by_id.get(seg.get("material_id"), {})
            name = material.get("name") or material.get("material_name") or ""
            found.append(({"kind": track["type"], "name": name, "project": folder.name,
                           "segment": seg["id"][:8],
                           "duration": round(seg["target_timerange"]["duration"] / 1e6, 3)},
                          (track["type"], name.lower() or seg["id"])))
    _SCAN_CACHE[str(folder)] = (stamp, found)
    return found


def scan(kind: str | None = None, project: str | None = None) -> list[dict]:
    """Library items found in the user's projects, newest project first, without repeats."""
    seen: set[tuple] = set()
    items: list[dict] = []
    for folder in project_folders():
        if project and folder.name.lower() != project.strip().lower():
            continue
        for entry, key in _project_items(folder):
            if (kind is None or entry["kind"] == kind) and key not in seen:
                seen.add(key)
                items.append(dict(entry))
    return items


def listing(kind: str | None = None, project: str | None = None, search: str = "",
            source: str = "all", limit: int = 60) -> dict:
    """What can be applied: items from the user's projects, and matches in the catalogue."""
    out: dict = {}
    wanted = search.strip().lower()
    if source in ("projects", "all") and (kind is None or kind in PROJECT_KINDS):
        terms = catalogue.search_terms(search)
        mine = [i for i in scan(kind, project)
                if not terms or any(term in catalogue._key(i["name"]) for term in terms)]
        out["from_your_projects"] = mine

    if kind == "effect" and not CATALOGUE_EFFECTS_ENABLED:
        out["catalogue"] = {"available": False, "note": EFFECTS_OFF}
    elif source in ("catalogue", "all") and project is None and (kind is None or kind in catalogue.KINDS):
        if not catalogue.available():
            out["catalogue"] = {"available": False,
                                "note": "The pycapcut package is not installed; run 'uv sync'."}
        elif not wanted:
            totals = catalogue.counts()
            if not CATALOGUE_EFFECTS_ENABLED:
                totals.pop("effect", None)
            out["catalogue"] = {
                "counts": {kind: totals.get(kind, 0)} if kind else totals,
                "note": "Too many to list. Pass `search` with part of a name. Most names are "
                        "Chinese, so try the Chinese word as well as the English one.",
            }
        else:
            matches = [e for e in catalogue.search(kind, search, limit=limit + 200)
                       if CATALOGUE_EFFECTS_ENABLED or e.kind != "effect"][:limit + 1]
            out["catalogue"] = {"matches": [e.summary() for e in matches[:limit]]}
            if len(matches) > limit:
                out["catalogue"]["note"] = f"More than {limit} matches; narrow the search."
    return out


EFFECTS_OFF = ("Effects cannot be taken from the catalogue: CapCut did not load them when "
               "tested. An effect works once the user has used it in any CapCut project; it then "
               "appears under from_your_projects.")


def _shown_type(stored: str) -> str:
    return "combo" if stored == "group" else stored


def _from_projects(kind: str, name: str, from_project: str | None, **match) -> dict | None:
    wanted = name.strip().lower()
    return next((i for i in scan(kind, from_project) if i["name"].lower() == wanted
                 and all(i.get(k) == v for k, v in match.items() if v)), None)


def _not_found(kind: str, name: str, from_project: str | None) -> CapCutError:
    if from_project:
        mine = sorted({i["name"] for i in scan(kind, from_project)})
        return CapCutError(f"No {kind} named '{name}' in project '{from_project}'. "
                           f"It has: {', '.join(mine[:25]) or 'none'}.")
    close = []
    if catalogue.available():
        close = [e.name for e in (catalogue.search(kind, name, limit=12)
                                  or catalogue.search(kind, name[:2], limit=12))]
    hint = f" Similar names: {', '.join(close)}." if close else ""
    return CapCutError(
        f"No {kind} named '{name}' in the user's projects or the catalogue.{hint} "
        "Call list_library with `search` to find the exact name.")


# ----------------------------------------------------------------------- transitions

def add_transition(project: Project, ref: str, name: str, duration: float | None = None,
                   from_project: str | None = None) -> dict:
    """Put a transition between a clip and the clip right after it."""
    track, seg = project.find(ref)
    if track.get("type") != "video":
        raise CapCutError("Transitions go between clips on a video track.")
    following = next((s for s in track["segments"]
                      if abs(project._start(s) - project._end(seg)) <= 1_000_000 / project.fps
                      and s is not seg), None)
    if following is None:
        raise CapCutError(
            f"Segment {seg['id'][:8]} has no clip directly after it on its track; a transition "
            "needs two clips that touch.")
    length = project.us(duration) if duration is not None else None

    mine = _from_projects("transition", name, from_project)
    if mine:
        source = Project.open(mine["project"]).content
        original = next(m for m in source["materials"]["transitions"]
                        if m.get("name", "").lower() == name.strip().lower())
        transition = copy.deepcopy(original)
        transition["id"] = schema.new_id()
        if length is not None:
            transition["duration"] = length
    else:
        entry = None if from_project else catalogue.find("transition", name)
        if entry is None:
            raise _not_found("transition", name, from_project)
        transition = catalogue.transition(entry, length)

    longest = min(seg["target_timerange"]["duration"], following["target_timerange"]["duration"])
    transition["duration"] = max(1, min(int(transition.get("duration", 0)), longest))

    remove_transition(project, seg)
    project.materials.setdefault("transitions", []).append(transition)
    _attach(seg, transition["id"])
    return seg


def _attach(seg: dict, material_id: str) -> None:
    refs = seg.setdefault("extra_material_refs", [])
    refs.insert(min(ATTACHMENT_SLOT, len(refs)), material_id)


def remove_transition(project: Project, seg: dict) -> bool:
    existing = project._helper(seg, "transitions")
    if not existing:
        return False
    seg["extra_material_refs"].remove(existing["id"])
    project.materials["transitions"].remove(existing)
    return True


# ------------------------------------------------------------------------ animations

def add_animation(project: Project, ref: str, name: str, duration: float | None = None,
                  animation_type: str | None = None, from_project: str | None = None) -> dict:
    """Give a clip or text an entrance, exit, looping or combo animation."""
    track, seg = project.find(ref)
    if not isinstance(seg.get("clip"), dict):
        raise CapCutError("Only video, image, text and sticker segments can be animated.")
    on_text = track.get("type") in ("text", "sticker")
    length = seg["target_timerange"]["duration"]

    mine = _from_projects("animation", name, from_project, type=animation_type,
                          **{"for": "text" if on_text else "clips"})
    if mine:
        source = Project.open(mine["project"]).content
        animation = copy.deepcopy(next(
            a for holder in source["materials"].get("material_animations", [])
            for a in holder.get("animations", []) or []
            if a.get("name", "").lower() == name.strip().lower()
            and _shown_type(a.get("type", "")) == mine["type"]
            and (a.get("material_type") == "sticker") == on_text))
        default = int(animation.get("duration", 0))
    else:
        groups = tuple(g for g in (("text-in", "text-out", "text-loop") if on_text
                                   else ("clip-in", "clip-out", "clip-combo"))
                       if animation_type is None or g.endswith("-" + animation_type))
        entry = None if from_project else catalogue.find("animation", name, groups)
        if entry is None:
            raise _not_found("animation", name, from_project)
        default = catalogue.animation_default_duration(entry)
        animation = catalogue.animation(entry, 0, default)

    span = max(1, min(project.us(duration) if duration is not None else default, length))
    kind = animation.get("type")
    holder = project._helper(seg, "material_animations")
    if holder is None:
        holder = {"animations": [], "id": schema.new_id(), "multi_language_current": "none",
                  "type": "sticker_animation"}
        project.materials.setdefault("material_animations", []).append(holder)
        seg.setdefault("extra_material_refs", []).append(holder["id"])

    # One of each type; a combo animation replaces entrance and exit, and the other way round.
    replaced = {kind} | ({"in", "out"} if kind == "group" else {"group"} if kind in ("in", "out") else set())
    holder["animations"] = [a for a in holder.get("animations") or [] if a.get("type") not in replaced]
    if kind == "out":
        animation.update(start=length - span, duration=span)
    elif kind == "group":
        animation.update(start=0, duration=length if duration is None else span)
    else:
        animation.update(start=0, duration=span)
    holder["animations"].append(animation)
    # Entrance, exit and loop share the segment: the loop fills what the other two leave free.
    project._refit_animations(seg)
    return seg


# ------------------------------------------------------------ effects and filters

def _add_item(project: Project, kind: str, name: str, start: float, duration: float, track: int,
              from_project: str | None, build) -> dict:
    """Shared by add_effect and add_filter: reuse the user's own copy, else build from the catalogue."""
    mine = _from_projects(kind, name, from_project)
    if mine:
        return copy_segment(project, mine["project"], mine["segment"], start, duration, track)
    if kind == "effect" and not CATALOGUE_EFFECTS_ENABLED:
        raise CapCutError(f"No effect named '{name}' in the user's projects. {EFFECTS_OFF}")
    entry = None if from_project else catalogue.find(kind, name)
    if entry is None:
        raise _not_found(kind, name, from_project)

    begin, length = project.us(start), project.us(duration)
    if length <= 0:
        raise CapCutError("Duration must be greater than 0.")
    target = project.track(kind, track, create=True)
    project._require_free(target, begin, length)
    group, material, base = build(entry)
    project.materials.setdefault(group, []).append(material)
    seg = schema.item_segment(material["id"], begin, length, base + track)
    project._place(target, seg)
    return seg


def add_effect(project: Project, name: str, start: float, duration: float, track: int = 0,
               params: dict[str, float] | None = None, from_project: str | None = None) -> dict:
    seg = _add_item(project, "effect", name, start, duration, track, from_project,
                    lambda entry: ("video_effects", catalogue.effect(entry, None), EFFECT_RENDER_INDEX))
    if params:
        _apply_settings(project.find_material(seg["material_id"])[1], params)
    return seg


def _apply_settings(effect: dict, params: dict[str, float]) -> None:
    """Set an effect's adjustable values, given as 0 to 100 like CapCut's sliders.

    Works the same on an effect built from the catalogue and on one copied from the user's
    project, because both carry their settings in `adjust_params`.
    """
    slots = {p.get("name", "").removeprefix("effects_adjust_"): p
             for p in effect.get("adjust_params") or []}
    for key, value in params.items():
        slot = slots.get(key.removeprefix("effects_adjust_"))
        if slot is None:
            raise CapCutError(f"'{effect.get('name', '')}' has no setting '{key}'. "
                              f"Its settings: {', '.join(slots) or 'none'}.")
        if not 0 <= value <= 100:
            raise CapCutError("Effect settings run from 0 to 100.")
        low, high = float(slot.get("min_value", 0.0)), float(slot.get("max_value", 1.0))
        slot["value"] = low + (high - low) * float(value) / 100.0


def add_filter(project: Project, name: str, start: float, duration: float, track: int = 0,
               intensity: float | None = None, from_project: str | None = None) -> dict:
    if intensity is not None and not 0 <= intensity <= 100:
        raise CapCutError("Filter intensity runs from 0 to 100.")
    seg = _add_item(project, "filter", name, start, duration, track, from_project,
                    lambda entry: ("effects", catalogue.filter_(entry, 100.0), FILTER_RENDER_INDEX))
    if intensity is not None:
        project.find_material(seg["material_id"])[1]["value"] = intensity / 100.0
    return seg


def _blend_of(project: Project, seg: dict) -> dict | None:
    refs = set(seg.get("extra_material_refs") or [])
    return next((m for m in project.materials.get("effects", [])
                 if m.get("id") in refs and m.get("type") == "mix_mode"), None)


def set_blend_mode(project: Project, ref: str, mode: str | None) -> dict:
    """How a clip mixes with what is under it: multiply, screen, overlay and so on."""
    track, seg = project.find(ref)
    if track.get("type") != "video":
        raise CapCutError("Blend modes apply to video and image clips.")
    existing = _blend_of(project, seg)
    if existing:
        seg["extra_material_refs"].remove(existing["id"])
        project.materials["effects"].remove(existing)
    if mode and mode.strip().lower() != "normal":
        entry = catalogue.find("blend", mode)
        if entry is None:
            raise CapCutError(f"Unknown blend mode '{mode}'. Choose from: normal, "
                              + ", ".join(catalogue.BLEND_NAMES) + ".")
        material = catalogue.blend(entry)
        local = _local_blend_path(material.get("resource_id", ""))
        if local:
            material["path"] = local
        project.materials.setdefault("effects", []).append(material)
        _attach(seg, material["id"])
    return seg


def _local_blend_path(resource_id: str) -> str | None:
    """Blend modes ship with CapCut, so point at the copy on disk when it can be found."""
    user_data = capcut_user_data()
    if not user_data:
        return None
    folder = user_data / "Resources" / "MixMode"
    try:
        listed = json.loads((folder / "MixMode.json").read_text(encoding="utf-8"))["resourceList"]
    except (OSError, ValueError, KeyError):
        return None
    match = next((item for item in listed if item.get("resourceId") == resource_id), None)
    if match and (folder / match.get("path", "")).is_dir():
        return schema.capcut_path(folder / match["path"])
    return None


def font_reference(name: str) -> dict:
    entry = catalogue.find("font", name)
    if entry is None:
        raise _not_found("font", name, None)
    return catalogue.font_reference(entry)


# ------------------------------------------------- masks, chroma key, voice effects

def _picture(project: Project, ref: str, what: str) -> tuple[dict, dict]:
    track, seg = project.find(ref)
    material = project._main_material(track, seg)
    if track.get("type") != "video" or not material:
        raise CapCutError(f"{what} applies to video and image clips.")
    return seg, material


def _detach(project: Project, seg: dict, group: str) -> bool:
    existing = project._helper(seg, group)
    if not existing:
        return False
    project.materials[group].remove(existing)
    if existing["id"] in seg["extra_material_refs"]:
        seg["extra_material_refs"].remove(existing["id"])
    return True


def remove_mask(project: Project, seg: dict) -> bool:
    """A mask can sit in two lists under one id; take it out of both."""
    refs = set(seg.get("extra_material_refs") or [])
    removed: set[str] = set()
    for group in ("common_mask", "masks"):
        items = project.materials.get(group)
        if isinstance(items, list):
            for item in [m for m in items if isinstance(m, dict) and m.get("id") in refs]:
                items.remove(item)
                removed.add(item["id"])
    seg["extra_material_refs"] = [r for r in seg.get("extra_material_refs") or [] if r not in removed]
    return bool(removed)


def set_mask(project: Project, ref: str, shape: str, x: float = 0.0, y: float = 0.0,
             size: float = 0.5, width: float | None = None, rotation: float = 0.0,
             feather: float = 0.0, invert: bool = False, round_corner: float = 0.0) -> dict:
    """Show only part of a clip: a circle, rectangle, heart, star, band or half of the picture."""
    seg, material = _picture(project, ref, "A mask")
    if shape not in schema.MASK_SHAPES:
        raise CapCutError("Unknown mask shape. Choose from: " + ", ".join(schema.MASK_SHAPES) + ".")
    if not 0 < size <= 4:
        raise CapCutError("size is a share of the picture's height: 0.5 is half of it.")
    if width is not None and not 0 < width <= 4:
        raise CapCutError("width is a share of the picture's width: 0.5 is half of it.")
    if (width is not None or round_corner) and shape != "rectangle":
        raise CapCutError("width and round_corner are for the rectangle mask only.")
    if not 0 <= feather <= 100 or not 0 <= round_corner <= 100:
        raise CapCutError("feather and round_corner run from 0 to 100.")
    canvas = project.content["canvas_config"]
    old, new = catalogue.mask(
        shape, x=float(x), y=float(y), size=float(size), width=width, rotation=float(rotation),
        feather=float(feather), invert=bool(invert), round_corner=float(round_corner),
        media_width=int(material.get("width") or canvas.get("width") or 1920),
        media_height=int(material.get("height") or canvas.get("height") or 1080))
    remove_mask(project, seg)
    # CapCut 8.3 keeps masks in `common_mask`. Older builds used `masks`; it is written only
    # where the project already has that list, so nothing unknown is added to a newer project.
    project.materials.setdefault("common_mask", []).append(new)
    if isinstance(project.materials.get("masks"), list):
        project.materials["masks"].append(old)
    seg.setdefault("extra_material_refs", []).append(new["id"])
    return seg


def set_chroma_key(project: Project, ref: str, color: str, intensity: float = 20.0,
                   shadow: float = 0.0, edge_smooth: float = 0.0, spill: float = 0.0) -> dict:
    """Make one colour of a clip transparent (green screen)."""
    seg, _ = _picture(project, ref, "Chroma key")
    for name, value in (("intensity", intensity), ("shadow", shadow),
                        ("edge_smooth", edge_smooth), ("spill", spill)):
        if not 0 <= value <= 100:
            raise CapCutError(f"{name} runs from 0 to 100.")
    hex_color, _ = schema.parse_color(color)
    material = catalogue.chroma(hex_color + "FF", float(intensity), float(shadow),
                                float(edge_smooth), float(spill))
    _detach(project, seg, "chromas")
    project.materials.setdefault("chromas", []).append(material)
    seg.setdefault("extra_material_refs", []).append(material["id"])
    return seg


def _audio_effects_of(project: Project, seg: dict) -> list[dict]:
    refs = set(seg.get("extra_material_refs") or [])
    return [m for m in project.materials.get("audio_effects", []) if m.get("id") in refs]


def _is_scene_effect(material: dict) -> bool:
    return material.get("category_id") == "sound_effect"


def set_audio_effect(project: Project, ref: str, name: str | None,
                     settings: dict[str, float] | None = None) -> dict:
    """Put a voice changer or sound-scene effect on an audio clip; name=None removes them."""
    track, seg = project.find(ref)
    if track.get("type") != "audio":
        raise CapCutError(
            "Voice and sound effects go on clips of an audio track. For the sound of a video "
            "clip, add the same file with add_audio and set the video clip's volume to 0.")
    existing = _audio_effects_of(project, seg)
    if name is None:
        doomed = existing
    else:
        entry = catalogue.find("audio_effect", name)
        if entry is None:
            raise _not_found("audio_effect", name, None)
        material = catalogue.audio_effect(entry, settings)
        # A clip holds one voice effect and one scene effect; a new one replaces its own kind.
        doomed = [m for m in existing if _is_scene_effect(m) == _is_scene_effect(material)]
    for old in doomed:
        project.materials["audio_effects"].remove(old)
        seg["extra_material_refs"].remove(old["id"])
    if name is not None:
        project.materials.setdefault("audio_effects", []).append(material)
        seg.setdefault("extra_material_refs", []).append(material["id"])
    return seg


# ------------------------------------------------------------- copying between projects

def copy_segment(project: Project, source_project: str, ref: str, start: float | None = None,
                 duration: float | None = None, track: int = 0, text: str | None = None) -> dict:
    """Copy a segment, with everything attached to it, from another project into this one."""
    source = Project.open(source_project)
    source_track, original = source.find(ref)
    kind = source_track.get("type")

    existing = len(project.tracks(kind))
    target = project.track(kind, track, create=True)
    if len(project.tracks(kind)) > existing and kind != "video":
        target["flag"] = source_track.get("flag", 0)
        target["attribute"] = source_track.get("attribute", 0)

    seg = copy.deepcopy(original)
    seg["id"] = schema.new_id()
    seg["group_id"] = ""
    renamed: dict[str, str] = {}
    for old in [original.get("material_id"), *(original.get("extra_material_refs") or [])]:
        found = source.find_material(old) if old else None
        if not found:
            continue
        group, item = found
        clone = copy.deepcopy(item)
        clone["id"] = renamed[old] = schema.new_id()
        project.materials.setdefault(group, []).append(clone)
    seg["material_id"] = renamed.get(original.get("material_id"), original.get("material_id"))
    seg["extra_material_refs"] = [renamed[r] for r in original.get("extra_material_refs") or []
                                  if r in renamed]
    for prop in seg.get("common_keyframes") or []:
        prop["id"] = schema.new_id()
        for frame in prop.get("keyframe_list", []):
            frame["id"] = schema.new_id()
    seg["keyframe_refs"] = []

    if duration is not None:
        if seg.get("source_timerange") and kind in ("video", "audio"):
            raise CapCutError("A copied clip keeps its length; use trim_segment afterwards.")
        old_length = seg["target_timerange"]["duration"]
        seg["target_timerange"]["duration"] = project.us(duration)
        if seg["target_timerange"]["duration"] <= 0:
            raise CapCutError("Duration must be greater than 0.")
        project._rebase_keyframes(seg, 0, seg["target_timerange"]["duration"])
        project._refit_animations(seg, old_length)
    if start is not None:
        seg["target_timerange"]["start"] = project.us(start)
    project._require_free(target, seg["target_timerange"]["start"], seg["target_timerange"]["duration"])

    if text is not None:
        material = project.find_material(seg["material_id"])
        if kind != "text" or not material:
            raise CapCutError("Only a copied text segment can be given new wording.")
        schema.apply_text_style(material[1], text=text)

    project._place(target, seg)
    return seg


# ------------------------------------------------------------------------- removing

CLEARABLE = ("transition", "animations", "keyframes", "fade", "crop", "background", "blend",
             "mask", "chroma", "audio_effects")


def clear(project: Project, ref: str, what: list[str]) -> dict:
    """Remove settings from a segment and report which were actually present."""
    track, seg = project.find(ref)
    unknown = [w for w in what if w not in CLEARABLE]
    if unknown:
        raise CapCutError(f"Unknown: {', '.join(unknown)}. Choose from: {', '.join(CLEARABLE)}.")
    cleared = []
    if "transition" in what and remove_transition(project, seg):
        cleared.append("transition")
    if "animations" in what:
        holder = project._helper(seg, "material_animations")
        if holder and holder.get("animations"):
            holder["animations"] = []
            cleared.append("animations")
    if "keyframes" in what and seg.get("common_keyframes"):
        seg["common_keyframes"] = []
        cleared.append("keyframes")
    if "fade" in what:
        fade = project._helper(seg, "audio_fades")
        if fade:
            seg["extra_material_refs"].remove(fade["id"])
            project.materials["audio_fades"].remove(fade)
            cleared.append("fade")
        opacity = schema.KEYFRAME_PROPERTIES["opacity"]
        if any(k.get("property_type") == opacity for k in seg.get("common_keyframes") or []):
            seg["common_keyframes"] = [k for k in seg["common_keyframes"]
                                       if k.get("property_type") != opacity]
            if "fade" not in cleared:
                cleared.append("fade")
    if "crop" in what and track.get("type") == "video":
        material = project._main_material(track, seg)
        if material and not _uncropped(material):
            project.crop(ref)  # zero on every side
            cleared.append("crop")
    if "background" in what and track.get("type") == "video":
        canvas = project._helper(seg, "canvases")
        if canvas and (canvas.get("type") != "canvas_color" or canvas.get("color")):
            project.set_background(ref)
            cleared.append("background")
    if "blend" in what and _blend_of(project, seg):
        set_blend_mode(project, ref, None)
        cleared.append("blend")
    if "mask" in what and remove_mask(project, seg):
        cleared.append("mask")
    if "chroma" in what and _detach(project, seg, "chromas"):
        cleared.append("chroma")
    if "audio_effects" in what:
        found = _audio_effects_of(project, seg)
        for old in found:
            project.materials["audio_effects"].remove(old)
            seg["extra_material_refs"].remove(old["id"])
        if found:
            cleared.append("audio_effects")
    return {"segment": seg, "cleared": cleared}
