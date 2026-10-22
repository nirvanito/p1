"""The voice loop: say a question and hear a cited answer; say a note and it
lands in the quick-note inbox. A front end over two paths that already
exist — never a third way into the database.

- Questions go to recall.answer(), the same call `ask --json` makes, over a
  read-only connection. The loop cannot know anything `ask` does not, and a
  model answer that cites evidence which does not exist is replaced by the
  deterministic one before it is spoken: `ask` warns on stderr, but nobody
  hears stderr.
- Notes ("note ...", "remember ...", "log that ...", "journal ...") become a
  text file in the quick-note inbox, named exactly like the phone Shortcut's,
  so intake dates them the same way and /log reviews them like any phone
  note. Nothing is applied, and nothing — note or question — writes
  lifecrm.db.
- Everything heavy is optional and imported on first use: faster-whisper
  (already a dependency) for speech-to-text, sounddevice for the microphone,
  openWakeWord for "hey jarvis", Piper for a nicer voice. A missing piece
  becomes one sentence saying how to install it; `--text` needs none of them.
- Speech goes out through the OS's own engine (System.Speech, `say`,
  espeak/spd-say) as a subprocess whose script is a constant: the words
  travel on stdin, or as one argv element after `--`, so nothing an answer
  quotes — a journal line, someday a text message — can become a command.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import wave
from array import array
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator
from zoneinfo import ZoneInfo

from . import platforms, recall
from .config import PROJECT_ROOT, Config, ConfigError
from .domain import days
from .domain.util import DomainError
from .importer import normalize_text

SAMPLE_RATE = 16_000            # what whisper and openWakeWord both expect
FRAME_SAMPLES = 1_280           # 80 ms: openWakeWord's native frame
MIN_UTTERANCE_SEC = 0.3
SILENT_MIC_RMS = 2.0            # below this a whole recording is digital zero
SPEAK_TIMEOUT_SEC = 120
MORE_ON_SCREEN = "The rest is on screen."

REQUIREMENTS = PROJECT_ROOT / "requirements-voice.txt"


class VoiceUnavailable(Exception):
    """An optional piece (a package, the microphone, a wake-word model) is
    missing. The message is one line saying how to get it — the CLI prints
    it as-is."""


def _pip_hint(package: str) -> str:
    return (f'"{sys.executable}" -m pip install -r "{REQUIREMENTS}" '
            f"(or just: pip install {package})")


# ---------------------------------------------------------------- settings

def _flag(raw: dict, key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "yes", "on", "1"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("false", "no", "off", "0"):
        return False
    raise ConfigError(f"[voice] {key} must be true or false, got {value!r}")


def _number(raw: dict, key: str, default: float, lo: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < lo:
        raise ConfigError(f"[voice] {key} must be a number >= {lo:g}, got {value!r}")
    return float(value)


def settings(cfg: Config) -> dict:
    """The [voice] table with defaults. Every key is optional; a bad value
    is a ConfigError naming the key, not a traceback mid-conversation."""
    raw = cfg.extra.get("voice") or {}
    if not isinstance(raw, dict):
        raise ConfigError("[voice] must be a table of settings")
    piper_model = raw.get("piper_model")
    return {
        # Voice turns are seconds long, so 'small' even where [whisper] uses
        # a larger model for the nightly journal: latency is the experience.
        "model": str(raw.get("model") or "small"),
        "vad_filter": _flag(raw, "vad_filter", True),
        "use_llm": _flag(raw, "use_llm", True),
        "llm_model": raw.get("llm_model") or None,
        "speak": _flag(raw, "speak", True),
        "voice": raw.get("voice") or None,
        "max_spoken_chars": int(_number(raw, "max_spoken_chars", 400, 40)),
        "piper_model": str(Path(piper_model).expanduser()) if piper_model else None,
        "device": None if raw.get("device") == "" else raw.get("device"),   # 0 is a valid index
        "wake_model": str(raw.get("wake_model") or "hey_jarvis"),
        "wake_threshold": _number(raw, "wake_threshold", 0.5, 0.01),
        "silence_rms": _number(raw, "silence_rms", 400, 1),
        "silence_sec": _number(raw, "silence_sec", 1.2, 0.2),
        "max_record_sec": _number(raw, "max_record_sec", 30, 1),
    }


# ---------------------------------------------------------------- routing

@dataclass(frozen=True)
class Route:
    kind: str           # 'note' | 'question' | 'empty'
    text: str           # the note body, or the question as heard
    command: str = ""   # the trigger that made it a note


# The trigger must be the whole first word: "notes from Tuesday" and
# "journaling streak" are questions. "note to self" is one trigger.
_NOTE_RE = re.compile(
    r"^\W*(?P<cmd>log\s+that|note\s+to\s+self|note|remember|journal(?:\s+entry)?)\b"
    r"(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL)
_LEAD_PUNCT = re.compile(r"^[\s,.:;!\-\u2013\u2014]+")
_THAT = re.compile(r"^that\b", re.IGNORECASE)


def route(utterance: str) -> Route:
    """Note or question, decided by the first words only.

    One exception to the prefix rule: an utterance ending in '?' is a
    question ("Remember when we went to Lake Bled?"). Whisper punctuates
    questions reliably, and the two mistakes are not equal — a note misread
    as a question writes nothing, a question misread as a note puts a file
    in the review queue.
    """
    text = " ".join(utterance.split())
    if not text.strip(" .,!?"):
        return Route("empty", "")
    m = _NOTE_RE.match(text)
    if not m or text.endswith("?"):
        return Route("question", text)
    cmd = " ".join(m.group("cmd").lower().split())
    rest = _LEAD_PUNCT.sub("", m.group("rest"))
    if cmd == "remember" and re.match(r"to\b", rest, re.IGNORECASE):
        # "remember to renew the passport" is a reminder — the verb is the
        # content, so the note keeps it.
        body = text[m.start("cmd"):]
    else:
        body = _LEAD_PUNCT.sub("", _THAT.sub("", rest))
    body = body.strip()
    if body:
        body = body[0].upper() + body[1:]
    return Route("note", body, cmd)


# ---------------------------------------------------------------- notes

def note_filename(cfg: Config, now: datetime) -> tuple[str, str]:
    """('<journal date> <HH.mm.ss> <+-HHMM>.txt', journal_date).

    The Quick Note Shortcut's name (docs/iphone.md): the journal date rolled
    back before the rollover hour, the real clock time, and the UTC offset
    that pins the instant — so intake dates a spoken note exactly as it
    dates a phone note, and recorded_at comes back to `now`.
    """
    local = now.astimezone(ZoneInfo(cfg.timezone))
    journal_date = days.journal_date_for(now, cfg.timezone, cfg.day_rollover_hour)
    return f"{journal_date} {local:%H.%M.%S} {local:%z}.txt", journal_date


def write_note(cfg: Config, text: str, now: datetime | None = None) -> dict:
    """Drop one quick note into the text inbox. Touches no database: intake
    imports it, /log extracts it, the review gate decides what it becomes."""
    inbox = cfg.inbox_text_dir
    if inbox is None:
        raise DomainError("inbox_text_dir is not configured — set "
                          "[paths].inbox_text_dir in config.toml (voice notes "
                          "go to the quick-note inbox)")
    # Never created here: a typo in config.toml would quietly become a folder
    # the phone never syncs to, hiding the mistake that strands phone notes.
    if not inbox.is_dir():
        raise DomainError(f"inbox_text_dir {inbox} does not exist — create it, "
                          f"or fix [paths].inbox_text_dir in config.toml")
    body = normalize_text(text)
    if not body:
        raise DomainError("the note is empty — nothing written")
    now = now or datetime.now(timezone.utc)
    name, journal_date = note_filename(cfg, now)
    path, n = inbox / name, 2
    while True:
        try:
            # 'x': two notes in one second get '-2', never an overwrite.
            with open(path, "x", encoding="utf-8", newline="\n") as f:
                f.write(body + "\n")
            break
        except FileExistsError:
            path = inbox / f"{Path(name).stem}-{n}.txt"
            n += 1
    return {"path": str(path), "file": path.name, "journal_date": journal_date,
            "recorded_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "text": body}


# ---------------------------------------------------------------- questions

def vetted(result: dict, warn: Callable[[str], None] | None = None) -> dict:
    """A model answer that cites evidence which does not exist — or cites
    none while evidence exists — is swapped for the deterministic answer.
    The swap is visible in answer_source; the evidence is untouched."""
    c = result["citations"]
    if not result["answer_source"].startswith("model:") \
            or not (c["unknown"] or c["uncited"]):
        return result
    if warn:
        bad = (f"cited evidence that does not exist ({', '.join(c['unknown'])})"
               if c["unknown"] else "cited no evidence")
        warn(f"warning: the model {bad} — using the deterministic answer")
    text = recall.deterministic_answer(result["evidence"])
    return {**result, "answer": text,
            "answer_source": "deterministic (model answer rejected)",
            "citations": recall.check_citations(text, result["evidence"])}


def answer_question(conn: sqlite3.Connection, cfg: Config, question: str, *,
                    use_llm: bool, model: str | None = None,
                    warn: Callable[[str], None] | None = None) -> dict:
    return vetted(recall.answer(conn, cfg, question, use_llm=use_llm,
                                model=model, warn=warn), warn)


def answer_lines(result: dict) -> list[str]:
    """The answer as `ask` prints it: text, the sources it cited, the window."""
    lines = [result["answer"]]
    index = recall.evidence_index(result["evidence"])
    cited = result["citations"]["cited"]
    if cited:
        lines += ["", "sources:"]
        lines += [f"  [{cid}] {index.get(cid, '')}".rstrip() for cid in cited]
    w = result["evidence"]["window"]
    state = result.get("model") or {}
    reason = state.get("reason") or ""
    note = (f" · {reason}" if reason and not state.get("available")
            and reason != "--no-llm" else "")
    lines += ["", f"window: {w['label']} ({w['start']} to {w['end']}) · "
                  f"answer: {result['answer_source']}{note}"]
    return lines


# ---------------------------------------------------------------- spoken text

_CITATION = re.compile(r"\s*\[[A-Za-z]{1,8}\d*(?:\s*,\s*[A-Za-z]{1,8}\d*)*\]")
_ISO_DAY = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_UNITS = {"h": "hours", "min": "minutes", "kcal": "calories",
          "bpm": "beats per minute", "km": "kilometers", "kg": "kilograms",
          "ml": "milliliters"}
_UNIT_RE = re.compile(r"(?<=\d) (" + "|".join(_UNITS) + r")\b")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"]


def _say_date(m: re.Match, today: date) -> str:
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return m.group(0)
    said = f"{_MONTH_NAMES[d.month - 1]} {d.day}"
    return said if d.year == today.year else f"{said}, {d.year}"


def spoken(text: str, limit: int = 400, today: date | None = None) -> str:
    """What the speaker says: the printed answer minus its bookkeeping.

    Citations and snippet markers are for reading; ISO dates, '(s)' plurals
    and unit abbreviations come out of every engine as noise. A long answer
    stops at a sentence boundary — the whole answer and its sources stay on
    screen.
    """
    today = today or date.today()
    s = _CITATION.sub("", text)
    s = s.replace("«", "").replace("»", "")
    s = s.replace("entr(y/ies)", "entries")
    s = re.sub(r"(\w)\((s|es)\)", r"\1\2", s)
    s = _ISO_DAY.sub(lambda m: _say_date(m, today), s)
    s = _UNIT_RE.sub(lambda m: " " + _UNITS[m.group(1)], s)
    s = re.sub(r"(?<=\d)/10\b", " out of 10", s)
    s = " ".join(s.split())
    s = re.sub(r"\s+([.,;:!?])", r"\1", s)
    if len(s) <= limit:
        return s
    out = ""
    for sentence in _SENTENCE_END.split(s):
        candidate = f"{out} {sentence}".strip()
        if len(candidate) > limit:
            break
        out = candidate
    if not out:                     # one sentence longer than the cap
        out = s[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return f"{out} {MORE_ON_SCREEN}"


# ---------------------------------------------------------------- speech out

@dataclass(frozen=True)
class Step:
    argv: tuple[str, ...]
    stdin: bytes | None = None


@dataclass(frozen=True)
class Engine:
    name: str
    steps: tuple[Step, ...]


# Constant scripts: the words never appear in them. They arrive as JSON on
# stdin — ASCII-escaped by json.dumps, so no console code page can mangle a
# name like José — and ConvertFrom-Json turns them back into one string.
# `wav` renders to a file instead of the speakers (tests use it to run the
# real engine without making a sound).
_SAPI_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$raw = New-Object System.IO.MemoryStream
[Console]::OpenStandardInput().CopyTo($raw)
$job = [System.Text.Encoding]::UTF8.GetString($raw.ToArray()) | ConvertFrom-Json
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
if ($job.voice) { try { $s.SelectVoice([string]$job.voice) } catch { } }
if ($job.wav) { $s.SetOutputToWaveFile([string]$job.wav) }
$s.Speak([string]$job.text)
$s.Dispose()
""".strip()

