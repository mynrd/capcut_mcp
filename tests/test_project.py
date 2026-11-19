"""Editing operations, checked against the files they leave on disk."""

from __future__ import annotations

import json

import pytest

from capcut_mcp.draft import Project, list_projects
from capcut_mcp.env import CapCutError


def check_integrity(project: Project) -> None:
    """What CapCut needs to be true of any draft we write."""
    content = project.content
    known = {m["id"] for group in content["materials"].values() if isinstance(group, list)
             for m in group if isinstance(m, dict)}
    longest = 0
    for track in content["tracks"]:
        segments = sorted(track["segments"], key=lambda s: s["target_timerange"]["start"])
        cursor = 0
        for seg in segments:
            target = seg["target_timerange"]
            assert target["duration"] > 0
            assert target["start"] >= cursor, "segments overlap on a track"
            cursor = target["start"] + target["duration"]
            assert seg["material_id"] in known
            assert set(seg["extra_material_refs"]) <= known
            ids = [s["id"] for s in track["segments"]]
            assert len(ids) == len(set(ids))
        longest = max(longest, cursor)
    assert content["duration"] == longest


def reopen(project: Project) -> Project:
    project.save()
    again = Project.open(project.name)
    check_integrity(again)
    return again


def segments(project: Project, kind: str = "video", track: int = 0) -> list[dict]:
    return sorted(project.track(kind, track)["segments"], key=lambda s: s["target_timerange"]["start"])


def test_create_writes_both_timeline_copies(drafts):
    project = Project.create("Demo", 1080, 1920, 30)
    root_copy = drafts / "Demo" / "draft_content.json"
    timeline_copies = list((drafts / "Demo" / "Timelines").glob("*/draft_content.json"))
    assert len(timeline_copies) == 1
    assert root_copy.read_bytes() == timeline_copies[0].read_bytes()
    assert json.loads(root_copy.read_text("utf-8"))["id"] == timeline_copies[0].parent.name
    meta = json.loads((drafts / "Demo" / "draft_meta_info.json").read_text("utf-8"))
    assert meta["draft_name"] == "Demo" and meta["draft_id"]
    assert [p["name"] for p in list_projects()] == ["Demo"]
    assert project.describe()["duration"] == 0


def test_duplicate_names_and_bad_names_are_rejected(drafts):
    Project.create("Demo", 1920, 1080, 30)
    with pytest.raises(CapCutError):
        Project.create("Demo", 1920, 1080, 30)
    with pytest.raises(CapCutError):
        Project.create("a/b", 1920, 1080, 30)
    with pytest.raises(CapCutError, match="Available"):
        Project.open("Missing")


