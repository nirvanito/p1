"""Sample AI chat exports: the persona's Claude and ChatGPT conversations.

Both are rendered in the vendor's own export format, as the `ai_chat`
connector reads them:

  Claude    claude.ai Settings > Privacy > Export data: a zip holding
            conversations.json (conversation -> chat_messages, each with
            `text` AND typed `content` blocks), users.json and projects.json.
  ChatGPT   Settings > Data controls > Export: a zip holding
            conversations.json (conversation -> `mapping`, a message TREE
            rooted at a message-less node, with a hidden system turn),
            user.json naming the account, and chat.html.

The same accounts exist on either computer — the chats are cloud history,
not device data — so both combos get both exports. Identity (conversation,
message and account ids) is derived from the anchor, so a sample is
byte-identical for the same (anchor, combo) and two anchors never collide.
"""

from __future__ import annotations

import json
import uuid
import zipfile
from datetime import date, timedelta
from pathlib import Path

from .. import persona

VENDORS = ("claude", "chatgpt")
_NS = uuid.UUID("6f1c1f9e-6d1a-4b8e-9a51-1c0de0a1c4a7")
# Seconds between turns: the person types, the model answers.
_USER_GAP, _ASSISTANT_GAP = 150, 25


def _id(*parts) -> str:
    return str(uuid.uuid5(_NS, "|".join(str(p) for p in parts)))


def _check(combo: str, vendor: str) -> None:
    if combo not in persona.COMBOS:
        raise ValueError(f"unknown combo '{combo}' (expected one of "
                         f"{', '.join(persona.COMBOS)})")
    if vendor not in VENDORS:
        raise ValueError(f"unknown AI chat vendor '{vendor}' (expected one of "
                         f"{', '.join(VENDORS)})")


def _turn_times(chat: persona.AiChat, anchor: date) -> list:
    """Local start, then alternating answer/next-question gaps."""
    t = persona.local_dt(anchor, chat.offset, chat.time)
    out = []
    for role, _ in chat.turns:
        out.append(t)
        t = t + timedelta(seconds=_ASSISTANT_GAP if role == "user" else _USER_GAP)
    return out


def _iso_micro(dt, n: int) -> str:
    """Claude writes microsecond timestamps; a fixed fraction per turn keeps
    the sample deterministic (the connector drops sub-seconds anyway)."""
    utc = persona.utc_iso(dt)[:-1]
    return f"{utc}.{(n * 104729) % 1000000:06d}Z"


def _zip(path: Path, members: list[tuple[str, str]], anchor: date) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = (anchor.year, anchor.month, anchor.day, 9, 0, 0)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in members:
            info = zipfile.ZipInfo(name, date_time=stamp)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, text.encode("utf-8"))
    return path


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- Claude

def claude_conversations(anchor: date) -> list[dict]:
    account = _id("claude-account", persona.OWNER.email)
    out = []
    for chat in persona.AI_CHATS:
        if chat.vendor != "claude":
            continue
        conv_id = _id(anchor, "claude", chat.title)
        times = _turn_times(chat, anchor)
        messages = []
        for n, ((role, text), t) in enumerate(zip(chat.turns, times)):
            stamp = _iso_micro(t, n + 1)
            messages.append({
                "uuid": _id(anchor, "claude", chat.title, n),
                "text": text,
                "content": [{"start_timestamp": stamp, "stop_timestamp": stamp,
                             "type": "text", "text": text, "citations": []}],
                "sender": "human" if role == "user" else "assistant",
                "created_at": stamp,
                "updated_at": stamp,
                "attachments": [],
                "files": [],
            })
        out.append({
            "uuid": conv_id,
            "name": chat.title,
            "summary": "",
            "created_at": _iso_micro(times[0], 0),
            "updated_at": messages[-1]["updated_at"],
            "account": {"uuid": account},
            "chat_messages": messages,
        })
    return out


def _write_claude(dest: Path, anchor: date) -> Path:
    users = [{"uuid": _id("claude-account", persona.OWNER.email),
              "full_name": persona.OWNER.name,
              "email_address": persona.OWNER.email,
              "verified_phone_number": None}]
    return _zip(dest / f"data-{anchor.isoformat()}-claude-export.zip",
                [("conversations.json", _dump(claude_conversations(anchor))),
                 ("users.json", _dump(users)),
                 ("projects.json", "[]")], anchor)


