# Development notes

For anyone changing the server. Users should start with the [usage guide](usage.md).

## Layout

```
src/capcut_mcp/
    server.py        The MCP tools. Thin: open the project, call one method, save, describe
    draft.py         Project: reading, writing, stored versions, locking, and every timeline edit
    schema.py        Builders for tracks, segments, materials, text styling, keyframes
    templates.json   Blank copies of the structures CapCut 8.3.0 writes (generated)
    library.py       Applying library items, masks, chroma key and voice effects
    catalogue.py     Names to CapCut ids, through the pycapcut package (the only module using it)
    analysis.py      Facts about a media file: silences, scene cuts, loudness, levels, speech to text
    assist.py        Edits driven by the sound: cut_silence, duck_audio, auto_captions, add_voiceover
    speech.py        Text to speech with the Windows voices, through a PowerShell child process
    lint.py          check_project: what is broken or likely to disappoint
    doctor.py        The setup report: CapCut version, FFmpeg, optional parts
    compat.py        Which CapCut versions are confirmed, warned about or refused
    render.py        The FFmpeg preview renderer
    sheet.py         Contact sheets: many stills in one labelled picture
    captions.py      .srt reading and writing
    media.py         ffprobe and frame extraction
    env.py           Folder locations, CapCut process check, file locks, the error type
tools/
    extract_templates.py   Rebuild templates.json from projects CapCut wrote
    feature_tour.py        Build a demo project (animation, framing) through the server over stdio
    catalogue_tour.py      Build a demo project that uses catalogue items
    new_features_tour.py   Build a demo project for masks, chroma key, voice effects, highlights
    diff_resave.py         Show what CapCut changed in a project after opening it
    gen_tool_reference.py  Regenerate docs/tools.md from the server
tests/
```

## How an edit flows

1. The `edit_tool` decorator in `server.py` takes the project's lock (`draft.editing`), so two
   edits of one project never interleave. A thread lock covers this process and an operating
   system file lock covers other server processes.
2. The tool calls `Project.open(name)`, which finds the folder and reads the newest of the
   timeline copies.
3. It calls a method on `Project` that changes the in-memory `content` dictionary. Methods raise
   `CapCutError` with a message written for the assistant to act on.
4. `_saved()` calls `Project.save()`, which refuses if the project is open in CapCut or was
   saved by a CapCut version that is not supported (`compat.py`), stores the current file in
   the backups folder, sorts segments, recomputes the duration, writes every timeline copy
   through a temporary file, and updates `draft_meta_info.json`.
5. The tool returns the new duration and a description of the segments it touched.

A `batch` runs the same tools inside a `draft.Session`. While a session is active (a context
variable, which the SDK carries into the worker thread each tool runs in), `Project.open`
returns the session's in-memory project and `Project.save` only marks it changed. The session
writes once at the end, or not at all if a step failed.

Stored versions live in `backups/<project name>/`: undo steps at the top, `redo/` and
`checkpoints/` below. Each file name carries the first eight characters of the timeline's id,
and only versions with the project's own id are ever restored, because a project name can be
reused after a project is deleted.

Internally all times are integer microseconds. `Project.us()` converts seconds and snaps to the
frame grid; `Project._speed_length()` gives the timeline length of a sped-up clip the way CapCut
rounds it.

## Design decisions

- **Templates over hand-written structures.** New segments and materials are deep copies of
  what CapCut itself wrote, blanked. A hand-written structure is accepted by CapCut but misses
  fields, as text did at first (87 of 125). `templates.json` contains no ids, paths or device
  identifiers; the extractor blanks them.
- **Copy, do not guess, for library items.** Transition and effect ids are not published.
  `library.py` first copies an item from a project where the user already used it, so it
  carries whatever CapCut wrote. Only when there is no such copy does it fall back to the
  catalogue.
- **The catalogue is a dependency, not data in this repository.** `catalogue.py` asks the
  `pycapcut` package for ids and has it build the material, then gives it one of our ids.
  `pycapcut`'s repository declares no license, so nothing from it is copied here, the import is
  lazy and wrapped, and the server works without it. It is pinned to `0.0.4` because the project
  announced breaking changes for `0.1.0`.
- **Refuse rather than race.** CapCut overwrites the project file when it saves, so the server
  will not write to a project that is open.
- **No deletion.** There is no tool to delete a project, and nothing is removed from a project
  folder. Backups make every change reversible.
- **Errors are instructions.** `CapCutError` subclasses the SDK's `ToolError`. In MCP Python SDK
  2.x only `ToolError` messages reach the client; any other exception is replaced with a generic
  "Error executing tool". Raise `CapCutError` for anything the assistant can fix.
