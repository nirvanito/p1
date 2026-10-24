# Talking to your data (`lifecrm voice`)

```bash
.venv\Scripts\python.exe -m lifecrm voice            # Windows: push-to-talk
.venv/bin/python -m lifecrm voice                    # macOS
```

Press Enter, speak, press Enter again. What you said is transcribed on this
machine and goes one of two ways:

| you say | what happens |
|---|---|
| "**note** I owe Jake twenty bucks" | a quick note lands in the text inbox |
| "**remember** to renew the passport" | a quick note (reminders keep their verb) |
| "**log that** I ran 5k this morning" | a quick note |
| "**journal**: long day, board deck finally done" | a quick note |
| anything else — "how did I sleep last week" | a question, answered aloud with its sources printed |

**It never applies anything, and it never writes the database.** A note is a
`.txt` file in `[paths].inbox_text_dir`, named exactly like the phone's Quick
Note shortcut makes them (`2026-10-21 14.05.33 -0500.txt`). The next
`lifecrm intake` imports it like any phone note, `/log` extracts it into a
draft, and you review that draft before anything is applied. A question is
answered by the same retrieval as `lifecrm ask`, over a read-only connection
— unlike `ask`, the voice loop does not even record the question in
`recall_log`.

## Try it with no microphone and nothing installed

```bash
python -m lifecrm voice --text "how did I sleep last week"
python -m lifecrm voice --text "note Maya's birthday dinner is on Friday"
python -m lifecrm voice --file clip.m4a         # any audio whisper can read
```

`--text` skips audio entirely (handy for checking routing, or for typing).
`--file` transcribes a recording and leaves the file where it is — it is not
moved into the archive the way `transcribe --file` does.

Other switches: `--once` handles one utterance and exits (scripts, tests);
`--no-speak` prints only; `--no-llm` keeps answers deterministic; `--json`
prints each turn as JSON (route, note file or the full `ask --json` answer,
and the words it would have spoken) and speaks nothing.

## Setup

The microphone needs one optional package, `sounddevice`; the wake word needs
`openwakeword`. Both are in `requirements-voice.txt`, kept out of
`requirements.txt` so the core install stays small. If one is missing the
command says exactly what to run, and `--text` / `--file` still work.

### Windows laptop (with the Android phone)

1. `.venv\Scripts\python.exe -m pip install -r requirements-voice.txt`
2. **Microphone privacy:** Settings → Privacy & security → Microphone →
   turn on *Microphone access* and *Let desktop apps access your
   microphone*. Terminal apps are desktop apps; they have no per-app toggle.
   If this is off, the stream opens but records digital silence — the loop
   notices and says so instead of transcribing nothing.
3. **Speech** is built in: Windows' own System.Speech, driven through
   Windows PowerShell 5.1 (`powershell.exe`, not `pwsh` — PowerShell 7
   cannot load System.Speech). Extra voices: Settings → Time & language →
   Speech → Manage voices; then set `voice = "Microsoft Zira Desktop"` (or
   any installed SAPI voice name) under `[voice]`.

### MacBook (with the iPhone)

1. `.venv/bin/python -m pip install -r requirements-voice.txt`
2. **Microphone permission** belongs to the app you run the command in
   (Terminal, iTerm, VS Code). The first recording triggers macOS's prompt;
   allow it. If you clicked Don't Allow: System Settings → Privacy &
   Security → Microphone → turn your terminal on, then quit and reopen it.
   A denied terminal records silence, and the loop tells you so.
3. **Speech** is the built-in `say`. Voices: System Settings →
   Accessibility → Spoken Content → System voice → Manage Voices; then
   `voice = "Samantha"` under `[voice]` (`say -v '?'` lists them).

### Linux

`sudo apt install libportaudio2 espeak-ng` (or `speech-dispatcher` for
`spd-say`), then the pip line above.

## Models and latency

Speech-to-text is the same local faster-whisper the journal uses, with two
voice-specific settings: `[voice] model` (default `small`) and VAD on (it
trims the silence around a push-to-talk recording, which is also where
whisper invents phantom "Thank you."s). Language comes from
`[whisper] language`. Known entity names seed whisper's prompt — for a
seconds-long utterance that actually helps with names like "Veracruz".

Measured on the Windows laptop this was built on (CPU, int8), for a
four-second utterance:

