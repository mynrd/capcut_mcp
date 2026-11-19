"""Highlighted words in text, masks, chroma key, voice effects, search words, narrowed timelines."""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp import Client

from capcut_mcp import catalogue, library, schema
from capcut_mcp.draft import Project
from capcut_mcp.env import CapCutError
from capcut_mcp.server import mcp


def styles(project: Project, seg: dict) -> list[dict]:
    material = project.find_material(seg["material_id"])[1]
    return json.loads(material["content"])["styles"]


def colour(style: dict) -> str:
    return schema._fill_hex(style)


# ------------------------------------------------------------------- highlighted words

def test_highlight_colours_the_given_words_and_nothing_else(drafts):
    project = Project.create("Text", 1920, 1080, 30)
    seg = project.add_text("Buy the NEW phone today", start=0, duration=2, color="#FFFFFF",
                           bold=True, highlight=["new"], highlight_color="#FF0000")
    parts = styles(project, seg)
    assert [s["range"] for s in parts] == [[0, 8], [8, 11], [11, 23]]
    assert [colour(s) for s in parts] == ["#FFFFFF", "#FF0000", "#FFFFFF"]
    assert all(s["bold"] and s["size"] == parts[0]["size"] for s in parts)
    look = schema.text_style_of(project.find_material(seg["material_id"])[1])
    assert look["other_styles"] == [{"text": "NEW", "color": "#FF0000"}] and look["color"] == "#FFFFFF"


def test_highlight_matches_whole_words_and_counts_like_capcut(drafts):
    project = Project.create("Text", 1920, 1080, 30)
    seg = project.add_text("start the art", start=0, duration=2, highlight=["art"])
    assert [s["range"] for s in styles(project, seg)] == [[0, 10], [10, 13]]
    # An emoji is two UTF-16 units, which is how CapCut counts characters.
    seg = project.add_text("😀 big sale", start=3, duration=2, highlight=["sale"])
    assert [s["range"] for s in styles(project, seg)] == [[0, 7], [7, 11]]
    # Chinese has no spaces between words, so it matches anywhere.
    seg = project.add_text("今日特价商品", start=6, duration=2, highlight=["特价"])
    assert [s["range"] for s in styles(project, seg)] == [[0, 2], [2, 4], [4, 6]]
    assert colour(styles(project, seg)[1]) == schema.DEFAULT_HIGHLIGHT


def test_a_change_of_look_keeps_the_highlight_and_new_wording_resets_it(drafts):
    project = Project.create("Text", 1920, 1080, 30)
    seg = project.add_text("Buy the NEW phone", start=0, duration=2, highlight=["new"],
                           highlight_color="#FF0000")
    project.update_text(seg["id"], size=20, stroke_color="#000000", stroke_width=0.1, font_path="")
    parts = styles(project, seg)
    assert len(parts) == 3 and all(s["size"] == 20 and s["strokes"] for s in parts)
    assert colour(parts[1]) == "#FF0000"

    project.update_text(seg["id"], text="Something else")
    (only,) = styles(project, seg)
    assert only["range"] == [0, 14] and colour(only) == "#FFFFFF" and only["size"] == 20

    project.update_text(seg["id"], highlight=["else"])
    assert len(styles(project, seg)) == 2
    project.update_text(seg["id"], highlight=[])
    assert len(styles(project, seg)) == 1
    with pytest.raises(CapCutError, match="highlight_color needs"):
        project.update_text(seg["id"], highlight_color="#00FF00")


# ---------------------------------------------------------- masks, chroma, voice effects

