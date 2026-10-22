"""`lifecrm voice` — talk to the second brain: questions answered aloud with
their citations, notes dropped into the quick-note inbox for /log to review.

Logic lives in lifecrm/voice.py; this module wires options, prints, and
decides when to stop. The database is opened read-only, for questions only.
Nothing is applied and nothing is written to lifecrm.db — a spoken note is a
file in the inbox, exactly like one dictated on the phone.
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import typer


def register(app: typer.Typer) -> None:
    app.command("voice")(voice)


def _prompt(text: str) -> str:
    # Prompts go to stderr so `--json` output stays parseable.
    typer.echo(text, nl=False, err=True)
    return input()


def _show(turn: dict) -> None:
    from .. import voice as voice_mod
    if turn["route"] == "note":
        note = turn["note"]
        typer.echo(f"noted -> {note['path']}")
        typer.echo(f"  journal date {note['journal_date']}; the next `lifecrm "
                   f"intake` imports it and /log reviews it — nothing is applied")
    elif turn["route"] == "question":
        for line in voice_mod.answer_lines(turn["answer"]):
            typer.echo(line)
    else:
        typer.echo("heard nothing to act on")


def voice(
    text: str = typer.Option(
        None, "--text", help="Skip audio: handle this as if it were heard"),
    file: str = typer.Option(
        None, "--file", help="Transcribe this audio file instead of the "
                             "microphone (the file is left where it is)"),
    once: bool = typer.Option(False, "--once", help="Handle one utterance, then exit"),
    wake: bool = typer.Option(
        False, "--wake", help="Hands-free: wait for the wake word (needs openwakeword)"),
    no_speak: bool = typer.Option(False, "--no-speak", help="Print only, never speak"),
    no_llm: bool = typer.Option(
        False, "--no-llm", help="Deterministic answers only, never a model"),
    as_json: bool = typer.Option(
        False, "--json", help="Print each turn as JSON (speaks nothing)"),
) -> None:
    """Talk to your data: ask a question, or say "note ..." to leave a note.

    Questions get the same cited retrieval as `ask`, printed with their
    sources and spoken without them. Utterances that start with note,
    remember, log that or journal become a quick note in the text inbox:
    the next intake imports it and /log reviews it like a phone note.
    Push-to-talk by default (Enter to start, Enter to stop); see
    docs/voice.md for setup.
    """
    from .. import voice as voice_mod
    from ..cli import _cfg, _conn, _echo_json, _fail
    from ..config import ConfigError
    from ..domain.util import DomainError

    cfg = _cfg()
    try:
        prefs = voice_mod.settings(cfg)
    except ConfigError as e:
        _fail(str(e))
    if text is not None and file is not None:
        _fail("use one of --text or --file, not both")
    if wake and (text is not None or file is not None):
        _fail("--wake listens to the microphone — drop --text / --file")

    speaking = prefs["speak"] and not no_speak and not as_json
    use_llm = prefs["use_llm"] and not no_llm
    muted: list[bool] = []

    def warn(msg: str) -> None:
        typer.echo(msg, err=True)

    def say(words: str | None) -> None:
        if not speaking or not words:
            return
        if voice_mod.speak(words, prefs) is None and not muted:
            muted.append(True)
            warn("no speech engine found — answers are printed only "
                 "(see docs/voice.md)")

    def run_turn(utterance: str) -> bool:
        try:
            turn = voice_mod.handle(
                utterance, cfg=cfg, prefs=prefs,
                open_conn=lambda: _conn(cfg, readonly=True),
                use_llm=use_llm, warn=warn)
        except DomainError as e:
            warn(f"error: {e}")
            return False
        except sqlite3.Error as e:
            warn(f"error: retrieval failed: {e}")
            return False
        if as_json:
            _echo_json(turn)
        else:
            _show(turn)
        say(turn.get("say"))
        return turn["route"] != "empty"

    transcriber = voice_mod.Transcriber(cfg, prefs)

    def hear(audio: Path) -> str | None:
        try:
            heard = transcriber(audio)
        except Exception as e:      # model download/load, audio decode: name it, keep going
            warn(f"error: transcription failed: {e}")
            return None
        if not as_json:
            typer.echo(f'heard: "{heard}"')
        return heard

    if text is not None:
        if not text.strip():
            _fail('say what? e.g. python -m lifecrm voice --text "how did I '
                  'sleep last week"')
        raise typer.Exit(0 if run_turn(text) else 1)

    if file is not None:
        path = Path(file).expanduser()
        if not path.is_file():
            _fail(f"file not found: {path}")
        heard = hear(path)
        raise typer.Exit(0 if heard is not None and run_turn(heard) else 1)

    try:
        sd = voice_mod.require_sounddevice()
        detector = voice_mod.require_wakeword(prefs) if wake else None
    except voice_mod.VoiceUnavailable as e:
        _fail(str(e))

    ok = False
    try:
        while True:
            with tempfile.TemporaryDirectory(prefix="lifecrm-voice-",
                                             ignore_cleanup_errors=True) as tmp:
                wav = Path(tmp) / "utterance.wav"
                if detector is not None:
                    warn(f"say '{prefs['wake_model'].replace('_', ' ')}' "
                         f"(Ctrl+C to quit)")
                    frames = voice_mod.mic_frames(sd, prefs["device"])
                    try:
                        pcm = voice_mod.listen_after_wake(
                            frames, detector, silence_rms=prefs["silence_rms"],
                            silence_sec=prefs["silence_sec"],
                            max_record_sec=prefs["max_record_sec"],
                            on_wake=lambda: warn("listening..."))
                    finally:
                        frames.close()
                    audio = voice_mod.write_wav(wav, pcm) if pcm else None
                    if audio is None:
                        warn("(nothing heard)")
                else:
                    audio = voice_mod.record_push_to_talk(
                        sd, wav, wait=_prompt, warn=warn, device=prefs["device"])
                heard = hear(audio) if audio is not None else None
            ok = heard is not None and run_turn(heard)
            if once:
                break
    except voice_mod.VoiceUnavailable as e:
        _fail(str(e))
    except (KeyboardInterrupt, EOFError):
        typer.echo("", err=True)
        return
    raise typer.Exit(0 if ok else 1)