- **Child processes never share stdin.** The server speaks MCP over its own standard input.
  FFmpeg reads keypresses from standard input, so a child that inherits it could swallow
  protocol bytes. Every subprocess is started with `stdin=subprocess.DEVNULL`, and FFmpeg with
  `-nostdin`. Use `media.run_tool` for new calls.
- **Text to speech uses what Windows already has.** `speech.py` runs System.Speech in a
  PowerShell child process: no package, no download, nothing sent anywhere. PowerShell 7 is
  tried first because its newer .NET also lists the newer voices (Microsoft Mark and the
  voices of added languages); Windows PowerShell 5.1 only sees the older "Desktop" voices. The
  script goes in as `-EncodedCommand`, the job (text, voice, rate, output paths) as a JSON file
  named in an environment variable, and the answer comes back as a JSON file, errors included,
  because stderr from an encoded command arrives wrapped in CLIXML. Several lines are spoken in
  one PowerShell run, since starting PowerShell costs more than speaking a sentence.
- **Voice files are media, not cache.** A spoken line is stored as
  `voiceovers/<words>-<hash>.wav`, the hash covering engine, voice, rate and text, so the same
  request reuses the file. Projects play these files, so nothing ever clears that folder.
- **Keep what a cut does to a segment consistent.** A trim, split or speed change calls
  `_rebase_keyframes` (which adds a keyframe at the cut where a value is still changing) and
  `_refit_animations`. A new operation that changes a segment's length must call both.
- **Refuse versions that are known to break.** `compat.py` holds the confirmed versions and the
  newest major version other tools report as working. Update it when a version is checked.

## How the server is started

`.mcp.json` runs `uv run --no-sync python -m capcut_mcp.server`, not `uv run capcut-mcp`.

Plain `uv run` re-installs the project when `pyproject.toml` changes, which means replacing the
`capcut-mcp.exe` launcher. Windows will not let that file be replaced while a server started
from it is still running in another session. The sync then fails, and because `uv run` syncs
first, every new session fails to connect. This happened twice: while adding the `pycapcut`
dependency with three sessions open, and again on 2026-11-18 when a user-level registration
still used `uv run ... capcut-mcp`. `--no-sync` skips the sync and `python -m` avoids the
launcher, so neither can happen. It also needs no machine-specific path.

The cost is that the environment is no longer updated automatically: run `uv sync` (with
`--extra transcribe` if the speech package is wanted) after changing dependencies. If a server
process started through the launcher is still running, close those sessions first.

## MCP SDK notes

The server uses the MCP Python SDK **2.x**, which renamed `FastMCP` to `MCPServer`
(`from mcp.server.mcpserver import MCPServer, Image`) and is pinned as `mcp>=2,<3`. A tool
returning a list sends one content block per item. Tools are marked read-only or not through
`ToolAnnotations`, which clients can use to decide what needs approval.

- **Tools run in worker threads.** A plain (non-async) tool function is run by the SDK in a
  thread, so two tool calls can run at the same moment. This is why edits take a lock.
- **Context variables reach the worker thread.** `batch` relies on this to hand its in-memory
  project to the tools it runs.
- **`batch` uses one private SDK attribute**, `mcp._tool_manager.call_tool`, to run a tool with
  the same argument checks as a direct call while getting its plain result back. The public
  `call_tool` converts the result to content blocks. If an SDK update breaks this, the test
  `test_batch_is_one_save_and_one_undo_step` fails first.
- **Only `transcribe_media` is marked open-world**, because the first use of a speech model
  downloads it. Everything else works on local files only, `add_voiceover` included.

## Tests

```
uv sync
.venv\Scripts\python.exe -m pytest
```

- Tests never touch the real CapCut folder. The `drafts` fixture points `CAPCUT_DRAFTS_DIR` and
  `CAPCUT_MCP_HOME` at a temporary folder and treats CapCut as not running.
- The `media` fixture generates small video, audio and image files with FFmpeg, so the suite
  needs FFmpeg installed and is skipped without it.
- `test_project.py` covers the basic edits, `test_features.py` the newer ones and the numeric
  details matched to CapCut, `test_library.py` reuse from the user's projects,
  `test_catalogue.py` catalogue items, and `test_server.py` drives the tools through an
  in-memory MCP client.
- `test_safety.py` covers undo, redo, checkpoints, locking, batches, the version guard and what
  cuts do to keyframes and animations. `test_assist.py` covers the sound-driven edits, the
  project check and the setup report. `test_text_and_masks.py` covers highlighted words, masks,
  chroma key, voice effects and search. `test_analysis.py` and `test_render.py` check the media
  analysis and the preview renderer against generated media, down to pixel colours.
