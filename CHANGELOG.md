# Changelog

## 0.2.0 (2026-11-18)

### Fixed

- **`undo` could destroy work done in CapCut.** It restored the older version without keeping
  the current one. The state before an undo is now kept: `redo` brings it back, and when the
  project had also been edited in CapCut, a checkpoint is saved and the result says so.
- **A new project could inherit a deleted project's history.** Stored versions were filed by
  project name only. They now carry the timeline's id and are only restored into the project
  they came from.
- **Edits sent in parallel overwrote each other or failed.** Every edit read the project,
  changed it and wrote it back with a shared temporary file name. Edits of one project now take
  turns, inside one server and across server processes.
- **Cutting inside a fade or other keyframe animation dropped keyframes.** A clip split in the
  middle of a fade-in was left invisible. A keyframe with the in-between value is now added at
  the cut.
- **Animations were not re-timed** after a trim, split or speed change, and a split copied the
  entrance and exit to both halves.
- **`render_preview` left a temporary folder behind on every run** and had no time limit.
- **FFmpeg could swallow protocol messages.** Child processes inherited the server's standard
  input, which carries the MCP protocol. It is now closed for every child process.
- **`relink_media` left CapCut's media bin pointing at the old files.** It now repoints the
  media list too, including entries no clip uses, and says when CapCut is running and may put
  its own list back.
- On macOS the check for a running CapCut matched the server's own process.

### Added

- `batch`: several edits in one save and one undo step, all or nothing.
- `redo` and `checkpoint` (named copies that are never dropped). Undo history grew from 30 to
  100 steps and is stored compressed.
- `doctor`: CapCut version, projects folder, FFmpeg and optional parts in one report.
- `check_project`: finds transitions that lead nowhere, text outside the frame, gaps on the
  main track, animations that no longer fit, missing media and more.
- A version guard: projects saved by a CapCut version the server has not been confirmed on
  carry a warning, and versions from 10 on are refused unless `CAPCUT_MCP_ALLOW_UNTESTED=1`.
- `analyze_media`: silences, scene cuts, loudness and a level graph of a media file.
- `cut_silence`: removes the pauses from a talking clip.
- `duck_audio`: lowers music while a voice plays.
- `transcribe_media` and `auto_captions`: speech to text and captions timed to the speech,
  with the optional `transcribe` extra.
- `set_mask`, `set_chroma_key`, `set_audio_effect`.
- Highlighted words in `add_text`, `add_captions` and `update_text`. A change of look no longer
  flattens text that has several styles.
- `get_timeline` can be narrowed to a track type, a track and a stretch of time, or show one
  segment in full.
- `extract_frames` and `preview_timeline` can return one labelled contact sheet.
- English search words for the mostly Chinese catalogue, and several words in one search.
- `render_preview` draws keyframed movement, scale and rotation, blurred and coloured
  backgrounds and text fades, and renders only the part of the timeline that was asked for.
- Projects stored as `draft_info.json` (macOS builds) are read and written in place, and
  CapCut's own mirror copies of the timeline (`template-2.tmp`, which 9.8 also writes under
  `Timelines/<id>/`) are kept identical.
- A third demo builder, `tools/new_features_tour.py`, for checking the new structures in
  CapCut. The demo builders now pass folder overrides on to the server they start; before,
  a test run with `CAPCUT_DRAFTS_DIR` set still wrote into the real CapCut folder.

### Changed

- `undo` returns a short summary of what is left instead of the whole timeline.
- The server is started with `uv run --no-sync python -m capcut_mcp.server`, which works from
  any checkout without a machine-specific path.
- The code is licensed under MIT. A linter configuration and a CI workflow were added.

## 0.1.0 (2026-11-17)

First version: 42 tools for building and editing CapCut desktop projects, a preview renderer,
reuse of library items from the user's projects and a catalogue of items by name.
