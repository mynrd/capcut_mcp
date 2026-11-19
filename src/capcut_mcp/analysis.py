"""Facts about a media file for an assistant that cannot hear or watch it.

Silences, scene cuts, loudness, a coarse level graph and speech-to-text. Every time going in or
coming out is in seconds inside the media file (source time). When `start`/`end` narrow the
range, FFmpeg sees the range as starting at 0 (`-ss` before `-i` resets timestamps), so the
offset is added back before anything is returned.
"""

from __future__ import annotations

import functools
import hashlib
import importlib
import importlib.util
import json
import math
import os
import re
import subprocess
from pathlib import Path

from .env import CapCutError, data_dir
from .media import _NO_WINDOW, MediaInfo, _tool, probe

SILENCE_FLOOR_DB = -90
MAX_LEVEL_BUCKETS = 1200
MIN_LEVEL_STEP = 0.05
_LEVEL_RATE = 16000  # resample rate for audio_levels; keeps everything below 8 kHz
_EDGE_SNAP = 0.05  # a silence this close to the range edge is taken to reach it
_GATE_LUFS = -70.0  # EBU R128 absolute gate: nothing quieter is measured


# ------------------------------------------------------------------------------ silence

def detect_silence(path, *, start: float = 0.0, end: float | None = None,
                   noise_db: float = -35.0, min_silence: float = 0.4) -> dict:
    """Find the quiet stretches of a file's audio, and the parts with sound between them.

    A silence is at least `min_silence` seconds below `noise_db` dBFS. `sounds` is everything
    in the analysed range that is not silence.
    """
    info = _with_audio(path)
    start, end = _range(info, start, end)
    noise_db = _number(noise_db, "noise_db", "a level in dB")
    if not -120 <= noise_db < 0:
        raise CapCutError(
            f"noise_db must be a negative level in dB between -120 and 0, not {noise_db}. "
            "Around -35 suits speech; use -45 or lower for quiet recordings.")
    min_silence = _number(min_silence, "min_silence", "seconds")
    if min_silence < 0.01:
        raise CapCutError(f"min_silence must be at least 0.01 seconds, not {min_silence}.")

    result = _ffmpeg(info, start, end, audio=True, what="Silence detection",
                     filters=[f"silencedetect=noise={noise_db:.2f}dB:d={min_silence:.3f}"])
    silences = []
    for rel_start, rel_end in _parse_silences(result.stderr, end - start):
        a, b = max(start, rel_start + start), min(end, rel_end + start)
        if a - start < _EDGE_SNAP:
            a = start
        if end - b < _EDGE_SNAP:
            b = end
        if b - a >= 0.001:
            if silences and a <= silences[-1][1]:
                silences[-1] = (silences[-1][0], max(silences[-1][1], b))
            else:
                silences.append((a, b))

    sounds, cursor = [], start
    for a, b in silences:
        if a - cursor >= 0.001:
            sounds.append((cursor, a))
        cursor = max(cursor, b)
    if end - cursor >= 0.001:
        sounds.append((cursor, end))

    return {
        "file": str(info.path),
        "analysed": _span(start, end),
        "silences": [_span(a, b) for a, b in silences],
        "sounds": [_span(a, b) for a, b in sounds],
        "silent_seconds": round(sum(b - a for a, b in silences), 3),
    }


_SILENCE_EVENT = re.compile(r"silence_(start|end):\s*([-+0-9.eE]+|-?inf|nan)")


def _parse_silences(stderr: str, span: float) -> list[tuple[float, float]]:
    """(start, end) pairs, relative to the analysed range, from silencedetect's log.

    A silence still open when the input ends has no `silence_end` in FFmpeg before 7.x; it runs
    to the end of the range.
    """
    spans, open_at = [], None
    for kind, value in _SILENCE_EVENT.findall(stderr):
        try:
            seconds = float(value)
        except ValueError:
            continue
        if not math.isfinite(seconds):
            continue
        if kind == "start":
            open_at = seconds if open_at is None else open_at
        else:
            spans.append((max(0.0, open_at if open_at is not None else 0.0), seconds))
            open_at = None
    if open_at is not None:
        spans.append((max(0.0, open_at), span))
    return spans


