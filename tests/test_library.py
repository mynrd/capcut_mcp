"""Reusing library items found in other projects.

No project with a real CapCut transition, animation, effect or sticker was available when this
was written, so the "CapCut-made" source project below is built by hand from what is publicly
known about the format. The copying itself does not depend on those details: it carries over
whatever the source project contains.
"""

from __future__ import annotations

import pytest

from capcut_mcp import library, schema
from capcut_mcp.draft import Project
from capcut_mcp.env import CapCutError
from test_project import reopen, segments


@pytest.fixture
def palette(drafts, media):
    """A project standing in for one the user made in CapCut with library items in it."""
    project = Project.create("Palette", 1920, 1080, 30)
    clip = project.add_clip(media["video"])
    project.add_clip(media["silent"])
    title = project.add_text("Styled", start=0, duration=2, size=20, color="#FF0000")
    materials = project.materials

    transition = {"category_id": "", "category_name": "", "duration": 500_000, "effect_id": "321",
                  "id": schema.new_id(), "is_overlap": True, "name": "Dissolve", "platform": "all",
                  "resource_id": "777", "path": "C:/cache/effect/321", "type": "transition"}
    materials["transitions"].append(transition)
    clip["extra_material_refs"].append(transition["id"])

    clip_animation = {"animations": [
        {"name": "Zoom In", "type": "in", "material_type": "video", "id": "11", "resource_id": "12",
         "start": 0, "duration": 600_000, "path": "C:/cache/effect/11"},
        {"name": "Fade Out", "type": "out", "material_type": "video", "id": "13", "resource_id": "14",
         "start": 3_400_000, "duration": 600_000, "path": "C:/cache/effect/13"},
    ], "id": schema.new_id(), "multi_language_current": "none", "type": "sticker_animation"}
    materials["material_animations"].append(clip_animation)
    clip["extra_material_refs"].append(clip_animation["id"])

    text_animation = project._helper(title, "material_animations")
    text_animation["animations"].append(
        {"name": "Typewriter", "type": "in", "material_type": "sticker", "id": "21",
         "resource_id": "22", "start": 0, "duration": 800_000, "path": "C:/cache/effect/21"})

    effect = {"id": schema.new_id(), "name": "Film Grain", "type": "video_effect", "effect_id": "55",
              "resource_id": "56", "path": "C:/cache/effect/55", "value": 1.0,
              "adjust_params": [{"name": "effects_adjust_intensity", "value": 0.8}]}
    materials["video_effects"].append(effect)
    effect_track = project.track("effect", 0, create=True)
    effect_track["segments"].append({
        "id": schema.new_id(), "material_id": effect["id"], "extra_material_refs": [],
        "target_timerange": {"start": 1_000_000, "duration": 2_000_000}, "source_timerange": None,
        "render_index": 11000, "track_render_index": 2, "clip": None, "common_keyframes": [],
        "keyframe_refs": [], "group_id": "x", "speed": 1.0, "volume": 1.0, "visible": True})
    project.save()
    return project


def test_library_lists_what_is_in_the_users_projects(palette):
    items = library.scan()
    found = {(i["kind"], i["name"]) for i in items}
    assert found == {("transition", "Dissolve"), ("animation", "Zoom In"), ("animation", "Fade Out"),
                     ("animation", "Typewriter"), ("effect", "Film Grain")}
    typewriter = next(i for i in items if i["name"] == "Typewriter")
    assert typewriter["for"] == "text" and typewriter["type"] == "in"
    assert [i["name"] for i in library.scan("transition")] == ["Dissolve"]
    assert library.scan(project="Somewhere else") == []


