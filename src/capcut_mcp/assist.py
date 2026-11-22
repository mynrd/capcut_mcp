"""Edits that depend on the sound: cutting silences, ducking music, captions from speech, and
voice-overs made from text.

analysis.py reports facts about a media file in the file's own time, and speech.py makes speech
from text. This module turns both into changes on the timeline, where a clip may start later, be
trimmed, and play faster or slower.
"""

from __future__ import annotations

import math

from . import analysis, schema, speech
from .draft import Project
from .env import CapCutError

VOICE_GAP = 0.1  # seconds kept free between one spoken line and the next
SENTENCE_PAUSE = 1.0  # a pause this long between captions ends a sentence without punctuation
FIT_ROUNDS = 4  # attempts at a faster rate for a line that runs into the next one


def _sound_clip(project: Project, ref: str, purpose: str) -> tuple[dict, dict, dict]:
    track, seg = project.find(ref)
    material = project._main_material(track, seg)
    usable = (track.get("type") in ("video", "audio") and material and seg.get("source_timerange")
              and material.get("type") != "photo"
              and (track.get("type") == "audio" or material.get("has_audio")))
    if not usable:
        raise CapCutError(f"{purpose} needs a video clip with sound or an audio clip.")
    return track, seg, material


def _source_window(seg: dict) -> tuple[float, float, float]:
    """(start, end) of the part of the file a clip plays, in seconds, and its speed."""
    source = seg["source_timerange"]
    return source["start"] / 1e6, (source["start"] + source["duration"]) / 1e6, float(seg.get("speed") or 1.0)


def _to_timeline(project: Project, seg: dict, file_seconds: float) -> float:
    begin, _, speed = _source_window(seg)
    return project._start(seg) / 1e6 + (file_seconds - begin) / speed


def cut_silence(project: Project, ref: str, *, noise_db: float = -35.0, min_silence: float = 0.5,
                keep: float = 0.12, all_tracks: bool = False, dry_run: bool = False) -> dict:
    """Remove the silent stretches of one clip from the timeline, closing the gaps."""
    track, seg, material = _sound_clip(project, ref, "Cutting silence")
    if keep < 0 or min_silence <= 0:
        raise CapCutError("min_silence must be greater than 0 and keep cannot be negative.")
    kind, index = project._track_position(track)
    begin, end, _ = _source_window(seg)
    found = analysis.detect_silence(material["path"], start=begin, end=end,
                                    noise_db=noise_db, min_silence=min_silence)
    clip_start, clip_end = project._start(seg) / 1e6, project._end(seg) / 1e6
    shortest = max(2 / project.fps, 0.1)
    ranges = []
    for silence in found["silences"]:
        a = _to_timeline(project, seg, silence["start"])
        b = _to_timeline(project, seg, silence["end"])
        # Leave a little air around the speech, except where the silence runs to the clip's edge.
        a = clip_start if a <= clip_start + 0.02 else a + keep
        b = clip_end if b >= clip_end - 0.02 else b - keep
        if b - a >= shortest:
            ranges.append((round(a, 3), round(b, 3)))
    removed = round(sum(b - a for a, b in ranges), 3)
    out: dict = {"segment": seg["id"][:8], "silences_found": len(ranges), "seconds": removed,
                 "ranges": [{"start": a, "end": b} for a, b in ranges[:60]]}
    if not ranges:
        out["note"] = ("No silence long enough to cut. Lower min_silence, or raise noise_db "
                       "(for example -30) if the recording has background noise.")
        return out
    if dry_run:
        out["note"] = "Nothing was changed (dry_run). The ranges are timeline times."
        return out
    if ranges[0][0] <= clip_start + 0.001 and ranges[0][1] >= clip_end - 0.001:
        raise CapCutError("The whole clip is silent at this noise level; nothing would be left. "
                          "Lower noise_db (for example -45) or delete the clip instead.")
    # Last range first, so cutting one does not move the ranges still to be cut.
    for a, b in reversed(ranges):
        project.cut_range(a, b, kind=kind, track=index, all_tracks=all_tracks, ripple=True)
    out["pieces_left"] = len(project.track(kind, index)["segments"])
    return out