# ------------------------------------------------------------------------------- scenes

def detect_scenes(path, *, start: float = 0.0, end: float | None = None,
                  threshold: float = 0.3, limit: int = 200) -> dict:
    """Times where the picture changes abruptly (hard cuts), with FFmpeg's scene score 0-1.

    Lower `threshold` finds softer changes. At most `limit` cuts are returned: the strongest,
    in time order, with `truncated` set when some were left out.
    """
    info = probe(path)
    if info.kind == "audio":
        raise CapCutError(f"{info.path.name} is an audio file; scene detection needs a video.")
    if info.kind == "photo":
        raise CapCutError(f"{info.path.name} is a still image, so it has no scene changes. "
                          "Scene detection needs a video.")
    start, end = _range(info, start, end)
    threshold = _number(threshold, "threshold", "a score")
    if not 0 < threshold < 1:
        raise CapCutError(
            f"threshold must be between 0 and 1, not {threshold}. 0.3 finds clear cuts; "
            "try 0.15-0.2 for softer changes or 0.5 for only the starkest ones.")
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        raise CapCutError(f"limit must be a whole number, not {limit!r}.") from None
    if limit < 1:
        raise CapCutError(f"limit must be at least 1, not {limit}.")

    result = _ffmpeg(info, start, end, audio=False, what="Scene detection", quiet=True,
                     filters=[f"scale=320:-2,select='gt(scene,{threshold:.4f})',metadata=print:file=-"])
    cuts = []
    for time, value in _metadata(result.stdout, "lavfi.scene_score"):
        try:
            score = float(value)
        except ValueError:
            continue
        absolute = time + start
        if start < absolute < end:
            cuts.append((absolute, score))

    truncated = len(cuts) > limit
    if truncated:
        cuts = sorted(cuts, key=lambda c: (-c[1], c[0]))[:limit]
    cuts.sort()
    return {
        "file": str(info.path),
        "analysed": _span(start, end),
        "cuts": [{"time": round(t, 3), "score": round(s, 3)} for t, s in cuts],
        "truncated": truncated,
    }


# ----------------------------------------------------------------------------- loudness

def measure_loudness(path, *, start: float = 0.0, end: float | None = None) -> dict:
    """EBU R128 loudness: integrated LUFS, true peak dBTP and loudness range LU.

    A value is None when there is nothing to measure (silence, or a range under 0.4 seconds).
    """
    info = _with_audio(path)
    start, end = _range(info, start, end)
    # framelog arrived in FFmpeg 4.4; without it every 100 ms block is logged.
    result = _ffmpeg(info, start, end, audio=True, what="Loudness measurement",
                     filters=["ebur128=peak=true:framelog=quiet", "ebur128=peak=true"])
    log = result.stderr
    at = log.rfind("Summary:")
    if at < 0:
        raise CapCutError(f"FFmpeg did not report a loudness summary for {info.path.name}: "
                          f"{log.strip()[-300:]}")
    summary = log[at:]
    integrated = _summary_value(summary, r"^\s*I:\s*(\S+)\s*LUFS")
    loudness_range = _summary_value(summary, r"^\s*LRA:\s*(\S+)\s*LU\b")
    true_peak = _summary_value(summary, r"^\s*Peak:\s*(\S+)\s*dBFS")
    if integrated is not None and integrated <= _GATE_LUFS + 0.05:
        integrated = None  # ebur128 prints -70.0 when no block passed the gate
    if integrated is None:
        loudness_range = None

    report = {
        "file": str(info.path),
        "analysed": _span(start, end),
        "integrated_lufs": _round(integrated, 1),
        "true_peak_db": _round(true_peak, 1),
        "loudness_range_lu": _round(loudness_range, 1),
    }
    if integrated is None:
        if end - start < 0.4:
            report["note"] = ("The range is shorter than 0.4 seconds, too short for integrated "
                              "loudness. Widen start/end.")
        elif true_peak is None:
            report["note"] = "This range is silent."
        else:
            report["note"] = "Everything in this range is quieter than -70 LUFS: effectively silent."
    return report


