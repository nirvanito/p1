"""Optional local model, talked to over plain HTTP.

Ollama exposes an HTTP API on localhost, so this needs no SDK and no
dependency beyond the standard library — which keeps the promise the project
is built on: no metered API calls, nothing leaves the machine, and the
feature degrades to deterministic answers when no model is installed.

The model is never a source of facts. It receives an evidence block built by
lifecrm/recall.py and is instructed to cite it; the caller then verifies the
citations resolve. Swapping Ollama for another local runtime means replacing
this file only.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

DEFAULT_ENDPOINT = "http://localhost:11434"
DEFAULT_MODEL = "llama3.1:8b"

ANSWER_SYSTEM = """You answer questions about the user's own life database.

Rules, without exception:
- Use ONLY the EVIDENCE block. It is the complete set of facts available.
- Cite the evidence id in square brackets after every factual claim, like
  [M1] or [E2]. A claim with no citation is not allowed.
- Never invent or estimate numbers, dates, entries, or events. Never cite an
  id that is not in the EVIDENCE block.
- If the evidence does not answer the question, say plainly what is missing.
  "I don't have that data" is a correct and useful answer.
- Be brief and concrete: two or three sentences unless asked for more. Speak
  to the user as "you". Do not restate the question or add encouragement."""


def config(cfg) -> dict:
    ask = cfg.ask or {}
    return {"endpoint": str(ask.get("endpoint", DEFAULT_ENDPOINT)).rstrip("/"),
            "model": ask.get("model", DEFAULT_MODEL),
            "timeout": float(ask.get("timeout_sec", 120)),
            "enabled": bool(ask.get("enabled", True))}


def _get_json(url: str, timeout: float):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_json(url: str, payload: dict, timeout: float):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def status(cfg) -> dict:
    """Is a local model reachable, and is the configured one pulled?

    Probing is cheap and always short-timeout: `ask` must stay responsive
    when nothing is installed, which is the normal state until the user
    chooses to set a model up.
    """
    c = config(cfg)
    if not c["enabled"]:
        return {"available": False, "reason": "disabled in config ([ask] enabled=false)",
                "model": c["model"], "models": []}
    try:
        data = _get_json(f"{c['endpoint']}/api/tags", timeout=3)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        # The raw socket error says nothing a person can act on; the actionable
        # fact is that prose answers need a local model running.
        return {"available": False,
                "reason": (f"no local model at {c['endpoint']} — evidence "
                           f"answers only (see docs/ask.md to add one)"),
                "model": c["model"], "models": []}
    names = sorted(m.get("name", "") for m in data.get("models", []))
    if c["model"] not in names:
        stem = c["model"].split(":")[0]
        match = next((n for n in names if n.split(":")[0] == stem), None)
        if match is None:
            return {"available": False, "model": c["model"], "models": names,
                    "reason": (f"model '{c['model']}' is not pulled "
                               f"(available: {', '.join(names) or 'none'}) — "
                               f"run: ollama pull {c['model']}")}
        c["model"] = match
    return {"available": True, "reason": "", "model": c["model"], "models": names}


def answer(cfg, evidence: str, question: str, model: str | None = None) -> str:
    """Phrase the evidence as an answer. Raises on transport failure — the
    caller falls back to the deterministic answer rather than failing the
    command."""
    c = config(cfg)
    prompt = (f"EVIDENCE\n{evidence}\n\nQUESTION: {question}\n\n"
              f"Answer using only the evidence above, citing ids.")
    data = _post_json(f"{c['endpoint']}/api/chat", {
        "model": model or c["model"],
        "messages": [{"role": "system", "content": ANSWER_SYSTEM},
                     {"role": "user", "content": prompt}],
        "stream": False,
        "options": {"temperature": 0.1},
    }, timeout=c["timeout"])
    return (data.get("message") or {}).get("content", "").strip()
