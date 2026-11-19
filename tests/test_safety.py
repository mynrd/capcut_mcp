"""Keeping the user's work safe: undo, redo, checkpoints, one edit at a time, version checks."""

from __future__ import annotations

import asyncio
import json
import shutil
import threading

import pytest
from mcp import Client

from capcut_mcp import compat, draft, schema, server
from capcut_mcp.draft import Project
from capcut_mcp.env import CapCutError, data_dir
from capcut_mcp.server import mcp


def texts(name: str) -> list[str]:
    return sorted(schema.text_of(m) for m in Project.open(name).materials.get("texts", []))


def edit_in_capcut(name: str, change) -> None:
    """Change a project the way CapCut would: straight in its files, behind the server's back."""
    project = Project.open(name)
    change(project)
    project._normalise()
    for file in draft._content_files(project.folder):
        draft._write_json(file, project.content)


def run(scenario):
    async def wrapper():
        async with Client(mcp) as client:
            return await scenario(client)
    return asyncio.run(wrapper())


def payload(result):
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


# ------------------------------------------------------------------------- undo and redo

def test_undo_keeps_work_done_in_capcut_reachable(drafts):
    Project.create("Demo", 1920, 1080, 30)
    project = Project.open("Demo")
    project.add_text("from the server", start=0, duration=1)
    project.save()
    edit_in_capcut("Demo", lambda p: p.add_text("typed in CapCut", start=2, duration=1))

    result = Project.open("Demo").undo()
    assert texts("Demo") == []
    assert result["changed_in_capcut"] and "CapCut" in result["note"]
    assert [c["name"] for c in Project.open("Demo").checkpoints()] == [result["checkpoint"]]

    Project.open("Demo").redo()
    assert texts("Demo") == ["from the server", "typed in CapCut"]
    # ... and the checkpoint still holds it after a later edit has ended the chance to redo.
    Project.open("Demo").undo()
    project = Project.open("Demo")
    project.add_text("something else", start=0, duration=1)
    project.save()
    with pytest.raises(CapCutError, match="nothing to redo"):
        Project.open("Demo").redo()
    Project.open("Demo").restore_checkpoint(result["checkpoint"])
    assert texts("Demo") == ["from the server", "typed in CapCut"]


def test_undo_without_outside_changes_says_nothing_about_capcut(drafts):
    Project.create("Demo", 1920, 1080, 30)
    project = Project.open("Demo")
    project.add_text("one", start=0, duration=1)
    project.save()
    result = Project.open("Demo").undo()
    assert "changed_in_capcut" not in result and Project.open("Demo").checkpoints() == []


def test_history_of_a_deleted_project_is_not_restored_into_a_new_one(drafts):
    Project.create("Reuse", 1920, 1080, 30)
    for wording in ("old one", "old two"):
        project = Project.open("Reuse")
        project.add_text(wording, start=len(texts("Reuse")) * 2, duration=1)
        project.save()
    shutil.rmtree(drafts / "Reuse")  # the user deletes it in CapCut ...

    Project.create("Reuse", 1080, 1920, 30)  # ... and later makes a project with the same name
    project = Project.open("Reuse")
    project.add_text("new", start=0, duration=1)
    project.save()
    Project.open("Reuse").undo()
    assert texts("Reuse") == [] and Project.open("Reuse").content["canvas_config"]["width"] == 1080
    with pytest.raises(CapCutError, match="No earlier version"):
        Project.open("Reuse").undo()


def test_versions_stored_by_older_releases_can_still_be_undone(drafts):
    Project.create("Demo", 1920, 1080, 30)
    before = (drafts / "Demo" / "draft_content.json").read_bytes()
    project = Project.open("Demo")
    project.add_text("one", start=0, duration=1)
    project.save()
    history = data_dir() / "backups" / "Demo"
    for stored in history.glob("*.json.gz"):
        stored.unlink()
    (history / "20260101-000000-000000.json").write_bytes(before)  # the old, plain format
    Project.open("Demo").undo()
    assert texts("Demo") == []


