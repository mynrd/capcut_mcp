"""The preview render, checked by reading pixels back from it, and the contact sheet."""

from __future__ import annotations

import copy
import subprocess
from pathlib import Path

import pytest

from capcut_mcp import render, schema
from capcut_mcp.draft import Project
from capcut_mcp.env import CapCutError
from capcut_mcp.media import extract_frame, probe
from capcut_mcp.render import render_preview
from capcut_mcp.sheet import contact_sheet

FPS = 30


@pytest.fixture(scope="session")
def flashes(tmp_path_factory):
    """4 s videos at 30 and 25 fps whose every frame is one flat grey, unlike its neighbours,
    so a wrong frame shows up as a different picture."""
    folder = tmp_path_factory.mktemp("flashes")
    files = {}
    for rate in (30, 25):
        files[rate] = str(folder / f"flash {rate}.mp4")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c=black:s=320x180:r={rate}:d=4,geq=lum='40+mod(N*37,160)':cb=128:cr=128",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", files[rate]], check=True)
    return files


# ------------------------------------------------------------------------- pixel helpers

def _image(path: Path) -> tuple[int, int, bytes]:
    info = probe(str(path))
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "rawvideo",
                          "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    assert len(raw) == info.width * info.height * 3
    return info.width, info.height, raw


def _frame(video: Path, number: int, folder: Path) -> tuple[int, int, bytes]:
    """Frame `number` of a rendered video. Seeking returns the first frame at or after the time,
    so aim a little before the frame's own time."""
    png = folder / f"{video.stem} {number}.png"
    extract_frame(video, max(number - 0.4, 0) / FPS, png, width=None)
    return _image(png)


def _mean(image, left: float, top: float, right: float, bottom: float) -> tuple[float, ...]:
    """Average colour of a region given as fractions of the frame."""
    width, height, raw = image
    total, count = [0, 0, 0], 0
    for y in range(int(top * height), int(bottom * height)):
        for x in range(int(left * width), int(right * width)):
            i = (y * width + x) * 3
            total[0] += raw[i]
            total[1] += raw[i + 1]
            total[2] += raw[i + 2]
            count += 1
    return tuple(c / count for c in total)