def _summary_value(summary: str, pattern: str) -> float | None:
    match = re.search(pattern, summary, re.MULTILINE)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    return value if math.isfinite(value) else None


# ------------------------------------------------------------------------------- levels

def audio_levels(path, *, start: float = 0.0, end: float | None = None,
                 step: float = 0.5) -> dict:
    """RMS level in dBFS for each `step` seconds, so loud and quiet parts can be seen as numbers.

    `db[i]` covers `start + i*step` to `start + (i+1)*step`; silence reads -90. More than 1200
    buckets would be too many to read, so `step` is raised to fit and the step used is returned.
    """
    info = _with_audio(path)
    start, end = _range(info, start, end)
    step = _number(step, "step", "seconds")
    if step < MIN_LEVEL_STEP:
        raise CapCutError(f"step must be at least {MIN_LEVEL_STEP} seconds, not {step}.")
    span = end - start
    if math.ceil(span / step - 1e-9) > MAX_LEVEL_BUCKETS:
        step = math.ceil(span / MAX_LEVEL_BUCKETS * 10 - 1e-9) / 10
        while math.ceil(span / step - 1e-9) > MAX_LEVEL_BUCKETS:
            step = round(step + 0.1, 1)
    samples = max(1, round(step * _LEVEL_RATE))
    step = samples / _LEVEL_RATE
    count = max(1, math.ceil(span / step - 1e-9))

    key = "lavfi.astats.Overall.RMS_level"
    tail = f"ametadata=print:key={key}:file=-"
    head = f"aresample={_LEVEL_RATE},asetnsamples=n={samples}:p=0"
    result = _ffmpeg(info, start, end, audio=True, what="Measuring audio levels", quiet=True,
                     filters=[
                         f"{head},astats=metadata=1:reset=1:measure_perchannel=none:"
                         f"measure_overall=RMS_level,{tail}",
                         f"{head},astats=metadata=1:reset=1,{tail}",  # FFmpeg before 4.4
                     ])
    db = [SILENCE_FLOOR_DB] * count
    for time, value in _metadata(result.stdout, key):
        index = round(time / step)
        if not 0 <= index < count:
            continue
        try:
            level = float(value)
        except ValueError:
            continue
        db[index] = max(SILENCE_FLOOR_DB, round(level)) if math.isfinite(level) else SILENCE_FLOOR_DB
    return {"file": str(info.path), "start": round(start, 3), "step": round(step, 6), "db": db}


# ------------------------------------------------------------------------- transcription

def transcription_available() -> bool:
    """Whether the optional faster-whisper package is installed (without importing it)."""
    importlib.invalidate_caches()
    try:
        return importlib.util.find_spec("faster_whisper") is not None
    except (ImportError, ValueError):
        return False


def transcribe(path, *, language: str | None = None, model: str = "base",
               start: float = 0.0, end: float | None = None) -> dict:
    """Speech to text with word timestamps, using faster-whisper (an optional extra).

    The whole file is transcribed once and cached; `start`/`end` only choose what is returned.
    A word is returned when its midpoint is in the range, a segment when it overlaps the range.
    """
    info = _with_audio(path)
    start, end = _range(info, start, end)
    if not isinstance(model, str) or not model.strip():
        raise CapCutError("model must be a Whisper model name such as tiny, base, small, "
                          "medium or large-v3.")
    model = model.strip()
    if language is not None:
        if not isinstance(language, str):
            raise CapCutError("language must be a language code such as en, es or ja, "
                              "or left out to detect it.")
        language = language.strip().lower() or None

    cache_file = _cache_path(info.path, model, language)
    full = _read_cache(cache_file)
    cached = full is not None
    if full is None:
        raw = _run_whisper(info.path, model, language)
        full = {
            "file": str(info.path),
            "model": model,
            "requested_language": language,
            "language": raw.get("language") or language,
            "words": _clean_words(raw.get("words") or []),
            "segments": _clean_segments(raw.get("segments") or []),
        }
        _write_cache(cache_file, full)

    words = [w for w in full["words"] if start <= (w["start"] + w["end"]) / 2 <= end]
    segments = [s for s in full["segments"] if s["end"] > start and s["start"] < end]
    return {
        "file": str(info.path),
        "language": full.get("language"),
        "model": model,
        "cached": cached,
        "words": words,
        "segments": segments,
    }


