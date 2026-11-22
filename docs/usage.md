# Usage guide

How to edit CapCut projects through Claude with this server. For the exact parameters of each
tool see the [tool reference](tools.md).

## The working loop

1. **Close the project in CapCut** if it is open (go back to CapCut's home screen).
2. **Ask Claude** for the edit in plain words.
3. **Open the project in CapCut** to see it. New projects appear on the home screen on their own
   within a few seconds.
4. Polish in CapCut if you like, then **export in CapCut**.

You can go around the loop as often as you want. Edits you make in CapCut are kept: the server
always reads the project as CapCut last saved it.

## Concepts

### Projects

A project is addressed by its name as shown on CapCut's home screen. `list_projects` shows them
and marks any that are currently open in CapCut.

### Tracks

| Track | What goes on it |
| --- | --- |
| video track 0 | The main track: your clips, one after another |
| video track 1 and up | Overlays drawn on top: picture-in-picture, logos, cutaways |
| audio tracks | Music, voice-over, sound effects. Sounds that overlap in time need separate tracks |
| text tracks | Titles and subtitles. Text that overlaps in time needs separate tracks |
| effect, filter, sticker tracks | CapCut library items, added with `copy_segment` |

Tracks are numbered per type, starting at 0, and are created when first used.

### Segments and ids

Everything on a track is a segment: a clip, an image, a sound, a piece of text. `get_timeline`
lists them with a short id such as `7E5CA538`. Every editing tool takes that id. Ids change when
a segment is split (the right-hand part gets a new one), so Claude re-reads the timeline when
unsure.

### Time

All times are seconds, with decimals.

- **Timeline time** is a position in the finished video.
- **Source time** is a position inside a media file. `source_start=3` means "begin three seconds
  into the file".
- **Keyframe time** counts from the start of the segment being animated.

Times are snapped to the project's frame rate.

### Ripple

Segments on one track never overlap. When an edit changes a segment's length or removes it,
`ripple=true` (the default for most tools) moves the later segments on **that track** so they
stay adjacent. Other tracks do not move. After shortening the main track, check that text and
audio still line up, or use `cut_range` with `all_tracks=true`, which removes the same stretch
of time from every track at once.

### Position and size

Position uses `x` and `y` from -1 to 1 with `0, 0` at the centre of the frame. `x=1` is the right
edge and `y=1` the top edge. Subtitles sit at about `y=-0.75`.

`scale=1` is the size at which a clip is fitted to the frame; `0.5` is half that. `rotation` is
in degrees, clockwise. `opacity` runs from 0 (invisible) to 1.

## Example requests

### Start an edit

> Create a vertical CapCut project called "Episode 7" and add every shot in
> `D:\footage\episode-7\renders` in order.

Claude calls `create_project`, `probe_media` and `add_clips`. The frame size follows the first
clip, so vertical footage gives a vertical project. `set_canvas` changes the frame size later.

### Let Claude look before cutting

> Look at shot-03 and shot-04 and tell me where the action starts in each.

`extract_frames` returns stills that Claude can see, one by one or as a single labelled
contact sheet for skimming a long clip. It cannot hear, but `analyze_media` tells it where a
file is silent or loud and where the picture cuts, and with the optional speech package
`transcribe_media` gives it the words with their timing.

### Trim, cut and reorder

> Cut the first two seconds off the third clip. Remove everything between 0:42 and 0:47 from all
> tracks. Move the last clip to the front.

`trim_segment`, `cut_range` with `all_tracks=true`, and `arrange_track`. `split_segment` cuts
one clip in two so each part can be treated on its own, and `move_segment` puts a segment at
another time or on another track.

### Sound

> Add `voiceover.mp3` from the start. Put `music.mp3` under it at 25% with a one second fade in
> and a two second fade out, ending with the video. Mute the footage's own sound.

`add_audio` twice on two tracks, then `set_track_volume` with volume 0 on the video track.
`set_volume` changes one clip's loudness and fades afterwards.

To lower music while someone speaks, say so: "bring the music down whenever she talks". Claude
uses `duck_audio`, which follows the voice clips, or the speech inside them. For a fixed
stretch, give the times: "bring the music down to 10% between 12 and 20 seconds" becomes
volume keyframes with `set_keyframes`.

### Titles and subtitles

> Add the title "Welcome to the Channel" for the first three seconds near the top, yellow, bold,
> with a black outline. Then add the subtitles from `episode.srt`.

`add_text` and `add_captions`. Fonts come from CapCut's library by name, or from a font file on
your computer; `list_fonts` shows those. If the subtitle file was timed against footage that starts later
on the timeline, give an offset: "shift the subtitles by 4.5 seconds".

`export_captions` writes the project's text back out as an `.srt` file.

### Framing

> The landscape clip in my vertical project has black bars. Fill them with a blurred copy.
> Crop the phone screenshot to its middle third. Put the logo in the top right at 20% size.

`set_background`, `crop_segment`, and `add_clips` on track 1 followed by `set_transform`.

### Movement and fades

> Fade the first clip in from black and the last one out. Slowly zoom in on the photo. Slide the
> title in from the left.

`fade_segment` and `set_keyframes`. A fade is the way to soften a cut when you have no library
transition available.

### Freeze, replace, speed

> Hold the frame at 0:15 for two seconds. Replace the second clip with `shot-02-v2.mp4`. Play
> the drone shot at double speed.

`freeze_frame`, `replace_media`, `set_speed`.

### Check the result without opening CapCut

> Render a preview and show me six frames from it.

`render_preview` builds a small video with FFmpeg and returns stills. `preview_timeline` is
the quick version: frames of the footage at points on the timeline, without text or overlays,
singly or as one contact sheet. It is a rough check of
order, timing, framing and the audio mix. It does not draw transitions, effects, filters,
stickers or animations, and text only approximates CapCut's size and font. The file path is in
the reply if you want to play it.

### Remove pauses, caption the speech, lower the music

> Cut the pauses out of the interview clip. Add captions for what is said, short lines, with
> the product name in yellow. Put `music.mp3` under it and bring it down whenever she talks.

`cut_silence` listens to the clip and removes the quiet stretches, leaving several clips back
to back. `auto_captions` writes subtitles timed to the speech; it needs the optional speech
package (`uv sync --extra transcribe`). `duck_audio` lowers the music while the voice plays.
Do the cutting first and the captions after, so the captions match the final timing.

### Voice-over from text

> Read the captions aloud with a male voice.

> Say "Follow for more facts" at 24 seconds in Zira's voice, a little faster.

`add_voiceover` uses the speech voices built into Windows; `list_voices` shows which ones this
computer has. With `from_text_track` it reads the captions on that text track aloud: captions
are joined into sentences, and each sentence becomes its own audio clip starting when its first
caption appears. A sentence that would run into the next one is spoken faster until it fits;
if even the fastest pace is too slow, it starts late and Claude says which lines. With `text`
it speaks the words you give as one clip. `rate` sets the pace, from -10 to 10.

The voice goes on an audio track of its own. The WAV files are kept in the server's data folder
(see below), or in a folder you name, for example next to your footage. The project plays them
from there, so leave them in place. The voices are free and work offline, but they sound like a
computer. For a natural voice, make the file with another service and ask Claude to place it:
that is `add_audio`. CapCut's own text-to-speech voices cannot be used from outside CapCut.

`analyze_media` reports where a file is silent, where its picture cuts, how loud it is, and a
level graph over time. `transcribe_media` returns what is said with timestamps, which is how
Claude can cut on a sentence: "remove the part where I talk about pricing".

### Many edits at once

> Put a dissolve on every cut and fade the last clip out.

Claude sends these as one `batch`: one save and one undo step. If any edit in the batch
cannot be done, none of them is applied and Claude is told which one failed.

### Check for mistakes

> Check the project before I open it.

`check_project` lists what is broken or likely to disappoint: a transition with no clip after
it, text placed outside the frame, a gap on the main track, an animation that no longer fits
its clip, audio that runs past the picture, missing media. `doctor` checks the setup instead:
the CapCut version, FFmpeg and what is not installed.

### Masks and green screen

> Make the webcam clip a circle in the bottom right. Remove the green background from the
> presenter clip.

`set_mask` shows a clip through a shape: circle, rectangle (with rounded corners), heart, star,
one side of a line, or a band. `set_chroma_key` makes one colour transparent. Both go on
overlay clips. They are not yet confirmed in CapCut by this server: look at the result.

### Reuse a project as a template

> Make this week's episode from my "Episode template" project: swap in these three clips and
> change the title to "Week 12".

Claude duplicates the project (`duplicate_project`), replaces each clip's file
(`replace_media`) and rewrites the text (`update_text`). Everything you designed in CapCut
stays: transitions, effects, stickers, text styles, animations.

### Undo, redo and checkpoints

> Undo that. ... Actually, bring it back.

Each `undo` steps back one change made through the server; a batch counts as one. `redo`
reverses the last undo, until another change is made. The last 100 changes are kept.

If you edited the project in CapCut after Claude's last change, an undo takes your edits back
too, because it restores the earlier state of the whole timeline. The server says so when it
happens and keeps the state from before the undo, both for `redo` and as a checkpoint.

> Save a checkpoint called "before captions".

A `checkpoint` is a named copy of the timeline that is never dropped. Ask for one before a
large change; "go back to the checkpoint before captions" restores it.

## Transitions, effects, filters, animations, fonts and stickers

CapCut's library lives online. The server reaches its items in two ways and tries them in this
order.

### 1. Items you have already used

Anything that appears in one of your CapCut projects can be reused in another, copied exactly
as CapCut wrote it. This is the only route for **video effects, stickers, library music and
stock clips**, and the most dependable one for everything else.

A small project kept just for this, holding the items you like, works as a palette. Use an
item once in CapCut, close the project, and it is available by name.

### 2. The built-in catalogue

About 3,100 items can be applied by name without ever having used them:

| Kind | Items | Applied with |
| --- | --- | --- |
| Transitions | 1,137 | `add_transition` |
| Filters | 454 | `add_filter` |
| Clip and text animations | 941 | `add_animation` |
| Fonts | 348 | the `font` setting of `add_text`, `add_captions`, `update_text` |
| Voice and sound-scene effects | 224 | `set_audio_effect` |
| Blend modes | 10 | `set_blend_mode` |

Things to know:

- **Ask for what you want, not for a name.** "Put a dissolve between every clip", "make the
  title type itself in", "give the flashback an old VHS look". Claude searches the catalogue
  (`list_library`) and picks.
- **Most names are Chinese.** The catalogue was collected from CapCut in Chinese, so a dissolve
  is `叠化` and a typewriter text animation is `打字机`. The server translates about 130 common
  English words when searching ("dissolve", "glitch", "zoom", "typewriter"), and a search can
  hold several words separated by commas. Around a tenth of the items have English names,
  such as `White Flash`, `Film Burn` and `Betamax`.
- **Pro items are marked.** About three quarters of the catalogue is Pro. They need a CapCut Pro
  account to export, exactly as if you had added them by hand.
- **It is a snapshot from August 2025.** An item CapCut has since removed shows as missing, or
  does nothing, when you open the project. Ask for another one. Items added to CapCut after
  that date are not in the catalogue; use them once in CapCut and they become available through
  the first route.
- **CapCut downloads the item when you open the project**, so the first open needs a connection.
- **Video effects are not included.** The catalogue lists 1,837 of them, but CapCut did not
  load them when tested, so they are switched off. Use an effect once in CapCut and it becomes
  available through the first route.

### What each tool does

| You want | Tool | Notes |
| --- | --- | --- |
| A transition between two clips | `add_transition` | Goes on the first clip of the pair; the clips must touch |
| An entrance, exit, loop or combo animation | `add_animation` | Text and clips have separate sets. A combo covers a whole clip and replaces its entrance and exit |
| An effect over part of the video | `add_effect` | Only effects you have already used in a project. Sits on its own track and affects everything under it. Settings run 0 to 100 |
| A colour filter over part of the video | `add_filter` | Same, with an intensity from 0 to 100 |
| An overlay that mixes with the picture | `set_blend_mode` | `screen` for light leaks and sparks on black, `multiply` for dark textures on white |
| A library font | `font` on the text tools | `font_path` takes a font file on your computer instead |
| A voice changer or room sound | `set_audio_effect` | On clips of an audio track. One voice effect and one scene effect per clip. Not yet confirmed in CapCut |
| A clip shown through a shape | `set_mask` | Circle, rectangle, heart, star, line, band. Not yet confirmed in CapCut |
| A colour made transparent | `set_chroma_key` | For green-screen footage on an overlay track. Not yet confirmed in CapCut |
| A sticker, library music, a stock clip | `copy_segment` | From a project where you already placed it |
| A text look you designed in CapCut | `copy_segment` with `text` | Same style, new wording |
| To remove any of these | `clear_segment`, or `delete_segments` for effects, filters and stickers | |

Transitions, filters, animations and fonts from the catalogue were confirmed in CapCut 8.3.0:
it downloaded every one in the test, Pro items included. See
[the findings](capcut-format.md#what-has-been-confirmed) for the full status.

## Troubleshooting

| What you see | Why | What to do |
| --- | --- | --- |
| "…is open in CapCut right now" | The project is open in CapCut's editor | Go back to CapCut's home screen, then retry |
| The edit does not show in CapCut | CapCut loaded the project before the edit | Close the project and open it again |
| A project card shows `0.0B | 00:00` | CapCut listed it while it was still empty | Open it once; the card updates |
| "Media not found" in CapCut, or `missing_media` in the timeline | A file was moved, renamed or deleted | `relink_media` with the old and new folder |
| CapCut asks to "Link media" for a file you already relinked | CapCut keeps its own media list and puts it back while it is running (seen on 9.8) | Link the file once in that dialog, or close CapCut completely and run `relink_media` again |
| "That would overlap segment…" | Two segments would share time on one track | Use another track, another time, or `ripple=true` when inserting |
| "No transition named…" | The name is not in your projects or the catalogue | Ask Claude to search for it (`list_library` with a search word), or use it once in CapCut |
| A transition, filter or animation is missing in CapCut | CapCut no longer offers that catalogue item | Ask for a different one |
| "Effects cannot be taken from the catalogue" | Catalogue effects are switched off because CapCut did not load them | Add the effect once in CapCut, then ask again |
| The server will not connect after an update | The environment is out of date | Close every Claude session using it, run `uv sync` in the server folder (with `--extra transcribe` if you use captions from speech), start again |
| "…was not changed. This project was last saved by CapCut 10…" | CapCut updated itself to a version nobody has confirmed | Ask Claude to run `doctor`. To try anyway, set `CAPCUT_MCP_ALLOW_UNTESTED=1` and work on a duplicate |
| A note that the project "was last saved by CapCut 9.x" | That version has not been through the check here | Edits are allowed. Look at the result in CapCut; [the findings](capcut-format.md#re-checking-against-a-new-capcut-version) say how to check properly |
| "Another edit of '…' is still running" | Two edits of one project at the same moment | Retry; send edits one after another, or together as a `batch` |
| "Transcription is an optional extra that is not installed" | The speech package is missing | Run `uv sync --extra transcribe` in the server folder and restart the server |
| "No voice called…" | That voice is not installed on this computer | Ask Claude to `list_voices` and pick one of those |
| A voice-over line "starts late" | The sentence is too long to say before the next caption, even at the fastest pace | Shorten the wording, give the captions more room, or ask for a higher `rate` |
| A mask, chroma key or voice effect does nothing in CapCut | Not yet confirmed for your CapCut version | Apply it once by hand in CapCut and tell Claude; it can then copy your own with `copy_segment` |
| "…is not readable JSON" | This CapCut version encrypts its project files | Not supported; the project is left untouched |
| "ffprobe was not found" | FFmpeg is not installed or not on `PATH` | Install FFmpeg, or set `FFPROBE_PATH` and `FFMPEG_PATH` |
| Clip lengths differ by a frame from what was asked | Times are snapped to the frame rate, as CapCut does | Expected |

## Where things are stored

Everything the server writes outside CapCut's own folders is under `%LOCALAPPDATA%\capcut-mcp\`:

| Folder | Contents | Safe to delete |
| --- | --- | --- |
| `backups\<project>\` | The last 100 versions of each project's timeline, for `undo`, with `redo\` and `checkpoints\` inside | Yes, but undo history and checkpoints go with it |
| `frames\` | Stills and contact sheets returned by `extract_frames` and the previews. Cleared after a day | Yes |
| `transcripts\` | Speech-to-text results, so a file is only transcribed once | Yes |
| `locks\` | Empty files used to make edits of one project take turns | Yes, when no server is running |
| `tour\` | Test media made by `tools/new_features_tour.py`. **Its demo project refers to these files** | Once that project is deleted |
| `previews\` | Videos made by `render_preview` | Yes |
| `stills\<project>\` | Images made by `freeze_frame`. **Projects refer to these files** | No, not while a project uses them |
| `voiceovers\` | Speech made by `add_voiceover`, one WAV file per line. **Projects refer to these files** | No, not while a project uses them |