def test_checkpoints(drafts):
    Project.create("Demo", 1920, 1080, 30)
    project = Project.open("Demo")
    project.add_text("title", start=0, duration=1)
    project.save()
    Project.open("Demo").save_checkpoint("before: captions?")
    for number in range(3):
        project = Project.open("Demo")
        project.add_text(f"caption {number}", start=2 + number, duration=1)
        project.save()
    assert [c["name"] for c in Project.open("Demo").checkpoints()] == ["before captions"]

    Project.open("Demo").restore_checkpoint("Before Captions")
    assert texts("Demo") == ["title"]
    Project.open("Demo").undo()  # restoring is itself one step that can be undone
    assert len(texts("Demo")) == 4
    with pytest.raises(CapCutError, match="no checkpoint named"):
        Project.open("Demo").restore_checkpoint("nope")


def test_backups_are_small_and_bounded(drafts, monkeypatch):
    monkeypatch.setattr(draft, "MAX_BACKUPS", 3)
    Project.create("Demo", 1920, 1080, 30)
    for number in range(5):
        project = Project.open("Demo")
        project.add_text(f"line {number}", start=number, duration=1)
        project.save()
    stored = list((data_dir() / "backups" / "Demo").glob("*.json.gz"))
    assert len(stored) == 3
    assert not list(drafts.rglob("*.mcp-tmp"))


# --------------------------------------------------------------------- one edit at a time

