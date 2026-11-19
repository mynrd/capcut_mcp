"""Regenerate docs/tools.md from the server's own tool definitions.

Usage:  uv run --no-sync python tools/gen_tool_reference.py

Run it after adding or changing a tool so the reference cannot drift from the code. A tool that
is not listed in GROUPS below makes the script fail, so new tools cannot be left undocumented.
"""

import asyncio
from pathlib import Path

from mcp import Client

from capcut_mcp.server import mcp

GROUPS = [
    ("Projects", ["list_projects", "create_project", "duplicate_project", "get_timeline",
                  "set_canvas", "relink_media", "check_project", "doctor"]),
    ("Undo, checkpoints and batches", ["undo", "redo", "checkpoint", "batch"]),
    ("Looking at and listening to media", ["probe_media", "extract_frames", "preview_timeline",
                                           "render_preview", "analyze_media", "transcribe_media",
                                           "list_fonts"]),
    ("Adding clips, audio and text", ["add_clips", "add_audio", "add_text", "add_captions",
                                      "auto_captions", "update_text", "export_captions"]),
    ("Cutting and arranging", ["trim_segment", "split_segment", "cut_range", "cut_silence",
                               "move_segment", "delete_segments", "arrange_track", "replace_media",
                               "freeze_frame"]),
    ("Speed and sound", ["set_speed", "set_volume", "set_track_volume", "duck_audio",
                         "set_audio_effect"]),
    ("Framing and animation", ["set_transform", "crop_segment", "set_background", "fade_segment",
                               "set_keyframes", "set_mask", "set_chroma_key", "clear_segment"]),
    ("CapCut library items", ["list_library", "add_transition", "add_animation", "add_effect",
                              "add_filter", "set_blend_mode", "copy_segment"]),
]


def type_of(schema: dict, definitions: dict) -> str:
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if "anyOf" in schema:
        options = [type_of(option, definitions) for option in schema["anyOf"]
                   if option.get("type") != "null"]
        return " or ".join(dict.fromkeys(options))
    if "enum" in schema:
        return " / ".join(f'"{value}"' for value in schema["enum"])
    kind = schema.get("type", "any")
    if kind == "array":
        return f"list of {type_of(schema.get('items', {}), definitions)}"
    return {"string": "text", "number": "number", "integer": "whole number",
            "boolean": "true/false"}.get(kind, kind)


def default_of(name: str, schema: dict, required: list[str]) -> str:
    if name in required:
        return "required"
    value = schema.get("default")
    if value is None:
        return "optional"
    return f"`{str(value).lower() if isinstance(value, bool) else value}`"


def table(properties: dict, required: list[str], definitions: dict) -> list[str]:
    lines = ["| Parameter | Type | Default | Notes |", "| --- | --- | --- | --- |"]
    for name, schema in properties.items():
        lines.append(f"| `{name}` | {type_of(schema, definitions)} | "
                     f"{default_of(name, schema, required)} | {schema.get('description', '')} |")
    return lines


async def main() -> None:
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    listed = [name for _, names in GROUPS for name in names]
    missing, unknown = set(tools) - set(listed), set(listed) - set(tools)
    if missing or unknown:
        raise SystemExit(f"GROUPS is out of date. Not listed: {sorted(missing)}; no such tool: {sorted(unknown)}")

    out = ["# Tool reference", "",
           "Generated from the server by `tools/gen_tool_reference.py`; do not edit by hand.", "",
           f"The server has {len(tools)} tools. Most take `project`, the name of a CapCut project as "
           "shown by `list_projects`. `segment` is a segment id from `get_timeline`; its first 8 "
           "characters are enough. All times are in seconds. See the [usage guide](usage.md) for "
           "how the pieces fit together.", "", "## Contents", ""]
    for title, names in GROUPS:
        out.append(f"- **{title}:** " + ", ".join(f"[`{name}`](#{name})" for name in names))
    out.append("")

    shared: dict[str, dict] = {}
    for title, names in GROUPS:
        out += [f"## {title}", ""]
        for name in names:
            tool = tools[name]
            schema = tool.input_schema
            definitions = schema.get("$defs", {})
            shared.update(definitions)
            description = "\n".join(line.strip() for line in (tool.description or "").strip().splitlines())
            read_only = tool.annotations and tool.annotations.read_only_hint
            out += [f"### {name}", "", description, ""]
            if read_only:
                out += ["*Does not change the project.*", ""]
            if schema.get("properties"):
                out += table(schema["properties"], schema.get("required", []), definitions) + [""]
            else:
                out += ["Takes no parameters.", ""]

    out += ["## Shared parameter types", ""]
    for name, schema in sorted(shared.items()):
        out += [f"### {name}", ""] + table(schema["properties"], schema.get("required", []), shared) + [""]

    target = Path(__file__).resolve().parent.parent / "docs" / "tools.md"
    target.parent.mkdir(exist_ok=True)
    target.write_text("\n".join(out), encoding="utf-8", newline="\n")  # LF on every platform
    print(f"wrote {target} ({len(tools)} tools)")


asyncio.run(main())
