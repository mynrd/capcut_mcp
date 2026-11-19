"""Silence, scene cuts, loudness, level graph, transcription caching and caption grouping."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from capcut_mcp import analysis
from capcut_mcp.analysis import (audio_levels, detect_scenes, detect_silence, group_words,
                                 measure_loudness, transcribe, transcription_available)
from capcut_mcp.env import CapCutError
from capcut_mcp.media import probe

# Tone 0-2s, silence 2-3.5s, tone 3.5-5.5s, silence 5.5-6.5s (to the end).
SPEECH = "aevalsrc='if(between(t,2,3.5)+gte(t,5.5),0,0.5*sin(2*PI*440*t))':s={rate}:d=6.5"
TOLERANCE = 0.1


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


@pytest.fixture(scope="session")
def clips(tmp_path_factory):
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        pytest.skip("FFmpeg is not installed")
    folder = tmp_path_factory.mktemp("analysis")
    files = {
        "speech": folder / "speech.wav",  # 6.5s, see SPEECH
        "speech.mp4": folder / "speech.mp4",  # the same sound as AAC under a grey picture
        "tone": folder / "tone.wav",  # 4s, no silence
        "silence": folder / "silence.wav",  # 3s of digital silence
        "long": folder / "long.wav",  # 70s of silence
        "cuts.mp4": folder / "cuts.mp4",  # hard cuts at 2s and 4s, no audio
        "still.png": folder / "still.png",
    }
    _ffmpeg("-f", "lavfi", "-i", SPEECH.format(rate=16000), str(files["speech"]))
    _ffmpeg("-f", "lavfi", "-i", "color=c=gray:size=160x120:rate=25:duration=6.5",
            "-f", "lavfi", "-i", SPEECH.format(rate=48000),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(files["speech.mp4"]))
    _ffmpeg("-f", "lavfi", "-i", "aevalsrc='0.5*sin(2*PI*440*t)':s=16000:d=4", str(files["tone"]))
    _ffmpeg("-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "3", str(files["silence"]))
    _ffmpeg("-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "70", str(files["long"]))
    _ffmpeg("-f", "lavfi", "-i", "color=c=black:size=320x240:rate=25:duration=2",
            "-f", "lavfi", "-i", "color=c=white:size=320x240:rate=25:duration=2",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=2",
            "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(files["cuts.mp4"]))
    _ffmpeg("-f", "lavfi", "-i", "color=c=red:size=64x64", "-frames:v", "1", str(files["still.png"]))
    return {name: str(path) for name, path in files.items()}


def spans(items) -> list[tuple[float, float]]:
    return [(item["start"], item["end"]) for item in items]


def assert_spans(actual, expected, tolerance=TOLERANCE):
    assert len(actual) == len(expected), (actual, expected)
    for (a, b), (x, y) in zip(actual, expected):
        assert abs(a - x) <= tolerance and abs(b - y) <= tolerance, (actual, expected)


# ---------------------------------------------------------------------------- silence

def test_silences_and_sounds_match_the_truth(clips):
    result = detect_silence(clips["speech"])
    assert result["analysed"] == {"start": 0.0, "end": 6.5}
    assert_spans(spans(result["silences"]), [(2, 3.5), (5.5, 6.5)])
    assert_spans(spans(result["sounds"]), [(0, 2), (3.5, 5.5)])
    assert result["silent_seconds"] == pytest.approx(2.5, abs=2 * TOLERANCE)
    assert result["file"].endswith("speech.wav")


def test_a_window_reports_file_times_not_window_times(clips):
    result = detect_silence(clips["speech"], start=1, end=6)
    assert result["analysed"] == {"start": 1.0, "end": 6.0}
    # The second silence runs to the end of the window.
    assert_spans(spans(result["silences"]), [(2, 3.5), (5.5, 6.0)])
    assert_spans(spans(result["sounds"]), [(1, 2), (3.5, 5.5)])
    assert result["silences"][-1]["end"] == 6.0


def test_a_window_ending_inside_a_silence(clips):
    result = detect_silence(clips["speech"], start=1, end=3)
    assert_spans(spans(result["silences"]), [(2, 3)])
    assert_spans(spans(result["sounds"]), [(1, 2)])


def test_silence_in_a_video_with_compressed_audio(clips):
    length = round(probe(clips["speech.mp4"]).duration_us / 1e6, 3)  # about 6.5 with -shortest
    result = detect_silence(clips["speech.mp4"], start=3, end=10)
    assert result["analysed"] == {"start": 3.0, "end": length}  # end is cut to the file's length
    assert_spans(spans(result["silences"]), [(3, 3.5), (5.5, length)])
    assert_spans(spans(result["sounds"]), [(3.5, 5.5)])


def test_a_file_without_silence(clips):
    result = detect_silence(clips["tone"])
    assert result["silences"] == []
    assert spans(result["sounds"]) == [(0.0, 4.0)]
    assert result["silent_seconds"] == 0


def test_a_completely_silent_file(clips):
    result = detect_silence(clips["silence"])
    assert spans(result["silences"]) == [(0.0, 3.0)]
    assert result["sounds"] == []
    assert result["silent_seconds"] == 3.0


def test_a_silence_left_open_at_the_end_runs_to_the_end_of_the_range():
    # FFmpeg before 7.x prints no silence_end for a silence that lasts until the input ends.
    log = ("[silencedetect @ 0x1] silence_start: 1.5\n"
           "[silencedetect @ 0x1] silence_end: 2 | silence_duration: 0.5\n"
           "[silencedetect @ 0x1] silence_start: 3.25\n")
    assert analysis._parse_silences(log, 4.0) == [(1.5, 2.0), (3.25, 4.0)]
    assert analysis._parse_silences("silence_start: -0.002\n", 2.0) == [(0.0, 2.0)]
    assert analysis._parse_silences("nothing here", 2.0) == []


def test_silence_needs_audio(clips):
    with pytest.raises(CapCutError, match="no audio track"):
        detect_silence(clips["cuts.mp4"])
    with pytest.raises(CapCutError, match="still image"):
        detect_silence(clips["still.png"])
    with pytest.raises(CapCutError, match="not found"):
        detect_silence(clips["speech"] + ".missing.wav")


def test_a_slow_analysis_suggests_a_shorter_range(clips, monkeypatch):
    monkeypatch.setattr(analysis, "_timeout", lambda span: 0.001)
    with pytest.raises(CapCutError, match="shorter part"):
        detect_silence(clips["speech"])


def test_the_timeout_grows_with_the_range_and_is_capped():
    assert analysis._timeout(10) == 80
    assert analysis._timeout(5000) == 1800


@pytest.mark.parametrize("kwargs, message", [
    ({"start": -1}, "0 or more"),
    ({"start": 7}, "past the end"),
    ({"start": 3, "end": 2}, "after start"),
    ({"start": "soon"}, "must be a number"),
    ({"end": float("nan")}, "must be a number"),
    ({"noise_db": 10}, "negative"),
    ({"min_silence": 0}, "at least 0.01"),
])
def test_silence_rejects_bad_input(clips, kwargs, message):
    with pytest.raises(CapCutError, match=message):
        detect_silence(clips["speech"], **kwargs)


# ----------------------------------------------------------------------------- scenes

def test_scene_cuts_are_found_at_the_right_time(clips):
    result = detect_scenes(clips["cuts.mp4"])
    assert result["analysed"] == {"start": 0.0, "end": 6.0}
    assert result["truncated"] is False
    times = [cut["time"] for cut in result["cuts"]]
    assert len(times) == 2 and times == sorted(times)
    assert times[0] == pytest.approx(2.0, abs=0.05)
    assert times[1] == pytest.approx(4.0, abs=0.05)
    assert all(0.3 < cut["score"] <= 1 for cut in result["cuts"])


def test_scene_window_reports_file_times(clips):
    result = detect_scenes(clips["cuts.mp4"], start=1, end=3.5)
    assert result["analysed"] == {"start": 1.0, "end": 3.5}
    assert [round(cut["time"], 1) for cut in result["cuts"]] == [2.0]


def test_scene_limit_keeps_the_strongest_cuts(clips):
    every = detect_scenes(clips["cuts.mp4"])["cuts"]
    strongest = max(every, key=lambda cut: cut["score"])
    result = detect_scenes(clips["cuts.mp4"], limit=1)
    assert result["truncated"] is True
    assert result["cuts"] == [strongest]


def test_scene_detection_needs_video(clips):
    with pytest.raises(CapCutError, match="audio file"):
        detect_scenes(clips["speech"])
    with pytest.raises(CapCutError, match="still image"):
        detect_scenes(clips["still.png"])
    with pytest.raises(CapCutError, match="between 0 and 1"):
        detect_scenes(clips["cuts.mp4"], threshold=1.5)
    with pytest.raises(CapCutError, match="at least 1"):
        detect_scenes(clips["cuts.mp4"], limit=0)


# --------------------------------------------------------------------------- loudness

def test_loudness_of_a_tone(clips):
    result = measure_loudness(clips["tone"])
    assert -30 < result["integrated_lufs"] < 0
    assert result["true_peak_db"] == pytest.approx(-6.0, abs=1.0)  # amplitude 0.5
    assert result["loudness_range_lu"] is not None and result["loudness_range_lu"] >= 0
    assert "note" not in result


def test_loudness_of_a_video_soundtrack(clips):
    result = measure_loudness(clips["speech.mp4"])
    assert -30 < result["integrated_lufs"] < 0


def test_loudness_of_silence_is_none(clips):
    result = measure_loudness(clips["silence"])
    assert result["integrated_lufs"] is None
    assert result["true_peak_db"] is None
    assert result["loudness_range_lu"] is None
    assert "silent" in result["note"]


def test_loudness_of_a_silent_window(clips):
    result = measure_loudness(clips["speech"], start=2.2, end=3.3)
    assert result["analysed"] == {"start": 2.2, "end": 3.3}
    assert result["integrated_lufs"] is None


def test_loudness_of_a_range_too_short_to_measure(clips):
    result = measure_loudness(clips["tone"], start=1, end=1.2)
    assert result["integrated_lufs"] is None
    assert result["true_peak_db"] is not None
    assert "0.4 seconds" in result["note"]


# ----------------------------------------------------------------------------- levels

def test_levels_show_loud_and_quiet_parts(clips):
    result = audio_levels(clips["speech"])
    assert result["start"] == 0.0 and result["step"] == 0.5
    db = result["db"]
    assert len(db) == 13 and all(isinstance(value, int) for value in db)
    loud = db[0:4] + db[7:11]  # 0-2s and 3.5-5.5s
    assert all(-15 < value <= 0 for value in loud), db
    assert db[5] == analysis.SILENCE_FLOOR_DB  # 2.5-3s
    assert db[12] == analysis.SILENCE_FLOOR_DB  # 6-6.5s
    assert min(loud) - max(db[5], db[12]) > 50


def test_levels_window(clips):
    result = audio_levels(clips["speech"], start=3.5, end=5.5, step=0.25)
    assert result["start"] == 3.5 and result["step"] == 0.25
    assert len(result["db"]) == 8
    assert all(value > -15 for value in result["db"])


def test_levels_raise_the_step_to_stay_readable(clips):
    result = audio_levels(clips["long"], step=0.05)  # 70s / 0.05 = 1400 buckets
    assert result["step"] > 0.05
    assert len(result["db"]) <= analysis.MAX_LEVEL_BUCKETS
    assert len(result["db"]) * result["step"] >= 70 - result["step"]
    assert set(result["db"]) == {analysis.SILENCE_FLOOR_DB}


def test_levels_reject_bad_input(clips):
    with pytest.raises(CapCutError, match="at least 0.05"):
        audio_levels(clips["speech"], step=0.01)
    with pytest.raises(CapCutError, match="no audio track"):
        audio_levels(clips["cuts.mp4"])


# ------------------------------------------------------------------------- transcribe

FAKE_WORDS = [
    {"word": " Hello", "start": 0.2, "end": 0.6},
    {"word": " there.", "start": 0.7, "end": 1.1},
    {"word": "   ", "start": 1.2, "end": 1.3},
    {"word": " Second", "start": 3.6, "end": 4.0},
    {"word": " part", "start": 4.1, "end": 4.5},
]
FAKE_SEGMENTS = [
    {"start": 0.2, "end": 1.1, "text": " Hello there. "},
    {"start": 3.6, "end": 4.5, "text": " Second part"},
]


@pytest.fixture
def whisper(monkeypatch, tmp_path):
    """Replace the backend with a fake that records its calls; cache in a temporary home."""
    monkeypatch.setenv("CAPCUT_MCP_HOME", str(tmp_path / "home"))
    calls = []

    def fake(file, model, language):
        calls.append((file, model, language))
        return {"language": language or "en", "words": [dict(w) for w in FAKE_WORDS],
                "segments": [dict(s) for s in FAKE_SEGMENTS]}

    monkeypatch.setattr(analysis, "_run_whisper", fake)
    return calls


def test_transcribe_cleans_words_and_caches_the_result(clips, whisper, tmp_path):
    first = transcribe(clips["speech"])
    assert first["cached"] is False
    assert first["language"] == "en" and first["model"] == "base"
    assert [w["word"] for w in first["words"]] == ["Hello", "there.", "Second", "part"]
    assert first["segments"][0] == {"start": 0.2, "end": 1.1, "text": "Hello there."}

    second = transcribe(clips["speech"])
    assert second["cached"] is True
    assert second["words"] == first["words"] and second["segments"] == first["segments"]
    assert len(whisper) == 1

    stored = list((tmp_path / "home" / "transcripts").glob("*.json"))
    assert len(stored) == 1
    assert json.loads(stored[0].read_text(encoding="utf-8"))["words"] == first["words"]


def test_transcribe_filters_to_the_range_in_file_times(clips, whisper):
    transcribe(clips["speech"])
    part = transcribe(clips["speech"], start=3.5, end=5)
    assert part["cached"] is True
    assert [w["word"] for w in part["words"]] == ["Second", "part"]
    assert [s["text"] for s in part["segments"]] == ["Second part"]
    assert part["words"][0]["start"] == 3.6  # times stay file times

    straddling = transcribe(clips["speech"], start=0.65, end=3)
    assert [w["word"] for w in straddling["words"]] == ["there."]  # midpoint rule
    assert [s["text"] for s in straddling["segments"]] == ["Hello there."]  # overlap rule
    assert len(whisper) == 1


def test_transcribe_cache_key_covers_model_language_and_file_changes(clips, whisper, tmp_path):
    copy = tmp_path / "copy.wav"
    shutil.copy(clips["speech"], copy)
    transcribe(copy)
    transcribe(copy, model="tiny")
    transcribe(copy, language="EN")
    assert [call[1:] for call in whisper] == [("base", None), ("tiny", None), ("base", "en")]
    assert transcribe(copy, language="en")["cached"] is True

    stat = copy.stat()
    os.utime(copy, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    assert transcribe(copy)["cached"] is False
    assert len(whisper) == 4


def test_a_damaged_cache_file_is_ignored(clips, whisper, tmp_path):
    transcribe(clips["speech"])
    for stored in (tmp_path / "home" / "transcripts").glob("*.json"):
        stored.write_text("{not json", encoding="utf-8")
    assert transcribe(clips["speech"])["cached"] is False
    assert len(whisper) == 2


def test_transcribe_without_the_package_says_how_to_install_it(clips, monkeypatch, tmp_path):
    monkeypatch.setenv("CAPCUT_MCP_HOME", str(tmp_path / "home"))
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # makes the import fail
    analysis._whisper_model.cache_clear()
    assert transcription_available() is False
    with pytest.raises(CapCutError, match=r"uv sync --extra transcribe"):
        transcribe(clips["speech"])
    analysis._whisper_model.cache_clear()


def test_transcribe_rejects_bad_input(clips, whisper):
    with pytest.raises(CapCutError, match="no audio track"):
        transcribe(clips["cuts.mp4"])
    with pytest.raises(CapCutError, match="model must be"):
        transcribe(clips["speech"], model=" ")
    with pytest.raises(CapCutError, match="after start"):
        transcribe(clips["speech"], start=2, end=1)
    assert whisper == []


# ----------------------------------------------------------------------- group_words

def w(word, start, end):
    return {"word": word, "start": start, "end": end}


def test_a_pause_starts_a_new_line():
    lines = group_words([w("Hello", 0, 0.4), w("world", 0.5, 0.9), w("again", 2.0, 2.4)])
    assert lines == [{"start": 0.0, "end": 0.9, "text": "Hello world"},
                     {"start": 2.0, "end": 2.4, "text": "again"}]


def test_lines_stay_within_max_chars():
    words = [w(f"word{i}", i * 0.3, i * 0.3 + 0.25) for i in range(20)]
    lines = group_words(words, max_chars=16, max_duration=60)
    assert all(len(line["text"]) <= 16 for line in lines)
    assert " ".join(line["text"] for line in lines) == " ".join(x["word"] for x in words)


def test_lines_stay_within_max_duration():
    words = [w("go", i * 0.5, i * 0.5 + 0.45) for i in range(12)]
    lines = group_words(words, max_chars=200, max_duration=2.0)
    assert len(lines) > 1
    assert all(line["end"] - line["start"] <= 2.0 for line in lines)


def test_a_sentence_end_closes_a_long_enough_line():
    words = [w("This", 0, 0.2), w("is", 0.25, 0.4), w("a", 0.45, 0.5), w("long", 0.55, 0.8),
             w("sentence.", 0.85, 1.3), w("Next", 1.35, 1.6)]
    lines = group_words(words, max_chars=32)
    assert [line["text"] for line in lines] == ["This is a long sentence.", "Next"]

    short = group_words([w("Hi.", 0, 0.3), w("there", 0.35, 0.6)], max_chars=32)
    assert [line["text"] for line in short] == ["Hi. there"]  # 3 chars < half of 32

    quoted = group_words([w("He", 0, 0.2), w("said", 0.25, 0.5), w('"stop!"', 0.55, 0.9),
                          w("Then", 1.0, 1.2)], max_chars=20)
    assert [line["text"] for line in quoted] == ['He said "stop!"', "Then"]


def test_cjk_is_joined_without_spaces():
    words = [w("我们", 0, 0.3), w("明天", 0.3, 0.6), w("去", 0.6, 0.8), w("东京", 0.8, 1.1),
             w("吧。", 1.1, 1.3), w("好", 1.4, 1.6)]
    lines = group_words(words, max_chars=12)
    assert [line["text"] for line in lines] == ["我们明天去东京吧。", "好"]  # sentence end, 9 >= 6

    narrow = group_words(words, max_chars=8)
    assert [line["text"] for line in narrow] == ["我们明天去东京", "吧。好"]
    assert group_words([w("用", 0, 0.2), w("iPhone", 0.2, 0.6), w("拍", 0.6, 0.8)])[0]["text"] \
        == "用iPhone拍"


def test_a_word_longer_than_max_chars_gets_its_own_line():
    long_word = "supercalifragilisticexpialidocious"
    lines = group_words([w("a", 0, 0.2), w(long_word, 0.25, 1.0), w("b", 1.05, 1.2)], max_chars=10)
    assert [line["text"] for line in lines] == ["a", long_word, "b"]


def test_lines_are_never_empty_or_zero_length():
    lines = group_words([w("  ", 0, 0.1), w("a", 1, 1), w("", 2, 3)])
    assert lines == [{"start": 1.0, "end": 1.3, "text": "a"}]

    crowded = group_words([w("x" * 40, 1, 1), w("y", 1, 1), w("z", 1.1, 1.4)], max_chars=32)
    assert crowded
    assert all(line["text"] and line["end"] > line["start"] for line in crowded)
    assert "".join(line["text"].replace(" ", "") for line in crowded) == "x" * 40 + "yz"
    assert all(a["end"] <= b["start"] for a, b in zip(crowded, crowded[1:]))

    assert group_words([]) == []


def test_words_out_of_order_are_sorted():
    lines = group_words([w("world", 0.5, 0.9), w("Hello", 0, 0.4)])
    assert lines == [{"start": 0.0, "end": 0.9, "text": "Hello world"}]


@pytest.mark.parametrize("words, kwargs, message", [
    ([], {"max_chars": 0}, "max_chars"),
    ([], {"max_duration": 0}, "max_duration"),
    ([], {"max_gap": -1}, "max_gap"),
    ("hello", {}, "must be a list"),
    ([{"word": "hi", "start": 0}], {}, "needs a start and an end"),
    (["hi"], {}, "Word 1"),
])
def test_group_words_rejects_bad_input(words, kwargs, message):
    with pytest.raises(CapCutError, match=message):
        group_words(words, **kwargs)