def test_edits_sent_in_parallel_all_land(drafts):
    Project.create("Race", 1920, 1080, 30)
    failures = []

    def edit(number: int) -> None:
        try:
            server.add_text(project="Race", text=f"line {number}", start=float(number), duration=0.5)
        except Exception as error:  # noqa: BLE001 - the test reports whatever went wrong
            failures.append(repr(error))

    threads = [threading.Thread(target=edit, args=(n,)) for n in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    assert texts("Race") == sorted(f"line {n}" for n in range(10))


def test_a_busy_project_is_reported_not_overwritten(drafts, monkeypatch):
    Project.create("Demo", 1920, 1080, 30)
    monkeypatch.setattr(draft, "LOCK_WAIT_SECONDS", 0.2)
    with draft.editing("Demo"):
        with pytest.raises(CapCutError, match="Another edit"):
            with draft.editing("Demo"):
                pass
    with draft.editing("Demo"):  # released again afterwards
        pass


def test_batch_is_one_save_and_one_undo_step(drafts, media):
    async def scenario(client):
        payload(await client.call_tool("create_project", {"name": "Batch"}))
        clips = payload(await client.call_tool("add_clips", {"project": "Batch", "clips": [
            {"path": media["video"]}, {"path": media["silent"]}, {"path": media["image"], "duration": 2}]}))
        ids = [s["id"] for s in clips["segments"]]
        done = payload(await client.call_tool("batch", {"project": "Batch", "edits": [
            {"tool": "trim_segment", "args": {"segment": ids[0], "source_end": 2}},
            {"tool": "add_text", "args": {"text": "one", "start": 0, "duration": 1}},
            {"tool": "add_text", "args": {"text": "two", "start": 1, "duration": 1}},
            {"tool": "fade_segment", "args": {"segment": ids[2], "fade_out": 1}},
        ]}))
        after = payload(await client.call_tool("get_timeline", {"project": "Batch"}))
        undone = payload(await client.call_tool("undo", {"project": "Batch"}))
        return done, after, undone

    done, after, undone = run(scenario)
    assert done["saved"] and done["edits"] == 4 and [s["tool"] for s in done["steps"]][0] == "trim_segment"
    assert after["duration"] == 7 and len(after["tracks"][-1]["segments"]) == 2
    assert undone["duration"] == 9 and "text" not in undone["segments_per_track"]


def test_a_failing_batch_saves_nothing_and_releases_the_project(drafts, media):
    async def scenario(client):
        payload(await client.call_tool("create_project", {"name": "Batch"}))
        failed = await client.call_tool("batch", {"project": "Batch", "edits": [
            {"tool": "add_text", "args": {"text": "kept?", "start": 0, "duration": 1}},
            {"tool": "add_text", "args": {"text": "clash", "start": 0.5, "duration": 1}},
        ]})
        refused = await client.call_tool("batch", {"project": "Batch", "edits": [
            {"tool": "undo", "args": {}}]})
        later = payload(await client.call_tool("add_text", {
            "project": "Batch", "text": "fine", "start": 0, "duration": 1}))
        return failed, refused, later

    failed, refused, later = run(scenario)
    assert failed.is_error and "Edit 2 of 2 (add_text) failed, so nothing was saved" in failed.content[0].text
    assert "overlap" in failed.content[0].text
    assert refused.is_error and "Not usable in a batch: undo" in refused.content[0].text
    assert texts("Batch") == ["fine"] and later["duration"] == 1


# ------------------------------------------------------------------------ CapCut versions

def test_version_levels():
    assert compat.status("8.3.0")[0] == "verified"
    assert compat.status("8.3.0.3497")[0] == "verified"
    assert compat.status("9.8.0")[0] == "unverified"
    assert compat.status("10.1.0")[0] == "untested"
    assert compat.status("")[0] == "unknown"
    assert compat.status("5.9.0", "lv")[0] == "unverified"


def test_projects_from_an_untested_capcut_are_not_written(drafts, monkeypatch):
    Project.create("Demo", 1920, 1080, 30)

    def saved_by(version):
        def change(project):
            project.content["last_modified_platform"] = {"app_version": version, "app_source": "cc"}
        edit_in_capcut("Demo", change)

    saved_by("9.8.0")
    project = Project.open("Demo")
    assert "9.8.0" in project.describe()["capcut_version_note"]
    project.add_text("fine on a nearby version", start=0, duration=1)
    project.save()

    saved_by("10.2.0")
    before = (drafts / "Demo" / "draft_content.json").read_bytes()
    project = Project.open("Demo")
    project.add_text("too new", start=2, duration=1)
    with pytest.raises(CapCutError, match="CAPCUT_MCP_ALLOW_UNTESTED"):
        project.save()
    assert (drafts / "Demo" / "draft_content.json").read_bytes() == before
    monkeypatch.setenv("CAPCUT_MCP_ALLOW_UNTESTED", "1")
    project.save()
    assert "too new" in texts("Demo")


def test_a_project_stored_as_draft_info_is_read_and_written_in_place(drafts):
    Project.create("Mac", 1920, 1080, 30)
    for file in list((drafts / "Mac").rglob("draft_content.json")):  # how macOS builds name it
        file.rename(file.with_name("draft_info.json"))
    project = Project.open("Mac")
    project.add_text("hello", start=0, duration=1)
    project.save()
    assert texts("Mac") == ["hello"]
    assert not list((drafts / "Mac").rglob("draft_content.json"))
    copies = list((drafts / "Mac").rglob("draft_info.json"))
    assert len(copies) == 2 and copies[0].read_bytes() == copies[1].read_bytes()


def test_capcuts_own_copy_of_the_timeline_is_kept_identical(drafts):
    Project.create("Demo", 1920, 1080, 30)
    folder = drafts / "Demo"
    (folder / "template-2.tmp").write_bytes((folder / "draft_content.json").read_bytes())
    (folder / "template-9.tmp").write_bytes(b"\x9f\x00something else entirely")
    project = Project.open("Demo")
    project.add_text("hello", start=0, duration=1)
    project.save()
    assert (folder / "template-2.tmp").read_bytes() == (folder / "draft_content.json").read_bytes()
    assert b"hello" in (folder / "template-2.tmp").read_bytes()
    assert (folder / "template-9.tmp").read_bytes() == b"\x9f\x00something else entirely"
    Project.open("Demo").undo()
    assert (folder / "template-2.tmp").read_bytes() == (folder / "draft_content.json").read_bytes()

    # CapCut 9.8 keeps the same kind of copy beside the timeline under Timelines/<id>/.
    (nested,) = (p for p in (folder / "Timelines").iterdir() if p.is_dir())
    (nested / "template-2.tmp").write_bytes((nested / "draft_content.json").read_bytes())
    project = Project.open("Demo")
    project.add_text("again", start=0, duration=1)
    project.save()
    assert b"again" in (nested / "template-2.tmp").read_bytes()
    assert (nested / "template-2.tmp").read_bytes() == (folder / "draft_content.json").read_bytes()


# ---------------------------------------------------- cutting through animated segments

def opacity(seg: dict) -> list[tuple[int, float]]:
    name = schema.KEYFRAME_PROPERTIES["opacity"]
    prop = next((k for k in seg.get("common_keyframes") or [] if k["property_type"] == name), None)
    return [(f["time_offset"], round(f["values"][0], 3)) for f in prop["keyframe_list"]] if prop else []


def test_a_split_inside_a_fade_carries_on_from_where_the_fade_was(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    seg = project.add_clip(media["image"], duration=4)
    project.fade(seg["id"], fade_in=2)
    left, right = project.split(seg["id"], 1)
    assert opacity(left) == [(0, 0.0), (1_000_000, 0.5)]
    assert opacity(right) == [(0, 0.5), (1_000_000, 1.0)]


def test_a_split_after_a_fade_leaves_the_second_part_plain(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    seg = project.add_clip(media["image"], duration=4)
    project.set_transform(seg["id"], opacity=0.8)
    project.fade(seg["id"], fade_in=1)
    left, right = project.split(seg["id"], 2)
    assert opacity(left) == [(0, 0.0), (1_000_000, 0.8)]
    assert opacity(right) == [] and right["clip"]["alpha"] == 0.8


def test_trimming_the_tail_keeps_a_zoom_at_the_size_it_reached(drafts, media):
    project = Project.create("Demo", 1920, 1080, 30)
    seg = project.add_clip(media["image"], duration=4)
    project.set_keyframes(seg["id"], "scale", [(0, 1.0), (4, 2.0)])
    project.trim(seg["id"], duration=1)
    scale = next(k for k in seg["common_keyframes"] if k["property_type"] == "KFTypeScaleX")
    assert [(f["time_offset"], f["values"][0]) for f in scale["keyframe_list"]] == [(0, 1.0), (1_000_000, 1.25)]


def animations(project: Project, seg: dict) -> dict:
    holder = project._helper(seg, "material_animations")
    return {a["type"]: (a["start"], a["duration"]) for a in holder["animations"]}


def test_animations_are_refitted_when_a_segment_changes_length(drafts):
    project = Project.create("Demo", 1920, 1080, 30)
    seg = project.add_text("Title", start=0, duration=4)
    holder = project._helper(seg, "material_animations")
    holder["animations"] = [
        {"type": "in", "name": "pop", "start": 0, "duration": 500_000},
        {"type": "loop", "name": "wave", "start": 500_000, "duration": 3_000_000},
        {"type": "out", "name": "fade", "start": 3_500_000, "duration": 500_000},
    ]
    project.trim(seg["id"], duration=2)
    assert animations(project, seg) == {"in": (0, 500_000), "loop": (500_000, 1_000_000),
                                        "out": (1_500_000, 500_000)}

    left, right = project.split(seg["id"], 1)
    assert animations(project, left) == {"in": (0, 500_000), "loop": (500_000, 500_000)}
    assert animations(project, right) == {"loop": (0, 500_000), "out": (500_000, 500_000)}

    project.trim(right["id"], duration=0.2)  # too short for its exit: the exit fills it
    assert animations(project, right)["out"] == (0, 200_000)