def _where(image, test) -> list[tuple[int, int]]:
    width, height, raw = image
    return [(i // 3 % width, i // 3 // width) for i in range(0, len(raw), 3)
            if test(raw[i], raw[i + 1], raw[i + 2])]


def _red(image) -> list[tuple[int, int]]:
    return _where(image, lambda r, g, b: r > 170 and g < 90 and b < 90)


def _centre(points, image) -> tuple[float, float]:
    width, height, _ = image
    return (sum(x for x, _ in points) / len(points) / width,
            sum(y for _, y in points) / len(points) / height)


def _difference(a, b) -> float:
    """Mean absolute difference per colour value, 0 to 255."""
    return sum(abs(x - y) for x, y in zip(a[2][::5], b[2][::5])) / len(a[2][::5])


# ------------------------------------------------------------------------- keyframes

def test_x_keyframes_move_a_clip_across(drafts, media, tmp_path):
    project = Project.create("Demo", 1920, 1080, FPS)
    seg = project.add_clip(media["image"], duration=2)
    project.set_canvas(1920, 1080)  # the first clip set the canvas to its own shape
    project.set_transform(seg["id"], scale=0.4)
    project.set_keyframes(seg["id"], "x", [(0, -0.5), (2, 0.5)])
    out = tmp_path / "x.mp4"
    render_preview(project, out, height=144)

    early, late = _frame(out, 3, tmp_path), _frame(out, 57, tmp_path)  # 0.1 s and 1.9 s
    # x runs -0.45 -> 0.45; x is in half-frames, so the centre sits at 0.5 + x / 2 of the width.
    assert _centre(_red(early), early)[0] == pytest.approx(0.275, abs=0.03)
    assert _centre(_red(late), late)[0] == pytest.approx(0.725, abs=0.03)
    assert _centre(_red(late), late)[1] == pytest.approx(0.5, abs=0.03)


def test_scale_and_rotation_keyframes(drafts, media, tmp_path):
    project = Project.create("Demo", 1920, 1080, FPS)
    seg = project.add_clip(media["image"], duration=4)
    project.set_canvas(1920, 1080)
    project.set_keyframes(seg["id"], "scale", [(0, 0.3), (2, 0.9)])
    project.set_keyframes(seg["id"], "rotation", [(2, 0), (4, 90)])
    out = tmp_path / "scale.mp4"
    render_preview(project, out, height=144)

    # The 4:3 picture fits the 16:9 frame by height: 144s tall and 192s wide at scale s.
    small, large = _frame(out, 3, tmp_path), _frame(out, 57, tmp_path)  # scale 0.33 and 0.87
    assert len(_red(small)) == pytest.approx(144 * 192 * 0.33 ** 2, rel=0.15)
    assert len(_red(large)) == pytest.approx(144 * 192 * 0.87 ** 2, rel=0.15)
    assert _centre(_red(small), small) == pytest.approx((0.5, 0.5), abs=0.03)

    def span(image):
        xs = [x for x, _ in _red(image)]
        return max(xs) - min(xs)
    upright = span(_frame(out, 60, tmp_path))       # 2 s: 0 degrees, 173 px wide
    tilted = span(_frame(out, 90, tmp_path))        # 3 s: 45 degrees, about 214 px wide
    sideways = span(_frame(out, 119, tmp_path))     # just before 4 s: about 90 degrees, 130 px
    assert upright == pytest.approx(173, abs=6)
    assert tilted > upright * 1.15
    assert sideways < upright * 0.85


# ------------------------------------------------------------------------- partial render

def test_partial_render_matches_the_full_render_frame_for_frame(drafts, media, flashes, tmp_path):
    project = Project.create("Demo", 1280, 720, FPS)
    project.add_clip(flashes[30])                       # 0 - 4 s
    project.set_canvas(1280, 720)
    project.add_clip(flashes[25], speed=1.5)            # 4 - 6.67 s
    overlay = project.add_clip(media["image"], track=1, start=1, duration=5)
    project.set_transform(overlay["id"], scale=0.3)
    project.set_keyframes(overlay["id"], "x", [(0, -0.8), (5, 0.8)])
    project.set_keyframes(overlay["id"], "rotation", [(0, 0), (5, 120)])
    words = project.add_text("Moving words", start=2, duration=3, size=20)
    project.fade(words["id"], fade_in=1)
    project.set_keyframes(words["id"], "y", [(0, -0.5), (3, 0.5)])
    project.add_audio(media["audio"], start=0, fade_out=1)

    full, part = tmp_path / "full.mp4", tmp_path / "part.mp4"
    render_preview(project, full, height=144)
    report = render_preview(project, part, height=144, start=2.5, end=5.5)
    assert report["duration"] == 3.0
    assert abs(probe(str(part)).duration_us / 1e6 - 3.0) < 0.1

    offset = round(2.5 * FPS)
    # The range starts inside the first clip; 44/45 straddle the cut to the sped-up 25 fps clip.
    for number in (0, 1, 2, 30, 44, 45, 46, 60, 89):
        window = _frame(part, number, tmp_path)
        same = _difference(window, _frame(full, offset + number, tmp_path))
        before = _difference(window, _frame(full, offset + number - 1, tmp_path))
        after = _difference(window, _frame(full, offset + number + 1, tmp_path))
        assert same < 2.5, f"frame {number} differs from the full render by {same:.1f}"
        assert same < min(before, after), f"frame {number} matches a neighbouring frame better"


# ------------------------------------------------------------------------- backgrounds

def test_colour_background_fills_the_bars(drafts, media, tmp_path):
    project = Project.create("Demo", 1080, 1920, FPS)
    seg = project.add_clip(media["silent"])
    project.set_canvas(1080, 1920)  # 16:9 in a 9:16 frame leaves bars above and below
    project.set_background(seg["id"], color="#00C000")
    out = tmp_path / "colour.mp4"
    render_preview(project, out, height=320)
    frame = _frame(out, 30, tmp_path)
    red, green, blue = _mean(frame, 0, 0, 1, 0.2)
    assert green == pytest.approx(192, abs=20) and red < 30 and blue < 30
    assert _mean(frame, 0, 0.8, 1, 1)[1] == pytest.approx(192, abs=20)
    assert _mean(frame, 0.1, 0.45, 0.9, 0.55) != pytest.approx((0, 192, 0), abs=40)


def test_blurred_background_fills_the_bars(drafts, media, tmp_path):
    project = Project.create("Demo", 1080, 1920, FPS)
    seg = project.add_clip(media["silent"])
    project.set_canvas(1080, 1920)
    plain = tmp_path / "plain.mp4"
    render_preview(project, plain, height=320)
    assert max(_mean(_frame(plain, 30, tmp_path), 0, 0, 1, 0.2)) < 10, "no background: black bars"

    project.set_background(seg["id"], blur=0.75)
    out = tmp_path / "blur.mp4"
    render_preview(project, out, height=320)
    frame = _frame(out, 30, tmp_path)
    for top, bottom in ((0, 0.2), (0.8, 1)):
        assert max(_mean(frame, 0, top, 1, bottom)) > 60
    # Blurred: neighbouring pixels in the bar are close, unlike the sharp bars of the clip itself.
    width, _, raw = frame
    row = 20 * width * 3
    steps = [abs(raw[row + i] - raw[row + i + 3]) for i in range(0, (width - 1) * 3, 3)]
    assert max(steps) < 40


# ------------------------------------------------------------------------- text

@pytest.mark.skipif(render.system_font() is None, reason="no font for drawtext")
def test_text_fades_in_and_follows_its_keyframes(drafts, tmp_path):
    project = Project.create("Demo", 1920, 1080, FPS)
    seg = project.add_text("MMMM", start=0, duration=3, size=30)
    project.fade(seg["id"], fade_in=1.5)
    project.set_keyframes(seg["id"], "x", [(0, -0.5), (3, 0.5)])
    out = tmp_path / "text.mp4"
    render_preview(project, out, height=144)

    def bright(image):
        return _where(image, lambda r, g, b: r > 128 and g > 128 and b > 128)
    assert bright(_frame(out, 1, tmp_path)) == [], "faded out at the start"
    middle, late = _frame(out, 30, tmp_path), _frame(out, 75, tmp_path)
    assert len(bright(middle)) > 50 and len(bright(late)) > len(bright(middle)) * 0.8
    assert _centre(bright(middle), middle)[0] == pytest.approx(0.5 - 1 / 12, abs=0.04)
    assert _centre(bright(late), late)[0] == pytest.approx(0.5 + 1 / 6, abs=0.04)


# ------------------------------------------------------------------------- running FFmpeg

def _remember_work_folders(monkeypatch) -> list[Path]:
    made = []
    original = render.tempfile.mkdtemp

    def mkdtemp(*args, **kwargs):
        made.append(Path(original(*args, **kwargs)))
        return str(made[-1])
    monkeypatch.setattr(render.tempfile, "mkdtemp", mkdtemp)
    return made


def test_work_folder_is_removed(drafts, media, tmp_path, monkeypatch):
    made = _remember_work_folders(monkeypatch)
    project = Project.create("Demo", 1920, 1080, FPS)
    project.add_clip(media["image"], duration=1)
    project.add_text("Hello", start=0, duration=1)
    render_preview(project, tmp_path / "ok.mp4", height=144)
    assert len(made) == 1 and not made[0].exists()


def test_timeout_stops_ffmpeg_and_cleans_up(drafts, media, tmp_path, monkeypatch):
    made = _remember_work_folders(monkeypatch)
    calls = []

    def slow(command, **kwargs):
        calls.append((command, kwargs))
        assert (made[-1] / "graph.txt").is_file()
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    project = Project.create("Demo", 1920, 1080, FPS)
    project.add_clip(media["image"], duration=2)
    monkeypatch.setattr(render.subprocess, "run", slow)
    with pytest.raises(CapCutError, match="shorter part with start and end"):
        render_preview(project, tmp_path / "slow.mp4", height=144)
    (command, kwargs), = calls
    assert kwargs["timeout"] == 300, "at least five minutes"
    assert command[1] == "-nostdin" and kwargs["stdin"] is subprocess.DEVNULL
    assert not made[0].exists()


def test_too_many_inputs_for_one_command_line(drafts, media, tmp_path, monkeypatch):
    def never(*args, **kwargs):
        raise AssertionError("FFmpeg should not be started")
    project = Project.create("Demo", 1920, 1080, FPS)
    seg = project.add_clip(media["image"], duration=0.2)
    track = project.tracks("video")[0]
    for number in range(1, 400):
        clone = copy.deepcopy(seg)
        clone["id"] = schema.new_id()
        clone["target_timerange"]["start"] = number * 200_000
        track["segments"].append(clone)
    real_run = render.subprocess.run
    monkeypatch.setattr(render.subprocess, "run", never)
    with pytest.raises(CapCutError, match="shorter part"):
        render_preview(project, tmp_path / "long.mp4", height=144)
    # A short part of the same timeline only opens the clips inside it.
    monkeypatch.setattr(render.subprocess, "run", real_run)
    report = render_preview(project, tmp_path / "short.mp4", height=144, start=10, end=12)
    assert report["duration"] == 2.0


# ------------------------------------------------------------------------- contact sheet

@pytest.fixture(scope="session")
def stills(media, tmp_path_factory):
    """Images of three shapes: 4:3 red, 9:16 portrait, 16:9 landscape."""
    folder = tmp_path_factory.mktemp("stills with spaces")
    portrait = extract_frame(media["video"], 1.0, folder / "portrait frame.png", width=None)
    landscape = extract_frame(media["silent"], 1.0, folder / "landscape frame.png", width=None)
    return [media["image"], str(portrait), str(landscape)]


@pytest.mark.parametrize("count, grid", [(1, (1, 1)), (5, (3, 2)), (12, (4, 3))])
def test_contact_sheet_grid(stills, tmp_path, count, grid):
    frames = [(stills[i % 3], f"{i * 2.5:.2f}s") for i in range(count)]
    out = contact_sheet(frames, tmp_path / "sheet dir" / "sheet.jpg", cell_width=200)
    columns, rows = grid
    sheet = _image(out)
    assert sheet[:2] == (200 * columns, 150 * rows), "cells take the 4:3 shape of the first image"
    # The red 4:3 image fills its cell edge to edge; the portrait one is padded at the sides.
    assert _mean(sheet, 0.4 / columns, 0.6 / rows, 0.6 / columns, 0.9 / rows)[0] > 200
    if count > 1:
        assert max(_mean(sheet, 1 / columns, 0.5 / rows, 1.1 / columns, 0.9 / rows)) < 50
    if render.system_font():
        label = _where(sheet, lambda r, g, b: r > 200 and g > 200 and b > 200)
        assert any(x < 60 and y < 25 for x, y in label), "label in the top-left corner"


def test_contact_sheet_limits(stills, tmp_path):
    assert probe(str(contact_sheet([(stills[1], "a"), (stills[0], "b")], tmp_path / "s.png",
                                   columns=1, cell_width=100))).height == 2 * 178
    with pytest.raises(CapCutError, match="1 to 24"):
        contact_sheet([], tmp_path / "none.jpg")
    with pytest.raises(CapCutError, match="1 to 24"):
        contact_sheet([(stills[0], "x")] * 25, tmp_path / "many.jpg")
    with pytest.raises(CapCutError, match="not found"):
        contact_sheet([(tmp_path / "gone.png", "x")], tmp_path / "gone.jpg")