_PLAY_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$raw = New-Object System.IO.MemoryStream
[Console]::OpenStandardInput().CopyTo($raw)
$job = [System.Text.Encoding]::UTF8.GetString($raw.ToArray()) | ConvertFrom-Json
(New-Object System.Media.SoundPlayer ([string]$job.wav)).PlaySync()
""".strip()


def _encoded(script: str) -> str:
    """-EncodedCommand form: base64 of UTF-16LE, immune to argv quoting."""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _job(**fields) -> bytes:
    return json.dumps(fields, ensure_ascii=True).encode("ascii")


def _windows_powershell(which) -> str | None:
    # System.Speech ships with .NET Framework, i.e. Windows PowerShell 5.1.
    # PowerShell 7 (pwsh) cannot load it, so it is never a fallback.
    found = which("powershell")
    if found:
        return found
    exe = (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
           / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    return str(exe) if exe.exists() else None


def _player(system: str, which, wav: Path) -> Step | None:
    if system == "windows":
        ps = _windows_powershell(which)
        return Step((ps, "-NoProfile", "-NonInteractive", "-EncodedCommand",
                     _encoded(_PLAY_SCRIPT)), _job(wav=str(wav))) if ps else None
    names = ("afplay",) if system == "mac" else ("aplay", "paplay", "pw-play")
    for name in names:
        exe = which(name)
        if exe:
            return Step((exe, str(wav)))
    return None


def engines(text: str, prefs: dict, *, system: str, which=shutil.which,
            wav_path: Path | None = None) -> list[Engine]:
    """Every way this machine can say `text`, best first. Pure: builds argv
    and stdin, runs nothing."""
    out: list[Engine] = []
    voice = prefs.get("voice")
    piper = which("piper") if prefs.get("piper_model") else None
    if piper and wav_path is not None:
        player = _player(system, which, wav_path)
        if player:
            out.append(Engine("piper", (
                Step((piper, "-m", str(prefs["piper_model"]), "-f", str(wav_path)),
                     text.encode("utf-8")),
                player)))
    if system == "windows":
        ps = _windows_powershell(which)
        if ps:
            out.append(Engine("windows-sapi", (Step(
                (ps, "-NoProfile", "-NonInteractive", "-EncodedCommand",
                 _encoded(_SAPI_SCRIPT)),
                _job(text=text, voice=voice)),)))
    elif system == "mac":
        say = which("say")
        if say:
            argv = (say,) + (("-v", str(voice)) if voice else ()) + ("-f", "-")
            out.append(Engine("say", (Step(argv, text.encode("utf-8")),)))
    else:
        for name in ("espeak-ng", "espeak"):
            exe = which(name)
            if exe:
                argv = (exe,) + (("-v", str(voice)) if voice else ()) + ("--stdin",)
                out.append(Engine(name, (Step(argv, text.encode("utf-8")),)))
                break
        spd = which("spd-say")
        if spd:
            # spd-say takes text only as an argument: one argv element after
            # '--', with no shell, can be read as nothing but text.
            out.append(Engine("spd-say", (Step((spd, "--wait", "--", text)),)))
    return out


def speak(text: str, prefs: dict, *, system: str | None = None,
          run=subprocess.run, which=shutil.which) -> str | None:
    """Say `text` with the first engine that works and return its name, or
    None when nothing could speak (the answer is on screen either way)."""
    if not text.strip():
        return None
    with tempfile.TemporaryDirectory(prefix="lifecrm-voice-",
                                     ignore_cleanup_errors=True) as tmp:
        for engine in engines(text, prefs, system=system or platforms.SYSTEM,
                              which=which, wav_path=Path(tmp) / "answer.wav"):
            try:
                for step in engine.steps:
                    run(list(step.argv),
                        input=step.stdin if step.stdin is not None else b"",
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        check=True, timeout=SPEAK_TIMEOUT_SEC)
            except (OSError, subprocess.SubprocessError):
                continue            # next engine: piper broken -> the OS voice
            return engine.name
    return None


# ---------------------------------------------------------------- microphone

def mic_permission_hint() -> str:
    if platforms.is_mac():
        return ("allow your terminal app under System Settings > Privacy & "
                "Security > Microphone, then restart it")
    if platforms.is_windows():
        return ("turn on Settings > Privacy & security > Microphone > 'Let "
                "desktop apps access your microphone'")
    return "check the input device and its volume (pavucontrol / alsamixer)"


def require_sounddevice():
    try:
        import sounddevice
    except ImportError:
        raise VoiceUnavailable(
            f"recording needs the optional package 'sounddevice' — install it "
            f"with: {_pip_hint('sounddevice')}; until then use --file note.wav "
            f'or --text "..."') from None
    except OSError as e:            # the wheel is there, PortAudio is not
        raise VoiceUnavailable(
            f"sounddevice is installed but cannot load PortAudio ({e}) — "
            f"install it (Debian/Ubuntu: sudo apt install libportaudio2), or "
            f"use --file / --text") from None
    return sounddevice


def rms(pcm: bytes) -> float:
    """Loudness of 16-bit mono PCM, stdlib only (audioop left in 3.13)."""
    samples = array("h")
    samples.frombytes(pcm[:len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def write_wav(path: Path, pcm: bytes) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return path


def _open_failed(e: Exception) -> VoiceUnavailable:
    return VoiceUnavailable(f"cannot open the microphone ({e}) — "
                            f"{mic_permission_hint()}")


def record_push_to_talk(sd, dest: Path, *, wait=input, warn=None,
                        device=None) -> Path | None:
    """Enter starts, Enter stops; 16 kHz mono int16 into a WAV at `dest`.
    None when nothing usable was recorded (too short, or digital silence —
    which is what a mic blocked by privacy settings delivers)."""
    chunks: list[bytes] = []

    def on_audio(indata, frames, time_info, status):
        chunks.append(bytes(indata))

    wait("Press Enter to talk (Ctrl+C to quit) ")
    try:
        stream = sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1,
                                   dtype="int16", callback=on_audio,
                                   device=device)
    except Exception as e:          # PortAudioError, no device, bad device
        raise _open_failed(e) from None
    with stream:
        wait("Listening... press Enter to stop ")
    pcm = b"".join(chunks)
    if len(pcm) < SAMPLE_RATE * 2 * MIN_UTTERANCE_SEC:
        if warn:
            warn("too short — press Enter, speak, then press Enter again")
        return None
    if rms(pcm) < SILENT_MIC_RMS:
        if warn:
            warn(f"the microphone delivered pure silence — {mic_permission_hint()}")
        return None
    return write_wav(dest, pcm)


def mic_frames(sd, device=None) -> Iterator[bytes]:
    """Endless 80 ms frames from the default mic; closing the generator
    closes the stream."""
    try:
        stream = sd.RawInputStream(samplerate=SAMPLE_RATE, channels=1,
                                   dtype="int16", blocksize=FRAME_SAMPLES,
                                   device=device)
    except Exception as e:
        raise _open_failed(e) from None
    with stream:
        while True:
            data, _overflowed = stream.read(FRAME_SAMPLES)
            yield bytes(data)


def record_until_silence(frames: Iterable[bytes], *, silence_rms: float,
                         silence_sec: float, max_record_sec: float,
                         lead_sec: float = 4.0) -> bytes:
    """Collect frames until `silence_sec` of quiet follows speech. Returns
    b'' when no speech starts within `lead_sec` (woken, then nothing said)."""
    out: list[bytes] = []
    heard = False
    quiet = elapsed = 0.0
    for frame in frames:
        dur = len(frame) / 2 / SAMPLE_RATE
        out.append(frame)
        elapsed += dur
        if rms(frame) >= silence_rms:
            heard, quiet = True, 0.0
        else:
            quiet += dur
        if heard and quiet >= silence_sec:
            break
        if not heard and elapsed >= lead_sec:
            return b""
        if elapsed >= max_record_sec:
            break
    return b"".join(out) if heard else b""


# ---------------------------------------------------------------- wake word

class WakeDetector:
    """openWakeWord behind one question: was the wake word in this frame?"""

    def __init__(self, model, threshold: float):
        self.model = model
        self.threshold = threshold

    def heard(self, frame: bytes) -> bool:
        import numpy as np          # openWakeWord's own dependency
        scores = self.model.predict(np.frombuffer(frame, dtype=np.int16))
        return max(scores.values(), default=0.0) >= self.threshold

    def reset(self) -> None:
        reset = getattr(self.model, "reset", None)
        if callable(reset):
            reset()


def require_wakeword(prefs: dict) -> WakeDetector:
    try:
        from openwakeword.model import Model
    except ImportError:
        raise VoiceUnavailable(
            f"--wake needs the optional package 'openwakeword' — install it "
            f"with: {_pip_hint('openwakeword')}; push-to-talk works without "
            f"it (drop --wake)") from None
    name = prefs["wake_model"]
    try:
        # onnx everywhere: openWakeWord's tflite default has no Windows or
        # macOS wheels, and onnxruntime already comes with faster-whisper.
        model = Model(wakeword_models=[name], inference_framework="onnx")
    except Exception as e:          # models not downloaded yet, unknown name
        raise VoiceUnavailable(
            f"openWakeWord could not load '{name}' ({e}) — download the "
            f'pre-trained models once with: "{sys.executable}" -c "import '
            f'openwakeword.utils as u; u.download_models()"') from None
    return WakeDetector(model, prefs["wake_threshold"])


def listen_after_wake(frames: Iterable[bytes], detector, *, silence_rms: float,
                      silence_sec: float, max_record_sec: float,
                      on_wake: Callable[[], None] | None = None) -> bytes:
    """Block until the wake word, then record one utterance."""
    frames = iter(frames)
    for frame in frames:
        if detector.heard(frame):
            break
    else:
        return b""
    detector.reset()
    if on_wake:
        on_wake()
    return record_until_silence(frames, silence_rms=silence_rms,
                                silence_sec=silence_sec,
                                max_record_sec=max_record_sec)


# ---------------------------------------------------------------- speech in

def entity_prompt(cfg: Config) -> str | None:
    """Known names for whisper's initial_prompt — a short utterance is the
    one case where that seeding actually helps. Read-only; None when there
    is no database yet."""
    from . import db as db_mod
    from . import transcribe as transcribe_mod
    if not cfg.whisper.get("seed_initial_prompt", True) or not cfg.db_path.exists():
        return None
    try:
        conn = db_mod.connect(cfg.db_path, readonly=True)
    except sqlite3.Error:
        return None
    try:
        return transcribe_mod.entity_prompt(conn)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


class Transcriber:
    """faster-whisper through transcribe.load_model, loaded on first use and
    kept for the session. Language comes from [whisper]; the model and VAD
    from [voice]."""

    def __init__(self, cfg: Config, prefs: dict):
        self.cfg = replace(cfg, whisper={**cfg.whisper,
                                         "vad_filter": prefs["vad_filter"]})
        self.model_name = prefs["model"]
        self._model = None
        self._prompt: str | None = None

    def __call__(self, audio: Path) -> str:
        from . import transcribe as transcribe_mod
        if self._model is None:
            self._model = transcribe_mod.load_model(self.cfg, self.model_name)
            self._prompt = entity_prompt(self.cfg)
        result = transcribe_mod.transcribe_audio(self.cfg, self._model, Path(audio),
                                                 initial_prompt=self._prompt)
        return result["text"].strip()


# ---------------------------------------------------------------- one turn

def handle(utterance: str, *, cfg: Config, prefs: dict,
           open_conn: Callable[[], sqlite3.Connection], use_llm: bool,
           warn: Callable[[str], None] | None = None,
           now: datetime | None = None) -> dict:
    """Route one utterance and act on it. `open_conn` (read-only) is called
    for questions only — a note never opens the database."""
    r = route(utterance)
    turn = {"heard": utterance, "route": r.kind}
    if r.kind == "empty" or (r.kind == "note" and not r.text):
        turn |= {"route": "empty", "say": "I didn't catch anything to do."}
        return turn
    if r.kind == "note":
        note = write_note(cfg, r.text, now=now)
        return turn | {"note": note, "say": "Noted."}
    conn = open_conn()
    try:
        result = answer_question(conn, cfg, r.text, use_llm=use_llm,
                                 model=prefs.get("llm_model"), warn=warn)
    finally:
        conn.close()
    return turn | {"answer": result,
                   "say": spoken(result["answer"], prefs["max_spoken_chars"])}
