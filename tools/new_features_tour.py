r"""Build a demo project for the features CapCut has not been seen to accept yet.

Usage:
    .venv\Scripts\python.exe tools/new_features_tour.py "<project name>" <video with sound> <image> <audio>

Masks, chroma key, voice effects and highlighted words are written the way other open-source
tools write them, and the automated tests check the files. What the tests cannot show is whether
CapCut draws them. This builds one project that uses each of them, with a caption on every
section saying what should be visible. Open it in CapCut, look, go back to the home screen, then
run `python tools/diff_resave.py "<project name>"` to see what CapCut changed, and record the
result in docs/capcut-format.md.

It talks to the server over stdio exactly as an assistant does. FFmpeg is used once, to make a
small green-screen picture for the chroma key section.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

SECTION = 4  # seconds per section


def green_screen_picture() -> str:
    home = Path(os.environ.get("CAPCUT_MCP_HOME")
                or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "capcut-mcp")
    out = home / "tour" / "green-screen.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x00FF00:size=640x360",
         "-vf", "drawbox=x=220:y=80:w=200:h=200:color=white:t=fill", "-frames:v", "1", str(out)],
        check=True, stdin=subprocess.DEVNULL)
    return str(out)


async def main(name: str, video: str, image: str, audio: str) -> None:
    server = StdioServerParameters(command=sys.executable, args=["-m", "capcut_mcp.server"],
                                   env=dict(os.environ))  # so folder overrides reach the server
    async with Client(server) as client:
        async def call(tool: str, **arguments) -> dict:
            result = await client.call_tool(tool, arguments)
            if result.is_error:
                raise SystemExit(f"FAILED {tool}: {result.content[0].text}")
            return json.loads(result.content[0].text)

        await call("create_project", name=name, width=1920, height=1080)
        sections = ["1. Round picture, top right (circle mask)",
                    "2. Rounded, soft-edged rectangle, top left (rectangle mask, feather)",
                    "3. Star-shaped picture in the middle (star mask)",
                    "4. White square with the green around it gone (chroma key)",
                    "5. Title with one yellow word; the sound is a robot voice",
                    "6. Picture fades in over 2s without a jump at 1s (split inside a fade)"]
        base = await call("add_clips", project=name, clips=[
            {"path": video, "source_end": SECTION} for _ in sections])
        starts = [seg["start"] for seg in base["segments"]]
        await call("set_track_volume", project=name, type="video", track=0, volume=0)

        async def overlay(path: str, at: float) -> str:
            added = await call("add_clips", project=name, track=1, start=at,
                               clips=[{"path": path, "duration": SECTION}])
            return added["segments"][0]["id"]

        one, two, three, four, six = [await overlay(p, starts[i]) for p, i in (
            (image, 0), (image, 1), (image, 2), (green_screen_picture(), 3), (image, 5))]
        done = await call("batch", project=name, edits=[
            {"tool": "set_transform", "args": {"segment": one, "x": 0.55, "y": 0.5, "scale": 0.4}},
            {"tool": "set_mask", "args": {"segment": one, "shape": "circle", "size": 0.8}},
            {"tool": "set_transform", "args": {"segment": two, "x": -0.55, "y": 0.5, "scale": 0.4}},
            {"tool": "set_mask", "args": {"segment": two, "shape": "rectangle", "size": 0.7, "width": 0.7,
                                          "round_corner": 40, "feather": 25}},
            {"tool": "set_transform", "args": {"segment": three, "scale": 0.5}},
            {"tool": "set_mask", "args": {"segment": three, "shape": "star", "size": 0.9}},
            {"tool": "set_transform", "args": {"segment": four, "scale": 0.6}},
            {"tool": "set_chroma_key", "args": {"segment": four, "color": "#00FF00", "intensity": 35}},
            {"tool": "set_transform", "args": {"segment": six, "scale": 0.5}},
            {"tool": "fade_segment", "args": {"segment": six, "fade_in": 2}},
            {"tool": "split_segment", "args": {"segment": six, "at": starts[5] + 1}},
        ])
        print(f"batch: {done['edits']} edits in one save")

        voice = await call("add_audio", project=name, path=audio, start=starts[4], duration=SECTION)
        await call("set_audio_effect", project=name, segment=voice["segments"][0]["id"], name="robot")
        await call("add_captions", project=name, captions=[
            {"text": text, "start": start, "end": start + SECTION}
            for text, start in zip(sections, starts, strict=True)])
        await call("add_text", project=name, track=1, text="One YELLOW word", start=starts[4],
                   duration=SECTION, y=0.3, size=16, bold=True, highlight=["yellow"])

        print(json.dumps(await call("check_project", project=name), ensure_ascii=False, indent=1))
        print(f"\nBuilt '{name}'. Open it in CapCut, compare each section with its caption, go back "
              f"to the home screen, then run:\n    python tools/diff_resave.py \"{name}\"")


if __name__ == "__main__":
    if len(sys.argv) != 5:
        raise SystemExit(__doc__)
    asyncio.run(main(*sys.argv[1:5]))
