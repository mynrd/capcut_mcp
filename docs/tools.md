# Tool reference

Generated from the server by `tools/gen_tool_reference.py`; do not edit by hand.

The server has 55 tools. Most take `project`, the name of a CapCut project as shown by `list_projects`. `segment` is a segment id from `get_timeline`; its first 8 characters are enough. All times are in seconds. See the [usage guide](usage.md) for how the pieces fit together.

## Contents

- **Projects:** [`list_projects`](#list_projects), [`create_project`](#create_project), [`duplicate_project`](#duplicate_project), [`get_timeline`](#get_timeline), [`set_canvas`](#set_canvas), [`relink_media`](#relink_media), [`check_project`](#check_project), [`doctor`](#doctor)
- **Undo, checkpoints and batches:** [`undo`](#undo), [`redo`](#redo), [`checkpoint`](#checkpoint), [`batch`](#batch)
- **Looking at and listening to media:** [`probe_media`](#probe_media), [`extract_frames`](#extract_frames), [`preview_timeline`](#preview_timeline), [`render_preview`](#render_preview), [`analyze_media`](#analyze_media), [`transcribe_media`](#transcribe_media), [`list_fonts`](#list_fonts)
- **Adding clips, audio and text:** [`add_clips`](#add_clips), [`add_audio`](#add_audio), [`add_text`](#add_text), [`add_captions`](#add_captions), [`auto_captions`](#auto_captions), [`update_text`](#update_text), [`export_captions`](#export_captions)
- **Cutting and arranging:** [`trim_segment`](#trim_segment), [`split_segment`](#split_segment), [`cut_range`](#cut_range), [`cut_silence`](#cut_silence), [`move_segment`](#move_segment), [`delete_segments`](#delete_segments), [`arrange_track`](#arrange_track), [`replace_media`](#replace_media), [`freeze_frame`](#freeze_frame)
- **Speed and sound:** [`set_speed`](#set_speed), [`set_volume`](#set_volume), [`set_track_volume`](#set_track_volume), [`duck_audio`](#duck_audio), [`set_audio_effect`](#set_audio_effect)
- **Framing and animation:** [`set_transform`](#set_transform), [`crop_segment`](#crop_segment), [`set_background`](#set_background), [`fade_segment`](#fade_segment), [`set_keyframes`](#set_keyframes), [`set_mask`](#set_mask), [`set_chroma_key`](#set_chroma_key), [`clear_segment`](#clear_segment)
- **CapCut library items:** [`list_library`](#list_library), [`add_transition`](#add_transition), [`add_animation`](#add_animation), [`add_effect`](#add_effect), [`add_filter`](#add_filter), [`set_blend_mode`](#set_blend_mode), [`copy_segment`](#copy_segment)

## Projects

### list_projects

List the CapCut projects on this computer, most recently changed first.

*Does not change the project.*

Takes no parameters.

### create_project

Create a new, empty CapCut project.

The canvas takes the size of the first clip added unless set_canvas is called, so width and
height only matter for projects that start with text or images. Common sizes: 1920x1080
(landscape), 1080x1920 (vertical, for Reels/TikTok/Shorts), 1080x1080 (square).

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `name` | text | required |  |
| `width` | whole number | `1920` |  |
| `height` | whole number | `1080` |  |
| `fps` | number | `30.0` |  |

### duplicate_project

Copy a project under a new name, for example before a risky edit or to make a variant.

This is also how a project the user designed in CapCut is used as a template: duplicate
it, then swap its footage with replace_media and its wording with update_text.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `new_name` | text | required |  |

### get_timeline

Show a project's tracks and the segments on them, with the segment ids used by other tools.

With nothing else given it lists everything. On a long project narrow it down: `type` and
`track` pick tracks, and `start`/`end` (timeline seconds) keep only the segments that
overlap that stretch. Give `segment` instead to see one segment in full: keyframe points,
animation timing, text style, effect settings. Also reports media files that can no longer
be found (see relink_media).

*Does not change the project.*

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `type` | "video" / "audio" / "text" / "effect" / "filter" / "sticker" / "adjust" | optional |  |
| `track` | whole number | optional |  |
| `start` | number | optional |  |
| `end` | number | optional |  |
| `segment` | text | optional |  |

### set_canvas

Change the project's frame size in pixels, e.g. 1080x1920 for vertical video.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `width` | whole number | required |  |
| `height` | whole number | required |  |

### relink_media

Fix clips whose media was moved or renamed.

old_path and new_path are either two folders (every file under the old folder is looked up
at the same relative place under the new one) or two files. A clip is only repointed when
the file exists at the new location. CapCut's own media list is repointed as well, which
stops its "Link media" prompt for files that were moved.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `old_path` | text | required |  |
| `new_path` | text | required |  |

### check_project

Look a project over for mistakes before the user opens it in CapCut.

Reports problems (broken references, missing media, overlaps), warnings (a transition with
no clip after it, text outside the frame, gaps on the main track, animations that no longer
fit their segment) and notes (audio running past the picture, captions that are hard to
read in time, library items still to be downloaded). Run it after a round of edits.

*Does not change the project.*

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |

### doctor

Check the setup: CapCut and its version, the projects folder, FFmpeg, optional parts.

Use it when something behaves oddly, when a tool refuses because of the CapCut version, or
when the user asks whether everything is installed. It says what is wrong and what to do.

*Does not change the project.*

Takes no parameters.

## Undo, checkpoints and batches

### undo

Restore the project to how it was before the last change made through this server.

Can be called repeatedly to step further back; a batch counts as one change. redo reverses
it. If the user also edited the project in CapCut since that change, undo takes those edits
back as well: the result says so, and the state before the undo is kept as a checkpoint.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |

### redo

Bring back what the last undo took away. Works until another change is made.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |

### checkpoint

Keep a named copy of the project's timeline, or go back to one.

"save" stores the project as it is now under `name` (the same name again replaces it).
"restore" puts the project back to that copy; the state it replaces can still be reached
with undo. "list" shows the checkpoints. Unlike undo steps, checkpoints are never dropped,
so save one before a large change ("before captions") and the user can always return.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `action` | "save" / "restore" / "list" | `save` |  |
| `name` | text | optional |  |

### batch

Make several edits to one project in one go: one save, one undo step, all or nothing.

Each edit names an editing tool and gives its parameters without `project`, for example
{"tool": "add_transition", "args": {"segment": "7E5CA538", "name": "叠化"}}. They run in
order on the same copy of the project, so a later edit sees what an earlier one did. If any
edit fails, nothing is saved and the error says which one. Use it for repeated edits such
as a transition on every cut, many trims or many titles. Segment ids that an earlier step
creates are not known in advance; put edits that need them in a second batch.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `edits` | list of BatchEdit | required |  |

## Looking at and listening to media

### probe_media

Report the type, duration, frame size and frame rate of media files.

*Does not change the project.*

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `paths` | list of text | required |  |

### extract_frames

Return still frames from a video file so its content can be seen.

Give `times` (seconds into the file) for specific moments, or leave it out to get `count`
frames spread evenly across the file. Returns the images and where each was saved. With
sheet=true the frames come back as one grid picture with the time written on each cell,
which costs less to look at and allows up to 24 frames instead of 12: good for skimming a
long clip before choosing moments to look at closely.

*Does not change the project.*

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `path` | text | required |  |
| `times` | list of number | optional |  |
| `count` | whole number | `4` |  |
| `width` | whole number | `480` |  |
| `sheet` | true/false | `false` |  |

### preview_timeline

Return frames of the footage playing at points on the project's timeline.

Fast, but shows only the lowest video clip at each time, without text or overlays. Use
render_preview to see the composed picture. Give `times` (timeline seconds) or get `count`
evenly spaced frames. With sheet=true they come back as one labelled grid picture (up to
24 frames), a quick storyboard of the whole edit.

*Does not change the project.*

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `times` | list of number | optional |  |
| `count` | whole number | `6` |  |
| `width` | whole number | `360` |  |
| `sheet` | true/false | `false` |  |

### render_preview

Render a rough low-resolution video of the project with FFmpeg, to check the edit.

Draws every video track with position, scale, rotation, crop, opacity, fades, keyframed
movement and blurred or coloured backgrounds, burns in text, and mixes all audio with
volumes and fades. It is an approximation, not CapCut's output: transitions, effects,
filters, stickers, masks and library animations are not drawn, and text size and font only
roughly match. `start`/`end` limit it to part of the timeline, which is much faster on a
long project. Set `frames` to also get that many stills from the result back as images.
The video file is saved to output_path (default: this server's data folder) for the user
to play.

*Does not change the project.*

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `output_path` | text | optional |  |
| `height` | whole number | `640` |  |
| `start` | number | optional |  |
| `end` | number | optional |  |
| `include_text` | true/false | `true` |  |
| `frames` | whole number | `0` |  |

### analyze_media

Measure a media file's sound and picture, since they cannot be heard or watched directly.

`what` lists the measurements wanted. All times are seconds inside the file.
- "silence": the silent stretches and, between them, the stretches with speech or sound.
A stretch counts as silent below `silence_db` for at least `min_silence` seconds. Use it
to find where someone talks, or pauses worth cutting (cut_silence does the cutting).
- "scenes": the moments the picture changes abruptly, i.e. cuts in the footage. Lower
`scene_threshold` (0.2) to catch cuts between similar-looking shots.
- "loudness": overall loudness in LUFS and the true peak, plus the `volume` setting that
would bring the clip to -14 LUFS, a common level for online video.
- "levels": loudness over time, one number in dB every `level_step` seconds; -90 is
silence. Shows where the loud and quiet parts are, for example to place a music drop.
`start` and `end` limit the measurement to part of the file.

*Does not change the project.*

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `path` | text | required |  |
| `what` | list of "silence" / "scenes" / "loudness" / "levels" | required |  |
| `start` | number | `0.0` |  |
| `end` | number | optional |  |
| `silence_db` | number | `-35.0` |  |
| `min_silence` | number | `0.4` |  |
| `scene_threshold` | number | `0.3` |  |
| `level_step` | number | `0.5` |  |

### transcribe_media

Turn the speech in a video or audio file into text with timestamps.

Returns sentences with their start and end in the file; words=true adds every single word
with its timing, for cutting on exact words. Leave `language` out to detect it, or give a
code such as "en" or "tl". `model` trades speed for accuracy: "tiny", "base", "small",
"medium", "large-v3". The first use of a model downloads it. Results are remembered, so
asking again for the same file is instant. Needs the optional speech package; if it is
missing the error says how to install it. To subtitle a project, auto_captions is simpler.

*Does not change the project.*

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `path` | text | required |  |
| `language` | text | optional |  |
| `model` | text | `base` |  |
| `start` | number | `0.0` |  |
| `end` | number | optional |  |
| `words` | true/false | `false` |  |

### list_fonts

List font files on this computer that can be given as font_path, optionally filtered by name.

*Does not change the project.*

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `search` | text | `` |  |

## Adding clips, audio and text

### add_clips

Put video or image files on a video track, one after another in the order given.

By default they are appended after whatever is already on the track. Give `start` to place
the first one at a specific timeline time: the space must be free, or set ripple=true to
insert there and push the later clips along (start must then be where a clip begins or
ends). Use track 1 or higher for overlays such as picture-in-picture or a logo.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `clips` | list of Clip | required |  |
| `track` | whole number | `0` |  |
| `start` | number | optional |  |
| `ripple` | true/false | `false` |  |

### add_audio

Put music, a voice-over or a sound effect on an audio track.

`start` is the timeline time it begins (default: after the last audio on that track).
`source_start`/`source_end` pick the part of the file to use. `duration` caps the length on
the timeline, e.g. the video's duration so music ends with the picture. Fades are in seconds.
A video file can be given to use only its sound. Sounds that overlap in time go on
different tracks.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `path` | text | required |  |
| `start` | number | optional |  |
| `source_start` | number | `0.0` |  |
| `source_end` | number | optional |  |
| `duration` | number | optional |  |
| `track` | whole number | `0` |  |
| `volume` | number | `1.0` |  |
| `fade_in` | number | `0.0` |  |
| `fade_out` | number | `0.0` |  |
| `speed` | number | `1.0` |  |

### add_text

Add a title or other text overlay.

y=-0.75 sits near the bottom of the frame, y=0.75 near the top. Colours are hex like
#FFD400. stroke_width around 0.08 gives a normal outline. `font` is the name of a font
from CapCut's library (find one with list_library kind="font"); `font_path` is a .ttf/.otf
file on this computer instead (see list_fonts). Leave both out for CapCut's default font.
letter_spacing and line_spacing use the same numbers as CapCut's text panel. wrap_width is
the share of the frame width a line may fill before wrapping (default 0.82). `highlight`
lists words or phrases to show in `highlight_color` (default yellow) while the rest keeps
`color`. Text that must overlap in time goes on different tracks.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `text` | text | required |  |
| `start` | number | required |  |
| `duration` | number | required |  |
| `track` | whole number | `0` |  |
| `x` | number | `0.0` |  |
| `y` | number | `0.0` |  |
| `size` | number | `15.0` |  |
| `color` | text | `#FFFFFF` |  |
| `bold` | true/false | optional |  |
| `italic` | true/false | optional |  |
| `underline` | true/false | optional |  |
| `align` | "left" / "center" / "right" | optional |  |
| `stroke_color` | text | optional |  |
| `stroke_width` | number | optional |  |
| `background_color` | text | optional |  |
| `background_opacity` | number | optional |  |
| `shadow` | true/false | optional |  |
| `font` | text | optional |  |
| `font_path` | text | optional |  |
| `letter_spacing` | number | optional |  |
| `line_spacing` | number | optional |  |
| `wrap_width` | number | optional |  |
| `highlight` | list of text | optional |  |
| `highlight_color` | text | optional |  |
| `scale` | number | `1.0` |  |
| `rotation` | number | `0.0` |  |
| `opacity` | number | `1.0` |  |

### add_captions

Add subtitles: many timed lines of text that share one style.

Give either `captions` (text with start and end timeline times) or `srt_path` (an .srt
file). `offset` shifts every caption by that many seconds, for a subtitle file timed
against footage that starts later on the timeline. They are placed low in the frame with a
black outline by default. `highlight` lists words or phrases to show in `highlight_color`
(default yellow) wherever they occur. Captions must not overlap each other in time. To
write captions from what is said in the footage, use auto_captions.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `captions` | list of Caption | optional |  |
| `srt_path` | text | optional |  |
| `track` | whole number | `0` |  |
| `y` | number | `-0.75` |  |
| `size` | number | `8.0` |  |
| `color` | text | `#FFFFFF` |  |
| `bold` | true/false | optional |  |
| `stroke_color` | text | `#000000` |  |
| `stroke_width` | number | `0.08` |  |
| `background_color` | text | optional |  |
| `background_opacity` | number | optional |  |
| `font` | text | optional |  |
| `font_path` | text | optional |  |
| `highlight` | list of text | optional |  |
| `highlight_color` | text | optional |  |
| `offset` | number | `0.0` |  |

### auto_captions

Write subtitles from what is said on a track, timed to the speech.

Listens to every clip on the given video or audio track (default: the main video track),
follows each clip's trimming and speed, and adds short caption lines where the words are
spoken. `max_chars` and `max_duration` limit how much goes into one line; short lines of
about 20 characters suit vertical video. The captions go on a new text track unless
`text_track` is given. `language` and `model` work as in transcribe_media. Needs the
optional speech package; if it is missing the error says how to install it. Run it after
cutting, so the captions match the final timing.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `type` | "video" / "audio" | `video` |  |
| `track` | whole number | `0` |  |
| `language` | text | optional |  |
| `model` | text | `base` |  |
| `max_chars` | whole number | `32` |  |
| `max_duration` | number | `4.0` |  |
| `text_track` | whole number | optional |  |
| `y` | number | `-0.75` |  |
| `size` | number | `8.0` |  |
| `color` | text | `#FFFFFF` |  |
| `bold` | true/false | optional |  |
| `stroke_color` | text | `#000000` |  |
| `stroke_width` | number | `0.08` |  |
| `font` | text | optional |  |
| `font_path` | text | optional |  |

### update_text

Change the wording or style of an existing text segment. Only what is given changes.

stroke_width=0 removes the outline; background_opacity=0 removes the background.
`highlight` lists words or phrases to show in `highlight_color`; an empty list removes the
highlighting. New wording puts the whole text back in one style, so pass `highlight` again
with it. Use set_transform to move or resize text, and trim_segment or move_segment for
its timing.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `segment` | text | required |  |
| `text` | text | optional |  |
| `size` | number | optional |  |
| `color` | text | optional |  |
| `bold` | true/false | optional |  |
| `italic` | true/false | optional |  |
| `underline` | true/false | optional |  |
| `align` | "left" / "center" / "right" | optional |  |
| `stroke_color` | text | optional |  |
| `stroke_width` | number | optional |  |
| `background_color` | text | optional |  |
| `background_opacity` | number | optional |  |
| `shadow` | true/false | optional |  |
| `font` | text | optional |  |
| `font_path` | text | optional |  |
| `letter_spacing` | number | optional |  |
| `line_spacing` | number | optional |  |
| `wrap_width` | number | optional |  |
| `highlight` | list of text | optional |  |
| `highlight_color` | text | optional |  |

### export_captions

Write the project's text segments to an .srt subtitle file (all text tracks, or one).

*Does not change the project.*

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `srt_path` | text | required |  |
| `track` | whole number | optional |  |

## Cutting and arranging

### trim_segment

Change which part of a clip plays, or how long text or an image stays on screen.

For video and audio, `source_start` and `source_end` are positions in the media file: raise
source_start to cut the beginning off, lower source_end to cut the end off. `duration` sets
the length on the timeline instead and is the only option for text and images. The segment
keeps its timeline start. With ripple, later segments on the track move to stay adjacent.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `segment` | text | required |  |
| `source_start` | number | optional |  |
| `source_end` | number | optional |  |
| `duration` | number | optional |  |
| `ripple` | true/false | `true` |  |

### split_segment

Cut one segment into two at a timeline time, so each part can be edited or removed alone.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `segment` | text | required |  |
| `at` | number | required |  |

### cut_range

Remove everything between two timeline times, e.g. a mistake from 12s to 15.5s.

Segments reaching into the range are split at its edges and the part inside is removed. By
default this affects one track; all_tracks=true cuts the same range out of every track so
picture, sound and text stay in step. With ripple, what follows moves earlier to close the gap.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `start` | number | required |  |
| `end` | number | required |  |
| `type` | "video" / "audio" / "text" / "effect" / "filter" / "sticker" / "adjust" | `video` |  |
| `track` | whole number | `0` |  |
| `all_tracks` | true/false | `false` |  |
| `ripple` | true/false | `true` |  |

### cut_silence

Remove the pauses from a talking clip, closing the gaps (jump cuts).

Listens to the part of the file the clip plays and cuts every stretch quieter than
`silence_db` that lasts at least `min_silence` seconds, leaving `keep` seconds of air on
each side of the speech. The clip becomes several clips. With all_tracks=true the same
stretches are cut from every track, which keeps captions and overlays in step but also
cuts music; add music afterwards instead. dry_run=true only reports what would be removed.
For a noisy recording raise silence_db (-30); if speech gets clipped, lower it (-40).

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `segment` | text | required |  |
| `silence_db` | number | `-35.0` |  |
| `min_silence` | number | `0.5` |  |
| `keep` | number | `0.12` |  |
| `all_tracks` | true/false | `false` |  |
| `dry_run` | true/false | `false` |  |

### move_segment

Move a segment to another timeline time and/or another track of the same type.

The destination must be free. To change the order of clips on a track, arrange_track is easier.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `segment` | text | required |  |
| `start` | number | optional |  |
| `track` | whole number | optional |  |

### delete_segments

Remove segments from the timeline. With ripple, later segments on the same track close the gap.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `segments` | list of text | required |  |
| `ripple` | true/false | `true` |  |

### arrange_track

Line a track's segments up back to back, optionally in a new order.

`order` lists segment ids in the wanted sequence; segments left out follow in their current
order. `start` is where the first one begins (default: where the track currently begins).
Use this to reorder clips or to close every gap on a track.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `type` | "video" / "audio" / "text" / "effect" / "filter" / "sticker" / "adjust" | `video` |  |
| `track` | whole number | `0` |  |
| `order` | list of text | optional |  |
| `start` | number | optional |  |
| `gap` | number | `0.0` |  |

### replace_media

Swap the file a clip plays for another one, keeping its position, speed, volume and framing.

If the new file is shorter than the part the clip was using, the clip is shortened and the
later clips on its track move up.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `segment` | text | required |  |
| `path` | text | required |  |

### freeze_frame

Hold the picture showing at a timeline time for `duration` seconds.

The clip is split at that time and a still of the frame is inserted, pushing the rest of
the track later. The still image is saved in this server's data folder.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `segment` | text | required |  |
| `at` | number | required |  |
| `duration` | number | `3.0` |  |

## Speed and sound

### set_speed

Speed a video or audio clip up or slow it down. 2 plays twice as fast, 0.5 at half speed.

The clip's length on the timeline changes; with ripple the later segments on its track follow.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `segment` | text | required |  |
| `speed` | number | required |  |
| `ripple` | true/false | `true` |  |

### set_volume

Set a clip's loudness and audio fades. volume 0 mutes, 1 is original, 2 is twice as loud.

fade_in and fade_out are lengths in seconds; 0 removes a fade.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `segment` | text | required |  |
| `volume` | number | optional |  |
| `fade_in` | number | optional |  |
| `fade_out` | number | optional |  |

### set_track_volume

Set the loudness of every clip on a track at once, e.g. 0 to silence all the footage's own sound.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `type` | "video" / "audio" | required |  |
| `track` | whole number | required |  |
| `volume` | number | required |  |

### duck_audio

Lower a clip's volume whenever the clips on another track play: music under a voice.

`segment` is the clip to lower, usually the music. `under_type` and `under_track` say where
the voice is (an audio track, or a video track for the footage's own sound). While those
clips play the volume drops to `level` times its normal value, gliding down and back up
over `fade` seconds. speech_only=true listens to the voice clips and ducks only while
someone is actually speaking, not during their pauses. This replaces any volume keyframes
on the clip.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `segment` | text | required |  |
| `under_type` | "video" / "audio" | `audio` |  |
| `under_track` | whole number | `0` |  |
| `level` | number | `0.25` |  |
| `fade` | number | `0.3` |  |
| `speech_only` | true/false | `false` |  |

### set_audio_effect

Put a voice changer or a sound-scene effect on an audio clip.

`name` comes from list_library kind="audio_effect": type "voice" changes how a voice sounds
(robot, chipmunk, monster), type "scene" makes it sound as if played somewhere. A clip
holds one of each type; a new one replaces the old one of its type. `settings` adjusts the
effect by setting name, each from 0 to 100. Applies to clips on audio tracks. Remove with
clear_segment what=["audio_effects"].

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `segment` | text | required |  |
| `name` | text | required |  |
| `settings` | object | optional |  |

## Framing and animation

### set_transform

Move, resize, rotate, fade or mirror a video, image or text segment.

scale 1 is the fitted size, 0.5 half of it. rotation is in degrees, clockwise. opacity runs
0 to 1.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `segment` | text | required |  |
| `x` | number | optional |  |
| `y` | number | optional |  |
| `scale` | number | optional |  |
| `rotation` | number | optional |  |
| `opacity` | number | optional |  |
| `flip_horizontal` | true/false | optional |  |
| `flip_vertical` | true/false | optional |  |

### crop_segment

Crop a video or image clip by cutting a fraction off each side.

Each value is the share of the picture removed from that side: left=0.1 removes the left
tenth. All zeros removes the crop. The cropped picture is then fitted to the frame again.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `segment` | text | required |  |
| `left` | number | `0.0` |  |
| `top` | number | `0.0` |  |
| `right` | number | `0.0` |  |
| `bottom` | number | `0.0` |  |

### set_background

Fill the bars around a clip that does not cover the whole frame.

Typical for landscape footage in a vertical project. Give `blur` (0.0625 light, 0.375,
0.75 or 1 heavy) for a blurred copy of the clip, or `color` as hex for a flat colour. Give
neither to go back to black. Applies to clips on the main video track.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `segment` | text | required |  |
| `blur` | number | optional |  |
| `color` | text | optional |  |

### fade_segment

Fade a clip, image or text in from and out to transparent. Lengths are in seconds.

On the main video track this is a fade from or to black. The clip's own sound fades with it
unless include_audio is false. On an audio clip only the sound fades. This is the way to
soften a cut when no library transition is available. Zeros remove the fade.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `segment` | text | required |  |
| `fade_in` | number | `0.0` |  |
| `fade_out` | number | `0.0` |  |
| `include_audio` | true/false | `true` |  |

### set_keyframes

Animate one property of a segment over time; CapCut moves smoothly between the points.

Each keyframe is a time counted from the segment's own start and the value at that time.
Examples: a slow zoom on an image is scale 1 at 0s and 1.2 at its end; a slide in from the
left is x -1.5 at 0s and 0 at 0.5s. Values use the same units as set_transform (and
set_volume for volume). This replaces any earlier keyframes of that property; an empty list
removes them.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `segment` | text | required |  |
| `property` | "x" / "y" / "scale" / "rotation" / "opacity" / "volume" | required |  |
| `keyframes` | list of Keyframe | required |  |

### set_mask

Show only part of a video or image clip, through a shape.

"circle", "rectangle", "heart" and "star" keep what is inside the shape; "linear" keeps one
side of a line through the centre; "mirror" keeps a band. x and y place the shape's centre
inside the clip, from -1 to 1 with 0,0 at the clip's centre. `size` is the shape's height as
a share of the clip's height (0.5 is half); `width`, for the rectangle only, is its share
of the clip's width. `rotation` is in degrees, `feather` softens the edge (0 to 100),
`round_corner` rounds a rectangle (0 to 100), invert=true keeps the outside instead.
Typical use: a round picture-in-picture, by putting a circle on an overlay clip. A clip has
one mask; a new one replaces it. Remove with clear_segment what=["mask"].

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `segment` | text | required |  |
| `shape` | "circle" / "rectangle" / "linear" / "mirror" / "heart" / "star" | required |  |
| `x` | number | `0.0` |  |
| `y` | number | `0.0` |  |
| `size` | number | `0.5` |  |
| `width` | number | optional |  |
| `rotation` | number | `0.0` |  |
| `feather` | number | `0.0` |  |
| `invert` | true/false | `false` |  |
| `round_corner` | number | `0.0` |  |

### set_chroma_key

Make one colour of a clip transparent, as for green-screen footage.

`color` is the colour to remove, as hex; look at a frame with extract_frames to pick the
actual shade of the background. `intensity` (0 to 100) is how much around that colour goes;
raise it if green remains, lower it if the subject starts to vanish. `edge_smooth` softens
the cut-out's edge and `spill` removes colour cast on it. Use it on an overlay clip (video
track 1 or higher) so the picture under it shows through. Remove with clear_segment
what=["chroma"].

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `segment` | text | required |  |
| `color` | text | `#00FF00` |  |
| `intensity` | number | `20.0` |  |
| `shadow` | number | `0.0` |  |
| `edge_smooth` | number | `0.0` |  |
| `spill` | number | `0.0` |  |

### clear_segment

Remove settings from a segment: transition, animations, keyframes, fade, crop, background,
blend mode, mask, chroma key, audio effects.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `segment` | text | required |  |
| `what` | list of "transition" / "animations" / "keyframes" / "fade" / "crop" / "background" / "blend" / "mask" / "chroma" / "audio_effects" | required |  |

## CapCut library items

### list_library

Find CapCut library items that can be applied, and their exact names.

Two sources are searched. `from_your_projects` lists items the user has already used in a
CapCut project; these are copied exactly and always work. `catalogue` is a built-in list of
thousands of transitions, filters, animations, fonts and voice effects by name; it is too
large to list, so give `search` with part of a name. Most catalogue names are Chinese.
Common English words are translated automatically (search="dissolve" also finds 叠化), and
several words can be tried at once, separated by commas: search="glitch, flash, 闪".

Apply a transition with add_transition, an animation with add_animation, an effect with
add_effect, a filter with add_filter, a font through the `font` parameter of add_text, a
blend mode with set_blend_mode and a voice or sound effect with set_audio_effect. Video
effects, stickers, music and stock clips exist only in `from_your_projects`; effects are
applied with add_effect and the rest with copy_segment.

*Does not change the project.*

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `kind` | "transition" / "animation" / "effect" / "filter" / "font" / "blend" / "audio_effect" / "sticker" / "adjust" | optional |  |
| `search` | text | `` |  |
| `source` | "all" / "projects" / "catalogue" | `all` |  |
| `project` | text | optional |  |

### add_transition

Put a transition between a clip and the clip right after it on the same track.

`segment` is the first of the two clips. `name` comes from list_library. `duration` is the
transition's length in seconds (default: the item's own). It cannot be longer than either
clip. Give from_project to take the user's own copy from one particular project. For a
transition on every cut, send them together with batch.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `segment` | text | required |  |
| `name` | text | required |  |
| `duration` | number | optional |  |
| `from_project` | text | optional |  |

### add_animation

Give a clip, image or text an animation from list_library.

"in" plays at the segment's start and "out" at its end, for `duration` seconds. "loop"
(text only) repeats between them. "combo" (clips only) is one animation covering the whole
clip and replaces any entrance and exit. Animations for text and for clips are separate
sets. Give `type` when the same name exists as more than one kind.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `segment` | text | required |  |
| `name` | text | required |  |
| `duration` | number | optional |  |
| `type` | "in" / "out" / "loop" / "combo" | optional |  |
| `from_project` | text | optional |  |

### add_effect

Lay a video effect over a stretch of the timeline. It affects everything under it.

Only effects the user has already used in one of their CapCut projects can be applied;
list_library kind="effect" shows them. `settings` adjusts the effect by setting name, each
from 0 to 100 (e.g. {"speed": 40}); leave it out to keep the user's values. Effects that
overlap in time go on different effect tracks.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `name` | text | required |  |
| `start` | number | required |  |
| `duration` | number | required |  |
| `track` | whole number | `0` |  |
| `settings` | object | optional |  |
| `from_project` | text | optional |  |

### add_filter

Lay a colour filter over a stretch of the timeline. It affects everything under it.

`name` comes from list_library kind="filter". `intensity` runs from 0 to 100 (default: full
strength, or the strength the user set when the filter is copied from their project).
Filters that overlap in time go on different filter tracks.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `name` | text | required |  |
| `start` | number | required |  |
| `duration` | number | required |  |
| `track` | whole number | `0` |  |
| `intensity` | number | optional |  |
| `from_project` | text | optional |  |

### set_blend_mode

Choose how an overlay clip mixes with the picture under it.

"screen" drops black areas (light leaks, sparks, smoke on black), "multiply" drops white
areas, "overlay" and "soft light" add texture. "normal" removes the blend mode. Use it on
clips on video track 1 or higher.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `segment` | text | required |  |
| `mode` | "normal" / "multiply" / "screen" / "overlay" / "soft light" / "hard light" / "lighten" / "darken" / "color dodge" / "color burn" / "linear burn" | required |  |

### copy_segment

Copy a segment from another project into this one, with everything attached to it.

This is how stickers, music from CapCut's library, stock clips and adjustment layers are
applied, and how a text style designed in CapCut is reused: copy the segment, then set
`start`, `duration` and, for text, new wording in `text`. `segment` is an id from
get_timeline or list_library of from_project. Without `start` it lands at the same time it
had in the source project.

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `project` | text | required |  |
| `from_project` | text | required |  |
| `segment` | text | required |  |
| `start` | number | optional |  |
| `duration` | number | optional |  |
| `track` | whole number | `0` |  |
| `text` | text | optional |  |

## Shared parameter types

### BatchEdit

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `tool` | text | required | Name of an editing tool, for example add_transition. |
| `args` | object | optional | That tool's parameters, without `project`. |

### Caption

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `text` | text | required |  |
| `start` | number | required | Timeline time the caption appears, in seconds. |
| `end` | number | required | Timeline time the caption disappears, in seconds. |

### Clip

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `path` | text | required | Full path to a video or image file. |
| `source_start` | number | `0.0` | Where in the file the clip begins, in seconds. |
| `source_end` | number | optional | Where in the file the clip ends. Default: end of file. |
| `duration` | number | optional | Length on the timeline. For images this is how long they show (default 5). |
| `speed` | number | `1.0` | Playback speed. 2 is twice as fast, 0.5 is slow motion. |
| `volume` | number | `1.0` | 0 is silent, 1 is the original level. |

### Keyframe

| Parameter | Type | Default | Notes |
| --- | --- | --- | --- |
| `time` | number | required | Seconds from the start of the segment (not the timeline). |
| `value` | number | required |  |
