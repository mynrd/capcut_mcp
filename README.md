# capcut-mcp

An MCP server that lets an AI assistant edit CapCut desktop projects. The assistant calls its
tools to build and change a timeline; you open the project in CapCut to watch it, polish it and
export it.

It works by rewriting the project files CapCut keeps on disk. It does not control the CapCut
window, and it is not affiliated with, endorsed by or supported by CapCut or ByteDance. "CapCut"
is their trademark; it is used here only to say what the tool works with.

## Documentation

| Document | What it covers |
| --- | --- |
| [Usage guide](docs/usage.md) | How to work with it: concepts, example requests, workflows, troubleshooting |
| [Tool reference](docs/tools.md) | Every tool and its parameters (generated from the code) |
| [CapCut format findings](docs/capcut-format.md) | How CapCut stores a project, what was confirmed and how |
| [Development notes](docs/development.md) | Code layout, tests, how to add a tool, how to re-verify against CapCut |
| [Changelog](CHANGELOG.md) | What changed in each version |

## Requirements

- CapCut desktop on Windows. Built and confirmed against **8.3.0**, whose project files are
  plain JSON. Other 9.x and earlier versions are edited with a warning; versions from 10 on are
  refused until someone confirms they work (see [Limits](#limits)). macOS is supported in the
  code but has not been tried on a Mac.
- [uv](https://docs.astral.sh/uv/) to install the server's environment.
- FFmpeg (`ffprobe` and `ffmpeg` on `PATH`) to read media, pull frames, listen for silence and
  render previews.

## Setup

Get the code:

```
git clone https://github.com/mynrd/capcut_mcp.git
cd capcut_mcp
```

Install the environment once, and again after pulling changes:

```
uv sync
```

To let the server turn speech into text (automatic captions), add the optional speech package:

```
uv sync --extra transcribe
```

Inside this folder the server is already registered through `.mcp.json`; Claude Code asks to
approve it the first time it starts here.

To use it from any folder, register it for your user account, with the path to this folder:

```
claude mcp add --scope user capcut -- uv run --no-sync --directory <path to this folder> python -m capcut_mcp.server
```

Restart Claude Code after registering. `/mcp` shows whether the server connected. Any other MCP
client can start it with the same command.

`--no-sync` matters. Without it, `uv run` re-installs the project whenever `pyproject.toml`
changes, and on Windows that fails while another session still has the server running, which
then stops new sessions from connecting. With it the environment is only changed when you run
`uv sync` yourself.

## What it can do

- **Projects:** list, create, duplicate, change frame size, relink moved media, check a project
  for mistakes, check the whole setup.
- **Build a timeline:** add videos and images in order, insert between clips, overlays on higher
  tracks, music and voice-over with volume and fades, titles, subtitles from a list or `.srt`.
- **Edit:** trim, split, cut out a time range (on one track or all), move, reorder, delete,
  replace a clip's file, freeze a frame, change speed. Many edits can go in one batch: one save,
  one undo step, all or nothing.
- **Frame and animate:** position, scale, rotate, opacity, mirror, crop, blurred or coloured
  background, fade in and out, keyframe animation, masks (circle, rectangle, heart, star, line,
  band), chroma key for green-screen footage.
- **Text:** fonts, outline, background, shadow, spacing, and single words or phrases highlighted
  in another colour.
- **Listen:** find silences, scene cuts and loudness in a file; remove the pauses from a talking
  clip; lower music while a voice plays; with the optional speech package, transcribe speech
  and write captions timed to it.
- **Speak:** turn text into a voice-over with the voices built into Windows, or read a
  project's captions aloud, each sentence starting with its caption and sped up where it would
  run into the next. Free and offline, but the voices sound synthetic.
- **See its own work:** read media details, pull frames from footage (singly or as one labelled
  contact sheet), and render a rough preview video of the whole edit with FFmpeg.
- **CapCut library items:** transitions, filters, clip and text animations, fonts, blend modes
  and voice effects by name from a built-in catalogue of about 3,100 items, plus anything you
  have already used in one of your projects, including effects, stickers, music and stock
  clips. Common English words find the mostly Chinese catalogue names ("dissolve" finds 叠化).

## What stays with you

1. **Close the project in CapCut before the assistant edits it** (go back to CapCut's home
   screen). The server refuses to write to a project that is open, because CapCut would
   overwrite the change. Open the project again afterwards to see the result.
2. **Export in CapCut.** CapCut has no way to start an export from outside. `render_preview`
   makes a rough low-resolution check copy, not a final video.

## Limits

- **CapCut's online library cannot be browsed live.** Library items come from two places: a
  built-in catalogue (a snapshot from August 2025, so newer items are missing and a few listed
  ones may be gone), and your own projects. Video effects, stickers, music and stock clips
  cannot be taken from the catalogue; use one once in CapCut and it becomes available. A small
  project holding your favourites works as a palette.
- **The catalogue comes from a third-party package.** [pycapcut](https://github.com/GuanYixuan/pyCapCut)
  supplies the item ids. Its repository declares no license, so it is used as an installed
  dependency and none of its data is copied into this repository. Without it the server still
  runs; only catalogue items, masks and chroma key are unavailable.
- **Not everything is confirmed in CapCut yet.** Masks, chroma key, voice effects and
  highlighted words are written the way other open-source tools write them and are covered by
  tests of the files, but have not been through the CapCut check described in
  [the findings](docs/capcut-format.md). `tools/new_features_tour.py` builds a project for that.
- **No CapCut AI features**: background removal, CapCut's own auto captions, CapCut's own
  text-to-speech voices and the like. CapCut makes those on its servers, and a project file
  only stores the finished result. Captions from speech are done by the optional speech
  package instead, and voice-overs from text by the voices built into Windows.
- **Text to speech is Windows only** for now. On macOS, make the voice-over elsewhere and
  place it with `add_audio`.
- **Encrypted projects cannot be edited.** Some CapCut and JianYing versions encrypt their
  project files; the server reports those as unreadable and leaves them alone. It makes no
  attempt to decrypt anything.
- **Confirmed on CapCut 8.3.0 only.** The format is undocumented and can change with an update,
  and CapCut updates itself. On 9.8.0 only two things have been confirmed so far: it still
  saves plain JSON, and it lists a new project on its own. A project records which version last saved it: the server warns
  for versions it has not been confirmed on and refuses versions from 10 on, which other tools
  report as rejecting outside edits. The `doctor` tool shows where you stand, and
  [the findings](docs/capcut-format.md) say how to re-check after an update.

## Safety

- Every change first stores the previous timeline under
  `%LOCALAPPDATA%\capcut-mcp\backups\<project>\` (the last 100 are kept, compressed). `undo`
  steps back and `redo` forward again. A `checkpoint` is a named copy that is never dropped.
- If you edited the project in CapCut after the assistant's last change, an `undo` would take
  that work back too. It says so, and keeps the state before the undo as a checkpoint.
- Stored versions carry the project's id, so the history of a deleted project is never
  restored into a new project that reuses its name.
- Edits of one project take turns, across tool calls sent in parallel and across several
  server processes, so one cannot write over another.
- Files are written to a temporary name and then swapped in, so a crash cannot leave half a file.
- Nothing is ever deleted from a project folder, and there is no tool that deletes a project.
- Media files are referenced where they are. Moving or deleting them breaks the project, as it
  would for any CapCut project; `relink_media` repairs moved files.

## Settings

| Environment variable | Purpose | Default |
| --- | --- | --- |
| `CAPCUT_DRAFTS_DIR` | CapCut's projects folder | `%LOCALAPPDATA%\CapCut\User Data\Projects\com.lveditor.draft` |
| `CAPCUT_MCP_HOME` | Where backups, frames, previews, transcripts and freeze-frame stills go | `%LOCALAPPDATA%\capcut-mcp` |
| `FFPROBE_PATH`, `FFMPEG_PATH` | Full path to the FFmpeg tools | found on `PATH` |
| `CAPCUT_MCP_ALLOW_UNTESTED` | Set to `1` to edit projects saved by a CapCut version the server refuses | not set |
| `CAPCUT_MCP_WHISPER_DEVICE`, `CAPCUT_MCP_WHISPER_COMPUTE` | Where and how speech is transcribed, e.g. `cuda` and `float16` | `cpu`, `int8` |

## Quick check

```
uv sync
uv run --no-sync pytest
```

## License

[MIT](LICENSE). The `pycapcut` package and the optional `faster-whisper` package are separate
works under their own terms and are not part of this repository.
