"""Text to a WAV file with the operating system's own voice.

Only the demo's `--with-audio` uses this: it speaks one evening note into the
inbox so the real audio path (local faster-whisper) runs on a fresh machine
with no recording of anyone's voice. Windows uses System.Speech through
PowerShell; macOS uses `say`. The text goes in through stdin or a file, never
interpolated into a command line.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_PS = ("Add-Type -AssemblyName System.Speech; "
       "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
       "$s.SetOutputToWaveFile($env:LIFECRM_TTS_OUT); "
       "$s.Speak([Console]::In.ReadToEnd()); $s.Dispose()")


class SpeechUnavailable(RuntimeError):
    pass


def synthesize(text: str, wav_path: Path) -> Path:
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        import os
        env = dict(os.environ, LIFECRM_TTS_OUT=str(wav_path))
        p = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                            "-Command", _PS], input=text, text=True, env=env,
                           capture_output=True, timeout=120)
        if p.returncode != 0 or not wav_path.exists():
            raise SpeechUnavailable(f"System.Speech failed: {p.stderr.strip()[:200]}")
        return wav_path
    if sys.platform == "darwin" and shutil.which("say"):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False,
                                         encoding="utf-8") as f:
            f.write(text)
            src = f.name
        try:
            p = subprocess.run(["say", "-o", str(wav_path), "--file-format=WAVE",
                                "--data-format=LEI16@16000", "-f", src],
                               capture_output=True, text=True, timeout=120)
        finally:
            Path(src).unlink(missing_ok=True)
        if p.returncode != 0 or not wav_path.exists():
            raise SpeechUnavailable(f"say failed: {p.stderr.strip()[:200]}")
        return wav_path
    raise SpeechUnavailable("no system text-to-speech found (Windows System.Speech "
                            "or macOS say); run the demo without --with-audio")
