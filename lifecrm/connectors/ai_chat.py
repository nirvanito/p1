"""AI chat export connector: Claude and ChatGPT conversation archives.

One observation per **message**, not per conversation. Recall works at turn
granularity ("what did I say about the Slovenia trip"), and a confirmed entry
citing this as provenance should point at the sentence, not at a 200-turn
thread. `title` carries the conversation name on every row so each row is
self-describing and FTS finds a thread by its name.

Both vendors ship a `conversations.json`; the shapes are entirely different.

  Claude    [{uuid, name, created_at, updated_at, account: {uuid},
              chat_messages: [{uuid, text, content: [...], sender, created_at}]}]

  ChatGPT   [{title, create_time, conversation_id, current_node,
              mapping: {node_id: {id, parent, children, message}}}]

ChatGPT's `mapping` is a tree, not a list: regenerated answers and edited
prompts leave every branch in the export. All branches are imported — the
archive remembers what happened — but each message records whether it lies on
the conversation's final path, so recall can prefer the answer that was kept.

Identity is the vendor's own message id, so re-importing a later export of the
same account is a no-op for everything already stored.

Known limitation, by design: chat exports carry no per-message timezone, so
journal dates use the configured home timezone. A conversation held while
travelling lands on the home-timezone day. The export does not say where you
were, and guessing would be worse than the documented rule.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..domain.util import DomainError, canonical_json
from . import base

name = "ai_chat"
streams = ("claude_chat", "chatgpt_chat")

# Roles worth keeping. 'system' is scaffolding, 'tool' is machine chatter.
_KEEP_ROLES = {"user", "assistant", "human"}
# ChatGPT content types that hold prose. Everything else is counted, not imported.
_TEXT_TYPES = {"text", "multimodal_text"}


# ---------------------------------------------------------------- detection

def _detect(data) -> str:
    if not isinstance(data, list):
        raise DomainError(
            f"conversations.json is a {type(data).__name__}, expected a list of "
            f"conversations — is this an AI chat export?")
    for conv in data:
        if not isinstance(conv, dict):
            continue
        if "chat_messages" in conv:
            return "claude"
        if "mapping" in conv:
            return "chatgpt"
    if not data:
        raise DomainError("conversations.json is an empty list — nothing to import")
    keys = ", ".join(sorted(list(data[0].keys())[:8])) if isinstance(data[0], dict) \
        else "(not an object)"
    raise DomainError(
        f"conversations.json has neither 'chat_messages' (Claude) nor 'mapping' "
        f"(ChatGPT) — first conversation has: {keys}")


# ---------------------------------------------------------------- Claude

def _claude_text(msg: dict) -> tuple[str, int]:
    """Prose plus a count of non-text blocks (thinking, tool use, images)."""
    text = msg.get("text")
    if isinstance(text, str) and text.strip():
        blocks = msg.get("content")
        other = sum(1 for b in blocks if isinstance(b, dict)
                    and b.get("type") != "text") if isinstance(blocks, list) else 0
        return text.strip(), other
    parts, other = [], 0
    for b in msg.get("content") or []:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text" and isinstance(b.get("text"), str):
            parts.append(b["text"])
        else:
            other += 1
    return "\n\n".join(p for p in parts if p.strip()).strip(), other


def _read_claude(data: list, cfg: Config) -> dict:
    records, accounts = [], {}
    skipped: dict[str, int] = {}
    empty = no_time = 0
    for conv in data:
        if not isinstance(conv, dict):
            skipped["conversation not an object"] = \
                skipped.get("conversation not an object", 0) + 1
            continue
        conv_id = conv.get("uuid")
        title = (conv.get("name") or "").strip() or "(untitled)"
        conv_created = base.iso_utc(conv.get("created_at"))
        conv_updated = base.iso_utc(conv.get("updated_at"))
        acct = (conv.get("account") or {}).get("uuid") \
            if isinstance(conv.get("account"), dict) else None
        if acct:
            accounts[acct] = accounts.get(acct, 0) + 1
        messages = conv.get("chat_messages")
        if not isinstance(messages, list):
            skipped["conversation without chat_messages"] = \
                skipped.get("conversation without chat_messages", 0) + 1
            continue
        for turn, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            mid = msg.get("uuid")
            sender = msg.get("sender")
            if not mid or sender not in _KEEP_ROLES:
                skipped[f"message role '{sender}'"] = \
                    skipped.get(f"message role '{sender}'", 0) + 1
                continue
            body, other_blocks = _claude_text(msg)
            if not body:
                empty += 1
                continue
            started = base.iso_utc(msg.get("created_at")) or conv_created
            if started is None:
                no_time += 1
                continue
            detail = {"conversation_id": conv_id, "turn": turn, "role": sender}
            if other_blocks:
                detail["non_text_blocks"] = other_blocks
            for key in ("attachments", "files"):
                items = msg.get(key)
                if isinstance(items, list) and items:
                    detail[key] = len(items)
            records.append({
                "stream": "claude_chat",
                "external_id": str(mid),
                "journal_date": base.journal_date_for_iso(started, cfg),
                "started_at": started,
                "ended_at": None,
                "utc_offset_sec": None,
                "direction": "out" if sender == "human" else "in",
                "title": title,
                "body": body,
                "value": None,
                "unit": None,
                "value_json": canonical_json(detail),
                "media_path": None,
                "origin_app": "claude",
                "last_modified_at": base.iso_utc(msg.get("updated_at")) or conv_updated,
                "_account": acct,
            })
    account = max(accounts, key=accounts.get) if accounts else None
    handles = []
    if account:
        handles.append({"kind": "app_account", "value": f"claude:{account}",
                        "display_name": "Claude account"})
    _attach_participants(records, handle_value=f"claude:{account}" if account else None,
                         kind="app_account")
    return {"records": records, "handles": handles, "account": account,
            "skipped": skipped, "empty": empty, "no_time": no_time,
            "unmapped": []}


# ---------------------------------------------------------------- ChatGPT

def _current_path(mapping: dict, current_node) -> set:
    """Node ids on the conversation's final branch, walking parents from the
    leaf. Cycle-guarded: a malformed export must not hang the import."""
    path, node, guard = set(), current_node, 0
    while node and node in mapping and guard <= len(mapping):
        path.add(node)
        node = (mapping[node] or {}).get("parent")
        guard += 1
    return path


def _chatgpt_text(content: dict) -> tuple[str, int]:
    parts = content.get("parts")
    if not isinstance(parts, list):
        return "", 0
    out, non_text = [], 0
    for p in parts:
        if isinstance(p, str):
            if p.strip():
                out.append(p)
        else:
            non_text += 1          # image_asset_pointer, audio, etc.
    return "\n\n".join(out).strip(), non_text


def _read_chatgpt(data: list, target: Path, cfg: Config) -> dict:
    records = []
    skipped: dict[str, int] = {}
    content_types: dict[str, int] = {}
    empty = no_time = 0
    for conv in data:
        if not isinstance(conv, dict):
            skipped["conversation not an object"] = \
                skipped.get("conversation not an object", 0) + 1
            continue
        conv_id = conv.get("conversation_id") or conv.get("id")
        title = (conv.get("title") or "").strip() or "(untitled)"
        conv_created = base.iso_utc(conv.get("create_time"))
        conv_updated = base.iso_utc(conv.get("update_time"))
        mapping = conv.get("mapping")
        if not isinstance(mapping, dict):
            skipped["conversation without mapping"] = \
                skipped.get("conversation without mapping", 0) + 1
            continue
        on_path = _current_path(mapping, conv.get("current_node"))
        # Deterministic order regardless of dict iteration: time, then node id.
        nodes = sorted(mapping.items(),
                       key=lambda kv: (((kv[1] or {}).get("message") or {})
                                       .get("create_time") or 0, str(kv[0])))
        for turn, (node_id, node) in enumerate(nodes):
            msg = (node or {}).get("message")
            if not isinstance(msg, dict):
                continue                       # root node carries no message
            role = (msg.get("author") or {}).get("role")
            if role not in _KEEP_ROLES:
                skipped[f"message role '{role}'"] = \
                    skipped.get(f"message role '{role}'", 0) + 1
                continue
            meta = msg.get("metadata") or {}
            if meta.get("is_visually_hidden_from_conversation"):
                skipped["hidden system context"] = \
                    skipped.get("hidden system context", 0) + 1
                continue
            content = msg.get("content") or {}
            ctype = content.get("content_type")
            content_types[str(ctype)] = content_types.get(str(ctype), 0) + 1
            if ctype not in _TEXT_TYPES:
                continue
            body, non_text = _chatgpt_text(content)
            if not body:
                empty += 1
                continue
            started = base.iso_utc(msg.get("create_time")) or conv_created
            if started is None:
                no_time += 1
                continue
            mid = msg.get("id") or node_id
            detail = {"conversation_id": conv_id, "turn": turn, "role": role,
                      "content_type": ctype, "on_current_path": node_id in on_path}
            if non_text:
                detail["non_text_parts"] = non_text
            if meta.get("model_slug"):
                detail["model"] = meta["model_slug"]
            records.append({
                "stream": "chatgpt_chat",
                "external_id": str(mid),
                "journal_date": base.journal_date_for_iso(started, cfg),
                "started_at": started,
                "ended_at": None,
                "utc_offset_sec": None,
                "direction": "out" if role == "user" else "in",
                "title": title,
                "body": body,
                "value": None,
                "unit": None,
                "value_json": canonical_json(detail),
                "media_path": None,
                "origin_app": "chatgpt",
                "last_modified_at": base.iso_utc(msg.get("update_time")) or conv_updated,
            })

    # user.json is optional and is the only place a ChatGPT export names the
    # account. When absent, the import simply has no handles — that is fine.
    account, handles = None, []
    user = base.optional_json_member(target, "user.json")
    if isinstance(user, dict):
        email = user.get("email")
        if isinstance(email, str) and "@" in email:
            account = email
            handles.append({"kind": "email", "value": email,
                            "display_name": user.get("name") or "ChatGPT account"})
    _attach_participants(records, handle_value=account, kind="email")
    unmapped = [{"content_type": k, "messages": v}
                for k, v in sorted(content_types.items())
                if k not in _TEXT_TYPES]
    return {"records": records, "handles": handles, "account": account,
            "skipped": skipped, "empty": empty, "no_time": no_time,
            "unmapped": unmapped}


# ---------------------------------------------------------------- shared

def _attach_participants(records: list, handle_value: str | None, kind: str) -> None:
    """The account is the sender of what you wrote and the recipient of what
    the model wrote. No other participant exists in a chat export — the
    assistant is not a person and gets no handle."""
    for rec in records:
        rec.pop("_account", None)
        if handle_value:
            rec["participants"] = [{
                "kind": kind, "value": handle_value,
                "role": "sender" if rec["direction"] == "out" else "recipient"}]
        else:
            rec["participants"] = []


def scan(target: Path, cfg: Config) -> base.Scan:
    data, member = base.read_json_member(target, "conversations.json")
    flavor = _detect(data)
    result = _read_claude(data, cfg) if flavor == "claude" \
        else _read_chatgpt(data, target, cfg)

    skipped = [f"{v} × {k}" for k, v in sorted(result["skipped"].items())]
    if result["empty"]:
        skipped.append(f"{result['empty']} × message with no text")
    if result["no_time"]:
        skipped.append(f"{result['no_time']} × message with no usable timestamp")

    records = sorted(result["records"],
                     key=lambda r: (r["stream"], r["started_at"], r["external_id"]))
    return base.empty_scan(
        name,
        account=result["account"],
        source_file=target.name,
        source_hash=base.file_hash(target) if target.is_file() else None,
        records=records,
        handles=result["handles"],
        unmapped=result["unmapped"],
        skipped=skipped,
        flavor=flavor,
        member=member,
        conversations=len(data))


# ---------------------------------------------------------------- report

def _sample(recs: list[dict]) -> str:
    r = max(recs, key=lambda x: (x["started_at"], x["external_id"]))
    body = " ".join(r["body"].split())
    if len(body) > 60:
        body = body[:59] + "…"
    arrow = "you" if r["direction"] == "out" else "assistant"
    return f"{r['journal_date']} {arrow}: {body}"


def report_lines(scan: base.Scan, plan: dict) -> list[str]:
    lines = [f"ai-chat export: {scan['source_file']} "
             f"({scan['flavor']}, {scan['conversations']} conversation(s), "
             f"member: {scan['member']})"]
    if scan["account"]:
        lines.append(f"  account: {scan['account']}")

    by_stream: dict[str, dict] = {}
    for rec in plan["new"]:
        by_stream.setdefault(rec["stream"], {"new": [], "upd": 0})["new"].append(rec)
    for _, rec, _ in plan["updates"]:
        by_stream.setdefault(rec["stream"], {"new": [], "upd": 0})["upd"] += 1
    for stream in sorted(by_stream):
        info = by_stream[stream]
        recs = info["new"] or [rec for _, rec, _ in plan["updates"]
                               if rec["stream"] == stream]
        span = (f"{min(r['journal_date'] for r in recs)}"
                f"..{max(r['journal_date'] for r in recs)}") if recs else "-"
        lines.append(f"  {stream}: {len(info['new'])} new, {info['upd']} changed "
                     f"| {span} | e.g. {_sample(recs)}")
    if not by_stream:
        lines.append("  nothing new — export matches the database")

    tail = []
    if plan["identical"]:
        tail.append(f"{plan['identical']} identical (skipped)")
    if plan["refreshed"]:
        tail.append(f"{len(plan['refreshed'])} modified-time refresh(es)")
    if plan["dup_incoming"]:
        tail.append(f"{plan['dup_incoming']} duplicate id(s) inside the export")
    if tail:
        lines.append("  " + ", ".join(tail))

    for h in scan["handles"]:
        lines.append(f"  handle: {h['kind']}:{h['value']} — resolve it with "
                     f"'lifecrm handles link' to attribute these to a person")
    for c in plan["conflicts"]:
        lines.append(f"  CONFLICT {c['stream']} {c['external_id'][:12]}…: differs "
                     f"from the stored record but is not newer — kept existing "
                     f"({', '.join(sorted(c['changes']))})")
    for err in plan["invalid"][:5]:
        lines.append(f"  INVALID {err}")
    if len(plan["invalid"]) > 5:
        lines.append(f"  … and {len(plan['invalid']) - 5} more invalid record(s)")
    for s in scan["skipped"]:
        lines.append(f"  skipped: {s}")
    if scan["unmapped"]:
        lines.append("  not imported (no prose): " + ", ".join(
            f"{u['content_type']} ({u['messages']})" for u in scan["unmapped"]))
    return lines
