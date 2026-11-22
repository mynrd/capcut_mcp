"""Text to speech with the voices built into Windows.

Speech is made by System.Speech, the speech engine that comes with Windows, run in a PowerShell
child process. Nothing has to be installed and nothing leaves the computer. PowerShell 7 is
preferred because it also sees the newer voices (Microsoft Mark and the voices of added
languages) that Windows PowerShell 5.1 does not; 5.1 is used when 7 is not installed.

Each line becomes one WAV file named after what was said and how, so asking for the same line
again reuses the file. Projects point at these files, so they are kept like media, not cleared
like a cache.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import wave
from pathlib import Path

from .env import CapCutError, data_dir
from .media import run_tool

MIN_RATE, MAX_RATE = -10, 10
ENGINE = "windows-sapi"  # part of every file's key, so another engine later cannot collide

_UNAVAILABLE = (
    "Text to speech uses the voices built into Windows, and Windows PowerShell was not found "
    "on this computer. Make the voice-over with another program or service and place the file "
    "with add_audio.")

# Reads its job from the file named in CAPCUT_MCP_SPEECH_JOB and writes its answer as JSON to
# the job's `result` path. Errors are caught and written there too: the answer file is the only
# channel read back, because stderr from an encoded command arrives wrapped in CLIXML.
_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$job = Get-Content -Raw -Encoding UTF8 -LiteralPath $env:CAPCUT_MCP_SPEECH_JOB | ConvertFrom-Json
$answer = @{}
try {
    Add-Type -AssemblyName System.Speech
    $speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
    try {
        if ($job.action -eq 'voices') {
            $answer.default = $speaker.Voice.Name
            $answer.voices = @($speaker.GetInstalledVoices() | Where-Object { $_.Enabled } | ForEach-Object {
                @{ name = $_.VoiceInfo.Name; language = $_.VoiceInfo.Culture.Name;
                   gender = [string]$_.VoiceInfo.Gender }
            })
        } else {
            $done = @()
            foreach ($item in $job.items) {
                if ($item.voice) { $speaker.SelectVoice([string]$item.voice) }
                $speaker.Rate = [int]$item.rate
                $speaker.SetOutputToWaveFile([string]$item.out)
                try { $speaker.Speak([string]$item.text) } finally { $speaker.SetOutputToNull() }
                $done += [string]$item.out
            }
            $answer.done = $done
        }
    } finally {
        $speaker.Dispose()
    }
} catch {
    $answer.error = $_.Exception.Message
}
ConvertTo-Json -InputObject $answer -Depth 4 -Compress | Set-Content -Encoding UTF8 -LiteralPath $job.result
"""
_ENCODED = base64.b64encode(_SCRIPT.encode("utf-16-le")).decode("ascii")


def _powershell() -> str | None:
    """PowerShell 7 if installed, else Windows PowerShell; None away from Windows."""
    if sys.platform != "win32":
        return None
    found = shutil.which("pwsh") or shutil.which("powershell")
    if found:
        return found
    fallback = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" \
        / "v1.0" / "powershell.exe"
    return str(fallback) if fallback.is_file() else None


def available() -> bool:
    """Whether speech can be made on this computer at all (voices are checked by voices())."""
    return _powershell() is not None