def duck(project: Project, ref: str, *, under_type: str = "audio", under_track: int = 0,
         level: float = 0.25, fade: float = 0.3, speech_only: bool = False) -> dict:
    """Lower a clip's volume while the clips on another track play (music under a voice)."""
    track, seg, _ = _sound_clip(project, ref, "Ducking")
    if not 0 <= level < 1:
        raise CapCutError("level is the share of the volume kept while ducked: 0.25 is a quarter.")
    if fade < 0:
        raise CapCutError("fade cannot be negative.")
    other = project.track(under_type, under_track)
    start, end = project._start(seg) / 1e6, project._end(seg) / 1e6

    spans: list[tuple[float, float]] = []
    for voice in other["segments"]:
        if voice is seg:
            continue
        material = project._main_material(other, voice)
        if under_type == "video" and not (material and material.get("has_audio")):
            continue
        a, b = project._start(voice) / 1e6, project._end(voice) / 1e6
        if b <= start or a >= end:
            continue
        pieces = [(a, b)]
        if speech_only and material and voice.get("source_timerange") and material.get("type") != "photo":
            window_start, window_end, _ = _source_window(voice)
            sounds = analysis.detect_silence(material["path"], start=window_start, end=window_end,
                                             min_silence=0.6)["sounds"]
            pieces = [(_to_timeline(project, voice, s["start"]), _to_timeline(project, voice, s["end"]))
                      for s in sounds]
        spans += [(max(a, start), min(b, end)) for a, b in pieces if min(b, end) > max(a, start)]
    if not spans:
        raise CapCutError(f"Nothing on {under_type} track {under_track} plays during this clip, "
                          "so there is nothing to duck under.")

    merged: list[list[float]] = []
    for a, b in sorted(spans):  # dips closer together than two fades become one
        if merged and a - merged[-1][1] <= 2 * fade + 0.2:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    length = end - start
    if merged[0][0] - start <= fade and end - merged[-1][1] <= fade and len(merged) == 1:
        raise CapCutError("The other track plays through this whole clip, so there is nothing "
                          "to duck around. Lower the clip with set_volume instead.")

    base = float(seg.get("volume", 1.0))
    low = round(base * level, 4)
    points: list[tuple[float, float]] = []

    def add(moment: float, value: float) -> None:
        moment = min(max(moment, 0.0), length)
        if points and moment <= points[-1][0] + 1.5 / project.fps:
            return
        points.append((round(moment, 3), value))

    for a, b in merged:
        a, b = a - start, b - start
        if a - fade > 0:
            add(a - fade, base)
        add(max(a, 0.0), low)
        if b < length:
            add(b, low)
            if b + fade <= length:
                add(b + fade, base)
    project.set_keyframes(ref, "volume", points)
    return {"segment": seg["id"][:8], "dips": len(merged), "volume": base, "ducked_to": low,
            "between": [{"start": round(a, 3), "end": round(b, 3)} for a, b in merged[:40]]}


def spoken_words(project: Project, kind: str, track: int, language: str | None,
                 model: str) -> tuple[list[dict], str]:
    """Every word spoken on a track, timed on the timeline. Returns (words, language found)."""
    if kind not in ("video", "audio"):
        raise CapCutError("Speech can be read from a video or an audio track.")
    target = project.track(kind, track)
    words: list[dict] = []
    language_found = language or ""
    for seg in sorted(target["segments"], key=project._start):
        material = project._main_material(target, seg)
        if not material or not seg.get("source_timerange") or material.get("type") == "photo":
            continue
        if kind == "video" and not material.get("has_audio"):
            continue
        begin, end, _ = _source_window(seg)
        result = analysis.transcribe(material["path"], language=language, model=model,
                                     start=begin, end=end)
        language_found = language_found or result.get("language") or ""
        clip_start, clip_end = project._start(seg) / 1e6, project._end(seg) / 1e6
        for word in result["words"]:
            a = max(_to_timeline(project, seg, word["start"]), clip_start)
            b = min(_to_timeline(project, seg, word["end"]), clip_end)
            if b > a:
                words.append({"word": word["word"], "start": a, "end": b})
    words.sort(key=lambda w: w["start"])
    return words, language_found


def auto_captions(project: Project, *, kind: str = "video", track: int = 0,
                  language: str | None = None, model: str = "base", max_chars: int = 32,
                  max_duration: float = 4.0, text_track: int | None = None, y: float = -0.75,
                  **style) -> dict:
    """Write subtitles for what is said on a track, timed to the speech."""
    words, language_found = spoken_words(project, kind, track, language, model)
    if not words:
        raise CapCutError(f"No speech was found on {kind} track {track}.")
    lines = analysis.group_words(words, max_chars=max_chars, max_duration=max_duration)
    if text_track is None:
        text_track = len(project.tracks("text"))  # a track of their own, so nothing can collide
    frame = 1_000_000 / project.fps
    added, cursor = [], 0
    for line in lines:
        begin = max(project.us(line["start"]), cursor)
        finish = max(project.us(line["end"]), begin + round(frame))
        seg = project.add_text(line["text"], start=begin / 1e6, duration=(finish - begin) / 1e6,
                               track=text_track, y=y, kind="subtitle", **style)
        cursor = project._end(seg)
        added.append(seg)
    return {"captions_added": len(added), "language": language_found, "text_track": text_track,
            "first_id": added[0]["id"][:8], "last_id": added[-1]["id"][:8],
            "sample": [line["text"] for line in lines[:5]]}


