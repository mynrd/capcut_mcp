"""Rebuild src/capcut_mcp/templates.json from projects CapCut itself wrote on this machine.

Usage:  python tools/extract_templates.py <out.json> <empty> <with video> <with audio> <with text>

The four names are CapCut project folders, all saved by the CapCut version the templates should
match: one empty, one whose first track holds a video clip, one with an audio clip, and one
containing a text item (open and save it in CapCut once, so CapCut writes the full text fields).

Everything machine- or project-specific (ids, paths, names, device hashes) is blanked, so the
output only describes the shape CapCut expects.
"""
import json
import os
import sys
from pathlib import Path

if len(sys.argv) != 6:
    sys.exit(__doc__)
drafts = Path(os.environ.get("CAPCUT_DRAFTS_DIR")
              or Path(os.environ["LOCALAPPDATA"]) / "CapCut/User Data/Projects/com.lveditor.draft")
out = Path(sys.argv[1])
empty_name, video_name, audio_name, text_name = sys.argv[2:6]


def load(rel):
    return json.loads((drafts / rel).read_text(encoding="utf-8"))


empty = load(f"{empty_name}/draft_content.json")
full = load(f"{video_name}/draft_content.json")
audio_only = load(f"{audio_name}/draft_content.json")
with_text = load(f"{text_name}/draft_content.json")

for key in ("platform", "last_modified_platform"):
    for field in ("device_id", "hard_disk_id", "mac_address"):
        empty[key][field] = ""
empty["id"] = ""

vseg = full["tracks"][0]["segments"][0]
vseg.update(id="", material_id="", extra_material_refs=[], volume=1.0, last_nonzero_volume=1.0,
            track_attribute=0, track_render_index=0, render_index=0)
vseg["source_timerange"] = {"duration": 0, "start": 0}
vseg["target_timerange"] = {"duration": 0, "start": 0}

aseg = [t for t in audio_only["tracks"] if t["type"] == "audio"][0]["segments"][0]
aseg.update(id="", material_id="", extra_material_refs=[], track_render_index=0)
aseg["source_timerange"] = {"duration": 0, "start": 0}
aseg["target_timerange"] = {"duration": 0, "start": 0}

vmat = full["materials"]["videos"][0]
vmat.update(id="", local_material_id="", material_name="", path="", duration=0, width=0, height=0,
            has_audio=False)

amat = audio_only["materials"]["audios"][0]
amat.update(id="", local_material_id="", name="", path="", duration=0, type="extract_music",
            category_name="local")


def aux(source, group):
    item = source["materials"][group][0]
    item["id"] = ""
    return item


track = full["tracks"][0]
track.update(id="", segments=[], attribute=0, flag=0, type="")

meta = load(f"{empty_name}/draft_meta_info.json")
meta.update(draft_fold_path="", draft_id="", draft_name="", draft_root_path="",
            tm_draft_create=0, tm_draft_modified=0, tm_duration=0)

meta_material = load(f"{video_name}/draft_meta_info.json")["draft_materials"][0]["value"][0]
meta_material.update(create_time=0, duration=0, extra_info="", file_Path="", height=0, width=0,
                     id="", import_time=0, import_time_ms=0, metetype="")
meta_material["roughcut_time_range"] = {"duration": 0, "start": 0}

project = load(f"{empty_name}/Timelines/project.json")
project.update(create_time=0, update_time=0, id="", main_timeline_id="")
project["timelines"][0].update(create_time=0, update_time=0, id="")

text = with_text["materials"]["texts"][0]
text.update(id="", content="", font_path="", font_size=15.0, text_color="#FFFFFF", border_color="",
            border_width=0.08, check_flag=7, type="text")

templates = {
    "empty_draft": empty,
    "text_material": text,
    "video_segment": vseg,
    "audio_segment": aseg,
    "video_material": vmat,
    "audio_material": amat,
    "speed": aux(full, "speeds"),
    "placeholder_info": aux(full, "placeholder_infos"),
    "canvas": aux(full, "canvases"),
    "sound_channel_mapping": aux(full, "sound_channel_mappings"),
    "material_color": aux(full, "material_colors"),
    "loudness": aux(full, "loudnesses"),
    "vocal_separation": aux(full, "vocal_separations"),
    "beats": aux(audio_only, "beats"),
    "track": track,
    "meta_info": meta,
    "meta_material": meta_material,
    "timelines_project": project,
    "agency_config": load(f"{empty_name}/draft_agency_config.json"),
    "timeline_layout": load(f"{empty_name}/timeline_layout.json"),
}
templates["timeline_layout"]["dockItems"][0]["timelineIds"] = []  # an id of the source project
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(templates, indent=1, ensure_ascii=False), encoding="utf-8", newline="\n")
print(f"wrote {out} ({out.stat().st_size} bytes, {len(templates)} templates)")
