"""Shared fixtures: a throwaway CapCut projects folder and small generated media files."""

from __future__ import annotations

import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="FFmpeg is not installed")


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    """Paths to: a 4s video with sound, a 3s silent video, a 6s audio file, an image."""
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("FFmpeg is not installed")
    folder = tmp_path_factory.mktemp("media")
    files = {
        "video": folder / "clip a.mp4",
        "silent": folder / "silent.mp4",
        "audio": folder / "music.wav",
        "image": folder / "still.png",
    }
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x568:rate=30:duration=4",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(files["video"]))
    _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=3",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(files["silent"]))
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=220:duration=6", str(files["audio"]))
    _ffmpeg("-f", "lavfi", "-i", "color=c=red:size=400x300", "-frames:v", "1", str(files["image"]))
    return {name: str(path) for name, path in files.items()}


@pytest.fixture
def drafts(tmp_path, monkeypatch):
    """An empty projects folder; CapCut is treated as not running."""
    root = tmp_path / "drafts"
    root.mkdir()
    monkeypatch.setenv("CAPCUT_DRAFTS_DIR", str(root))
    monkeypatch.setenv("CAPCUT_MCP_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("capcut_mcp.draft.capcut_running", lambda: False)
    return root