def caption_sentences(project: Project, track: int) -> list[dict]:
    """The text on a text track joined into sentences: [{"text", "start", "end"}] in seconds.

    Captions follow on from each other until one ends a sentence (. ? ! and the like) or the
    next one comes more than SENTENCE_PAUSE seconds later.
    """
    target = project.track("text", track)
    sentences: list[dict] = []
    current: dict | None = None
    for seg in sorted(target["segments"], key=project._start):
        material = project._main_material(target, seg)
        text = " ".join(schema.text_of(material).split()) if material else ""
        if not text:
            continue
        start, end = project._start(seg) / 1e6, project._end(seg) / 1e6
        if current is not None and start - current["end"] > SENTENCE_PAUSE:
            sentences.append(current)
            current = None
        if current is None:
            current = {"text": text, "start": start, "end": end}
        else:
            current["text"], current["end"] = analysis._join(current["text"], text), end
        if analysis._ends_sentence(text):
            sentences.append(current)
            current = None
    if current is not None:
        sentences.append(current)
    return sentences


def _faster(rate: int, length: float, room: float) -> int:
    """A rate that should make `length` seconds of speech fit in `room`.

    Each step of the Windows rate scale is about 3^(1/10) faster: rate 10 is three times the
    normal pace. The result is checked by speaking again, so this only has to be close.
    """
    steps = math.ceil(10 * math.log(length / max(room, 0.05), 3))
    return min(speech.MAX_RATE, rate + max(1, steps))


def _frame_at_or_after(project: Project, moment_us: int) -> float:
    """The first frame boundary at or after a time, in seconds, for a clip that must not overlap."""
    frames = math.ceil(moment_us * project.fps / 1_000_000 - 1e-6)
    return frames / project.fps


def voiceover(project: Project, *, text: str | None = None, text_track: int | None = None,
              voice: str | None = None, rate: int = 0, start: float | None = None,
              track: int | None = None, volume: float = 1.0, fit: bool = True,
              folder: str | None = None) -> dict:
    """Speak text, or the captions on a text track, with a Windows voice onto an audio track."""
    has_text = text is not None and bool(str(text).strip())
    if has_text == (text_track is not None):
        raise CapCutError("Give either `text` to speak, or `from_text_track` to read the captions "
                          "on a text track aloud; not both and not neither.")
    if text_track is not None and start is not None:
        raise CapCutError("`start` is for `text`. Captions are read aloud where each sentence "
                          "first appears on screen.")
    rate = speech.check_rate(rate)
    name = speech.resolve_voice(voice)
    files = speech.voice_folder(folder)
    if track is None:
        track = len(project.tracks("audio"))  # a track of its own, so nothing can collide

    if has_text:
        plans = [{"text": speech.clean_text(text), "at": start, "room": None}]
    else:
        sentences = caption_sentences(project, text_track)
        if not sentences:
            raise CapCutError(f"Text track {text_track} has no text to read aloud.")
        end_of_video = project.duration_us() / 1e6
        plans = []
        for index, sentence in enumerate(sentences):
            if index + 1 < len(sentences):
                room = sentences[index + 1]["start"] - sentence["start"] - VOICE_GAP
            else:
                room = max(end_of_video, sentence["end"]) - sentence["start"]
            plans.append({"text": sentence["text"], "at": sentence["start"], "room": room})

    rates = [rate] * len(plans)
    spoken = speech.synthesize([(plan["text"], rate) for plan in plans], voice=name, folder=files)
    if fit:
        for _ in range(FIT_ROUNDS):
            over = [i for i, plan in enumerate(plans)
                    if plan["room"] is not None and spoken[i]["duration"] > plan["room"]
                    and rates[i] < speech.MAX_RATE]
            if not over:
                break
            for i in over:
                rates[i] = _faster(rates[i], spoken[i]["duration"], plans[i]["room"])
            again = speech.synthesize([(plans[i]["text"], rates[i]) for i in over],
                                      voice=name, folder=files)
            for i, result in zip(over, again):
                spoken[i] = result

    lines, cursor = [], 0
    for plan, result in zip(plans, spoken):
        at = plan["at"]
        if at is not None and cursor:
            at = max(at, _frame_at_or_after(project, cursor))
        seg = project.add_audio(result["path"], track=track, start=at, volume=volume)
        cursor = project._end(seg)
        line = {"text": plan["text"] if len(plan["text"]) <= 60 else plan["text"][:57] + "...",
                "segment": seg["id"][:8], "start": round(project._start(seg) / 1e6, 3),
                "end": round(cursor / 1e6, 3), "rate": result["rate"]}
        if plan["at"] is not None and line["start"] - plan["at"] > 0.001:
            line["late_by"] = round(line["start"] - plan["at"], 3)
        lines.append(line)

    out: dict = {"voice": name, "audio_track": track, "lines_spoken": len(lines),
                 "files_folder": str(files), "lines": lines[:40]}
    faster = sum(1 for line in lines if line["rate"] > rate)
    late = [line for line in lines if "late_by" in line]
    if faster:
        out["spoken_faster"] = faster
    if late:
        out["note"] = (f"{len(late)} line(s) did not fit before the next caption"
                       + ("" if fit else " (fit=false)") + " and start late, so later captions "
                       "lead the voice. Shorten the wording, space the captions out, or use a "
                       "higher rate.")
    return out