def test_mask_is_written_where_capcut_keeps_it_and_replaced_not_stacked(drafts, media):
    project = Project.create("Mask", 1920, 1080, 30)
    seg = project.add_clip(media["silent"])  # 640x360
    library.set_mask(project, seg["id"], "circle", size=0.5, feather=20)
    (mask,) = project.materials["common_mask"]
    assert mask["id"] in seg["extra_material_refs"] and mask["resource_type"] == "circle"
    assert mask["config"]["height"] == 0.5 and mask["config"]["feather"] == 0.2
    assert mask["config"]["width"] == pytest.approx(0.5 * 360 / 640)  # round in pixels
    assert "masks" not in project.materials
    assert project._describe_segment(project.track("video", 0), seg)["mask"] == "circle"

    library.set_mask(project, seg["id"], "rectangle", size=0.4, width=0.8, round_corner=30, invert=True)
    (mask,) = project.materials["common_mask"]
    assert mask["resource_type"] == "rectangle" and mask["config"]["width"] == 0.8
    assert mask["config"]["invert"] is True and mask["config"]["roundCorner"] == 0.3
    assert project.detail(seg["id"])["mask"]["shape"] == "rectangle"

    left, right = project.split(seg["id"], 1)  # each half needs a mask of its own
    assert len(project.materials["common_mask"]) == 2
    assert project._helper(left, "common_mask")["id"] != project._helper(right, "common_mask")["id"]

    assert library.clear(project, left["id"], ["mask"])["cleared"] == ["mask"]
    assert len(project.materials["common_mask"]) == 1 and project._helper(left, "common_mask") is None
    with pytest.raises(CapCutError, match="rectangle mask only"):
        library.set_mask(project, right["id"], "circle", width=0.5)


def test_older_projects_also_get_the_mask_in_their_old_list(drafts, media):
    project = Project.create("Mask", 1920, 1080, 30)
    project.materials["masks"] = []  # a project from a CapCut build that still used this list
    seg = project.add_clip(media["silent"])
    library.set_mask(project, seg["id"], "heart")
    assert project.materials["masks"][0]["id"] == project.materials["common_mask"][0]["id"]
    library.set_mask(project, seg["id"], "star")
    assert len(project.materials["masks"]) == len(project.materials["common_mask"]) == 1
    assert seg["extra_material_refs"].count(project.materials["masks"][0]["id"]) == 1


def test_chroma_key(drafts, media):
    project = Project.create("Green", 1920, 1080, 30)
    project.add_clip(media["video"])
    seg = project.add_clip(media["silent"], track=1, start=0)
    library.set_chroma_key(project, seg["id"], "#00ff00", intensity=35, edge_smooth=10)
    library.set_chroma_key(project, seg["id"], "#00FF00", intensity=40)  # replaces the first
    (chroma,) = project.materials["chromas"]
    assert chroma["color"] == "#00ff00ff" and chroma["intensity_value"] == 0.4
    assert chroma["id"] in seg["extra_material_refs"]
    assert project._describe_segment(project.track("video", 1), seg)["chroma_key"] == "#00ff00"
    assert library.clear(project, seg["id"], ["chroma"])["cleared"] == ["chroma"]
    assert project.materials["chromas"] == []
    with pytest.raises(CapCutError, match="0 to 100"):
        library.set_chroma_key(project, seg["id"], "#00FF00", intensity=150)


def test_voice_effects(drafts, media):
    project = Project.create("Voice", 1920, 1080, 30)
    clip = project.add_clip(media["video"])
    voice = project.add_audio(media["audio"])
    library.set_audio_effect(project, voice["id"], "机器人")
    library.set_audio_effect(project, voice["id"], "Squirrel")  # one voice effect at a time
    (effect,) = project.materials["audio_effects"]
    assert effect["name"] == "Squirrel" and effect["id"] in voice["extra_material_refs"]
    assert project._describe_segment(project.track("audio", 0), voice)["audio_effects"] == ["Squirrel"]
    with pytest.raises(CapCutError, match="audio track"):
        library.set_audio_effect(project, clip["id"], "机器人")
    with pytest.raises(CapCutError, match="No audio_effect named"):
        library.set_audio_effect(project, voice["id"], "definitely not an effect")
    assert library.clear(project, voice["id"], ["audio_effects"])["cleared"] == ["audio_effects"]
    assert project.materials["audio_effects"] == []


# ------------------------------------------------------------------------------- search