# ---------------------------------------------------------------- ChatGPT

def chatgpt_conversations(anchor: date) -> list[dict]:
    out = []
    for chat in persona.AI_CHATS:
        if chat.vendor != "chatgpt":
            continue
        conv_id = _id(anchor, "chatgpt", chat.title)
        times = _turn_times(chat, anchor)
        root, system = "client-created-root", _id(anchor, "chatgpt", chat.title, "system")
        node_ids = [_id(anchor, "chatgpt", chat.title, n) for n in range(len(chat.turns))]
        mapping = {
            root: {"id": root, "message": None, "parent": None, "children": [system]},
            system: {"id": system, "message": {
                "id": system, "author": {"role": "system", "name": None, "metadata": {}},
                "create_time": None, "update_time": None,
                "content": {"content_type": "text", "parts": [""]},
                "status": "finished_successfully", "end_turn": True, "weight": 0.0,
                "metadata": {"is_visually_hidden_from_conversation": True},
                "recipient": "all", "channel": None},
                "parent": root, "children": node_ids[:1]},
        }
        for n, ((role, text), t) in enumerate(zip(chat.turns, times)):
            epoch = t.timestamp() + (n % 7) / 1000
            meta = {"model_slug": "gpt-4o", "default_model_slug": "gpt-4o"} \
                if role == "assistant" else {}
            mapping[node_ids[n]] = {
                "id": node_ids[n],
                "message": {
                    "id": node_ids[n],
                    "author": {"role": role, "name": None, "metadata": {}},
                    "create_time": epoch, "update_time": None,
                    "content": {"content_type": "text", "parts": [text]},
                    "status": "finished_successfully",
                    "end_turn": role == "assistant", "weight": 1.0,
                    "metadata": meta, "recipient": "all", "channel": None},
                "parent": node_ids[n - 1] if n else system,
                "children": node_ids[n + 1:n + 2],
            }
        out.append({
            "title": chat.title,
            "create_time": times[0].timestamp(),
            "update_time": times[-1].timestamp(),
            "mapping": mapping,
            "moderation_results": [],
            "current_node": node_ids[-1],
            "plugin_ids": None,
            "conversation_id": conv_id,
            "conversation_template_id": None,
            "gizmo_id": None,
            "is_archived": False,
            "safe_urls": [],
            "default_model_slug": "gpt-4o",
            "id": conv_id,
        })
    return out


def _write_chatgpt(dest: Path, anchor: date) -> Path:
    convs = chatgpt_conversations(anchor)
    user = {"id": "user-" + _id("chatgpt-account", persona.OWNER.email)[:24],
            "email": persona.OWNER.email, "chatgpt_plus_user": False,
            "phone_number": None}
    html = ("<html><head><meta charset=\"utf-8\"><title>ChatGPT Data Export</title>"
            "</head><body>" + "".join(f"<h4>{c['title']}</h4>" for c in convs)
            + "</body></html>")
    return _zip(dest / f"chatgpt-export-{anchor.isoformat()}.zip",
                [("conversations.json", _dump(convs)),
                 ("user.json", _dump(user)),
                 ("message_feedback.json", "[]"),
                 ("shared_conversations.json", "[]"),
                 ("chat.html", html)], anchor)


# ---------------------------------------------------------------- the contract

def write_sample(dest: Path, anchor: date, combo: str, vendor: str = "claude") -> Path:
    """One vendor's export (the Claude one unless `vendor` says otherwise),
    ready for `lifecrm observe ai_chat <path>`."""
    _check(combo, vendor)
    dest = Path(dest)
    return _write_claude(dest, anchor) if vendor == "claude" \
        else _write_chatgpt(dest, anchor)


def write_samples(dest: Path, anchor: date, combo: str) -> list[Path]:
    """Every AI chat export this combo has: Claude first, then ChatGPT —
    each is its own import, because each is its own account."""
    return [write_sample(dest, anchor, combo, vendor) for vendor in VENDORS]