| model | first turn (model load + transcribe) | each turn after |
|---|---|---|
| `small` (default) | ~6 s | ~3.5 s |
| `distil-large-v3` (the journal's model) | ~18 s | ~13 s |

The model stays loaded for the session, so only the first turn pays the
load. On top of that: a deterministic answer is instant; a local model
(Ollama, `[ask]` settings) adds a few seconds to tens of seconds on CPU
depending on the model; speech starts about a second after the answer
prints (PowerShell startup on Windows, near-instant with `say`). Apple
Silicon runs whisper on CPU too (ctranslate2 has no Metal backend), but its
cores are fast — expect `small` to feel similar or better. These are
estimates for a Mac; only the Windows row was measured.

## Answers: what is said and what is shown

The full answer prints first, exactly as `ask` prints it — sources and the
resolved window included — then the speaker reads a cleaned copy: citation
ids (`[M1]`) and snippet markers (`« »`) are dropped, ISO dates become
"July 27", unit abbreviations become words, and anything past
`max_spoken_chars` (default 400) stops at a sentence boundary with "The rest
is on screen."

With a local model turned on (`[ask] enabled = true`, off by default — docs/ask.md)
and running, `[voice] use_llm = true` (the default) lets it
phrase the evidence, exactly as in `ask`. One difference: a model answer
that cites nothing at all is kept by `ask` (with a warning on stderr), but
the voice loop uses the deterministic answer instead. Whenever the model's
answer is set aside — that case, or one citing evidence that does not exist
or stating numbers or dates its citations do not contain, which `ask` sets
aside too — the voice loop says so in the printed window line
(`answer: deterministic (model answer rejected)`), because nobody hears
stderr.

The words reach the speech engine only as data: on stdin (System.Speech,
`say -f -`, espeak, Piper) or as a single argument after `--` (spd-say),
never through a shell. A journal line or an imported message containing
`'; Remove-Item ...` is read aloud, not run — the test suite checks this
against the real Windows engine.

## Hands-free: the wake word

```bash
python -m lifecrm voice --wake
```

Uses openWakeWord's pre-trained "hey jarvis" model (`[voice] wake_model`),
running on onnxruntime (already installed with faster-whisper). Download the
pre-trained models once:

```bash
.venv\Scripts\python.exe -c "import openwakeword.utils as u; u.download_models()"
```

Say "hey jarvis", wait for `listening...`, speak; recording stops after
`silence_sec` (1.2 s) of quiet or `max_record_sec` (30 s). The microphone is
closed while it answers, so it never hears itself. Ctrl+C quits. The quiet
threshold `silence_rms` (default 400 on a 16-bit scale) depends on the mic
and the room; raise it if recordings never end, lower it if they cut you
off.

## A nicer voice: Piper (optional)

If `[voice] piper_model` points at a Piper `.onnx` voice and `piper` is on
PATH, answers are synthesized with Piper and played with the OS player
(System.Media on Windows, `afplay` on macOS, `aplay`/`paplay` on Linux).
Any failure falls back to the built-in voice. Piper is a separate install
(its binary release, or `pip install piper-tts`) and is not in
`requirements-voice.txt`.

## `[voice]` settings

All optional:

```toml
[voice]
model = "small"              # whisper model for voice turns (latency first)
vad_filter = true
use_llm = true               # phrase answers with the [ask] local model, when [ask] enabled = true
# llm_model = "llama3.1:8b"  # override [ask] model for voice only
speak = true                 # false = print only (same as --no-speak)
# voice = "Samantha"         # system voice name (say -v / SAPI voice)
max_spoken_chars = 400
# piper_model = "~/voices/en_US-lessac-medium.onnx"
# device = "MacBook Pro Microphone"   # input device name or index (default mic otherwise)
wake_model = "hey_jarvis"
wake_threshold = 0.5
silence_rms = 400
silence_sec = 1.2
max_record_sec = 30
```

## Routing, precisely

- The trigger has to be the **first word** (leading punctuation ignored):
  "notes from Tuesday", "journaling streak" and "remembered anything" are
  questions. "note to self", "journal entry" and a following "that" are
  part of the trigger and are dropped from the note.
- "remember to …" keeps the whole sentence — the reminder is the content.
- An utterance that **ends in "?"** is a question whatever its first word
  ("Remember when we went to Lake Bled?"). Whisper punctuates questions
  reliably, and the safe mistake is the one that writes nothing.
- A bare "note." with nothing after it writes nothing.
- Two notes in the same second get `-2`, `-3` — never an overwrite.
- Intake skips inbox files younger than 60 seconds (the still-uploading
  guard), so a note spoken just now is picked up by the *next* scheduled
  intake, not one run in the same minute.

## Privacy

Everything runs on this machine: whisper, the wake word, the speech engine,
and the optional local model. A push-to-talk recording is a temporary WAV,
deleted after transcription; only the text of a note is kept, in the inbox,
like a dictated phone note.

## Not yet verified on real hardware

The capture and wake-word code is tested with simulated audio, and the
Windows speech engine against the real System.Speech. Not exercised:
a physical microphone on either laptop, the macOS permission prompt, `say`
on a Mac, openWakeWord's real model and its 0.5 threshold, the default
`silence_rms`, Piper, and Linux engines. Please report what works.
