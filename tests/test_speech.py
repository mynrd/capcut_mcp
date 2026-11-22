"""Voice-overs made from text: the speech engine wrapper, reading captions aloud, and the tools."""

from __future__ import annotations

import asyncio
import json
import math
import wave
from pathlib import Path

import pytest
from mcp import Client

from capcut_mcp import assist, doctor, speech
from capcut_mcp.draft import Project
from capcut_mcp.env import CapCutError
from capcut_mcp.server import mcp

from test_project import check_integrity, reopen

VOICES = [{"name": "Microsoft David Desktop", "language": "en-US", "gender": "Male"},
          {"name": "Microsoft David", "language": "en-US", "gender": "Male"},
          {"name": "Microsoft Zira Desktop", "language": "en-US", "gender": "Female"}]
PER_CHAR = 0.06  # seconds of fake speech per character at rate 0


def fake_length(text: str, rate: int) -> float:
    return len(text) * PER_CHAR / 3 ** (rate / 10)


def write_wav(path: str, seconds: float) -> None:
    with wave.open(path, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(22050)
        audio.writeframes(b"\x00\x00" * int(seconds * 22050))


@pytest.fixture
def voice(monkeypatch):
    """Stand in for PowerShell: lists VOICES and 'speaks' silence as long as fake_length."""
    spoken: list[dict] = []

    def fake_run(job, *, timeout, what):
        if job["action"] == "voices":
            return {"default": VOICES[0]["name"], "voices": VOICES}
        for item in job["items"]:
            spoken.append(item)
            write_wav(item["out"], fake_length(item["text"], item["rate"]))
        return {"done": [item["out"] for item in job["items"]]}

    monkeypatch.setattr(speech, "_powershell", lambda: "powershell")
    monkeypatch.setattr(speech, "_run", fake_run)
    return spoken


def audio_spans(project: Project, track: int) -> list[tuple[float, float]]:
    return [(round(project._start(s) / 1e6, 3), round(project._end(s) / 1e6, 3))
            for s in sorted(project.track("audio", track)["segments"], key=project._start)]


def captioned(project: Project, cues: list[tuple[str, float, float]], track: int = 0) -> None:
    for text, start, end in cues:
        project.add_text(text, start=start, duration=end - start, track=track, kind="subtitle")


BANANA = [("Did you know", 0.2, 0.7), ("bananas are slightly radioactive?", 0.7, 3.5),
          ("They are rich", 4.1, 5.3), ("in potassium.", 5.3, 6.3),
          ("One banana", 7.0, 7.6), ("won't hurt you.", 7.6, 9.0)]


# ------------------------------------------------------------------------------ voices

def test_voices_are_found_by_part_of_their_name(voice):
    assert speech.resolve_voice(None) == "Microsoft David Desktop"  # the default
    assert speech.resolve_voice("zira") == "Microsoft Zira Desktop"
    assert speech.resolve_voice("david") == "Microsoft David"  # the shortest of two matches
    assert speech.resolve_voice("David  DESKTOP") == "Microsoft David Desktop"
    with pytest.raises(CapCutError, match="No voice called 'Hazel'.*Microsoft Zira Desktop"):
        speech.resolve_voice("Hazel")


def test_rate_must_be_a_whole_number_in_range():
    assert speech.check_rate(-10) == -10 and speech.check_rate(3.0) == 3
    for bad in (11, -11, 1.5, "fast", True):
        with pytest.raises(CapCutError, match="rate"):
            speech.check_rate(bad)


def test_without_powershell_the_error_points_to_add_audio(drafts, media, monkeypatch):
    monkeypatch.setattr(speech, "_powershell", lambda: None)
    assert not speech.available()
    project = Project.create("Voice", 1080, 1920, 30)
    with pytest.raises(CapCutError, match="add_audio"):
        assist.voiceover(project, text="Hello there.")
    assert doctor.report()["text_to_speech"]["available"] is False


# --------------------------------------------------------------------------- speaking

def test_text_is_spoken_as_one_clip_on_a_track_of_its_own(drafts, media, voice):
    project = Project.create("Voice", 1080, 1920, 30)
    project.add_audio(media["audio"])  # music on audio track 0
    result = assist.voiceover(project, text="  Hello   there, this is a test.  ", start=1.5,
                              voice="zira", volume=0.8)
    assert result["voice"] == "Microsoft Zira Desktop" and result["audio_track"] == 1
    (line,) = result["lines"]
    assert line["text"] == "Hello there, this is a test." and line["start"] == 1.5
    length = fake_length("Hello there, this is a test.", 0)
    assert line["end"] == pytest.approx(1.5 + length, abs=0.002)

    project = reopen(project)
    seg = project.track("audio", 1)["segments"][0]
    assert seg["volume"] == pytest.approx(0.8)
    path = Path(project._main_material(project.track("audio", 1), seg)["path"])
    assert path.parent == Path(result["files_folder"]) == drafts.parent / "home" / "voiceovers"
    assert path.name.startswith("hello-there-this-is-a-test-") and path.suffix == ".wav"


def test_the_same_line_is_not_spoken_twice(drafts, media, voice):
    project = Project.create("Voice", 1080, 1920, 30)
    assist.voiceover(project, text="Once only.")
    assist.voiceover(project, text="Once only.", track=0)  # appended after the first
    assist.voiceover(project, text="Once only.", rate=2, track=0)  # a new rate is a new file
    assert [item["rate"] for item in voice] == [0, 2]
    assert len(project.track("audio", 0)["segments"]) == 3
    check_integrity(reopen(project))


def test_a_sentence_said_twice_is_spoken_once_and_placed_twice(drafts, media, voice):
    project = Project.create("Voice", 1080, 1920, 30)
    project.add_clip(media["image"], duration=8)
    captioned(project, [("Say it again.", 0, 1), ("Say it again.", 3, 4)])
    result = assist.voiceover(project, text_track=0)
    assert [line["start"] for line in result["lines"]] == [0.0, 3.0]
    assert len(voice) == 1
    files = {project._main_material(project.track("audio", 0), s)["path"]
             for s in project.track("audio", 0)["segments"]}
    assert len(files) == 1 and not list(Path(result["files_folder"]).glob("*.tmp.wav"))


def test_files_can_go_to_a_folder_of_the_users_choice(drafts, media, voice, tmp_path):
    project = Project.create("Voice", 1080, 1920, 30)
    result = assist.voiceover(project, text="Kept with the footage.", folder=str(tmp_path / "vo"))
    assert Path(result["files_folder"]) == tmp_path / "vo"
    assert len(list((tmp_path / "vo").glob("*.wav"))) == 1
    assert not list((tmp_path / "vo").glob("*.tmp.wav"))


# ------------------------------------------------------------------- reading captions

def test_captions_are_read_aloud_sentence_by_sentence_in_time(drafts, media, voice):
    project = Project.create("Voice", 1080, 1920, 30)
    project.add_clip(media["image"], duration=10)
    captioned(project, BANANA)
    project.add_text("RADIOACTIVE?!", start=0, duration=3, track=1)  # a title, not read
    sentences = assist.caption_sentences(project, 0)
    assert [s["text"] for s in sentences] == [
        "Did you know bananas are slightly radioactive?", "They are rich in potassium.",
        "One banana won't hurt you."]

    result = assist.voiceover(project, text_track=0)
    assert result["lines_spoken"] == 3 and "note" not in result
    spans = audio_spans(project, 0)
    assert [a for a, _ in spans] == [0.2, 4.1, 7.0]  # each where its first caption appears
    for (_, end), next_start in zip(spans, [4.1, 7.0]):
        assert end <= next_start - assist.VOICE_GAP + 0.001
    check_integrity(reopen(project))


def test_a_pause_between_captions_ends_a_sentence(drafts, media, voice):
    project = Project.create("Voice", 1080, 1920, 30)
    captioned(project, [("no full stop here", 0, 1), ("still the same thought", 1.2, 2),
                        ("a new thought", 4, 5)])
    assert [s["text"] for s in assist.caption_sentences(project, 0)] == [
        "no full stop here still the same thought", "a new thought"]


def test_a_sentence_too_long_for_its_gap_is_spoken_faster(drafts, media, voice):
    project = Project.create("Voice", 1080, 1920, 30)
    project.add_clip(media["image"], duration=8)
    long = "This sentence has far too many words to say before the next caption comes."
    captioned(project, [(long, 0, 3), ("Next.", 3.2, 4)])
    assert fake_length(long, 0) > 3.1  # it would not fit at the normal pace
    result = assist.voiceover(project, text_track=0)
    first = result["lines"][0]
    assert first["rate"] > 0 and result["spoken_faster"] == 1
    assert first["end"] <= 3.2 - assist.VOICE_GAP + 0.001
    assert fake_length(long, first["rate"] - 1) > 3.1  # no faster than needed
    assert result["lines"][1]["rate"] == 0 and "late_by" not in result["lines"][1]


def test_a_line_that_cannot_fit_starts_the_next_one_late(drafts, media, voice):
    project = Project.create("Voice", 1080, 1920, 30)
    project.add_clip(media["image"], duration=8)
    huge = "word " * 40 + "end."
    captioned(project, [(huge, 0, 1), ("Second.", 1.0, 2), ("Third.", 6, 7)])
    result = assist.voiceover(project, text_track=0)
    first, second, third = result["lines"]
    assert first["rate"] == speech.MAX_RATE
    assert second["start"] >= first["end"] and second["late_by"] > 0
    assert "late_by" not in third and third["start"] == 6.0
    assert "1 line(s) did not fit" in result["note"]
    check_integrity(reopen(project))


def test_fit_false_keeps_the_rate_given(drafts, media, voice):
    project = Project.create("Voice", 1080, 1920, 30)
    project.add_clip(media["image"], duration=8)
    long = "This sentence has far too many words to say before the next caption comes."
    captioned(project, [(long, 0, 3), ("Next.", 3.2, 4)])
    result = assist.voiceover(project, text_track=0, fit=False, rate=-2)
    assert [line["rate"] for line in result["lines"]] == [-2, -2]
    assert "late_by" in result["lines"][1] and "fit=false" in result["note"]
    assert {item["rate"] for item in voice} == {-2}


def test_the_last_sentence_fits_before_the_video_ends(drafts, media, voice):
    project = Project.create("Voice", 1080, 1920, 30)
    project.add_clip(media["image"], duration=4)
    line = "A closing line that is a little too long."
    captioned(project, [(line, 2, 3.5)])
    assert 2 + fake_length(line, 0) > 4
    (spoken,) = assist.voiceover(project, text_track=0)["lines"]
    assert spoken["rate"] > 0 and spoken["end"] <= 4.001


def test_wrong_requests_are_refused_before_anything_changes(drafts, media, voice):
    project = Project.create("Voice", 1080, 1920, 30)
    project.add_text("title", start=0, duration=1)
    for kwargs, message in [({}, "either `text`"), ({"text": "a", "text_track": 0}, "either `text`"),
                            ({"text": "   "}, "either `text`"),
                            ({"text_track": 0, "start": 2}, "`start` is for `text`"),
                            ({"text": "a", "rate": 20}, "rate runs from"),
                            ({"text": "a", "voice": "nobody"}, "No voice called"),
                            ({"text_track": 3}, "no text track 3")]:
        with pytest.raises(CapCutError, match=message):
            assist.voiceover(project, **kwargs)
    assert not project.tracks("audio") and not voice


# ---------------------------------------------------------------- through the server

def test_voice_tools_through_the_server(drafts, media, voice):
    async def scenario():
        async with Client(mcp) as client:
            async def call(tool, **arguments):
                result = await client.call_tool(tool, arguments)
                assert not result.is_error, result.content[0].text
                return json.loads(result.content[0].text)
            listed = await call("list_voices")
            await call("create_project", name="Voice", width=1080, height=1920)
            await call("add_clips", project="Voice", clips=[{"path": media["image"], "duration": 10}])
            await call("add_captions", project="Voice",
                       captions=[{"text": t, "start": a, "end": b} for t, a, b in BANANA])
            read = await call("add_voiceover", project="Voice", from_text_track=0, voice="zira")
            batched = await call("batch", project="Voice", edits=[
                {"tool": "add_voiceover", "args": {"text": "One more thing.", "start": 9.5}}])
            failed = await client.call_tool("add_voiceover", {"project": "Voice"})
            health = await call("doctor")
            return listed, read, batched, failed, health

    listed, read, batched, failed, health = asyncio.run(scenario())
    assert listed["default"] == "Microsoft David Desktop" and len(listed["voices"]) == 3
    assert read["lines_spoken"] == 3 and read["audio_track"] == 0 and read["duration"] == 10
    assert read["voice"] == "Microsoft Zira Desktop"
    assert batched["saved"] and batched["steps"][0]["audio_track"] == 1
    assert failed.is_error and "either `text`" in failed.content[0].text
    assert health["text_to_speech"]["voices"] == [v["name"] for v in VOICES]
    project = Project.open("Voice")
    assert [len(t["segments"]) for t in project.tracks("audio")] == [3, 1]
    check_integrity(project)


# ------------------------------------------------------------------ the real engine

def _real_voices() -> list[dict]:
    try:
        return speech.voices()["voices"] if speech.available() else []
    except CapCutError:
        return []


@pytest.mark.skipif(not _real_voices(), reason="No Windows speech voices on this machine")
def test_the_windows_voice_really_speaks(drafts, tmp_path):
    folder = speech.voice_folder(str(tmp_path / "real"))
    name = speech.resolve_voice(None)
    normal, faster = speech.synthesize([("Bananas are slightly radioactive, café.", 0),
                                        ("Bananas are slightly radioactive, café.", 6)],
                                       voice=name, folder=folder)
    assert not normal["cached"] and 1.0 < normal["duration"] < 6.0
    assert faster["duration"] < normal["duration"] * 0.85
    (again,) = speech.synthesize([("Bananas are slightly radioactive, café.", 0)],
                                 voice=name, folder=folder)
    assert again["cached"] and again["path"] == normal["path"]
    assert math.isclose(speech.wav_seconds(Path(normal["path"])), normal["duration"])