def _install_hint() -> str:
    root = Path(__file__).resolve().parents[2]
    where = f" ({root})" if (root / "pyproject.toml").is_file() else ""
    return (
        "Transcription is an optional extra that is not installed. To add it, run "
        f"`uv sync --extra transcribe` in the capcut-mcp server's folder{where}, then try "
        "again (restart the MCP server if this message persists). The first transcription "
        "also downloads the Whisper model, which needs an internet connection."
    )


def _run_whisper(file: Path, model: str, language: str | None) -> dict:
    """Transcribe the whole file: {"language", "words": [{word,start,end}], "segments": [...]}.

    The only place that touches faster-whisper, so tests can replace it.
    """
    device = os.environ.get("CAPCUT_MCP_WHISPER_DEVICE") or "cpu"
    compute = os.environ.get("CAPCUT_MCP_WHISPER_COMPUTE") or "int8"
    whisper = _whisper_model(model, device, compute)
    words, segments = [], []
    try:
        pieces, details = whisper.transcribe(
            str(file), language=language, word_timestamps=True, vad_filter=True)
        for piece in pieces:  # a generator: the work happens while iterating
            segments.append({"start": piece.start, "end": piece.end, "text": piece.text})
            words.extend({"word": w.word, "start": w.start, "end": w.end}
                         for w in (piece.words or []))
    except ValueError as exc:
        if language and "language" in str(exc).lower():
            raise CapCutError(
                f"Whisper does not know the language code {language!r}. Use a code such as "
                "en, es, fr, de, ja, zh or ko, or leave language out to detect it.") from exc
        raise CapCutError(f"Transcribing {file.name} failed: {exc}") from exc
    except Exception as exc:  # decoding and runtime errors from the backend
        raise CapCutError(f"Transcribing {file.name} failed: {exc}") from exc
    return {"language": language or details.language, "words": words, "segments": segments}


@functools.lru_cache(maxsize=1)
def _whisper_model(model: str, device: str, compute: str):
    if not transcription_available():
        raise CapCutError(_install_hint())
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:  # installed but broken, e.g. a native library failed to load
        raise CapCutError(
            f"faster-whisper is installed but could not be loaded ({exc}). Reinstall it with "
            "`uv sync --extra transcribe` in the capcut-mcp server's folder.") from exc
    try:
        return WhisperModel(model, device=device, compute_type=compute)
    except Exception as exc:
        raise CapCutError(
            f"Could not load the Whisper model {model!r} (device {device}, compute type "
            f"{compute}): {exc}. Use a model name such as tiny, base, small, medium or "
            "large-v3. The first use of a model downloads it, which needs an internet "
            "connection. CAPCUT_MCP_WHISPER_DEVICE and CAPCUT_MCP_WHISPER_COMPUTE override "
            "the device (cpu) and compute type (int8).") from exc


def _cache_path(file: Path, model: str, language: str | None) -> Path:
    stat = file.stat()
    key = json.dumps([os.path.normcase(str(file)), stat.st_size, stat.st_mtime_ns,
                      model, language or ""])
    return data_dir() / "transcripts" / f"{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"


def _read_cache(cache_file: Path) -> dict | None:
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("words"), list) \
            or not isinstance(data.get("segments"), list):
        return None
    return data


def _write_cache(cache_file: Path, data: dict) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache_file.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, cache_file)


def _clean_words(words: list) -> list[dict]:
    out = []
    for word in words:
        text = str(word.get("word") or "").strip()
        a, b = _finite(word.get("start")), _finite(word.get("end"))
        if text and a is not None and b is not None:
            out.append({"word": text, "start": round(a, 3), "end": round(max(a, b), 3)})
    return out


def _clean_segments(segments: list) -> list[dict]:
    out = []
    for segment in segments:
        text = str(segment.get("text") or "").strip()
        a, b = _finite(segment.get("start")), _finite(segment.get("end"))
        if text and a is not None and b is not None:
            out.append({"start": round(a, 3), "end": round(max(a, b), 3), "text": text})
    return out


