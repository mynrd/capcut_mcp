"""Build a demo project that uses items from the built-in catalogue, through the MCP server.

Usage:
    .venv\\Scripts\\python.exe tools/catalogue_tour.py "<project name>" <six video files> <image>

Catalogue items are written with CapCut's ids and no local file, so the only way to know they
work is to open the result in CapCut and look: each section carries a caption saying what
should be visible. Items that CapCut cannot resolve show up as missing or simply do nothing.
Afterwards `python tools/diff_resave.py "<project name>"` shows what CapCut filled in: an item
it fetched gets a `path` into its cache, an item it could not fetch keeps an empty one.

Video effects are left out. The first run of this tour included two (Betamax and VCR) and
CapCut 8.3.0 fetched neither, so catalogue effects are switched off in the server.

The items used are a mix of free and Pro ones:
    transitions  White Flash (free), 叠化 = dissolve (free), Film Burn (Pro)
    filter       BW 2 (free)
    animations   渐显 = fade in, 渐隐 = fade out, 缩放 = zoom combo (all free)
    text         font BANGERS, 打字机 = typewriter in, 波浪 = wave loop, 渐隐 = fade out
    blend mode   screen
"""

import asyncio
import os
import json
import sys

from mcp import Client, StdioServerParameters


async def main(name: str, videos: list[str], image: str) -> None:
    server = StdioServerParameters(command=sys.executable, args=["-m", "capcut_mcp.server"],
                                   env=dict(os.environ))  # so folder overrides reach the server
    async with Client(server) as client:
        async def call(tool: str, **arguments) -> dict:
            result = await client.call_tool(tool, arguments)
            if result.is_error:
                raise SystemExit(f"FAILED {tool}: {result.content[0].text}")
            return json.loads(result.content[0].text)

        await call("create_project", name=name, width=1080, height=1920)
        added = await call("add_clips", project=name, clips=[
            {"path": path, "source_end": length} for path, length in zip(videos, [4, 5, 5, 5, 5, 5])])
        ids = [s["id"] for s in added["segments"]]
        starts = [s["start"] for s in added["segments"]]
        total = added["duration"]

        await call("add_transition", project=name, segment=ids[0], name="White Flash")
        await call("add_transition", project=name, segment=ids[1], name="叠化", duration=1)
        await call("add_transition", project=name, segment=ids[2], name="Film Burn")

        await call("add_filter", project=name, name="BW 2", start=starts[3] + 1, duration=3, intensity=80)

        await call("add_animation", project=name, segment=ids[4], name="渐显", type="in", duration=1)
        await call("add_animation", project=name, segment=ids[4], name="渐隐", type="out", duration=1)
        await call("add_animation", project=name, segment=ids[5], name="缩放", type="combo")

        overlay = (await call("add_clips", project=name, track=1, start=starts[2] + 1,
                              clips=[{"path": image, "duration": 3}]))["segments"][0]["id"]
        await call("set_transform", project=name, segment=overlay, scale=0.6)
        await call("set_blend_mode", project=name, segment=overlay, mode="screen")

        await call("add_captions", project=name, captions=[
            {"text": "1. Title: BANGERS font, types in, waves, fades. Then a white flash",
             "start": starts[0], "end": starts[1]},
            {"text": "2. Plain clip. Then a 1s dissolve",
             "start": starts[1], "end": starts[2]},
            {"text": "3. Picture blended on top (screen). Then Film Burn transition (Pro)",
             "start": starts[2], "end": starts[3]},
            {"text": "4. Black and white filter in the middle",
             "start": starts[3], "end": starts[4]},
            {"text": "5. Clip fades in and fades out", "start": starts[4], "end": starts[5]},
            {"text": "6. Zoom combo animation", "start": starts[5], "end": total},
        ])

        title = (await call("add_text", project=name, track=1, text="CATALOGUE TEST", start=0,
                            duration=4, y=0.55, size=14, color="#FFD400",
                            font="BANGERS"))["segments"][0]["id"]
        await call("add_animation", project=name, segment=title, name="打字机", type="in", duration=1)
        await call("add_animation", project=name, segment=title, name="渐隐", type="out", duration=0.5)
        await call("add_animation", project=name, segment=title, name="波浪", type="loop")

        timeline = await call("get_timeline", project=name)
        for track in timeline["tracks"]:
            print(track["type"], track["track"])
            for seg in track["segments"]:
                extras = {k: v for k, v in seg.items()
                          if k in ("name", "text", "transition_after", "animations", "blend")}
                print(f"   {seg['start']:6.2f}-{seg['end']:6.2f}  {extras}")


if __name__ == "__main__":
    if len(sys.argv) != 9:
        raise SystemExit(__doc__)
    asyncio.run(main(sys.argv[1], sys.argv[2:8], sys.argv[8]))
