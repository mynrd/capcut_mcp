"""Show what CapCut changed in a project after opening it, compared with what the server wrote.

Usage:  python tools/diff_resave.py "<project name>"

Workflow: edit a project through the server, open it in CapCut, close it again, then run this.
Every line is a place where CapCut's saved copy differs from the server's last write:
  +  a field CapCut added          -  a field CapCut removed          ~  a value CapCut changed
An empty report means CapCut accepted the file exactly as written. Repeated differences are
shown once. This is how the format notes in docs/capcut-format.md were established.
"""
import json
import os
import sys
from pathlib import Path

if len(sys.argv) != 2:
    raise SystemExit(__doc__)
name = sys.argv[1]
local = Path(os.environ.get("CAPCUT_MCP_HOME") or Path(os.environ["LOCALAPPDATA"]) / "capcut-mcp")
drafts = Path(os.environ.get("CAPCUT_DRAFTS_DIR")
              or Path(os.environ["LOCALAPPDATA"]) / "CapCut/User Data/Projects/com.lveditor.draft")
snapshot = local / "backups" / name / "last-written.snapshot"
current = drafts / name / "draft_content.json"
if not snapshot.is_file():
    raise SystemExit(f"No record of the server's last write for '{name}' ({snapshot}).")
ours = json.loads(snapshot.read_text("utf-8"))
theirs = json.loads(current.read_text("utf-8"))
if ours == theirs:
    raise SystemExit("Identical: CapCut has not saved this project since the server last wrote it.")


def show(value, limit=120):
    return json.dumps(value, ensure_ascii=False)[:limit]


def diff(a, b, path, out):
    if isinstance(a, dict) and isinstance(b, dict):
        for key in sorted(set(a) | set(b)):
            if key not in a:
                out.append(f"+ {path}.{key} = {show(b[key])}")
            elif key not in b:
                out.append(f"- {path}.{key} (was {show(a[key], 70)})")
            else:
                diff(a[key], b[key], f"{path}.{key}", out)
    elif a != b:
        out.append(f"~ {path}: {show(a, 70)} -> {show(b)}")


def by_id(items):
    return {i["id"]: i for i in items if isinstance(i, dict) and "id" in i}


seen = set()


def report(lines):
    for line in lines:
        key = line.split(":")[0].split("=")[0]
        if key not in seen:
            seen.add(key)
            print(line)


out = []
diff({k: v for k, v in ours.items() if k not in ("tracks", "materials")},
     {k: v for k, v in theirs.items() if k not in ("tracks", "materials")}, "draft", out)
print("== project level")
report(out)

print("\n== tracks and segments")
their_tracks = by_id(theirs["tracks"])
print("track order after:", [(t["type"], len(t["segments"]), f"flag={t['flag']}") for t in theirs["tracks"]])
for track in ours["tracks"]:
    other = their_tracks.get(track["id"])
    if not other:
        print(f"! {track['type']} track removed by CapCut")
        continue
    out = []
    diff({k: v for k, v in track.items() if k != "segments"},
         {k: v for k, v in other.items() if k != "segments"}, f"track[{track['type']}]", out)
    report(out)
    their_segments = by_id(other["segments"])
    for seg in track["segments"]:
        if seg["id"] not in their_segments:
            print(f"! {track['type']} segment {seg['id'][:8]} removed by CapCut")
            continue
        out = []
        diff(seg, their_segments[seg["id"]], f"{track['type']}-segment", out)
        report(out)

print("\n== materials")
for group in sorted(set(ours["materials"]) | set(theirs["materials"])):
    a, b = by_id(ours["materials"].get(group) or []), by_id(theirs["materials"].get(group) or [])
    if len(a) != len(b):
        print(f"# {group}: {len(a)} -> {len(b)} items")
    for material_id, item in a.items():
        if material_id in b:
            out = []
            diff(item, b[material_id], group, out)
            report(out)
        else:
            report([f"! {group}: an item was removed by CapCut"])