def _run(job: dict, *, timeout: float, what: str) -> dict:
    """Run one PowerShell job and return its answer. The only place that starts PowerShell."""
    program = _powershell()
    if program is None:
        raise CapCutError(_UNAVAILABLE)
    with tempfile.TemporaryDirectory(prefix="capcut-mcp-speech-") as folder:
        job_file, result_file = Path(folder) / "job.json", Path(folder) / "result.json"
        job_file.write_text(json.dumps({**job, "result": str(result_file)}, ensure_ascii=False),
                            encoding="utf-8")
        try:
            finished = run_tool([program, "-NoProfile", "-NonInteractive", "-EncodedCommand", _ENCODED],
                                timeout=timeout, what=what, env={"CAPCUT_MCP_SPEECH_JOB": str(job_file)})
        except OSError as error:
            raise CapCutError(f"{what} failed: PowerShell ({program}) could not be started: "
                              f"{error}") from None
        try:
            answer = json.loads(result_file.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            detail = re.sub(r"<[^>]+>|#< CLIXML|_x000D__x000A_", " ", finished.stderr or "")
            detail = " ".join(detail.split())[:300]
            raise CapCutError(f"{what} failed: PowerShell gave no answer"
                              + (f" ({detail})." if detail else ".")) from None
    if not isinstance(answer, dict):
        raise CapCutError(f"{what} failed: PowerShell gave an unexpected answer.")
    if answer.get("error"):
        raise CapCutError(f"{what} failed: {' '.join(str(answer['error']).split())}")
    return answer


def voices() -> dict:
    """The installed voices: {"default": name, "voices": [{name, language, gender}]}."""
    answer = _run({"action": "voices"}, timeout=60, what="Listing the Windows voices")
    found = answer.get("voices") or []
    if isinstance(found, dict):  # PowerShell 5.1 can write a one-item list as the item itself
        found = [found]
    listed = [{"name": str(v.get("name") or ""), "language": str(v.get("language") or ""),
               "gender": str(v.get("gender") or "")} for v in found if isinstance(v, dict)]
    listed = [v for v in listed if v["name"]]
    return {"default": str(answer.get("default") or (listed[0]["name"] if listed else "")),
            "voices": listed}


def resolve_voice(wanted: str | None) -> str:
    """The exact name of an installed voice, from a full name or a part of one."""
    found = voices()
    names = [v["name"] for v in found["voices"]]
    if not names:
        raise CapCutError("Windows has no speech voices installed or enabled. Voices come with "
                          "Windows' speech and language settings.")
    if wanted is None or not str(wanted).strip():
        return found["default"] if found["default"] in names else names[0]
    key = " ".join(str(wanted).lower().split())
    exact = [name for name in names if name.lower() == key]
    if exact:
        return exact[0]
    # Every word must appear: "zira" or "david desktop". Several matches: the shortest name,
    # which for "david" is the newer voice rather than the "Desktop" one.
    partial = [name for name in names if all(word in name.lower() for word in key.split())]
    if partial:
        return min(partial, key=len)
    raise CapCutError(f"No voice called {wanted!r}. The voices on this computer are: "
                      f"{', '.join(names)}.")


def check_rate(rate) -> int:
    if isinstance(rate, bool) or not isinstance(rate, int | float) or rate != int(rate):
        raise CapCutError(f"rate must be a whole number from {MIN_RATE} to {MAX_RATE}, not {rate!r}.")
    rate = int(rate)
    if not MIN_RATE <= rate <= MAX_RATE:
        raise CapCutError(f"rate runs from {MIN_RATE} (slowest) to {MAX_RATE} (fastest), with 0 "
                          f"the voice's normal pace; {rate} is outside that.")
    return rate


def clean_text(text) -> str:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        raise CapCutError("There is no text to speak.")
    return cleaned


def voice_folder(folder: str | None = None) -> Path:
    path = Path(folder).expanduser().resolve() if folder else data_dir() / "voiceovers"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise CapCutError(f"Could not create the folder {path} for the voice files: {error}") from None
    return path


def _file_for(folder: Path, text: str, voice: str, rate: int) -> Path:
    key = hashlib.sha256(json.dumps([ENGINE, voice, rate, text], ensure_ascii=False).encode()).hexdigest()
    words = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].rstrip("-") or "voice"
    return folder / f"{words}-{key[:12]}.wav"


def wav_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as audio:
            return audio.getnframes() / float(audio.getframerate())
    except (OSError, EOFError, wave.Error, ZeroDivisionError):
        return 0.0


def synthesize(lines: list[tuple[str, int]], *, voice: str, folder: Path) -> list[dict]:
    """Speak each (text, rate) into a WAV file in `folder`. Lines already spoken are reused.

    Returns one {"path", "duration", "rate", "cached"} per line, in order. `voice` must be an
    exact installed name (see resolve_voice).
    """
    out: list[dict | None] = []
    wanted: dict[Path, dict] = {}  # one entry per file, however often its line comes up
    for text, rate in lines:
        text, rate = clean_text(text), check_rate(rate)
        path = _file_for(folder, text, voice, rate)
        length = wav_seconds(path) if path.is_file() and path not in wanted else 0.0
        if length > 0:
            out.append({"path": str(path), "duration": length, "rate": rate, "cached": True})
            continue
        out.append(None)
        if path in wanted:
            wanted[path]["indexes"].append(len(out) - 1)
            continue
        # A name of its own, so two edits speaking the same line at once cannot collide.
        temporary = path.with_suffix(f".{os.getpid()}-{secrets.token_hex(4)}.tmp.wav")
        wanted[path] = {"indexes": [len(out) - 1], "text": text, "rate": rate, "voice": voice,
                        "final": path, "out": str(temporary)}
    if wanted:
        # Speech comes out much faster than real time; this is room for a slow, busy machine.
        items = list(wanted.values())
        timeout = 60 + 0.2 * sum(len(item["text"]) for item in items)
        try:
            _run({"action": "speak", "items": [{key: item[key] for key in ("text", "rate", "voice", "out")}
                                               for item in items]},
                 timeout=timeout, what="Making speech with the Windows voice")
            for item in items:
                spoken = Path(item["out"])
                length = wav_seconds(spoken)
                if length <= 0:
                    raise CapCutError(f"The Windows voice made no sound for {item['text'][:40]!r}.")
                os.replace(spoken, item["final"])
                for index in item["indexes"]:
                    out[index] = {"path": str(item["final"]), "duration": length,
                                  "rate": item["rate"], "cached": False}
        finally:
            for item in items:
                Path(item["out"]).unlink(missing_ok=True)
    return [entry for entry in out if entry is not None]
