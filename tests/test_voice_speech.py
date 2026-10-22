"""Speech out and audio in for the voice loop.

Engine commands are built, not run: subprocess is mocked, and the claim
under test is that the words reach the engine only as data — on stdin, or
as one argv element after `--` — so text quoted from the database cannot
become a command. One test runs the real Windows engine, silently, to prove
that claim against PowerShell itself.
"""

import base64
import itertools
import json
import subprocess
import sys
import types
from array import array
from datetime import date

import pytest

from lifecrm import platforms, voice
from lifecrm.voice import VoiceUnavailable

PREFS = {"voice": None, "piper_model": None}

HOSTILE = [
    "'; Remove-Item -Recurse -Force C:\\ ; '",
    '"; rm -rf / ; echo "',
    "$(Start-Process calc) `whoami` & del *.* | Out-Null",
    "--help -v evil -f /etc/passwd",
    "line one\nline two\r\n; exit 1",
    "Jos\u00e9 \u00ab caf\u00e9 \u00bb \u2026 \U0001F600",
]


def fake_which(*present):
    return lambda name: f"/usr/bin/{name}" if name in present else None


def _frame(level: int) -> bytes:
    return array("h", [level, -level] * (voice.FRAME_SAMPLES // 2)).tobytes()


QUIET, LOUD = _frame(20), _frame(3000)


# ---------------------------------------------------------------- spoken text

def test_spoken_text_drops_citations_markers_and_reads_dates_and_units():
    printed = ("Sleep totalled 7.26 h per day over 4 day(s) (min 6.47 h "
               "2026-07-27, max 7.75 h 2026-07-26) [M1]. Related entries: "
               "2026-07-25 Dinner with «Jake» [E1]. Jake appears in 1 recent "
               "entr(y/ies).")
    assert voice.spoken(printed, today=date(2026, 10, 21)) == (
        "Sleep totalled 7.26 hours per day over 4 days (min 6.47 hours July 27, "
        "max 7.75 hours July 26). Related entries: July 25 Dinner with Jake. "
        "Jake appears in 1 recent entries.")


def test_spoken_text_handles_multi_citations_ratings_and_other_years():
    said = voice.spoken("Mood averaged 7/10 [M1, E2]; last time 2025-12-31 "
                        "[P3]; not a date 2026-13-45.", today=date(2026, 10, 21))
    assert said == ("Mood averaged 7 out of 10; last time December 31, 2025; "
                    "not a date 2026-13-45.")


def test_spoken_text_is_capped_at_a_sentence_boundary():
    text = " ".join(f"Sentence number {i} is here." for i in range(40))
    said = voice.spoken(text, limit=100)
    assert said.endswith(voice.MORE_ON_SCREEN)
    body = said[:-len(voice.MORE_ON_SCREEN)].strip()
    assert len(body) <= 100 and body.endswith("is here.")

    one_long = "word " * 200
    said = voice.spoken(one_long, limit=60)
    assert said.endswith("… " + voice.MORE_ON_SCREEN)
    assert len(said) < 60 + len(voice.MORE_ON_SCREEN) + 3


# ---------------------------------------------------------------- engines

@pytest.mark.parametrize("text", HOSTILE)
def test_windows_speech_passes_text_on_stdin_only(text):
    [engine] = voice.engines(text, PREFS, system="windows",
                             which=fake_which("powershell"))
    assert engine.name == "windows-sapi"
    [step] = engine.steps
    ps, *flags, encoded = step.argv
    assert flags == ["-NoProfile", "-NonInteractive", "-EncodedCommand"]
    # the script is the constant, whatever the text
    assert base64.b64decode(encoded).decode("utf-16-le") == voice._SAPI_SCRIPT
    assert all(text not in arg for arg in step.argv)
    step.stdin.decode("ascii")                      # pure ASCII: no code page games
    assert json.loads(step.stdin)["text"] == text


@pytest.mark.parametrize("text", HOSTILE)
def test_mac_speech_uses_say_reading_stdin(text):
    [engine] = voice.engines(text, PREFS, system="mac", which=fake_which("say"))
    [step] = engine.steps
    assert step.argv == ("/usr/bin/say", "-f", "-")
    assert step.stdin.decode("utf-8") == text
    [engine] = voice.engines(text, {**PREFS, "voice": "Samantha"}, system="mac",
                             which=fake_which("say"))
    assert engine.steps[0].argv == ("/usr/bin/say", "-v", "Samantha", "-f", "-")


@pytest.mark.parametrize("text", HOSTILE)
def test_linux_prefers_espeak_on_stdin_then_spd_say_after_double_dash(text):
    names = [e.name for e in voice.engines(
        text, PREFS, system="linux", which=fake_which("espeak-ng", "espeak", "spd-say"))]
    assert names == ["espeak-ng", "spd-say"]
    [espeak, spd] = voice.engines(text, PREFS, system="linux",
                                  which=fake_which("espeak", "spd-say"))
    assert espeak.steps[0].argv == ("/usr/bin/espeak", "--stdin")
    assert espeak.steps[0].stdin.decode("utf-8") == text
    assert spd.steps[0].argv == ("/usr/bin/spd-say", "--wait", "--", text)


def test_piper_comes_first_when_configured_and_installed(tmp_path):
    wav = tmp_path / "answer.wav"
    prefs = {**PREFS, "piper_model": "/models/en_US-lessac-medium.onnx"}
    mac = voice.engines("Hi Alex.", prefs, system="mac",
                        which=fake_which("piper", "afplay", "say"), wav_path=wav)
    assert [e.name for e in mac] == ["piper", "say"]
    synth, play = mac[0].steps
    assert synth.argv == ("/usr/bin/piper", "-m", "/models/en_US-lessac-medium.onnx",
                          "-f", str(wav))
    assert synth.stdin == "Hi Alex.".encode("utf-8")
    assert play.argv == ("/usr/bin/afplay", str(wav))

    win = voice.engines("Hi Alex.", prefs, system="windows",
                        which=fake_which("piper", "powershell"), wav_path=wav)
    play = win[0].steps[1]
    assert base64.b64decode(play.argv[-1]).decode("utf-16-le") == voice._PLAY_SCRIPT
    assert json.loads(play.stdin) == {"wav": str(wav)}

    # configured but not installed: the OS voice, no error
    assert [e.name for e in voice.engines("Hi.", prefs, system="mac",
                                          which=fake_which("say"), wav_path=wav)] \
        == ["say"]


def test_speak_runs_without_a_shell_and_falls_back_past_a_broken_engine():
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        if argv[0].endswith("piper"):
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0)

    prefs = {**PREFS, "piper_model": "/models/voice.onnx"}
    used = voice.speak(HOSTILE[0], prefs, system="mac", run=run,
                       which=fake_which("piper", "afplay", "say"))
    assert used == "say"
    for argv, kw in calls:
        assert isinstance(argv, list) and not kw.get("shell")
        assert kw["check"] is True and kw["timeout"] > 0
    assert calls[-1][1]["input"] == HOSTILE[0].encode("utf-8")


def test_speak_with_no_engine_says_so_by_returning_none():
    def run(argv, **kw):
        raise AssertionError("nothing to run")
    assert voice.speak("Hello.", PREFS, system="linux", run=run,
                       which=fake_which()) is None
    assert voice.speak("   ", PREFS, system="mac", run=run,
                       which=fake_which("say")) is None


@pytest.mark.skipif(not platforms.is_windows(), reason="System.Speech is Windows-only")
def test_real_windows_engine_reads_hostile_text_without_running_it(tmp_path):
    """The real PowerShell, rendering to a WAV instead of the speakers: the
    text is spoken, and the commands inside it never run."""
    canary = tmp_path / "pwned.txt"
    text = (f"Jos\u00e9 says '; New-Item -ItemType File -Path '{canary}'; ' and "
            f"$(New-Item -ItemType File -Path '{canary}') \" ` & | done.")
    [engine] = voice.engines(text, PREFS, system="windows")
    [step] = engine.steps
    wav = tmp_path / "said.wav"
    job = json.loads(step.stdin) | {"wav": str(wav)}
    subprocess.run(list(step.argv), input=voice._job(**job), check=True,
                   capture_output=True, timeout=120)
    assert wav.stat().st_size > 10_000
    assert not canary.exists()


# ---------------------------------------------------------------- capture

def test_rms_of_silence_and_of_a_square_wave():
    assert voice.rms(b"") == 0.0
    assert voice.rms(_frame(0)) == 0.0
    assert voice.rms(_frame(1000)) == pytest.approx(1000)


def test_record_until_silence_stops_after_the_speaker_does():
    frames = [QUIET] * 3 + [LOUD] * 10 + [QUIET] * 30
    pcm = voice.record_until_silence(frames, silence_rms=400, silence_sec=0.5,
                                     max_record_sec=30)
    # 3 lead-in + 10 speech + the 7 quiet frames that reach 0.5 s
    assert len(pcm) == 20 * len(LOUD)


def test_record_until_silence_gives_up_when_nothing_is_said():
    pcm = voice.record_until_silence(itertools.repeat(QUIET), silence_rms=400,
                                     silence_sec=0.5, max_record_sec=30)
    assert pcm == b""


def test_record_until_silence_caps_the_utterance_length():
    pcm = voice.record_until_silence(itertools.repeat(LOUD), silence_rms=400,
                                     silence_sec=0.5, max_record_sec=1.0)
    assert len(pcm) == 13 * len(LOUD)        # 13 x 80 ms is the first >= 1 s


class FakeDetector:
    def __init__(self, fire_on: int):
        self.seen, self.fire_on, self.resets = 0, fire_on, 0

    def heard(self, frame):
        self.seen += 1
        return self.seen == self.fire_on

    def reset(self):
        self.resets += 1


def test_wake_word_then_one_utterance():
    woke = []
    detector = FakeDetector(fire_on=6)
    frames = [QUIET] * 5 + [LOUD] + [LOUD] * 4 + [QUIET] * 20
    pcm = voice.listen_after_wake(frames, detector, silence_rms=400,
                                  silence_sec=0.4, max_record_sec=30,
                                  on_wake=lambda: woke.append(True))
    assert woke == [True] and detector.resets == 1
    # the wake word itself is not part of the utterance
    assert len(pcm) == (4 + 5) * len(LOUD)


def test_wake_detector_reads_openwakeword_scores():
    pytest.importorskip("numpy")
    model = types.SimpleNamespace(predict=lambda samples: {"hey_jarvis": 0.7})
    assert voice.WakeDetector(model, 0.5).heard(LOUD) is True
    assert voice.WakeDetector(model, 0.9).heard(LOUD) is False


def test_wake_model_loads_with_onnx_or_says_how_to_download(monkeypatch):
    seen = {}

    class Model:
        def __init__(self, **kw):
            seen.update(kw)

    pkg = types.ModuleType("openwakeword")
    mod = types.ModuleType("openwakeword.model")
    mod.Model = Model
    monkeypatch.setitem(sys.modules, "openwakeword", pkg)
    monkeypatch.setitem(sys.modules, "openwakeword.model", mod)
    prefs = {"wake_model": "hey_jarvis", "wake_threshold": 0.5}
    assert isinstance(voice.require_wakeword(prefs), voice.WakeDetector)
    assert seen == {"wakeword_models": ["hey_jarvis"], "inference_framework": "onnx"}

    class Missing:
        def __init__(self, **kw):
            raise ValueError("hey_jarvis_v0.1.onnx not found")
    mod.Model = Missing
    with pytest.raises(VoiceUnavailable, match="download_models"):
        voice.require_wakeword(prefs)


def test_push_to_talk_refuses_silence_and_names_the_privacy_setting(tmp_path):
    warned = []

    class SilentStream:
        def __init__(self, **kw):
            self.callback = kw["callback"]

        def __enter__(self):
            for _ in range(20):
                self.callback(_frame(0), voice.FRAME_SAMPLES, None, None)
            return self

        def __exit__(self, *exc):
            return False

    sd = types.SimpleNamespace(RawInputStream=SilentStream)
    got = voice.record_push_to_talk(sd, tmp_path / "u.wav", wait=lambda p: "",
                                    warn=warned.append)
    assert got is None and not (tmp_path / "u.wav").exists()
    assert "silence" in warned[0] and voice.mic_permission_hint() in warned[0]


def test_push_to_talk_names_the_privacy_setting_when_the_mic_will_not_open(tmp_path):
    def refuse(**kw):
        raise OSError("Error querying device -1")
    sd = types.SimpleNamespace(RawInputStream=refuse)
    with pytest.raises(VoiceUnavailable, match="cannot open the microphone"):
        voice.record_push_to_talk(sd, tmp_path / "u.wav", wait=lambda p: "")