def test_clips_append_and_first_clip_sets_canvas(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    project.add_clip(media["video"])
    project.add_clip(media["silent"], source_start=1, source_end=2.5)
    project.add_clip(media["image"], duration=2)
    project = reopen(project)

    first, second, third = segments(project)
    assert first["target_timerange"] == {"start": 0, "duration": 4_000_000}
    assert second["target_timerange"] == {"start": 4_000_000, "duration": 1_500_000}
    assert second["source_timerange"] == {"start": 1_000_000, "duration": 1_500_000}
    assert third["target_timerange"] == {"start": 5_500_000, "duration": 2_000_000}
    assert project.content["canvas_config"]["width"] == 320
    assert project.content["canvas_config"]["height"] == 568
    kinds = [m["type"] for m in project.materials["videos"]]
    assert kinds == ["video", "video", "photo"]
    assert project.materials["videos"][0]["has_audio"] is True
    assert project.materials["videos"][1]["has_audio"] is False
    meta = json.loads((drafts / "Demo" / "draft_meta_info.json").read_text("utf-8"))
    assert meta["tm_duration"] == 7_500_000
    assert len(meta["draft_materials"][0]["value"]) == 3
    assert (drafts / "Demo" / "draft_cover.jpg").is_file()


def test_same_file_twice_shares_one_material(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    project.add_clip(media["video"])
    project.add_clip(media["video"])
    project = reopen(project)
    assert len(project.materials["videos"]) == 1
    assert len(project.materials["speeds"]) == 2


def test_overlap_is_refused_and_overlay_track_is_allowed(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    project.add_clip(media["video"])
    with pytest.raises(CapCutError, match="overlap"):
        project.add_clip(media["silent"], start=2)
    overlay = project.add_clip(media["image"], track=1, start=1, duration=2)
    project = reopen(project)
    assert len(project.tracks("video")) == 2
    assert overlay["render_index"] == 1


def test_trim_start_keeps_position_and_ripples(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    first = project.add_clip(media["video"])
    project.add_clip(media["silent"])
    project.trim(first["id"][:8], source_start=1.0)
    project = reopen(project)
    first, second = segments(project)
    assert first["source_timerange"] == {"start": 1_000_000, "duration": 3_000_000}
    assert first["target_timerange"] == {"start": 0, "duration": 3_000_000}
    assert second["target_timerange"]["start"] == 3_000_000


def test_trim_cannot_run_past_the_file(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    seg = project.add_clip(media["video"], source_end=2)
    project.trim(seg["id"], source_end=99)
    assert seg["source_timerange"]["duration"] == 4_000_000
    with pytest.raises(CapCutError, match="nothing"):
        project.trim(seg["id"], source_start=3.99, source_end=4)


def test_split_keeps_footage_continuous(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    seg = project.add_clip(media["video"], source_start=0.5)
    left, right = project.split(seg["id"], 1.0)
    project = reopen(project)
    left, right = segments(project)
    assert left["target_timerange"] == {"start": 0, "duration": 1_000_000}
    assert right["target_timerange"] == {"start": 1_000_000, "duration": 2_500_000}
    assert left["source_timerange"] == {"start": 500_000, "duration": 1_000_000}
    assert right["source_timerange"] == {"start": 1_500_000, "duration": 2_500_000}
    assert left["material_id"] == right["material_id"]
    assert not set(left["extra_material_refs"]) & set(right["extra_material_refs"])
    with pytest.raises(CapCutError, match="not inside"):
        project.split(left["id"], 3.0)


def test_delete_ripples_and_removes_unused_materials(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    first = project.add_clip(media["video"])
    project.add_clip(media["silent"])
    project.delete([first["id"]])
    project = reopen(project)
    (only,) = segments(project)
    assert only["target_timerange"]["start"] == 0
    assert len(project.materials["videos"]) == 1
    assert len(project.materials["speeds"]) == 1
    assert len(project.materials["canvases"]) == 1


def test_arrange_reorders_back_to_back(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    a = project.add_clip(media["video"])
    b = project.add_clip(media["silent"])
    c = project.add_clip(media["image"], duration=1)
    project.arrange("video", 0, order=[c["id"][:8], a["id"][:8]])
    project = reopen(project)
    order = [s["id"] for s in segments(project)]
    assert order == [c["id"], a["id"], b["id"]]
    starts = [s["target_timerange"]["start"] for s in segments(project)]
    assert starts == [0, 1_000_000, 5_000_000]


def test_move_to_another_track_and_time(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    project.add_clip(media["video"])
    b = project.add_clip(media["silent"])
    project.move(b["id"], start=1, track=1)
    project = reopen(project)
    assert len(segments(project, "video", 0)) == 1
    assert segments(project, "video", 1)[0]["target_timerange"]["start"] == 1_000_000


def test_speed_changes_length_and_ripples(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    a = project.add_clip(media["video"])
    project.add_clip(media["silent"])
    project.set_speed(a["id"], 2.0)
    project = reopen(project)
    first, second = segments(project)
    assert first["speed"] == 2.0
    assert first["target_timerange"]["duration"] == 2_000_000
    assert first["source_timerange"]["duration"] == 4_000_000
    assert second["target_timerange"]["start"] == 2_000_000
    speed = next(m for m in project.materials["speeds"] if m["id"] in first["extra_material_refs"])
    assert speed["speed"] == 2.0
    with pytest.raises(CapCutError):
        project.set_speed(first["id"], 0)


def test_audio_with_fades_and_sound_from_a_video(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    project.add_clip(media["silent"])
    music = project.add_audio(media["audio"], start=0, duration=3, volume=0.4, fade_in=0.5, fade_out=1)
    project.add_audio(media["video"], track=1, start=0)
    with pytest.raises(CapCutError, match="no audio"):
        project.add_audio(media["silent"])
    project = reopen(project)
    seg = segments(project, "audio", 0)[0]
    assert seg["target_timerange"] == {"start": 0, "duration": 3_000_000}
    assert seg["volume"] == 0.4
    fade = project.materials["audio_fades"][0]
    assert fade["id"] in seg["extra_material_refs"]
    assert (fade["fade_in_duration"], fade["fade_out_duration"]) == (500_000, 1_000_000)
    assert [m["type"] for m in project.materials["audios"]] == ["extract_music", "video_original_sound"]
    meta = json.loads((drafts / "Demo" / "draft_meta_info.json").read_text("utf-8"))
    assert [e["metetype"] for e in meta["draft_materials"][0]["value"]] == ["video"], \
        "audio files are not listed in the media bin"

    project.set_volume(music["id"], volume=1.5, fade_out=0)
    project = reopen(project)
    assert segments(project, "audio", 0)[0]["volume"] == 1.5
    assert project.materials["audio_fades"][0]["fade_out_duration"] == 0


def test_text_content_and_style(drafts):
    project = Project.create("Demo", 1080, 1920, 30)
    seg = project.add_text("Kumusta 🌍", start=1, duration=2.5, y=0.6, size=12, color="#ffd400",
                           bold=True, stroke_color="#000", stroke_width=0.08)
    project = reopen(project)
    (text,) = project.materials["texts"]
    content = json.loads(text["content"])
    assert content["text"] == "Kumusta 🌍"
    style = content["styles"][0]
    assert style["range"] == [0, 10]  # the emoji is two UTF-16 units
    assert style["size"] == 12 and style["bold"] is True
    assert style["fill"]["content"]["solid"]["color"] == [1.0, 212 / 255, 0.0]
    assert style["strokes"][0]["width"] == 0.08
    assert text["check_flag"] & 8
    stored = segments(project, "text")[0]
    assert stored["target_timerange"] == {"start": 1_000_000, "duration": 2_500_000}
    assert stored["clip"]["transform"]["y"] == 0.6
    assert stored["source_timerange"] is None
    assert stored["render_index"] >= 14000

    project.update_text(seg["id"], text="Bago", stroke_width=0, italic=True)
    project = reopen(project)
    content = json.loads(project.materials["texts"][0]["content"])
    assert content["text"] == "Bago" and content["styles"][0]["range"] == [0, 4]
    assert "strokes" not in content["styles"][0]
    assert content["styles"][0]["bold"] is True, "untouched properties are kept"
    assert not project.materials["texts"][0]["check_flag"] & 8
    with pytest.raises(CapCutError):
        project.update_text(seg["id"], color="blue")


def test_splitting_text_gives_each_half_its_own_material(drafts):
    project = Project.create("Demo", 1080, 1920, 30)
    seg = project.add_text("Hello", start=0, duration=4)
    project.split(seg["id"], 1.5)
    project = reopen(project)
    left, right = segments(project, "text")
    assert left["material_id"] != right["material_id"]
    assert len(project.materials["texts"]) == 2


def test_transform_and_canvas(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    seg = project.add_clip(media["image"], duration=3)
    project.set_transform(seg["id"], x=0.5, y=-0.5, scale=0.4, rotation=15, opacity=0.8,
                          flip_horizontal=True)
    project.set_canvas(1080, 1920)
    project = reopen(project)
    clip = segments(project)[0]["clip"]
    assert clip["transform"] == {"x": 0.5, "y": -0.5}
    assert clip["scale"] == {"x": 0.4, "y": 0.4}
    assert (clip["rotation"], clip["alpha"], clip["flip"]["horizontal"]) == (15.0, 0.8, True)
    assert project.content["canvas_config"] == {
        "background": None, "height": 1920, "ratio": "9:16", "width": 1080}
    with pytest.raises(CapCutError):
        project.set_transform(seg["id"], opacity=3)


def test_times_snap_to_frames(drafts):
    project = Project.create("Demo", 1920, 1080, 30)
    assert project.us(1.0) == 1_000_000
    assert project.us(0.1) == 100_000
    assert project.us(9.2333) == 9_233_333  # 277 frames
    assert project.us(1 / 30) == 33_333
    with pytest.raises(CapCutError):
        project.us(-1)


def test_open_project_is_not_modified(drafts, media, monkeypatch):
    project = Project.create("Demo", 1920, 1080, 30)
    before = (drafts / "Demo" / "draft_content.json").read_bytes()
    (drafts / "Demo" / ".locked").write_bytes(b"")
    monkeypatch.setattr("capcut_mcp.draft.capcut_running", lambda: True)
    project.add_clip(media["video"])
    with pytest.raises(CapCutError, match="open in CapCut"):
        project.save()
    assert (drafts / "Demo" / "draft_content.json").read_bytes() == before
    # A lock left behind by a crash is ignored once CapCut is no longer running.
    monkeypatch.setattr("capcut_mcp.draft.capcut_running", lambda: False)
    project.save()


def test_undo_steps_back_one_change_at_a_time(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    project.add_clip(media["video"])
    project.save()
    project = Project.open("Demo")
    project.add_clip(media["silent"])
    project.save()

    project = Project.open("Demo")
    project.undo()
    assert len(segments(Project.open("Demo"))) == 1
    Project.open("Demo").undo()
    assert Project.open("Demo").content["tracks"] == []
    with pytest.raises(CapCutError, match="No earlier version"):
        Project.open("Demo").undo()


def test_newest_timeline_copy_wins(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    project.add_clip(media["video"])
    project.save()
    # CapCut saving only into Timelines/<id>/ must not be lost.
    (timeline_copy,) = (drafts / "Demo" / "Timelines").glob("*/draft_content.json")
    edited = json.loads(timeline_copy.read_text("utf-8"))
    edited["tracks"][0]["segments"][0]["volume"] = 0.25
    timeline_copy.write_text(json.dumps(edited), encoding="utf-8")
    import os
    import time
    os.utime(timeline_copy, (time.time() + 5, time.time() + 5))
    assert segments(Project.open("Demo"))[0]["volume"] == 0.25


def test_unreadable_project_is_reported_not_overwritten(drafts):
    Project.create("Demo", 1920, 1080, 30)
    for copy in (drafts / "Demo").rglob("draft_content.json"):
        copy.write_bytes(b"\x9f\x00encrypted")
    with pytest.raises(CapCutError, match="encrypt"):
        Project.open("Demo")


def test_duplicate_project(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    project.add_clip(media["video"])
    project.save()
    copy = Project.duplicate("Demo", "Demo v2")
    assert len(segments(copy)) == 1
    original = json.loads((drafts / "Demo" / "draft_meta_info.json").read_text("utf-8"))
    copied = json.loads((drafts / "Demo v2" / "draft_meta_info.json").read_text("utf-8"))
    assert copied["draft_name"] == "Demo v2"
    assert copied["draft_id"] != original["draft_id"]
    assert copied["draft_fold_path"].endswith("Demo v2")
    # The copy gets its own timeline id, consistent across every file that names it.
    (timeline_dir,) = (p for p in (drafts / "Demo v2" / "Timelines").iterdir() if p.is_dir())
    assert timeline_dir.name != project.content["id"]
    assert copy.content["id"] == timeline_dir.name
    index = json.loads((drafts / "Demo v2" / "Timelines" / "project.json").read_text("utf-8"))
    assert index["main_timeline_id"] == index["timelines"][0]["id"] == timeline_dir.name
    assert timeline_dir.name in (drafts / "Demo v2" / "timeline_layout.json").read_text("utf-8")
    assert (drafts / "Demo v2" / "draft_content.json").read_bytes() == \
        (timeline_dir / "draft_content.json").read_bytes()
