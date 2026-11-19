r"""Build a demo project that exercises the animation and framing tools, through the MCP server.

Usage:
    .venv\Scripts\python.exe tools/feature_tour.py "<project name>" <five video files> <image> <audio>

It talks to the server over stdio exactly as Claude Code does, so it doubles as a worked example
of the tool calls and as a check that a CapCut version still accepts what the server writes:
open the resulting project in CapCut, close it, and compare with tools/diff_resave.py.
Each section of the result carries a caption saying what should be visible there.
"""

import asyncio
import os
import json
import sys

from mcp import Client, StdioServerParameters


async def main(name: str, videos: list[str], image: str, audio: str) -> None:
    server = StdioServerParameters(command=sys.executable, args=["-m", "capcut_mcp.server"],
                                   env=dict(os.environ))  # so folder overrides reach the server
    async with Client(server) as client:
        async def call(tool: str, **arguments):
            result = await client.call_tool(tool, arguments)
            if result.is_error:
                raise SystemExit(f"FAILED {tool}: {result.content[0].text}")
            return result

        def data(result) -> dict:
            return json.loads(result.content[0].text)

        await call("create_project", name=name, width=1080, height=1920)
        lengths = [3, 5, 5, 4, 6]
        added = data(await call("add_clips", project=name, clips=[
            {"path": path, "source_end": length} for path, length in zip(videos, lengths)]))
        one, two, three, four, five = (s["id"] for s in added["segments"])
        starts = [s["start"] for s in added["segments"]]

        # 1. fade in from black
        await call("fade_segment", project=name, segment=one, fade_in=1)
        # 2. slow zoom
        await call("set_keyframes", project=name, segment=two, property="scale",
                   keyframes=[{"time": 0, "value": 1.0}, {"time": 5, "value": 1.3}])
        # 3. crop to a band, fill the rest with blur
        await call("crop_segment", project=name, segment=three, top=0.25, bottom=0.25)
        await call("set_background", project=name, segment=three, blur=0.375)
        # 4. slide in from the left, fade out
        await call("set_keyframes", project=name, segment=four, property="x",
                   keyframes=[{"time": 0, "value": -1.5}, {"time": 1, "value": 0}])
        await call("fade_segment", project=name, segment=four, fade_out=1)
        # 5. freeze the picture for two seconds in the middle of the clip
        await call("freeze_frame", project=name, segment=five, at=starts[4] + 3, duration=2)

        # a spinning, fading picture-in-picture
        overlay = data(await call("add_clips", project=name, track=1, start=3,
                                  clips=[{"path": image, "duration": 5}]))["segments"][0]["id"]
        await call("set_transform", project=name, segment=overlay, x=0.6, y=0.6, scale=0.3)
        await call("set_keyframes", project=name, segment=overlay, property="rotation",
                   keyframes=[{"time": 0, "value": 0}, {"time": 5, "value": 360}])
        await call("fade_segment", project=name, segment=overlay, fade_in=1, fade_out=1)

        total = data(await call("get_timeline", project=name))["duration"]
        music = data(await call("add_audio", project=name, path=audio, start=0, duration=total,
                                volume=0.5, fade_out=2))["segments"][0]["id"]
        # duck the music between 4s and 8s
        await call("set_keyframes", project=name, segment=music, property="volume", keyframes=[
            {"time": 0, "value": 0.5}, {"time": 3, "value": 0.5}, {"time": 4, "value": 0.1},
            {"time": 8, "value": 0.1}, {"time": 9, "value": 0.5}])

        await call("add_captions", project=name, captions=[
            {"text": "1. Fades in from black", "start": starts[0], "end": starts[1]},
            {"text": "2. Slow zoom in. Small picture spins, top right", "start": starts[1], "end": starts[2]},
            {"text": "3. Cropped to a band, blurred background", "start": starts[2], "end": starts[3]},
            {"text": "4. Slides in from the left, then fades out", "start": starts[3], "end": starts[4]},
            {"text": "5. Plays 3s, freezes 2s, plays on", "start": starts[4], "end": total},
        ])
        title = data(await call("add_text", project=name, track=1, text="SPACED TITLE", start=0,
                                duration=3, y=0.6, size=12, color="#FFD400", bold=True,
                                letter_spacing=6, background_color="#000000",
                                background_opacity=0.5))["segments"][0]["id"]
        await call("fade_segment", project=name, segment=title, fade_in=0.5, fade_out=0.5)

        print(json.dumps(data(await call("get_timeline", project=name)), ensure_ascii=False, indent=1))
        preview = await call("render_preview", project=name, height=480, frames=6)
        for block in preview.content:
            if block.type == "text":
                print(block.text)


if __name__ == "__main__":
    if len(sys.argv) != 9:
        raise SystemExit(__doc__)
    asyncio.run(main(sys.argv[1], sys.argv[2:7], sys.argv[7], sys.argv[8]))