# ------------------------------------------------------------------------------ captions

_SENTENCE_END = set(".?!…。？！")
_CLOSING = "\"'”’»)]}）】」』"


def group_words(words: list[dict], *, max_chars: int = 32, max_duration: float = 4.0,
                max_gap: float = 0.6) -> list[dict]:
    """Turn word timestamps into caption lines [{"start", "end", "text"}].

    A new line starts when the pause before a word is longer than `max_gap`, when the word
    would make the line longer than `max_chars` characters or `max_duration` seconds, and after
    a sentence ends once the line is at least half of `max_chars`. CJK text is joined without
    spaces. A word longer than `max_chars` gets a line of its own.
    """
    try:
        max_chars = int(max_chars)
    except (TypeError, ValueError):
        raise CapCutError(f"max_chars must be a whole number, not {max_chars!r}.") from None
    if max_chars < 1:
        raise CapCutError(f"max_chars must be at least 1, not {max_chars}.")
    max_duration = _number(max_duration, "max_duration", "seconds")
    if max_duration <= 0:
        raise CapCutError(f"max_duration must be more than 0 seconds, not {max_duration}.")
    max_gap = _number(max_gap, "max_gap", "seconds")
    if max_gap < 0:
        raise CapCutError(f"max_gap must be 0 seconds or more, not {max_gap}.")
    if not isinstance(words, list):
        raise CapCutError("words must be a list of {word, start, end} items, as transcribe returns.")

    clean = []
    for number, word in enumerate(words, 1):
        if not isinstance(word, dict):
            raise CapCutError(f"Word {number} is not a {{word, start, end}} item.")
        a, b = _finite(word.get("start")), _finite(word.get("end"))
        if a is None or b is None:
            raise CapCutError(f"Word {number} ({word.get('word')!r}) needs a start and an end "
                              "in seconds.")
        text = str(word.get("word") or "").strip()
        if text:
            clean.append((round(a, 3), round(max(a, b), 3), text))
    clean.sort(key=lambda w: w[0])

    lines: list[dict] = []
    line: dict | None = None
    for a, b, text in clean:
        if line is not None:
            joined = _join(line["text"], text)
            if (a - line["end"] > max_gap or len(joined) > max_chars
                    or b - line["start"] > max_duration):
                lines.append(line)
                line = None
        if line is None:
            line = {"start": a, "end": b, "text": text}
        else:
            line["text"], line["end"] = joined, max(line["end"], b)
        if len(text) > max_chars or (_ends_sentence(text) and len(line["text"]) >= max_chars / 2):
            lines.append(line)
            line = None
    if line is not None:
        lines.append(line)
    return _positive_lengths(lines)


def _positive_lengths(lines: list[dict]) -> list[dict]:
    """Give zero-length lines some time, up to the next line; fold them forward if none is free."""
    out: list[dict] = []
    carry: dict | None = None
    for index, line in enumerate(lines):
        if carry is not None:
            line = {"start": min(carry["start"], line["start"]), "end": line["end"],
                    "text": _join(carry["text"], line["text"])}
            carry = None
        if line["end"] - line["start"] < 0.0005:
            following = lines[index + 1]["start"] if index + 1 < len(lines) else math.inf
            new_end = min(line["start"] + 0.3, following)
            if new_end - line["start"] < 0.0005:
                carry = line
                continue
            line = {**line, "end": new_end}
        out.append({"start": round(line["start"], 3), "end": round(line["end"], 3),
                    "text": line["text"]})
    return out


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (0x3000 <= code <= 0x30FF  # CJK punctuation, hiragana, katakana
            or 0x31F0 <= code <= 0x31FF
            or 0x3400 <= code <= 0x4DBF
            or 0x4E00 <= code <= 0x9FFF
            or 0xF900 <= code <= 0xFAFF
            or 0xFF00 <= code <= 0xFFEF  # full-width forms
            or 0x20000 <= code <= 0x3FFFF)


