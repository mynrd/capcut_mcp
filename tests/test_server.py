"""The tools as an MCP client sees them, over the real protocol (in memory)."""

from __future__ import annotations

import asyncio
import json

from mcp import Client

from capcut_mcp.server import mcp


def call(client: Client, tool: str, /, **arguments):
    return client.call_tool(tool, arguments)


def payload(result) -> dict | list:
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def run(scenario):
    async def wrapper():
        async with Client(mcp) as client:
            return await scenario(client)
    return asyncio.run(wrapper())


def test_tools_are_listed_with_descriptions(drafts):
    async def scenario(client):
        return (await client.list_tools()).tools
    tools = run(scenario)
    names = {tool.name for tool in tools}
    assert {"list_projects", "create_project", "get_timeline", "add_clips", "add_audio", "add_text",
            "add_captions", "trim_segment", "split_segment", "move_segment", "delete_segments",
            "arrange_track", "set_speed", "set_volume", "set_transform", "undo", "probe_media",
            "extract_frames", "preview_timeline", "render_preview", "cut_range", "replace_media",
            "freeze_frame", "fade_segment", "set_keyframes", "crop_segment", "set_background",
            "list_library", "add_transition", "add_animation", "copy_segment", "clear_segment",
            "relink_media", "export_captions", "list_fonts", "set_track_volume"} <= names
    assert {"batch", "redo", "checkpoint", "doctor", "check_project", "analyze_media",
            "transcribe_media", "auto_captions", "cut_silence", "duck_audio", "set_audio_effect",
            "set_mask", "set_chroma_key"} <= names
    assert all(tool.description for tool in tools)
    read_only = {tool.name for tool in tools if tool.annotations and tool.annotations.read_only_hint}
    assert {"get_timeline", "list_library", "render_preview"} <= read_only
    assert not {"delete_segments", "add_clips", "undo"} & read_only


def test_an_edit_session_end_to_end(drafts, media, tmp_path):
    srt = tmp_path / "subs.srt"
    srt.write_text("1\n00:00:00,500 --> 00:00:02,000\nUna\n\n2\n00:00:02,000 --> 00:00:03,500\n"
                   "<i>Pangalawa</i>\nlinya\n", encoding="utf-8")

    async def scenario(client):
        payload(await call(client, "create_project", name="Session", width=1080, height=1920))
        added = payload(await call(client, "add_clips", project="Session", clips=[
            {"path": media["video"]}, {"path": media["silent"], "source_end": 2}]))
        assert [s["start"] for s in added["segments"]] == [0, 4]

        first = added["segments"][0]["id"]
        payload(await call(client, "trim_segment", project="Session", segment=first, source_end=3))
        payload(await call(client, "add_audio", project="Session", path=media["audio"], start=0,
                           duration=5, volume=0.5, fade_out=1))
        payload(await call(client, "add_text", project="Session", text="Pamagat", start=0,
                           duration=2, y=0.7, size=14))
        captions = payload(await call(client, "add_captions", project="Session",
                                      srt_path=str(srt), track=1))
        assert captions["captions_added"] == 2

        timeline = payload(await call(client, "get_timeline", project="Session"))
        frames = await call(client, "preview_timeline", project="Session", count=2)
        rejected = await call(client, "add_clips", project="Session",
                              clips=[{"path": media["image"]}], start=1)
        undone = payload(await call(client, "undo", project="Session"))
        after_undo = payload(await call(client, "get_timeline", project="Session", type="text"))
        return timeline, frames, rejected, undone, after_undo

    timeline, frames, rejected, undone, after_undo = run(scenario)

    assert timeline["duration"] == 5 and timeline["canvas"] == "320x568"
    by_type = {(t["type"], t["track"]): t["segments"] for t in timeline["tracks"]}
    assert [s["end"] for s in by_type[("video", 0)]] == [3, 5]
    assert by_type[("audio", 0)][0]["volume"] == 0.5
    assert by_type[("text", 0)][0]["text"] == "Pamagat"
    assert [s["text"] for s in by_type[("text", 1)]] == ["Una", "Pangalawa\nlinya"]

    assert [c.type for c in frames.content].count("image") == 2
    assert rejected.is_error and "overlap" in rejected.content[0].text
    # undo removed the captions, the last successful change, and says what is left
    assert undone["segments_per_track"]["text"] == [1] and undone["duration"] == 5
    assert [t["track"] for t in after_undo["tracks"]] == [0]
    assert after_undo["segments_per_track"]["video"] == [2]  # a narrowed view still counts every track