def test_transition_is_copied_verbatim_onto_the_first_clip(palette, media):
    project = Project.create("Edit", 1920, 1080, 30)
    first = project.add_clip(media["video"])
    last = project.add_clip(media["silent"])
    library.add_transition(project, first["id"], "dissolve", duration=1)
    project = reopen(project)

    (transition,) = project.materials["transitions"]
    assert transition["id"] in segments(project)[0]["extra_material_refs"]
    assert (transition["name"], transition["effect_id"], transition["resource_id"],
            transition["path"]) == ("Dissolve", "321", "777", "C:/cache/effect/321")
    assert transition["duration"] == 1_000_000
    assert project.describe()["tracks"][0]["segments"][0]["transition_after"] == "Dissolve"

    library.add_transition(project, first["id"], "Dissolve")  # replaces, does not stack
    assert len(project.materials["transitions"]) == 1
    with pytest.raises(CapCutError, match="no clip directly after"):
        library.add_transition(project, last["id"], "Dissolve")
    with pytest.raises(CapCutError, match="No transition named 'Zzyzx'"):
        library.add_transition(project, first["id"], "Zzyzx")
    assert library.clear(project, first["id"], ["transition", "crop"])["cleared"] == ["transition"]
    assert project.materials["transitions"] == []


def test_animations_are_placed_at_the_right_end(palette, media):
    project = Project.create("Edit", 1920, 1080, 30)
    clip = project.add_clip(media["video"])  # 4s
    text = project.add_text("Hello", start=0, duration=2)
    library.add_animation(project, clip["id"], "Zoom In")
    library.add_animation(project, clip["id"], "Fade Out", duration=1)
    library.add_animation(project, text["id"], "Typewriter")
    project = reopen(project)

    entries = {a["name"]: a for a in
               project._helper(segments(project)[0], "material_animations")["animations"]}
    assert (entries["Zoom In"]["start"], entries["Zoom In"]["duration"]) == (0, 600_000)
    assert (entries["Fade Out"]["start"], entries["Fade Out"]["duration"]) == (3_000_000, 1_000_000)
    assert entries["Zoom In"]["resource_id"] == "12"
    text_entries = project._helper(segments(project, "text")[0], "material_animations")["animations"]
    assert [a["name"] for a in text_entries] == ["Typewriter"]

    # The user's "Zoom In" is a clip animation and is not put on text. The catalogue's text
    # animation of that meaning (放大) is used instead; a name that means nothing is refused.
    library.add_animation(project, text["id"], "Zoom In")
    (on_text,) = project._helper(text, "material_animations")["animations"]
    assert on_text["name"] == "放大" and on_text["resource_id"] != "12"
    with pytest.raises(CapCutError, match="No animation named"):
        library.add_animation(project, text["id"], "No Such Thing")
    assert library.clear(project, clip["id"], ["animations"])["cleared"] == ["animations"]


def test_copy_segment_brings_an_effect_and_a_styled_text(palette, media):
    project = Project.create("Edit", 1920, 1080, 30)
    project.add_clip(media["video"])
    effect_id = next(i["segment"] for i in library.scan("effect"))
    library.copy_segment(project, "Palette", effect_id, start=0.5, duration=3)
    styled = segments(palette, "text")[0]["id"]
    library.copy_segment(project, "Palette", styled, start=1, text="New words")
    project = reopen(project)

    (effect_seg,) = segments(project, "effect")
    assert effect_seg["target_timerange"] == {"start": 500_000, "duration": 3_000_000}
    assert effect_seg["render_index"] == 11000 and effect_seg["group_id"] == ""
    effect = project.find_material(effect_seg["material_id"])[1]
    assert effect["name"] == "Film Grain" and effect["adjust_params"][0]["value"] == 0.8
    assert effect["id"] != palette.materials["video_effects"][0]["id"]

    (text_seg,) = segments(project, "text")
    text = project.find_material(text_seg["material_id"])[1]
    assert schema.text_of(text) == "New words" and text["text_color"] == "#FF0000"
    assert text_seg["target_timerange"]["start"] == 1_000_000
    assert len(project.materials["material_animations"]) == 1, "its animation holder came along"

    with pytest.raises(CapCutError, match="overlap"):
        library.copy_segment(project, "Palette", effect_id, start=1)
    with pytest.raises(CapCutError, match="keeps its length"):
        library.copy_segment(project, "Palette", segments(palette)[0]["id"], track=1, duration=1)
