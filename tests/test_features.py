"""Cutting, replacing, animation, framing, subtitles export and the preview render."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from capcut_mcp import captions, schema
from capcut_mcp.draft import Project
from capcut_mcp.env import CapCutError
from capcut_mcp.media import probe
from capcut_mcp.render import render_preview
from test_project import reopen, segments


@pytest.fixture(scope="session")
def odd_clip(tmp_path_factory):
    """185 frames at 30 fps: 6.1666... seconds, which does not land on a whole microsecond."""
    path = tmp_path_factory.mktemp("odd") / "odd.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30",
                    "-frames:v", "185", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)
    return str(path)


# ----------------------------------------------------------- matching CapCut's numbers

def test_duration_is_cut_off_like_capcut_not_rounded(odd_clip):
    assert probe(odd_clip).duration_us == 6_166_666


def test_sped_up_clip_lands_on_a_whole_frame(drafts, odd_clip):
    project = Project.create("Demo", 1920, 1080, 30)
    seg = project.add_clip(odd_clip, speed=1.5)
    # 6.1666s / 1.5 = 123.33 frames; CapCut rounds that up to 124 frames.
    assert seg["target_timerange"]["duration"] == 4_133_333
    assert seg["source_timerange"]["duration"] == 6_166_666


def test_overlay_video_tracks_carry_capcuts_flag(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    project.add_clip(media["video"])
    project.add_clip(media["image"], track=1, start=0, duration=1)
    project.add_text("Hi", start=0, duration=1)
    flags = [(t["type"], t["flag"]) for t in project.content["tracks"]]
    assert flags == [("video", 0), ("video", 2), ("text", 0)]


def test_fade_reference_sits_where_capcut_puts_it(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    seg = project.add_audio(media["audio"], fade_out=1)
    fade = project.materials["audio_fades"][0]
    assert seg["extra_material_refs"].index(fade["id"]) == 2


def test_text_uses_the_full_structure_capcut_writes(drafts):
    project = Project.create("Demo", 1080, 1920, 30)
    project.add_text("Hi", start=0, duration=1, letter_spacing=2, line_spacing=1, wrap_width=0.6)
    material = project.materials["texts"][0]
    assert len(material) >= 120
    assert {"caption_template_info", "lyrics_template", "single_char_bg_enable"} <= set(material)
    assert material["letter_spacing"] == pytest.approx(0.1)
    assert material["line_spacing"] == pytest.approx(0.07)
    assert material["line_max_width"] == 0.6


# ------------------------------------------------------------------ cutting and inserting

def test_ripple_insert_pushes_later_clips(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    project.add_clip(media["video"])
    last = project.add_clip(media["silent"])
    project.add_clip(media["image"], start=4, duration=2, ripple=True)
    project = reopen(project)
    assert [s["target_timerange"]["start"] for s in segments(project)] == [0, 4_000_000, 6_000_000]
    assert segments(project)[2]["id"] == last["id"]
    with pytest.raises(CapCutError, match="falls inside"):
        project.add_clip(media["image"], start=1, duration=2, ripple=True)


def test_cut_range_removes_time_and_closes_the_gap(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    project.add_clip(media["video"])   # 0-4
    project.add_clip(media["silent"])  # 4-7
    project.add_text("Across the cut", start=2, duration=4)  # 2-6
    removed = project.cut_range(3, 5, all_tracks=True)
    project = reopen(project)

    first, second = segments(project)
    assert first["target_timerange"] == {"start": 0, "duration": 3_000_000}
    assert second["target_timerange"] == {"start": 3_000_000, "duration": 2_000_000}
    assert second["source_timerange"]["start"] == 1_000_000
    texts = segments(project, "text")
    assert [(t["target_timerange"]["start"], t["target_timerange"]["duration"]) for t in texts] == \
        [(2_000_000, 1_000_000), (3_000_000, 1_000_000)]
    assert len(removed) == 3 and project.content["duration"] == 5_000_000


def test_cut_range_on_one_track_leaves_the_others(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    project.add_clip(media["video"])
    project.add_audio(media["audio"], start=0)
    project.cut_range(1, 2)
    assert segments(project)[1]["target_timerange"]["start"] == 1_000_000
    assert segments(project, "audio")[0]["target_timerange"]["duration"] == 6_000_000


def test_replace_media_keeps_place_and_shortens_if_needed(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    first = project.add_clip(media["video"], volume=0.5)
    project.add_clip(media["image"], duration=1)
    project.set_transform(first["id"], scale=0.8)
    project.replace_media(first["id"], media["silent"])  # 3s file into a 4s slot
    project = reopen(project)
    replaced, still = segments(project)
    assert replaced["target_timerange"] == {"start": 0, "duration": 3_000_000}
    assert still["target_timerange"]["start"] == 3_000_000
    assert (replaced["volume"], replaced["clip"]["scale"]["x"]) == (0.5, 0.8)
    assert [Path(m["path"]).name for m in project.materials["videos"]] == ["still.png", "silent.mp4"]
    with pytest.raises(CapCutError, match="audio file"):
        project.replace_media(replaced["id"], media["audio"])


def test_freeze_frame_inserts_a_still(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    seg = project.add_clip(media["video"])
    still = project.freeze_frame(seg["id"], at=2, duration=1.5)
    project = reopen(project)
    parts = segments(project)
    assert [(p["target_timerange"]["start"], p["target_timerange"]["duration"]) for p in parts] == \
        [(0, 2_000_000), (2_000_000, 1_500_000), (3_500_000, 2_000_000)]
    image = project.find_material(still["material_id"])[1]
    assert image["type"] == "photo" and Path(image["path"]).is_file()
    assert (image["width"], image["height"]) == (320, 568), "the still keeps the full frame size"
    assert parts[2]["source_timerange"]["start"] == 2_000_000


def test_relink_after_media_moved(drafts, media, tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    shutil.copy(media["silent"], old / "silent.mp4")
    project = Project.create("Demo", 1920, 1080, 30)
    project.add_clip(str(old / "silent.mp4"))
    project.save()
    shutil.move(old, new)

    project = Project.open("Demo")
    assert [Path(p).name for p in project.missing_media()] == ["silent.mp4"]
    assert "missing_media" in project.describe()
    result = project.relink(str(old), str(new))
    assert len(result["relinked"]) == 1 and project.missing_media() == []
    assert project.relink(str(tmp_path / "nowhere"), str(new)) == {
        "relinked": [], "not_found_at_new_location": []}
    # CapCut's media bin, kept in draft_meta_info.json, follows the clips to the new place.
    project.save()
    meta = json.loads((drafts / "Demo" / "draft_meta_info.json").read_text("utf-8"))
    (entry,) = next(b["value"] for b in meta["draft_materials"] if b["type"] == 0)
    assert Path(entry["file_Path"]) == new / "silent.mp4"

    # CapCut can write an entry for the old place back into its list after the clips were
    # repointed. Relinking again mends the list alone, without leaving the file listed twice.
    stale = dict(entry, file_Path=str(old / "silent.mp4").replace("\\", "/"), id="stale")
    next(b for b in meta["draft_materials"] if b["type"] == 0)["value"].append(stale)
    (drafts / "Demo" / "draft_meta_info.json").write_text(json.dumps(meta), encoding="utf-8")
    project = Project.open("Demo")
    result = project.relink(str(old), str(new))
    assert result["relinked"] == [] and result["media_list_entries_fixed"] == 1
    project.save()
    meta = json.loads((drafts / "Demo" / "draft_meta_info.json").read_text("utf-8"))
    (entry,) = next(b["value"] for b in meta["draft_materials"] if b["type"] == 0)
    assert Path(entry["file_Path"]) == new / "silent.mp4"


# ------------------------------------------------------------------------- animation

def test_keyframes_structure_and_limits(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    seg = project.add_clip(media["image"], duration=4)
    project.set_keyframes(seg["id"], "scale", [(4, 1.3), (0, 1.0)])
    project.set_keyframes(seg["id"], "x", [(0, -1.5), (0.5, 0)])
    project = reopen(project)
    stored = {k["property_type"]: k for k in segments(project)[0]["common_keyframes"]}
    assert set(stored) == {"KFTypeScaleX", "KFTypePositionX"}
    points = [(f["time_offset"], f["values"]) for f in stored["KFTypeScaleX"]["keyframe_list"]]
    assert points == [(0, [1.0]), (4_000_000, [1.3])]
    assert set(stored["KFTypeScaleX"]["keyframe_list"][0]) == {
        "curveType", "graphID", "id", "left_control", "right_control", "string_value",
        "time_offset", "values"}, "the fields CapCut 8.3.0 keeps on a keyframe"
    assert project.describe()["tracks"][0]["segments"][0]["keyframes"] == ["x", "scale"]

    with pytest.raises(CapCutError, match="past the end"):
        project.set_keyframes(seg["id"], "scale", [(9, 1.0)])
    with pytest.raises(CapCutError, match="Opacity"):
        project.set_keyframes(seg["id"], "opacity", [(0, 2)])
    with pytest.raises(CapCutError, match="Volume"):
        project.set_keyframes(seg["id"], "volume", [(0, 1)])
    project.set_keyframes(seg["id"], "scale", [])
    assert [k["property_type"] for k in seg_of(project, seg)["common_keyframes"]] == ["KFTypePositionX"]


def seg_of(project: Project, seg: dict) -> dict:
    return project.find(seg["id"])[1]


def test_fade_makes_opacity_keyframes_and_an_audio_fade(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    seg = project.add_clip(media["video"])
    project.fade(seg["id"], fade_in=0.5, fade_out=1)
    project = reopen(project)
    stored = segments(project)[0]
    (opacity,) = stored["common_keyframes"]
    assert opacity["property_type"] == "KFTypeAlpha"
    assert [(f["time_offset"], f["values"][0]) for f in opacity["keyframe_list"]] == \
        [(0, 0.0), (500_000, 1.0), (3_000_000, 1.0), (4_000_000, 0.0)]
    fade = project.materials["audio_fades"][0]
    assert (fade["fade_in_duration"], fade["fade_out_duration"]) == (500_000, 1_000_000)
    with pytest.raises(CapCutError, match="add up"):
        project.fade(seg["id"], fade_in=3, fade_out=3)


def test_keyframes_follow_a_split(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    seg = project.add_clip(media["image"], duration=4)
    project.set_keyframes(seg["id"], "opacity", [(0, 0), (1, 1), (3, 1), (4, 0)])
    left, right = project.split(seg["id"], 2)
    offsets = lambda s: [f["time_offset"] for f in s["common_keyframes"][0]["keyframe_list"]]
    assert offsets(left) == [0, 1_000_000]
    assert offsets(right) == [1_000_000, 2_000_000]
    assert left["common_keyframes"][0]["id"] != right["common_keyframes"][0]["id"]


# ---------------------------------------------------------------------------- framing

def test_crop_is_per_clip_even_when_the_file_is_shared(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    first = project.add_clip(media["video"])
    second = project.add_clip(media["video"])
    project.crop(first["id"], left=0.1, top=0.2, right=0.3, bottom=0.0)
    project.add_clip(media["video"])
    project = reopen(project)
    one, two, three = segments(project)
    assert one["material_id"] != two["material_id"] == three["material_id"]
    cropped = project.find_material(one["material_id"])[1]["crop"]
    assert (cropped["upper_left_x"], cropped["upper_left_y"]) == (0.1, 0.2)
    assert (cropped["lower_right_x"], cropped["lower_right_y"]) == (0.7, 1.0)
    described = project.describe()["tracks"][0]["segments"]
    assert described[0]["crop"] == {"left": 0.1, "top": 0.2, "right": 0.3, "bottom": 0.0}
    assert "crop" not in described[1]
    with pytest.raises(CapCutError):
        project.crop(second["id"], left=0.6, right=0.5)


def test_background_blur_colour_and_reset(drafts, media):
    project = Project.create("Demo", 1080, 1920, 30)
    seg = project.add_clip(media["silent"])
    canvas = project._helper(seg, "canvases")
    project.set_background(seg["id"], blur=0.375)
    assert (canvas["type"], canvas["blur"]) == ("canvas_blur", 0.375)
    project.set_background(seg["id"], color="#102030")
    assert (canvas["type"], canvas["color"]) == ("canvas_color", "#102030FF")
    project.set_background(seg["id"])
    assert (canvas["type"], canvas["blur"], canvas["color"]) == ("canvas_color", 0.0, "")
    with pytest.raises(CapCutError):
        project.set_background(seg["id"], blur=0.5, color="#000000")


def test_track_volume(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    project.add_clip(media["video"])
    project.add_clip(media["image"], duration=1)
    project.add_clip(media["silent"])
    changed = project.set_track_volume("video", 0, 0)
    assert len(changed) == 2, "images have no sound to change"
    assert [s["volume"] for s in segments(project)] == [0.0, 1.0, 0.0]


# -------------------------------------------------------------------------- subtitles

def test_srt_round_trip(drafts, tmp_path):
    source = tmp_path / "in.srt"
    source.write_text("1\n00:00:01,000 --> 00:00:02,500\nUna\n\n2\n00:01:00,250 --> 00:01:02,000\n"
                      "Dalawa\nlinya\n", encoding="utf-8")
    cues = captions.parse_srt(str(source))
    assert cues == [(1.0, 2.5, "Una"), (60.25, 62.0, "Dalawa\nlinya")]
    project = Project.create("Demo", 1080, 1920, 30)
    for begin, end, text in cues:
        project.add_text(text, start=begin, duration=end - begin)
    out = tmp_path / "out" / "result.srt"
    assert captions.export_srt(project, str(out)) == 2
    assert captions.parse_srt(str(out)) == [(1.0, 2.5, "Una"), (60.267, 62.0, "Dalawa\nlinya")]


# ---------------------------------------------------------------------------- preview

def test_render_preview_draws_tracks_text_and_sound(drafts, media, tmp_path):
    project = Project.create("Demo", 1080, 1920, 30)
    first = project.add_clip(media["video"], volume=0.5)
    project.add_clip(media["silent"], speed=1.5)
    overlay = project.add_clip(media["image"], track=1, start=1, duration=2)
    project.set_transform(overlay["id"], x=0.5, y=0.5, scale=0.3, rotation=10, opacity=0.8)
    project.crop(first["id"], left=0.1)
    project.fade(first["id"], fade_in=0.5)
    project.add_audio(media["audio"], start=0, duration=6, volume=0.3, fade_out=1)
    project.add_text("Pamagat: 'sipi' 100%", start=0, duration=3, y=0.6, bold=True,
                     stroke_color="#000000", stroke_width=0.08, background_color="#202020",
                     background_opacity=0.6)
    project.save()

    out = tmp_path / "preview.mp4"
    report = render_preview(Project.open("Demo"), out, height=320)
    info = probe(str(out))
    assert info.kind == "video" and info.has_audio
    assert (info.width, info.height) == (180, 320)
    assert abs(info.duration_us / 1e6 - 6.0) < 0.2
    assert report["duration"] == 6.0 and "Not drawn" in report["note"]

    part = render_preview(Project.open("Demo"), tmp_path / "part.mp4", height=240, start=1, end=3,
                          include_text=False)
    assert part["duration"] == 2.0
    assert abs(probe(str(tmp_path / "part.mp4")).duration_us / 1e6 - 2.0) < 0.2


def test_render_preview_reports_missing_media(drafts, media, tmp_path):
    copy = tmp_path / "gone.mp4"
    shutil.copy(media["silent"], copy)
    project = Project.create("Demo", 1920, 1080, 30)
    project.add_clip(media["video"])
    project.add_clip(str(copy))
    project.save()
    copy.unlink()
    report = render_preview(Project.open("Demo"), tmp_path / "p.mp4", height=180)
    assert [Path(p).name for p in report["missing_media_skipped"]] == ["gone.mp4"]


def test_json_stays_compact_utf8(drafts):
    project = Project.create("Demo", 1080, 1920, 30)
    project.add_text("Ñandú 🌍", start=0, duration=1)
    project.save()
    raw = (drafts / "Demo" / "draft_content.json").read_text(encoding="utf-8")
    assert "Ñandú 🌍" in raw and ", " not in raw[:200]
    assert json.loads(raw)["duration"] == 1_000_000
    assert schema.text_of(Project.open("Demo").materials["texts"][0]) == "Ñandú 🌍"