def test_english_words_find_chinese_names():
    assert "叠化" in {e.name for e in catalogue.search("transition", "dissolve")}
    names = {e.name for e in catalogue.search("transition", "glitch, flash", limit=200)}
    assert any("故障" in n for n in names) and any("闪" in n for n in names)
    assert catalogue.search("transition", "White Flash")[0].name == "White Flash"  # what was typed wins
    assert catalogue.search("audio_effect", "robot")[0].name == "机器人"
    assert catalogue.search_terms("Dissolve, 推") == ["dissolve", "推", "叠化", "溶解"]


def test_the_users_own_items_are_searched_with_the_same_words(drafts, media):
    palette = Project.create("Palette", 1920, 1080, 30)
    first = palette.add_clip(media["video"])
    palette.add_clip(media["silent"])
    library.add_transition(palette, first["id"], "叠化")
    palette.save()
    found = library.listing("transition", search="dissolve")
    assert [i["name"] for i in found["from_your_projects"]] == ["叠化"]
    assert library.listing("transition", search="zzz")["from_your_projects"] == []
    # Used twice in a row: the second lookup comes from the remembered scan, and still sees new edits.
    palette = Project.open("Palette")
    second = sorted(palette.track("video", 0)["segments"], key=palette._start)[1]
    palette.add_clip(media["image"], duration=2)
    library.add_transition(palette, second["id"], "闪白")
    palette.save()
    assert {i["name"] for i in library.scan("transition")} == {"叠化", "闪白"}


# ------------------------------------------------------------------- through the server

def test_new_tools_through_the_server(drafts, media):
    async def scenario():
        async with Client(mcp) as client:
            async def call(tool, **arguments):
                result = await client.call_tool(tool, arguments)
                assert not result.is_error, result.content[0].text
                return result

            def data(result):
                return json.loads(result.content[0].text)

            await call("create_project", name="Tour")
            clips = data(await call("add_clips", project="Tour", clips=[
                {"path": media["video"]}, {"path": media["silent"]}]))["segments"]
            overlay = data(await call("add_clips", project="Tour", track=1, start=0,
                                      clips=[{"path": media["image"], "duration": 3}]))["segments"][0]
            await call("set_mask", project="Tour", segment=overlay["id"], shape="circle", size=0.6)
            await call("set_chroma_key", project="Tour", segment=overlay["id"], color="#00FF00")
            title = data(await call("add_text", project="Tour", text="Big NEW thing", start=0,
                                    duration=2, highlight=["new"]))
            await call("set_keyframes", project="Tour", segment=clips[0]["id"], property="scale",
                       keyframes=[{"time": 0, "value": 1}, {"time": 4, "value": 1.2}])
            music = data(await call("add_audio", project="Tour", path=media["audio"]))["segments"][0]
            await call("set_audio_effect", project="Tour", segment=music["id"], name="机器人")
            await call("checkpoint", project="Tour", name="first pass")

            narrowed = data(await call("get_timeline", project="Tour", type="video", start=4.5, end=6))
            detail = data(await call("get_timeline", project="Tour", segment=clips[0]["id"]))
            listed = data(await call("checkpoint", project="Tour", action="list"))
            voices = data(await call("list_library", kind="audio_effect", search="robot"))
            sheet = await call("extract_frames", path=media["video"], count=6, sheet=True)
            board = await call("preview_timeline", project="Tour", count=4, sheet=True)
            return title, narrowed, detail, listed, voices, sheet, board

    title, narrowed, detail, listed, voices, sheet, board = asyncio.run(scenario())
    assert title["highlighted"] == 1
    assert [(t["track"], len(t["segments"])) for t in narrowed["tracks"]] == [(0, 1), (1, 0)]
    assert narrowed["segments_per_track"] == {"video": [2, 1], "text": [1], "audio": [1]}
    assert detail["segment"]["keyframes"]["scale"] == [{"time": 0, "value": 1.0}, {"time": 4, "value": 1.2}]
    assert detail["segment"]["media"]["type"] == "video" and len(detail["segment"]["full_id"]) == 36
    assert [c["name"] for c in listed["checkpoints"]] == ["first pass"]
    assert voices["catalogue"]["matches"][0]["type"] == "voice"
    for result in (sheet, board):
        kinds = [block.type for block in result.content]
        assert kinds.count("image") == 1 and "Contact sheet" in result.content[kinds.index("text")].text
