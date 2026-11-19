"""A contact sheet: still images tiled into one labelled grid, to look at many moments at once."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .env import CapCutError
from .media import _NO_WINDOW, _tool, probe
from .render import _quote, system_font

MAX_FRAMES = 24
_BACKDROP = "0x202020"


def _columns(count: int) -> int:
    return min(count, 3 if count <= 6 else 4 if count <= 12 else 6)


def contact_sheet(frames: list[tuple[str | Path, str]], out: Path, *,
                  columns: int | None = None, cell_width: int = 320) -> Path:
    """Tile (image file, label) pairs, in reading order, into one image saved at `out`.

    Every image is fitted into a cell with the first image's shape, padded with dark grey, and
    its label is written in the cell's top-left corner. The format follows `out`'s extension.
    Without a usable font the sheet is still made, without labels.
    """
    if not 1 <= len(frames) <= MAX_FRAMES:
        raise CapCutError(f"A contact sheet takes 1 to {MAX_FRAMES} images; {len(frames)} were given.")
    missing = [str(path) for path, _ in frames if not Path(path).is_file()]
    if missing:
        raise CapCutError("Images not found: " + ", ".join(missing[:5]))
    columns = max(1, min(int(columns or _columns(len(frames))), len(frames)))
    first = probe(frames[0][0])
    if not first.width or not first.height:
        raise CapCutError(f"{Path(frames[0][0]).name} has no picture in it.")
    width = max(2, int(cell_width) // 2 * 2)
    height = max(2, round(width * first.height / first.width / 2) * 2)
    font = system_font()
    size = max(10, round(width / 20))
    margin = max(3, size // 3)

    workdir = Path(tempfile.mkdtemp(prefix="capcut-mcp-sheet-"))
    try:
        inputs, graph = [], []
        for number, (path, label) in enumerate(frames):
            inputs += ["-i", str(path)]
            steps = [f"scale=w={width}:h={height}:force_original_aspect_ratio=decrease",
                     f"pad=w={width}:h={height}:x=(ow-iw)/2:y=(oh-ih)/2:color={_BACKDROP}",
                     "setsar=1", "format=yuv420p"]
            if font and str(label).strip():
                # The label goes through a file so that no character of it needs escaping.
                text_file = workdir / f"label{number}.txt"
                text_file.write_text(str(label), encoding="utf-8")
                steps.append(f"drawtext=textfile={_quote(text_file)}:fontfile={_quote(font)}"
                             f":expansion=none:fontsize={size}:fontcolor=white:x={margin}:y={margin}"
                             f":box=1:boxcolor=black@0.65:boxborderw={max(2, size // 4)}")
            graph.append(f"[{number}:v]{','.join(steps)}[c{number}]")
        if len(frames) == 1:
            graph.append("[c0]null[sheet]")
        else:
            layout = "|".join(f"{(i % columns) * width}_{(i // columns) * height}" for i in range(len(frames)))
            cells = "".join(f"[c{i}]" for i in range(len(frames)))
            graph.append(f"{cells}xstack=inputs={len(frames)}:layout={layout}:fill={_BACKDROP}[sheet]")
        script = workdir / "graph.txt"
        script.write_text(";\n".join(graph), encoding="utf-8")

        out.parent.mkdir(parents=True, exist_ok=True)
        quality = ["-q:v", "4"] if out.suffix.lower() in (".jpg", ".jpeg") else []
        result = None
        for graph_option in ("-/filter_complex", "-filter_complex_script"):  # FFmpeg 7+, then older
            # -nostdin and no stdin: the MCP protocol arrives on this process's stdin.
            command = [_tool("ffmpeg"), "-nostdin", "-v", "error", "-y", *inputs, graph_option,
                       str(script), "-map", "[sheet]", "-frames:v", "1", *quality, str(out)]
            try:
                result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                        text=True, encoding="utf-8", errors="replace",
                                        creationflags=_NO_WINDOW, timeout=300)
            except subprocess.TimeoutExpired:
                raise CapCutError("Making the contact sheet took too long and was stopped.") from None
            if result.returncode == 0 and out.is_file():
                break
        if result is None or result.returncode != 0 or not out.is_file():
            raise CapCutError("FFmpeg could not make the contact sheet: "
                              f"{(result.stderr if result else '').strip()[-500:]}")
        return out
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