def _join(left: str, right: str) -> str:
    if not left:
        return right
    if _is_cjk(left[-1]) or _is_cjk(right[0]):
        return left + right
    return f"{left} {right}"


def _ends_sentence(text: str) -> bool:
    stripped = text.rstrip(_CLOSING)
    return bool(stripped) and stripped[-1] in _SENTENCE_END


# -------------------------------------------------------------------------------- shared

def _with_audio(path) -> MediaInfo:
    info = probe(path)
    if info.kind == "photo":
        raise CapCutError(f"{info.path.name} is a still image, so it has no sound to analyse.")
    if not info.has_audio:
        raise CapCutError(f"{info.path.name} has no audio track, so there is no sound to "
                          "analyse. Use a file with sound, or detect_scenes for the picture.")
    return info


def _number(value, name: str, unit: str) -> float:
    number = _finite(value)
    if number is None:
        raise CapCutError(f"{name} must be a number ({unit}), not {value!r}.")
    return number


def _finite(value) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _range(info: MediaInfo, start, end) -> tuple[float, float]:
    """Validate start/end against the file's length; `end` past the end is cut to it."""
    duration = info.duration_us / 1e6
    start = round(_number(start, "start", "seconds"), 3)
    if start < 0:
        raise CapCutError(f"start must be 0 or more seconds, not {start}.")
    if start >= duration:
        raise CapCutError(f"start ({start}s) is at or past the end of {info.path.name}, which "
                          f"is {duration:.3f} seconds long.")
    if end is None:
        end = duration
    else:
        end = round(_number(end, "end", "seconds"), 3)
        if end <= start:
            raise CapCutError(f"end ({end}s) must be after start ({start}s).")
        end = min(end, duration)
    return start, round(end, 3)


def _span(a: float, b: float) -> dict:
    return {"start": round(a, 3), "end": round(b, 3)}


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _timeout(span: float) -> float:
    return min(60 + 2 * span, 1800)


def _ffmpeg(info: MediaInfo, start: float, end: float, *, audio: bool, filters: list[str],
            what: str, quiet: bool = False) -> subprocess.CompletedProcess:
    """One FFmpeg pass over [start, end) of the first audio or video stream, output discarded.

    `filters` are alternatives, newest FFmpeg first; the next is tried only when FFmpeg rejects
    an option of the previous one. `quiet` keeps the log to errors (for filters that print to
    stdout); otherwise the info-level log, where silencedetect and ebur128 report, is kept.
    """
    span = end - start
    window = (["-ss", f"{start:.3f}"] if start > 0 else []) + ["-t", f"{span:.3f}"]
    stream = ["-map", "0:a:0", "-vn"] if audio else ["-map", "0:V:0", "-an"]
    timeout = _timeout(span)
    result = None
    for chain in filters:
        command = [_tool("ffmpeg"), "-nostdin", "-hide_banner", "-nostats",
                   "-v", "error" if quiet else "info", *window, "-i", str(info.path), *stream,
                   "-sn", "-dn", "-af" if audio else "-vf", chain, "-f", "null", "-"]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                stdin=subprocess.DEVNULL, timeout=timeout, creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise CapCutError(
                f"{what} on {info.path.name} did not finish within {timeout:.0f} seconds. "
                "Analyse a shorter part of the file by passing start and end, for example "
                "a few minutes at a time.") from None
        log = result.stderr.lower()
        if result.returncode == 0 or not ("option" in log and "not found" in log):
            break
    assert result is not None
    if result.returncode != 0:
        raise CapCutError(f"{what} failed for {info.path.name}: {result.stderr.strip()[-400:]}")
    return result


_PTS_TIME = re.compile(r"pts_time:\s*(\S+)")


def _metadata(stdout: str, key: str) -> list[tuple[float, str]]:
    """(frame time, value) pairs from the `metadata`/`ametadata` filters' print output."""
    pairs, time = [], None
    for line in stdout.splitlines():
        if line.startswith("frame:"):
            match = _PTS_TIME.search(line)
            time = _finite(match.group(1)) if match else None
        elif time is not None and line.startswith(key + "="):
            pairs.append((time, line.split("=", 1)[1].strip()))
    return pairs
