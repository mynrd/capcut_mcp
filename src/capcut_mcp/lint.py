"""Looking a project over for things that are broken or likely to disappoint.

The assistant cannot open CapCut, so this is how it catches mistakes before the user does: a
transition with no clip after it, text that sits off the frame, music that runs past the picture.
"""

from __future__ import annotations

from pathlib import Path

from .draft import Project

MAX_LISTED = 40
LONG_CAPTION_CHARS = 90
FAST_READING = 25.0  # characters per second; above this a caption is hard to read in time


def _seconds(us: int | float) -> float:
    return round(us / 1e6, 3)


def check(project: Project) -> dict:
    """Problems (will misbehave), warnings (probably not intended) and notes (worth knowing)."""
    problems: list[str] = []
    warnings: list[str] = []
    notes: list[str] = []
    content = project.content
    frame = round(1_000_000 / project.fps)
    known = {item["id"] for group in project.materials.values() if isinstance(group, list)
             for item in group if isinstance(item, dict) and "id" in item}

    picture_end = max((project._end(s) for t in project.tracks("video") for s in t["segments"]), default=0)
    counters: dict[str, int] = {}
    for track in content["tracks"]:
        kind = track.get("type", "")
        index = counters.get(kind, 0)
        counters[kind] = index + 1
        where = f"{kind} track {index}"
        segments = sorted(track.get("segments", []), key=project._start)
        cursor = 0
        for position, seg in enumerate(segments):
            label = project._label(seg)
            length = seg["target_timerange"]["duration"]
            if length <= 0:
                problems.append(f"{label} on {where} has no length.")
            if project._start(seg) < cursor:
                problems.append(f"{label} on {where} overlaps the segment before it.")
            cursor = max(cursor, project._end(seg))
            if seg.get("material_id") not in known:
                problems.append(f"{label} on {where} points at content that is not in the project.")
            lost = [ref for ref in seg.get("extra_material_refs") or [] if ref not in known]
            if lost:
                problems.append(f"{label} on {where} has {len(lost)} broken internal reference(s).")

            material = project._main_material(track, seg)
            if kind in ("video", "audio") and material and material.get("type") != "photo":
                source = seg.get("source_timerange")
                total = int(material.get("duration") or 0)
                if source and total and source["start"] + source["duration"] > total + frame:
                    problems.append(
                        f"{label} on {where} plays to {_seconds(source['start'] + source['duration'])}s "
                        f"of a file that is only {_seconds(total)}s long.")

            following = segments[position + 1] if position + 1 < len(segments) else None
            transition = project._helper(seg, "transitions")
            if transition:
                if following is None or abs(project._start(following) - project._end(seg)) > frame:
                    warnings.append(
                        f"The transition '{transition.get('name', '')}' on {label} has no clip "
                        "directly after it, so it will not show. Remove it with clear_segment or "
                        "close the gap.")
                elif transition.get("duration", 0) > min(length, following["target_timerange"]["duration"]):
                    warnings.append(f"The transition on {label} is longer than one of its two clips.")

            holder = project._helper(seg, "material_animations")
            for animation in (holder or {}).get("animations") or []:
                if animation.get("start", 0) + animation.get("duration", 0) > length + frame:
                    warnings.append(
                        f"The '{animation.get('name', '')}' animation on {label} runs past the "
                        "end of the segment.")
            for prop in seg.get("common_keyframes") or []:
                if any(f["time_offset"] > length + frame for f in prop.get("keyframe_list", [])):
                    warnings.append(f"{label} on {where} has keyframes after its own end.")

            clip = seg.get("clip") if isinstance(seg.get("clip"), dict) else None
            if kind == "text" and clip:
                x, y = clip.get("transform", {}).get("x", 0.0), clip.get("transform", {}).get("y", 0.0)
                if abs(x) > 1 or abs(y) > 1:
                    warnings.append(f"Text {label} is placed outside the frame (x={x:.2f}, y={y:.2f}).")
                if picture_end and project._start(seg) >= picture_end:
                    warnings.append(f"Text {label} appears after the last picture has ended.")
                wording = _text(project, track, seg)
                seconds = length / 1e6
                longest = max((len(line) for line in wording.split("\n")), default=0)
                if longest > LONG_CAPTION_CHARS:
                    notes.append(f"Text {label} has a line of {longest} characters; it may wrap or look small.")
                elif seconds > 0 and len(wording.replace("\n", "")) / seconds > FAST_READING:
                    notes.append(f"Text {label} is on screen {seconds:.1f}s for {len(wording)} "
                                 "characters, which is fast to read.")

        if kind == "video" and index == 0 and segments:
            if project._start(segments[0]) > frame:
                warnings.append(
                    f"The main video track starts at {_seconds(project._start(segments[0]))}s, not 0. "
                    "CapCut's main track is magnetic and may pull the clips to the start.")
            gaps = [(project._end(a), project._start(b)) for a, b in zip(segments, segments[1:])
                    if project._start(b) - project._end(a) > frame]
            if gaps:
                listed = ", ".join(f"{_seconds(a)}-{_seconds(b)}s" for a, b in gaps[:5])
                warnings.append(
                    f"The main video track has {len(gaps)} gap(s) ({listed}). CapCut's main track "
                    "is magnetic and may close them, shifting everything after. Use arrange_track "
                    "to close them on purpose, or put a clip there.")
        if kind == "audio" and segments and picture_end and cursor > picture_end + 500_000:
            notes.append(f"Audio on {where} runs {_seconds(cursor - picture_end)}s past the last "
                         "picture; the video would end on black.")

    missing = project.missing_media()
    if missing:
        problems.append(f"{len(missing)} media file(s) cannot be found: "
                        + ", ".join(Path(p).name for p in missing[:8])
                        + (" ..." if len(missing) > 8 else "") + ". Use relink_media.")
    if not any(t.get("segments") for t in content["tracks"]):
        notes.append("The timeline is empty.")

    pending = _not_downloaded(project)
    if pending:
        notes.append(f"{pending} library item(s) will be downloaded by CapCut when the project is "
                     "first opened (needs a connection). If one shows as missing there, CapCut no "
                     "longer offers it: pick another.")
    version_note = project.version_note()
    if version_note:
        warnings.append(version_note)
    if project.changed_outside():
        notes.append("The project was changed in CapCut after the server's last edit. An undo "
                     "now would take those changes back too.")

    out: dict = {"project": project.name, "duration": _seconds(project.duration_us()),
                 "ok": not problems and not warnings}
    for name, found in (("problems", problems), ("warnings", warnings), ("notes", notes)):
        if found:
            out[name] = found[:MAX_LISTED]
            if len(found) > MAX_LISTED:
                out[name].append(f"... and {len(found) - MAX_LISTED} more.")
    return out


def _text(project: Project, track: dict, seg: dict) -> str:
    from . import schema
    material = project._main_material(track, seg)
    return schema.text_of(material) if material else ""


def _not_downloaded(project: Project) -> int:
    """Library items written by id that CapCut has not fetched yet (they have no local path)."""
    count = 0
    for group in ("transitions", "video_effects", "effects", "audio_effects"):
        for item in project.materials.get(group, []):
            if isinstance(item, dict) and item.get("resource_id") and not item.get("path"):
                count += 1
    for holder in project.materials.get("material_animations", []):
        for animation in holder.get("animations") or []:
            if animation.get("resource_id") and not animation.get("path"):
                count += 1
    return count
