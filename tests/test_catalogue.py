"""Library items taken from the built-in catalogue (the pycapcut package).

These tests check what is written to the project file. Whether CapCut then fetches and shows
an item can only be seen by opening the project in CapCut (tools/catalogue_tour.py).
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from mcp import Client

from capcut_mcp import catalogue, library, schema
from capcut_mcp.draft import Project
from capcut_mcp.env import CapCutError
from capcut_mcp.server import mcp
from test_project import reopen, segments

GUID = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9a-f]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")


@pytest.fixture
def effects_on(monkeypatch):
    """Catalogue effects are switched off by default (CapCut 8.3.0 did not load them).

    The code that writes them is kept and tested, so it can be switched back on once a form
    CapCut accepts is found.
    """
    monkeypatch.setattr(library, "CATALOGUE_EFFECTS_ENABLED", True)


def test_catalogue_is_loaded_and_searchable():
    totals = catalogue.counts()
    assert totals["transition"] > 500 and totals["effect"] > 500 and totals["font"] > 100
    assert {e.name for e in catalogue.search("transition", "叠化")} >= {"叠化"}
    flash = catalogue.search("transition", "white flash")
    assert flash[0].name == "White Flash" and not flash[0].pro
    assert catalogue.find("transition", "white_flash").name == "White Flash"
    assert catalogue.find("transition", "no such thing at all") is None
    assert any(e.pro for e in catalogue.search("transition", "", limit=2000))
    with pytest.raises(CapCutError, match="matches several"):
        catalogue.find("transition", "叠")


def test_catalogue_effects_are_off_until_capcut_accepts_them(drafts, media):
    assert "effect" not in library.listing()["catalogue"]["counts"]
    refused = library.listing("effect", search="betamax")["catalogue"]
    assert refused["available"] is False and "did not load" in refused["note"]
    assert all(m["kind"] != "effect" for m in library.listing(search="a")["catalogue"]["matches"])
    project = Project.create("Edit", 1920, 1080, 30)
    project.add_clip(media["video"])
    with pytest.raises(CapCutError, match="did not load"):
        library.add_effect(project, "Betamax", 0, 2)


def test_an_effect_the_user_already_used_is_copied_even_with_the_catalogue_off(drafts, media):
    palette = Project.create("Palette", 1920, 1080, 30)
    palette.add_clip(media["video"])
    used = {"id": schema.new_id(), "name": "Betamax", "type": "video_effect", "effect_id": "9",
            "resource_id": "9", "path": "C:/cache/effect/9/abc", "apply_target_type": 2,
            "adjust_params": [{"name": "effects_adjust_blur", "default_value": 0.65, "value": 0.65}]}
    palette.materials["video_effects"].append(used)
    track = palette.track("effect", 0, create=True)
    track["segments"].append(schema.item_segment(used["id"], 0, 2_000_000, 10000))
    palette.save()

    project = Project.create("Edit", 1920, 1080, 30)
    project.add_clip(media["video"])
    seg = library.add_effect(project, "betamax", 1, 2, params={"blur": 20})
    copied = project.find_material(seg["material_id"])[1]
    assert copied["path"] == "C:/cache/effect/9/abc" and copied["id"] != used["id"]
    assert copied["adjust_params"][0]["value"] == pytest.approx(0.2)
    assert seg["target_timerange"] == {"start": 1_000_000, "duration": 2_000_000}


def test_listing_combines_projects_and_catalogue(drafts, effects_on):
    overview = library.listing()
    assert overview["from_your_projects"] == []
    assert overview["catalogue"]["counts"]["filter"] > 100
    found = library.listing("effect", search="betamax")["catalogue"]["matches"]
    assert found == [{"kind": "effect", "name": "Betamax", "source": "catalogue",
                      "settings": ["filter", "blur", "sharpen", "distortion", "texture", "size"]}]
    animations = library.listing("animation", search="动感放大")["catalogue"]["matches"]
    assert {"for": "clips", "type": "in"}.items() <= animations[0].items()
    assert "catalogue" not in library.listing("sticker")
    assert "from_your_projects" not in library.listing("font", search="bangers")


def test_catalogue_transition_is_written_by_id_without_a_path(drafts, media):
    project = Project.create("Edit", 1920, 1080, 30)
    first = project.add_clip(media["video"])
    project.add_clip(media["silent"])
    library.add_transition(project, first["id"], "White Flash")
    project = reopen(project)
    (transition,) = project.materials["transitions"]
    assert (transition["name"], transition["type"]) == ("White Flash", "transition")
    assert transition["effect_id"] == transition["resource_id"] == "7345079500327096833"
    assert "path" not in transition, "CapCut resolves the asset from the ids"
    assert transition["duration"] == 400_000 and transition["is_overlap"] is True
    assert GUID.match(transition["id"])
    assert segments(project)[0]["extra_material_refs"].index(transition["id"]) == 2, \
        "third among the clip's helpers, where CapCut keeps it"

    library.add_transition(project, first["id"], "叠化", duration=9)  # longer than the clips
    (transition,) = project.materials["transitions"]
    assert transition["name"] == "叠化" and transition["duration"] == 3_000_000


def test_the_users_own_copy_wins_over_the_catalogue(drafts, media):
    palette = Project.create("Palette", 1920, 1080, 30)
    clip = palette.add_clip(media["video"])
    palette.add_clip(media["silent"])
    mine = {"id": schema.new_id(), "name": "White Flash", "type": "transition", "duration": 700_000,
            "effect_id": "local", "resource_id": "local", "path": "C:/cache/effect/local",
            "is_overlap": True}
    palette.materials["transitions"].append(mine)
    clip["extra_material_refs"].append(mine["id"])
    palette.save()

    project = Project.create("Edit", 1920, 1080, 30)
    first = project.add_clip(media["video"])
    project.add_clip(media["silent"])
    library.add_transition(project, first["id"], "white flash")
    assert project.materials["transitions"][0]["path"] == "C:/cache/effect/local"
    with pytest.raises(CapCutError, match="in project 'Palette'"):
        library.add_transition(project, first["id"], "叠化", from_project="Palette")


def test_clip_animations_from_the_catalogue(drafts, media):
    project = Project.create("Edit", 1920, 1080, 30)
    clip = project.add_clip(media["video"])  # 4s
    entrance = next(e for e in catalogue.search("animation", "", 500, ("clip-in",)) if not e.pro)
    leaving = next(e for e in catalogue.search("animation", "", 500, ("clip-out",)) if not e.pro)
    combo = next(e for e in catalogue.search("animation", "", 500, ("clip-combo",)) if not e.pro)

    library.add_animation(project, clip["id"], entrance.name, animation_type="in")
    library.add_animation(project, clip["id"], leaving.name, duration=1, animation_type="out")
    holder = project._helper(clip, "material_animations")
    by_type = {a["type"]: a for a in holder["animations"]}
    assert by_type["in"]["start"] == 0 and by_type["in"]["material_type"] == "video"
    assert (by_type["out"]["start"], by_type["out"]["duration"]) == (3_000_000, 1_000_000)
    assert by_type["in"]["id"] == by_type["in"]["resource_id"] and "path" not in by_type["in"]

    library.add_animation(project, clip["id"], combo.name, animation_type="combo")
    assert [(a["type"], a["start"], a["duration"]) for a in holder["animations"]] == \
        [("group", 0, 4_000_000)], "a combo replaces entrance and exit"
    library.add_animation(project, clip["id"], entrance.name, animation_type="in")
    assert [a["type"] for a in holder["animations"]] == ["in"]
    reopen(project)


def test_text_animations_and_loop_fills_the_middle(drafts):
    project = Project.create("Edit", 1080, 1920, 30)
    text = project.add_text("Hello", start=0, duration=4)
    pick = lambda group: next(e for e in catalogue.search("animation", "", 500, (group,)) if not e.pro)
    library.add_animation(project, text["id"], pick("text-in").name, duration=1, animation_type="in")
    library.add_animation(project, text["id"], pick("text-loop").name, animation_type="loop")
    library.add_animation(project, text["id"], pick("text-out").name, duration=0.5, animation_type="out")
    spans = {a["type"]: (a["start"], a["duration"], a["material_type"])
             for a in project._helper(text, "material_animations")["animations"]}
    assert spans == {"in": (0, 1_000_000, "sticker"), "loop": (1_000_000, 2_500_000, "sticker"),
                     "out": (3_500_000, 500_000, "sticker")}
    clip_only = next(e for e in catalogue.search("animation", "", 500, ("clip-combo",)))
    with pytest.raises(CapCutError, match="No animation named"):
        library.add_animation(project, text["id"], clip_only.name, animation_type="combo")


def test_effect_and_filter_get_their_own_tracks(drafts, media, effects_on):
    project = Project.create("Edit", 1920, 1080, 30)
    project.add_clip(media["video"])
    library.add_effect(project, "Betamax", 0.5, 2, params={"blur": 20, "size": 100})
    library.add_filter(project, "BW 2", 1, 3, intensity=50)
    project = reopen(project)

    (effect_seg,) = segments(project, "effect")
    assert effect_seg["target_timerange"] == {"start": 500_000, "duration": 2_000_000}
    assert effect_seg["render_index"] == 10000 and effect_seg["clip"] is None
    assert (effect_seg["enable_adjust"], effect_seg["enable_lut"]) == (False, False)
    assert effect_seg["source_timerange"] is None
    effect = project.materials["video_effects"][0]
    assert effect["id"] == effect_seg["material_id"] and GUID.match(effect["id"])
    assert (effect["name"], effect["type"], effect["apply_target_type"]) == ("Betamax", "video_effect", 2)
    values = {p["name"]: p["value"] for p in effect["adjust_params"]}
    assert values["effects_adjust_blur"] == pytest.approx(0.2)
    assert values["effects_adjust_size"] == pytest.approx(1.0)
    assert values["effects_adjust_filter"] == pytest.approx(0.7), "untouched settings keep defaults"

    (filter_seg,) = segments(project, "filter")
    assert filter_seg["render_index"] == 11000
    filter_material = project.materials["effects"][0]
    assert (filter_material["type"], filter_material["name"], filter_material["value"]) == \
        ("filter", "BW 2", 0.5)
    assert filter_material["apply_target_type"] == 0
    shown = {t["type"]: t["segments"] for t in project.describe()["tracks"]}
    assert shown["effect"][0]["name"] == "Betamax" and shown["filter"][0]["name"] == "BW 2"

    with pytest.raises(CapCutError, match="overlap"):
        library.add_effect(project, "Betamax", 1, 1)
    library.add_effect(project, "Betamax", 1, 1, track=1)
    assert len(project.tracks("effect")) == 2
    with pytest.raises(CapCutError, match="no setting 'loudness'"):
        library.add_effect(project, "Betamax", 5, 1, params={"loudness": 5})
    with pytest.raises(CapCutError, match="0 to 100"):
        library.add_filter(project, "BW 2", 6, 1, intensity=150)


def test_effect_tracks_can_be_cut_and_split_like_any_other(drafts, media, effects_on):
    project = Project.create("Edit", 1920, 1080, 30)
    project.add_clip(media["video"])
    library.add_effect(project, "Betamax", 0, 4)
    project.cut_range(1, 2, all_tracks=True)
    project = reopen(project)
    parts = segments(project, "effect")
    assert [(p["target_timerange"]["start"], p["target_timerange"]["duration"]) for p in parts] == \
        [(0, 1_000_000), (1_000_000, 2_000_000)]
    assert parts[0]["material_id"] != parts[1]["material_id"]
    assert len(project.materials["video_effects"]) == 2


def test_blend_mode(drafts, media):
    project = Project.create("Edit", 1920, 1080, 30)
    project.add_clip(media["video"])
    overlay = project.add_clip(media["image"], track=1, start=0, duration=2)
    library.set_blend_mode(project, overlay["id"], "screen")
    project = reopen(project)
    (blend,) = project.materials["effects"]
    assert (blend["type"], blend["name"]) == ("mix_mode", "滤色")
    assert segments(project, "video", 1)[0]["extra_material_refs"].index(blend["id"]) == 2
    assert "path" not in blend, "no CapCut installation here to point at"
    assert project.describe()["tracks"][1]["segments"][0]["blend"] == "screen"

    library.set_blend_mode(project, overlay["id"], "multiply")
    assert [m["name"] for m in project.materials["effects"]] == ["正片叠底"]
    assert library.clear(project, overlay["id"], ["blend"])["cleared"] == ["blend"]
    assert project.materials["effects"] == []
    with pytest.raises(CapCutError, match="Unknown blend mode"):
        library.set_blend_mode(project, overlay["id"], "sparkle")


def test_catalogue_font_on_text(drafts):
    project = Project.create("Edit", 1080, 1920, 30)
    seg = project.add_text("Hello", start=0, duration=2, font_ref=library.font_reference("bangers"))
    style = json.loads(project.materials["texts"][0]["content"])["styles"][0]
    assert style["font"]["id"].isdigit() and style["font"]["path"] == "D:"
    with pytest.raises(CapCutError, match="not both"):
        project.update_text(seg["id"], font_ref=style["font"], font_path="C:/Windows/Fonts/arial.ttf")
    with pytest.raises(CapCutError, match="No font named"):
        library.font_reference("definitely not a font")


def test_without_the_catalogue_the_users_own_items_still_work(drafts, media, monkeypatch):
    def missing():
        raise CapCutError("The item catalogue is not available")
    monkeypatch.setattr(catalogue, "_entries", missing)
    assert library.listing()["catalogue"]["available"] is False
    project = Project.create("Edit", 1920, 1080, 30)
    first = project.add_clip(media["video"])
    project.add_clip(media["silent"])
    with pytest.raises(CapCutError, match="not available"):
        library.add_transition(project, first["id"], "White Flash")


def test_catalogue_tools_through_the_server(drafts, media, effects_on):
    async def scenario():
        async with Client(mcp) as client:
            async def call(tool, **arguments):
                result = await client.call_tool(tool, arguments)
                assert not result.is_error, result.content[0].text
                return json.loads(result.content[0].text)
            await call("create_project", name="Tour")
            clips = await call("add_clips", project="Tour",
                               clips=[{"path": media["video"]}, {"path": media["silent"]}])
            first = clips["segments"][0]["id"]
            found = await call("list_library", kind="transition", search="white flash")
            name = found["catalogue"]["matches"][0]["name"]
            with_transition = await call("add_transition", project="Tour", segment=first, name=name)
            await call("add_effect", project="Tour", name="Betamax", start=0, duration=2,
                       settings={"blur": 10})
            await call("add_filter", project="Tour", name="BW 2", start=2, duration=2, intensity=70)
            await call("add_text", project="Tour", text="Title", start=0, duration=2, font="BANGERS")
            await call("set_blend_mode", project="Tour", segment=clips["segments"][1]["id"], mode="overlay")
            return with_transition, await call("get_timeline", project="Tour")

    with_transition, timeline = asyncio.run(scenario())
    assert with_transition["segments"][0]["transition_after"] == "White Flash"
    assert [t["type"] for t in timeline["tracks"]] == ["video", "effect", "filter", "text"]
    assert timeline["tracks"][0]["segments"][1]["blend"] == "overlay"
