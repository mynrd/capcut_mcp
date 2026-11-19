"""Edits driven by the sound, the project check and the setup check."""

from __future__ import annotations

import asyncio
import json
import subprocess

import pytest
from mcp import Client

from capcut_mcp import analysis, assist, doctor, draft, library, lint, schema
from capcut_mcp.draft import Project
from capcut_mcp.env import CapCutError
from capcut_mcp.server import mcp

# A tone from 0 to 2s, silence from 2 to 4s, a tone from 4 to 6s.
TALK = "aevalsrc='if(between(t,2,4),0,0.5*sin(2*PI*440*t))':d=6:s=44100"


@pytest.fixture(scope="session")
def talk(tmp_path_factory):
    folder = tmp_path_factory.mktemp("talk")
    video, voice = folder / "talking.mp4", folder / "voice.wav"
    base = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
    subprocess.run([*base, "-f", "lavfi", "-i", "testsrc=size=320x180:rate=30:duration=6",
                    "-f", "lavfi", "-i", TALK, "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(video)], check=True)
    subprocess.run([*base, "-f", "lavfi", "-i", TALK, str(voice)], check=True)
    return {"video": str(video), "voice": str(voice)}


def spans(project: Project, kind: str = "video", track: int = 0) -> list[tuple[float, float]]:
    return [(round(project._start(s) / 1e6, 2), round(project._end(s) / 1e6, 2))
            for s in sorted(project.track(kind, track)["segments"], key=project._start)]


# -------------------------------------------------------------------------- cut_silence

def test_cut_silence_removes_the_pause_and_closes_the_gap(drafts, talk):
    project = Project.create("Talk", 1920, 1080, 30)
    seg = project.add_clip(talk["video"])
    planned = assist.cut_silence(project, seg["id"], dry_run=True)
    assert planned["silences_found"] == 1 and spans(project) == [(0, 6)]
    (pause,) = planned["ranges"]
    assert pause["start"] == pytest.approx(2.12, abs=0.08) and pause["end"] == pytest.approx(3.88, abs=0.08)

    result = assist.cut_silence(project, seg["id"])
    assert result["pieces_left"] == 2
    first, second = spans(project)
    assert first == (0, pytest.approx(2.12, abs=0.08))
    assert second[0] == first[1] and second[1] == pytest.approx(6 - result["seconds"], abs=0.05)
    # The second piece starts in the file just before the sound returns.
    source = sorted(project.track("video", 0)["segments"], key=project._start)[1]["source_timerange"]
    assert source["start"] / 1e6 == pytest.approx(3.88, abs=0.08)


def test_cut_silence_follows_trimming_and_speed(drafts, talk):
    project = Project.create("Talk", 1920, 1080, 30)
    seg = project.add_clip(talk["video"], source_start=1, speed=2)  # plays 1..6s of the file in 2.5s
    (pause,) = assist.cut_silence(project, seg["id"], keep=0, dry_run=True)["ranges"]
    assert pause["start"] == pytest.approx(0.5, abs=0.06) and pause["end"] == pytest.approx(1.5, abs=0.06)


def test_cut_silence_on_all_tracks_keeps_text_in_step(drafts, talk):
    project = Project.create("Talk", 1920, 1080, 30)
    seg = project.add_clip(talk["video"])
    project.add_text("after the pause", start=4.5, duration=1)
    result = assist.cut_silence(project, seg["id"], all_tracks=True)
    (text,) = spans(project, "text")
    assert text[0] == pytest.approx(4.5 - result["seconds"], abs=0.05)


def test_cut_silence_refuses_what_it_cannot_listen_to(drafts, media):
    project = Project.create("Talk", 1920, 1080, 30)
    silent = project.add_clip(media["silent"])
    with pytest.raises(CapCutError, match="needs a video clip with sound"):
        assist.cut_silence(project, silent["id"])
    steady = project.add_clip(media["video"])  # a constant tone: nothing to cut
    assert "No silence long enough" in assist.cut_silence(project, steady["id"])["note"]


# ------------------------------------------------------------------------------ ducking

def volume_points(seg: dict) -> list[tuple[float, float]]:
    prop = next(k for k in seg["common_keyframes"] if k["property_type"] == "KFTypeVolume")
    return [(round(f["time_offset"] / 1e6, 2), round(f["values"][0], 3)) for f in prop["keyframe_list"]]


def test_duck_lowers_music_while_a_voice_plays(drafts, media, talk):
    project = Project.create("Mix", 1920, 1080, 30)
    music = project.add_audio(media["audio"], volume=0.8)                     # 0..6s
    project.add_audio(talk["voice"], track=1, start=2, source_end=2)          # voice 2..4s
    result = assist.duck(project, music["id"], under_track=1, level=0.25, fade=0.3)
    assert result["dips"] == 1 and result["ducked_to"] == 0.2
    assert volume_points(music) == [(1.7, 0.8), (2.0, 0.2), (4.0, 0.2), (4.3, 0.8)]


def test_duck_only_while_someone_speaks(drafts, media, talk):
    project = Project.create("Mix", 1920, 1080, 30)
    music = project.add_audio(media["audio"])
    project.add_audio(talk["voice"], track=1, start=0)  # sound 0..2 and 4..6, a pause between
    assist.duck(project, music["id"], under_track=1, speech_only=True, fade=0.2)
    points = volume_points(music)
    assert points[0] == (0.0, 0.25)
    assert [value for _, value in points] == [0.25, 0.25, 1.0, 1.0, 0.25]
    assert points[1][0] == pytest.approx(2.0, abs=0.1) and points[4][0] == pytest.approx(4.0, abs=0.1)


def test_duck_needs_something_to_duck_under(drafts, media):
    project = Project.create("Mix", 1920, 1080, 30)
    music = project.add_audio(media["audio"])
    project.add_audio(media["audio"], track=1, start=10)
    with pytest.raises(CapCutError, match="nothing to duck under"):
        assist.duck(project, music["id"], under_track=1)


# ------------------------------------------------------------------------ auto captions

WORDS = [("Hello", 0.2, 0.6), ("there.", 0.7, 1.1), ("This", 2.0, 2.3), ("is", 2.35, 2.5),
         ("a", 2.55, 2.6), ("test", 2.65, 3.0), ("of", 3.05, 3.2), ("captions.", 3.25, 3.9)]


@pytest.fixture
def heard(monkeypatch):
    """Stand in for the speech package: every file 'says' WORDS."""
    calls = []

    def fake(file, model, language):
        calls.append(str(file))
        return {"language": "en",
                "words": [{"word": w, "start": a, "end": b} for w, a, b in WORDS],
                "segments": [{"start": 0.2, "end": 3.9, "text": " ".join(w for w, _, _ in WORDS)}]}
    monkeypatch.setattr(analysis, "_run_whisper", fake)
    return calls


def test_auto_captions_follow_the_speech_on_the_timeline(drafts, media, heard):
    project = Project.create("Talk", 1920, 1080, 30)
    project.add_clip(media["image"], duration=2)       # a title card first: no speech
    project.add_clip(media["video"], source_start=1)   # the talk starts 1s into its file
    project.add_text("already here", start=0, duration=1)
    result = assist.auto_captions(project, max_chars=20, size=8.0, color="#FFFFFF")
    assert result["language"] == "en" and result["text_track"] == 1
    lines = [(schema.text_of(project._main_material(project.track("text", 1), s)),
              round(project._start(s) / 1e6, 2), round(project._end(s) / 1e6, 2))
             for s in project.track("text", 1)["segments"]]
    # "Hello there." is before the clip's in-point and is left out; the rest moves by 2s - 1s.
    assert [text for text, _, _ in lines] == ["This is a test of", "captions."]
    assert lines[0][1] == pytest.approx(3.0, abs=0.04) and lines[1][2] == pytest.approx(4.9, abs=0.04)
    assert lines[0][2] <= lines[1][1]
    assert all(m["type"] == "subtitle" for m in project.materials["texts"][1:])
    assert len(heard) == 1  # the photo was not sent to the speech package


def test_auto_captions_without_the_speech_package_says_how_to_get_it(drafts, media, monkeypatch):
    monkeypatch.setattr(analysis, "_whisper_model", lambda *a: (_ for _ in ()).throw(
        CapCutError(analysis._install_hint())))
    project = Project.create("Talk", 1920, 1080, 30)
    project.add_clip(media["video"])
    with pytest.raises(CapCutError, match="uv sync --extra transcribe"):
        assist.auto_captions(project)


# ------------------------------------------------------------------------ check_project

def test_a_clean_project_passes_the_check(drafts, media):
    project = Project.create("Clean", 1920, 1080, 30)
    project.add_clip(media["video"])
    project.add_clip(media["silent"])
    project.add_text("Title", start=0, duration=2, y=0.7)
    project.add_audio(media["audio"], duration=7)
    project.save()
    report = lint.check(Project.open("Clean"))
    assert report["ok"] and "problems" not in report and "warnings" not in report


def test_the_check_finds_what_would_disappoint(drafts, media):
    project = Project.create("Messy", 1920, 1080, 30)
    first = project.add_clip(media["video"])
    second = project.add_clip(media["silent"])
    library.add_transition(project, first["id"], "叠化")
    project.delete([second["id"]])                       # the transition now leads nowhere
    project.add_clip(media["image"], start=10, duration=2)  # a gap on the main track
    project.add_text("off the edge", start=0, duration=1, x=1.6)
    project.add_text("x" * 60, start=20, duration=1)     # after the picture, and fast to read
    project.add_audio(media["audio"], start=11)          # runs past the picture
    project.save()

    report = lint.check(Project.open("Messy"))
    assert not report["ok"]
    warnings, notes = " ".join(report["warnings"]), " ".join(report["notes"])
    assert "no clip directly after it" in warnings
    assert "gap(s)" in warnings and "4.0-10.0s" in warnings
    assert "outside the frame" in warnings and "after the last picture" in warnings
    assert "past the last picture" in notes and "fast to read" in notes
    assert "downloaded by CapCut" in notes


def test_the_check_reports_missing_media_and_broken_references(drafts, media, tmp_path):
    copy = tmp_path / "gone.mp4"
    copy.write_bytes(open(media["video"], "rb").read())
    project = Project.create("Broken", 1920, 1080, 30)
    seg = project.add_clip(str(copy))
    seg["extra_material_refs"].append("NOT-A-REAL-ID")
    project.save()
    copy.unlink()
    problems = " ".join(lint.check(Project.open("Broken"))["problems"])
    assert "cannot be found: gone.mp4" in problems and "broken internal reference" in problems


# ------------------------------------------------------------------------------- doctor

def test_doctor_describes_the_setup(drafts, media):
    Project.create("One", 1920, 1080, 30)
    report = doctor.report()
    assert report["ok"] and report["projects"]["count"] == 1
    assert report["ffmpeg"]["ffmpeg"] and report["ffmpeg"]["ffprobe"]
    assert report["catalogue"]["available"] and report["confirmed_on_capcut"] == ["8.3.0"]
    assert report["storage"]["folder"]

    project = Project.open("One")
    project.content["last_modified_platform"] = {"app_version": "9.8.0"}
    for file in draft._content_files(project.folder):
        draft._write_json(file, project.content)
    (drafts / "Locked").mkdir()
    (drafts / "Locked" / "draft_content.json").write_bytes(b"\x9f\x00encrypted")
    report = doctor.report()
    warnings = " ".join(report["warnings"])
    assert "9.8.0" in warnings and "not readable JSON" in warnings
    assert report["projects"]["unreadable"] == ["Locked"]
    assert report["capcut"]["status"] in ("unverified", "verified")


# ----------------------------------------------------------------- through the server

def test_listening_tools_through_the_server(drafts, talk):
    async def scenario():
        async with Client(mcp) as client:
            async def call(tool, **arguments):
                result = await client.call_tool(tool, arguments)
                assert not result.is_error, result.content[0].text
                return json.loads(result.content[0].text)
            measured = await call("analyze_media", path=talk["video"],
                                  what=["silence", "scenes", "loudness", "levels"])
            await call("create_project", name="Talk")
            clip = (await call("add_clips", project="Talk", clips=[{"path": talk["video"]}]))["segments"][0]
            cut = await call("cut_silence", project="Talk", segment=clip["id"])
            checked = await call("check_project", project="Talk")
            health = await call("doctor")
            return measured, cut, checked, health

    measured, cut, checked, health = asyncio.run(scenario())
    assert len(measured["silence"]["silences"]) == 1 and len(measured["silence"]["sounds"]) == 2
    assert measured["loudness"]["integrated_lufs"] < 0 and measured["loudness"]["volume_for_minus_14_lufs"] > 0
    assert len(measured["levels"]["db"]) == 12 and min(measured["levels"]["db"]) < -60
    assert "cuts" in measured["scenes"]
    assert cut["pieces_left"] == 2 and cut["duration"] == pytest.approx(6 - cut["seconds"], abs=0.05)
    assert checked["ok"] and health["ok"]