- The speech package is not needed by the tests: `analysis._run_whisper` is replaced.
- `test_speech.py` covers voice-overs. `speech._run`, the one place that starts PowerShell, is
  replaced by a stand-in that writes silent WAV files whose length follows the text and rate.
  One test uses the real Windows voice and is skipped on a machine without voices.
- `check_integrity()` in `test_project.py` asserts what CapCut needs of any file: every
  reference resolves, segments on a track do not overlap, the duration matches.

What the tests cannot show is whether CapCut accepts a file. That needs the loop below.

## Checking against CapCut

```
.venv\Scripts\python.exe tools/feature_tour.py "<project name>" <5 videos> <image> <audio>
.venv\Scripts\python.exe tools/catalogue_tour.py "<project name>" <6 videos> <image>
.venv\Scripts\python.exe tools/new_features_tour.py "<project name>" <video with sound> <image> <audio>
```

each build a project through the real server over stdio, in CapCut's real projects folder
unless `CAPCUT_DRAFTS_DIR` is set. Open it in CapCut, close it, then:

```
python tools/diff_resave.py "<project name>"
```

lists what CapCut changed. This loop found, among others, the overlay track flag, the rounding
of sped-up clips, and the full text structure. Record results in
[capcut-format.md](capcut-format.md), marking each point Confirmed or Assumed.

## Adding a tool

1. Add the behaviour as a method on `Project` (or a function in a module of its own), raising
   `CapCutError` for bad input. Keep microseconds inside, seconds at the edge.
2. Add the tool in `server.py`. The docstring is what the assistant reads to decide when and how
   to call it, so say what it is for and what the parameters mean in the user's terms. A tool
   that changes a project is registered with `@edit_tool` and takes the project's name as
   `project`: the decorator holds the project's lock while the tool runs and makes the tool
   usable as a step of `batch` (`@edit_tool(batchable=False)` keeps it out). A tool that only
   reads is registered with `@mcp.tool(annotations=READ_ONLY)`.
3. Add tests that check the file that ends up on disk, not just the return value.
4. Add the tool to `GROUPS` in `tools/gen_tool_reference.py` and run it. The script fails if a
   tool is missing from the list.
5. If the tool writes a structure CapCut has not been seen to accept, add it to the Assumed
   table in `capcut-format.md` and to `new_features_tour.py`, and name it in the "not yet
   confirmed" line of the server's instructions so the assistant asks the user to check it.
6. If the tool changes a segment's length, call `_rebase_keyframes` and `_refit_animations`.
   If it starts a program, use `media.run_tool`.
7. Add a line to `CHANGELOG.md`, and to the usage guide if it changes how people ask for things.

## Refreshing the templates

`tools/extract_templates.py` takes the source projects as arguments. For a new CapCut version,
give it four projects saved by that version (an empty one, one with a video, one with an audio
clip, one with text), run it, and review the diff of `templates.json` before committing:

```
uv run --no-sync python tools/extract_templates.py src/capcut_mcp/templates.json "<empty>" "<video>" "<audio>" "<text>"
```

## Known gaps and ideas

- Masks, chroma key, voice effects and highlighted words have not been through the CapCut
  check yet (`tools/new_features_tour.py` builds the project for it).
- Text to speech is Windows only. macOS has `say`, which could fill the same role in
  `speech.py`; nothing has been tried there. Voices from other engines (Piper, cloud services)
  would also go behind `speech.synthesize`.
- A voice-over line is timed to its caption's start; the words inside a line are not matched to
  the individual captions. `auto_captions` on the voice track can re-time captions to it.
- Nothing has been confirmed on CapCut 9.x or on macOS.
- Keyframes are linear only; CapCut's curve presets are not written, and a keyframe added at a
  cut assumes a straight line between its neighbours.
- Rich text (several styles in one text item) is collapsed to one style when its wording changes.
- `render_preview` does not draw transitions, effects, filters, masks or library animations,
  and estimates text size.
- The media bin entry for audio files is not written; CapCut lists them on the timeline only.
- Effects and filters can only be laid on their own track; attaching one to a single clip is
  not offered.
- Text effects, text bubbles and stickers cannot be applied by name: no list of their ids is
  published. They can be copied from a project where the user used them.
- Colour adjustment, mask keyframes and speed curves are untouched.
- `render_preview` reports no progress while it runs.
- Tools return plain JSON text; none declares an output schema.
