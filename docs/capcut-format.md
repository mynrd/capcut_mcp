# CapCut project format: findings

What was learned about how CapCut desktop stores a project, and how each point was established.
CapCut does not document this format; everything here comes from inspecting files.

## Sources and method

- **CapCut 8.3.0** on Windows 11 (`CapCut\Apps\8.3.0.3497\CapCut.exe`), inspected on 2026-11-17.
  A `9.8.0` folder already existed under `Apps`, but 8.3.0 was the version that ran and wrote
  `app_version: 8.3.0` into projects. On 2026-11-18 CapCut switched itself to 9.8.0; see
  [Other CapCut versions](#other-capcut-versions).
- **Projects CapCut itself wrote** on this machine: an empty one, one with a video and library
  music, one with only audio, one with a still image.
- **The re-save comparison.** The server writes a project, the user opens it in CapCut and
  closes it, and `tools/diff_resave.py` lists every field CapCut added, removed or changed.
  This is the main way anything here was confirmed, because CapCut rewrites the whole file when
  it saves and corrects what it does not like.

Each finding is marked:

- **Confirmed**: seen in a file CapCut wrote, or survived the re-save comparison.
- **Assumed**: taken from public descriptions of the format by other open-source projects. The
  server writes it, but it has not yet been through a re-save comparison here.

## Where projects live

```
%LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft\
    root_meta_info.json          CapCut's index of all projects
    <project name>\              one folder per project
```

**Confirmed.** A new folder dropped here is picked up by a running CapCut within about a second:
CapCut adds it to `root_meta_info.json` itself and shows it on the home screen. The server never
writes `root_meta_info.json`.

**Confirmed.** CapCut takes the card's name, duration and size from the project's
`draft_meta_info.json` at the moment it first sees the folder, and does not refresh the card
until the project is opened. A project created empty and filled afterwards shows `0.0B | 00:00`
until then.

## Files in a project folder

A project freshly created by CapCut 8.3.0 contains:

| File | Purpose |
| --- | --- |
| `draft_content.json` | The timeline: canvas, tracks, segments, materials |
| `draft_meta_info.json` | Name, id, folder path, timestamps, duration, media bin |
| `Timelines\project.json` | List of timelines and which one is the main one |
| `Timelines\<timeline id>\draft_content.json` | A second copy of the timeline |
| `timeline_layout.json` | Which timelines are docked in the editor |
| `draft_agency_config.json`, `draft_biz_config.json`, `draft_settings` | Small settings files |
| `.locked` | Empty file, present only while the project is open in the editor |

The server writes exactly this set when creating a project (without `.locked`). On first open
CapCut adds the rest itself: `draft_cover.jpg`, `draft_virtual_store.json`, `key_value.json`,
`attachment_*.json`, `draft_content.json.bak`, `template*.tmp` and several empty folders.
**Confirmed** by the re-save of the test projects.

### The timeline copies

**Confirmed.** `draft_content.json` at the root and under `Timelines\<id>\` are byte-identical
after CapCut saves, and the timeline's `id` equals the folder name under `Timelines` and
`main_timeline_id` in `project.json`. The server reads whichever copy is newer and writes both.
Which one CapCut prefers when they differ was not tested here; other tools report that it
depends on the version (see [Other CapCut versions](#other-capcut-versions)).

**Confirmed.** `template-2.tmp` in the project folder is a third copy. In two projects saved by
CapCut 8.3.0 it had the same size, the same hash and the same time as `draft_content.json`
(checked on 2026-11-18). The server does not create it, but when it exists and holds this
project's timeline as plain JSON, it is written with the same bytes, so no copy is left stale.
`draft_content.json.bak` holds the save before and is left alone.

**Assumed.** macOS builds name the file `draft_info.json`. The server reads and writes
whichever of the two names a project has, and writes both when it creates a project on a Mac.
This has not been tried on a Mac.

### The lock

**Confirmed.** `.locked` appears when a project is opened in the editor and disappears when it is
closed. The server treats a project as open when `.locked` exists **and** a `CapCut.exe` process
is running, so a lock left behind by a crash does not block editing.

## The timeline file

`draft_content.json` is one compact UTF-8 JSON object. The parts the server works with:

| Key | Meaning |
| --- | --- |
| `canvas_config` | `width`, `height`, `ratio` (`"original"` or e.g. `"9:16"`) |
| `fps` | Frame rate, a float |
| `duration` | Length of the timeline in microseconds |
| `tracks` | Ordered list of tracks |
| `materials` | About 60 named lists holding everything segments refer to |
| `platform`, `last_modified_platform` | App version and hashed device identifiers |
| `version`, `new_version` | `360000` and `"163.0.0"` for CapCut 8.3.0 |

### Time

**Confirmed.** All times are integer **microseconds**. A frame at 30 fps is 33333.

**Confirmed.** CapCut cuts fractions off rather than rounding. A 185-frame clip is `6166666`,
not `6166667`. ffprobe prints container durations rounded to six decimals, so the server takes
the video stream's exact tick count instead.

**Confirmed.** A clip whose length is not on the frame grid gets changed when CapCut opens the
project. A clip of 11.3667 s at 1.5x was written as 7.5778 s and came back as 7.6 s (228
frames): CapCut rounds a sped-up clip **up** to a whole frame, and because the main track is
magnetic, every later clip moved with it. The server now computes sped-up lengths the same way.

### Tracks

```json
{"type": "video", "flag": 0, "attribute": 0, "id": "...", "name": "", "is_default_name": true, "segments": []}
```

- **Confirmed.** `type` is `video`, `audio` or `text` for the tracks the server creates. CapCut
  also uses `effect`, `filter`, `sticker` and `adjust`.
- **Confirmed.** The first video track is the main track with `flag: 0`. Every other video track
  has `flag: 2`; the server first wrote 0 there and CapCut changed it. Audio and text tracks
  keep `flag: 0`.
- **Confirmed.** CapCut removes tracks that have no segments when it saves.
- **Observed once.** A video track whose clips were muted had `attribute: 1`, alongside
  `config.video_mute: true`. The server does not use this; it mutes by setting each clip's volume.
- **Confirmed.** `config.maintrack_adsorb: true` is the magnetic main track.

### Segments

Every item on a track is a segment. The fields that matter:

| Field | Meaning |
| --- | --- |
| `id` | GUID. CapCut writes them upper case with the third group in lower case |
| `material_id` | The main material: a video, audio or text entry |
| `extra_material_refs` | Ids of helper materials belonging to this segment |
| `target_timerange` | `{start, duration}` on the timeline |
| `source_timerange` | `{start, duration}` in the media file; `null` for text |
| `speed`, `volume` | Playback speed and loudness (1.0 is unchanged) |
| `clip` | `{alpha, flip{horizontal,vertical}, rotation, scale{x,y}, transform{x,y}}`; `null` for audio |
| `render_index` | Drawing order |
| `track_render_index` | Position of the segment's track in `tracks` |
| `common_keyframes` | Keyframe animation |

- **Confirmed.** `track_render_index` is the track's index in the `tracks` list.
- **Confirmed.** `render_index` is 0 on the main track and 1 on the first overlay track; text
  written with 14000 was kept.
- **Confirmed.** `clip.transform` uses half the frame as its unit: `x: 1` is the right edge and
  `y: 1` the top edge. The overlay written at `x: 0.6, y: 0.65` was kept and appeared top right.
- **Confirmed.** `last_nonzero_volume` is not "the current volume". The server wrote 0.25 and
  CapCut reset it to 1.0, so the server now leaves it alone.

### Helper materials

Each segment owns a set of small materials, listed in `extra_material_refs`. CapCut 8.3.0 wrote:

| Segment | Helpers, in order |
| --- | --- |
| Video clip | `speeds`, `placeholder_infos`, `canvases`, `sound_channel_mappings`, `material_colors`, `loudnesses`, `vocal_separations` |
| Still image | The same, without `loudnesses` in the sample seen |
| Audio clip | `speeds`, `placeholder_infos`, `beats`, `sound_channel_mappings`, `vocal_separations` |
| Text | `material_animations` (an empty animation holder) |

- **Confirmed.** The speed helper's `speed` must match the segment's `speed`.
- **Confirmed.** An audio fade is an `audio_fades` entry
  (`fade_in_duration`, `fade_out_duration`, `fade_type: 0`, `type: "audio_fade"`). CapCut keeps
  its reference in third position, after `speeds` and `placeholder_infos`; the server wrote it
  last and CapCut moved it.
- **Confirmed.** Splitting a clip keeps one media material for both halves but each half needs
  its own helpers.

### Media materials

- **Confirmed.** Video files: `materials.videos` with `type: "video"`, `path` using forward
  slashes, `duration`, `width`, `height`, `has_audio`.
- **Confirmed.** Still images: also in `materials.videos`, with `type: "photo"` and a nominal
  `duration` of three hours (`10800000000`). The segment's own length decides how long it shows.
- **Confirmed.** Audio files: `materials.audios` with `type: "extract_music"` and
  `category_name: "local"`. The sound of a video used as audio is `type: "video_original_sound"`.
- **Confirmed.** CapCut re-measures audio length itself: an MP3 that ffprobe reports as 85.68 s
  came back as 85.70 s. The value the server writes only needs to be close.
- **Confirmed.** Several clips of one file share a single material.

### Text

A text segment refers to a `materials.texts` entry with 125 fields in CapCut 8.3.0. The server
first wrote 87 of them by hand; CapCut accepted that and filled in the rest, and the template is
now the full structure CapCut wrote.

The wording and its styling live in `content`, a JSON document stored **as a string**:

```json
{"styles": [{"fill": {"alpha": 1.0, "content": {"render_type": "solid", "solid": {"alpha": 1.0, "color": [1.0, 0.83, 0.0]}}},
             "range": [0, 22], "size": 14.0, "bold": true,
             "strokes": [{"alpha": 1.0, "width": 0.08, "content": {"render_type": "solid", "solid": {"alpha": 1.0, "color": [0.0, 0.0, 0.0]}}}]}],
 "text": "Welcome to the Channel"}
```

- **Confirmed.** Colours are three floats from 0 to 1. `range` covers the styled characters.
- **Assumed.** `range` counts UTF-16 units, so an emoji counts as two. Text with an emoji was
  accepted, which is consistent with this but does not prove it.
- **Confirmed.** `check_flag` starts at 7; adding 8 turns the outline on. The outline also sets
  `border_color` and `border_width` on the material.
- **Assumed.** `check_flag` bit 16 is the background and bit 32 the shadow.
- **Confirmed.** `type: "text"` and `type: "subtitle"` are both accepted. The server uses
  `subtitle` for captions.
- **Confirmed.** An empty `font_path` is filled in by CapCut with its default,
  `<app>\Resources\Font\SystemFont\en.ttf`.
- **Confirmed.** Text position is `clip.transform` on the segment, like any clip.
- **Assumed.** `letter_spacing` and `line_spacing` are the panel values times 0.05, with line
  spacing on top of a 0.02 base.

## The media bin

`draft_meta_info.json` holds `draft_materials`, a list of bins by `type`. Type 0 is imported
media. **Confirmed:** video and image entries written by the server were kept. Audio entries
written there with `metetype: "music"` were **removed** by CapCut, so the server no longer adds
them; audio clips work on the timeline without a bin entry.

## Animation, framing and library items

These were first written from public descriptions of the format, then checked with two test
projects (`tools/feature_tour.py` and `tools/catalogue_tour.py`) that were opened in CapCut
8.3.0 on 2026-11-18 and compared afterwards.

| Feature | What is written | Result |
| --- | --- | --- |
| Keyframes | `common_keyframes` on the segment: one entry per property with `property_type` (`KFTypePositionX`, `KFTypePositionY`, `KFTypeScaleX`, `KFTypeRotation`, `KFTypeAlpha`, `KFTypeVolume`) and a `keyframe_list` of `{time_offset, values: [v], curveType: "Line"}`. `time_offset` counts from the segment's start | **Confirmed.** All eight animated properties in the test came back with identical points. CapCut added one field to each keyframe, `string_value: ""`, which is now written |
| Uniform scale animation | Only `KFTypeScaleX`, with `uniform_scale.on: true` | **Confirmed**, kept as written |
| Fades of picture and text | Opacity keyframes, as above | **Confirmed**, kept as written |
| Crop | The eight corner values in the video material's `crop`, 0 to 1. A cropped clip gets its own copy of the material | **Confirmed**, kept as written |
| Background fill | The segment's `canvases` helper with `type: "canvas_blur"` and `blur` (CapCut's presets are 0.0625, 0.375, 0.75, 1) | **Confirmed** for blur. A flat colour (`type: "canvas_color"`, `color: "#RRGGBBAA"`) was not in the test and stays assumed |
| Text spacing and background | `letter_spacing` as the panel value times 0.05; `background_style: 1` with colour and alpha, and `check_flag` bit 16 | **Confirmed**, kept as written |
| Transition | A `materials.transitions` entry referenced by the clip **before** the cut | **Confirmed.** CapCut keeps the reference third among the clip's helpers and moved it there; it is now written there |
| Animation | Entries in the `animations` list of the segment's `material_animations` helper, with `type` `in`, `out`, `loop` or `group`, `start` and `duration` | **Confirmed** for all six kinds (clip in, out and combo; text in, out and loop) |
| Filter track | A track of `type: "filter"` whose segments point at a `materials.effects` entry with `type: "filter"` and `value` as intensity, 0 to 1 | **Confirmed.** CapCut changed `apply_target_type` from 2 to 0 and switched `enable_adjust` and `enable_lut` off on the segment; both are now written that way |
| Library font | `{"id": <resource id>, "path": "D:"}` in the text style's `font` slot | **Confirmed.** CapCut replaced the placeholder path with the downloaded font file |
| Blend mode | A `materials.effects` entry with `type: "mix_mode"`, referenced from the clip, third among its helpers | **Kept as written**, but not proven to render: CapCut left its path empty. Blend modes ship with CapCut, so the server now points at the copy on disk (`User Data\Resources\MixMode`) |
| Effect track | A track of `type: "effect"` whose segments point at a `materials.video_effects` entry | **Structure kept, effect not loaded.** See below |
| Mask | A `materials.common_mask` entry referenced from the clip, with `config` holding `centerX`, `centerY`, `width`, `height`, `rotation`, `feather`, `invert`, `roundCorner`, `aspectRatio`, `expansion`. The older `materials.masks` list gets the same entry under the same id only when the project already has that list | **Assumed.** A project saved by CapCut 8.3.0 has a `common_mask` list and no `masks` list, which settles where it goes. The fields come from `pycapcut`, whose table records masks as checked on 9.5.0 |
| Chroma key | A `materials.chromas` entry: `type: "chroma"`, `color` as `#rrggbbaa`, and `intensity_value`, `shadow_value`, `edge_smooth_value`, `spill_value` from 0 to 1 | **Assumed.** From `pycapcut`, which records it as checked on 9.5.0 |
| Voice and sound-scene effects | A `materials.audio_effects` entry (`type: "audio_effect"`, `resource_id`, `audio_adjust_params`) referenced from the audio clip | **Assumed.** `pycapcut` records voice ("tone") effects as checked on 9.5.0 and has no record for scene effects |
| Several styles in one text | `content.styles` holds one style per stretch of text, each with a `range` in UTF-16 units, the ranges following each other without gaps | **Assumed.** A single style was confirmed; [capcut-cli issue 85](https://github.com/renezander030/capcut-cli/issues/85) reports UTF-16 ranges for several styles on CapCut 9.2.0 |
| A keyframe added at a cut | The same keyframe structure, with the value the animation has at that moment | **Built from confirmed parts.** No new structure; that CapCut interpolates in a straight line between `curveType: "Line"` keyframes is assumed |

`tools/new_features_tour.py` builds one project that uses each of the Assumed rows, for the
check in CapCut.

### Items written by id alone

A catalogue item carries `effect_id` and `resource_id` and no local path. When the test project
was opened, CapCut downloaded eleven of the thirteen such items within two seconds and filled
in their paths, for example
`User Data/Cache/effect/233653727/c1f7073a94d22565ace1ab3023d1c154` for the White Flash
transition. The folder's hash matches the `md5` the catalogue lists for the item.

| Kind | In the test | Downloaded by CapCut |
| --- | --- | --- |
| Transitions | White Flash, 叠化, Film Burn (Pro) | All three |
| Filter | BW 2 | Yes |
| Clip animations | 渐显 (in), 渐隐 (out), 缩放 (combo) | All three |
| Text animations | 打字机 (in), 渐隐 (out), 波浪 (loop) | All three |
| Font | BANGERS | Yes |
| **Video effects** | Betamax, VCR (Pro) | **Neither.** Path left empty, nothing in the cache |

So fetching by id works for transitions, filters, animations and fonts, including Pro items,
and **does not work for video effects** in the form `pycapcut` writes them. `pycapcut`'s own
feature table records no verified CapCut version for effects, which fits. Catalogue effects
are therefore switched off in the server (`CATALOGUE_EFFECTS_ENABLED` in `library.py`); an
effect can still be copied from a project where the user applied it in CapCut. Comparing one
such hand-made effect with what the server wrote is the next step to finding what is missing.

When an item is taken from one of the user's projects the server copies whatever CapCut wrote,
so its internal fields do not need to be known; only where it attaches matters.

## Reaching CapCut's library

Researched on 2026-11-17, by inspecting this machine and reading published projects.

### What is on the machine

- **The effect cache has no names.** `User Data\Cache\effect` holds 429 downloaded items in
  folders named by numeric id, each with a `config.json` describing shaders, not a title.
  `Cache\music` holds 69 MP3 files under hashed names. Nothing local maps a name to an id,
  apart from the user's own project files.
- **Only project files carry names.** A project that used a library item records its name,
  ids and cache path. Two library songs were found this way in existing projects, with their
  files still in the cache.
- **CapCut cannot be driven through accessibility.** A read-only query of the running CapCut
  8.3.0 window with Windows UI Automation found three top-level windows and **zero** child
  controls. The `pyJianYingDraft` project reports the same: its export automation works for
  version 6 and below because later versions hide their controls.
- **A `capcut://` link handler is registered** (`HKCU\Software\Classes\capcut`), pointing at
  `CapCut.exe`. No parameters are documented, and CapCut's help pages say the desktop app does
  not act on template links.

### What others have published

- **[pycapcut](https://github.com/GuanYixuan/pyCapCut)** ships name-to-id lists for CapCut:
  1,137 transitions, 1,837 video effects, 454 filters, 941 clip and text animations, 348 fonts,
  10 blend modes, 6 masks and 224 audio effects, each flagged free or Pro. Its own note dates
  the lists August 2025. About three quarters are Pro, and about a tenth have English names.
- **It writes items by id with no path.** Its source comments say so for transitions, filters
  and effects. Its feature table lists a verified CapCut version (9.5.0) only for fonts, blend
  modes and masks; effects, filters, transitions and animations have none recorded. Testing
  here showed CapCut 8.3.0 does fetch transitions, filters, animations and fonts written this
  way, and does not fetch video effects (see
  [Items written by id alone](#items-written-by-id-alone)).
- **Its README warns** that not every listed item is still available and that some need a
  membership.
- **Its repository declares no license.** The sister project
  [pyJianYingDraft](https://github.com/GuanYixuan/pyJianYingDraft) is Apache-2.0, which does not
  carry over. The server therefore uses `pycapcut` as an installed dependency and copies none
  of its data.
- **No catalogue exists for stickers, music or stock clips.** `pycapcut` extracts sticker ids
  from existing projects instead. Another CapCut MCP server
  ([LHenri88/capcut-mcp](https://glama.ai/mcp/servers/LHenri88/capcut-mcp)) states that there
  is no offline way to browse the catalogue and reuses items from the user's history, the same
  approach as the first route here.
- **JianYing ids are not interchangeable.** CapCut and JianYing (the Chinese edition) use
  separate asset stores, so ids collected for one may not resolve in the other
  ([capcut-cli compatibility notes](https://cdn.jsdelivr.net/npm/capcut-cli-david@2.5.0/COMPATIBILITY.md)).

### Conclusion

Two routes are implemented: reuse from the user's projects (exact, covers everything including
stickers, music and effects) and the `pycapcut` catalogue (about 3,100 transitions, filters,
animations, fonts, voice effects and blend modes by id). Driving CapCut's window,
deep links and CapCut's online service were ruled out.

## What has been confirmed

| Capability | Status | Evidence |
| --- | --- | --- |
| Create a project, appears on the home screen | Confirmed | Listed by CapCut on its own; opened by the user |
| Video clips, trims, order | Confirmed | Opened and re-saved unchanged apart from the notes above |
| Speed | Confirmed, after a fix | CapCut re-timed the first attempt; lengths now match its rounding |
| Images, overlay track, position and scale | Confirmed | Kept on re-save; overlay flag corrected |
| Audio, volume, audio fades | Confirmed | Kept on re-save |
| Titles and captions with colour, size, bold, outline | Confirmed | Seen by the user; kept on re-save |
| Duplicate a project | Confirmed | The copy opened in CapCut |
| Split, delete, cut range, move, arrange, replace media, freeze frame | Built from confirmed parts | Automated tests; no new file structures involved |
| Preview render, frames, `.srt` import and export, relink | Does not depend on CapCut | Automated tests and visual check of rendered frames |
| Keyframes, picture and text fades, crop, blurred background, text spacing and background | Confirmed | Kept unchanged on re-save; the user watched the result |
| Catalogue transitions, filters, clip and text animations, fonts | Confirmed | CapCut downloaded every one in the test, including Pro items, and filled in their paths |
| Blend modes | Kept, rendering unproven | CapCut kept the entry but did not fill in a path |
| Catalogue video effects | **Not working** | CapCut did not fetch either effect in the test; switched off |
| Coloured background | Assumed | Not in the test project |
| Masks, chroma key, voice effects, highlighted words | Assumed | Files checked by automated tests only; `tools/new_features_tour.py` builds the project for the CapCut check |
| Undo, redo, checkpoints, batches, cutting silence, ducking, captions from speech | Does not depend on new structures | Automated tests, and a run through the real server with synthesized speech |
| Anything on CapCut 9.x | Not confirmed | Only that 9.8.0 lists a new project folder on its own |
| Reuse of library items from the user's projects | Assumed | Tested against hand-made sample data only; no project of the user's contains one yet |

## Other CapCut versions

Everything above was established on CapCut 8.3.0. CapCut updates itself: on 2026-11-18, during
development, the same machine went from 8.3.0 to 9.8.0 without being asked.

### Seen on 9.8.0

- **Confirmed.** A project folder written by the server while CapCut 9.8.0 was running was
  added to `root_meta_info.json` by CapCut itself within a minute, as on 8.3.0.
- **Confirmed.** 9.8.0 saves plain JSON. A project it opened and saved was read back by the
  server without trouble; `version` and `new_version` were unchanged (`360000`, `163.0.0`).
- **Confirmed.** 9.8.0 writes more copies than 8.3.0: `template-2.tmp` and
  `draft_content.json.bak` also appear beside the copy under `Timelines\<id>\`. The server
  keeps both `template-2.tmp` files identical to the timeline.
- **Seen, purpose unknown.** On first open 9.8.0 wrote
  `Timelines\<id>\attachment\patch\mini_draft.json`, a slightly smaller copy of the timeline,
  and did not rewrite it on later saves. Whether a later edit by the server still shows in
  CapCut once this file exists has not been checked.
- **Confirmed. CapCut keeps its own media list and writes it back.** A project was listed by
  the running CapCut at 02:53 with media in one folder. At 02:57 the server repointed the
  clips and the media list to another folder. When the project was opened at 03:11, and again
  at 03:33 after the list had been mended a second time, `draft_meta_info.json` came back with
  the old entries beside new ones CapCut had made for the files on the timeline. CapCut then
  asked the user to link a listed file it could not find, although no clip used it. The old
  path was found nowhere else in the project folder, only in two of CapCut's own databases
  (`Cache\ILASDKDB\mediasearch.db`, `Cache\agentCache\agent_media_asr.db`). So a change to the
  media list only holds when CapCut is not running, or once the user links the file in
  CapCut's dialog. The timeline itself was not affected: CapCut kept the repointed clips.
- Whether masks, chroma key, voice effects and highlighted words are drawn by 9.8.0 has not
  been recorded yet.

### What other tools report

Read on 2026-11-18. These are other people's observations, not checked here.

- **Which copy is read differs by build.** On CapCut 8.7.0 for Windows the copy under
  `Timelines\<id>\` was seen to win on open and close, and on 9.2.8 for macOS the app reads
  `Timelines/<main_timeline_id>/draft_info.json`, so writing only the root file changes nothing
  ([capcut-cli version support](https://github.com/renezander030/capcut-cli/blob/master/docs/version-support.md)).
  The server writes every copy, which covers both.
- **Projects built from an old template can be refused** as coming "from an unusual path"
  (8.7.0 on Windows). The cause given is stale version markers in the template, not the path.
  The server copies the `platform` and version fields from a project the installed CapCut
  wrote, which is the fix those tools arrived at.
- **CapCut 10 and later are reported to reject projects edited by outside tools as corrupted**,
  with no working sample published (same page). The server refuses to write to a project last
  saved by version 10 or later unless `CAPCUT_MCP_ALLOW_UNTESTED=1` is set.
- **JianYing 6 and later encrypt their project files.** No report of CapCut doing so was found.
  The server never tries to decrypt.
- **`pycapcut` records masks, chroma key, fonts and voice effects as checked on CapCut
  9.5.0.4050**, and nothing for effects, filters, transitions and animations
  ([its README](https://github.com/GuanYixuan/pyCapCut)).
- **Exporting cannot be automated.** The `pycapcut` maintainer reports that the export window
  of 9.5.0 exposes no controls to Windows UI Automation
  ([issue 17](https://github.com/GuanYixuan/pyCapCut/issues/17)), matching what was found here
  on 8.3.0.

### How the server treats versions

`compat.py` reads the version that last saved a project from `last_modified_platform` and
sorts it into confirmed (8.3.0), unconfirmed (other versions up to 9.x: edits go through, with
a note in `get_timeline`, `check_project` and `doctor`) and untested (10 and later: refused).
After checking a version with the steps below, add it to `VERIFIED_VERSIONS`.

## Things that are not possible from files

- **Exporting.** Rendering happens inside CapCut; nothing in the project files triggers it.
- **Live refresh.** CapCut reads a project when it is opened and overwrites it when it saves.
- **Browsing the live library.** The catalogue is online, and nothing on disk maps names to
  ids. See [Reaching CapCut's library](#reaching-capcuts-library) for what is used instead.
- **Clicking buttons for the user.** CapCut 8.3.0 exposes no controls to Windows UI Automation.
- **Encrypted projects.** Some CapCut versions encrypt `draft_content.json`. 8.3.0 does not.

## Re-checking against a new CapCut version

1. Build a project with `tools/feature_tour.py`, one with `tools/catalogue_tour.py` for
   catalogue items, and one with `tools/new_features_tour.py` for masks, chroma key, voice
   effects and highlighted words (see their headers for the arguments).
2. Open the project in CapCut, then go back to the home screen.
3. Run `python tools/diff_resave.py "<project name>"`. Lines starting with `~` or `-` are
   things CapCut corrected and are worth fixing; `+` lines are fields it added.
4. If the structures changed, rebuild the templates from projects saved by the new version with
   `tools/extract_templates.py`.
