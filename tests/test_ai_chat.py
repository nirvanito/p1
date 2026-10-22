"""AI chat export connector: Claude and ChatGPT conversation archives.

The connector is a pure reader, so most of this exercises scan() directly
against fixtures shaped like the real exports — including the parts that are
easy to get wrong: content blocks with no top-level text, sub-second
timestamps, rollover-hour dating, ChatGPT's message tree with abandoned
branches, hidden system context, and non-prose content types.
"""

import json
import zipfile

import pytest
from conftest import FIXTURES, cli_project
from typer.testing import CliRunner

from lifecrm.cli import app
from lifecrm.connectors import ai_chat, base
from lifecrm.domain.util import DomainError

runner = CliRunner()


def claude_export(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = tmp_path / "conversations.json"
    p.write_bytes((FIXTURES / "ai_chat_claude.json").read_bytes())
    return p


def chatgpt_export(tmp_path, with_user=True):
    """Zipped, like the real ChatGPT export — and the only way user.json can
    accompany conversations.json."""
    z = tmp_path / "chatgpt-export.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("conversations.json",
                    (FIXTURES / "ai_chat_chatgpt.json").read_text(encoding="utf-8"))
        if with_user:
            zf.writestr("user.json", json.dumps(
                {"id": "user-1", "email": "me@example.com", "name": "Casey"}))
    return z


def _by_id(scan):
    return {r["external_id"]: r for r in scan["records"]}


# ---------------------------------------------------------------- Claude

def test_claude_scan_shape(tmp_path, cfg):
    scan = ai_chat.scan(claude_export(tmp_path), cfg)
    assert scan["connector"] == "ai_chat"
    assert scan["flavor"] == "claude"
    assert scan["conversations"] == 2
    recs = _by_id(scan)
    # m3 is whitespace-only, m4 is a system message: neither is imported.
    assert set(recs) == {"m1", "m2", "m5"}
    assert all(r["stream"] == "claude_chat" for r in scan["records"])
    assert all(r["origin_app"] == "claude" for r in scan["records"])


def test_claude_direction_and_title(tmp_path, cfg):
    recs = _by_id(ai_chat.scan(claude_export(tmp_path), cfg))
    assert recs["m1"]["direction"] == "out"        # you wrote it
    assert recs["m2"]["direction"] == "in"         # the assistant wrote it
    assert recs["m1"]["title"] == "Slovenia trip planning"
    assert recs["m5"]["title"] == "(untitled)"     # conversation with no name


def test_claude_falls_back_to_content_blocks(tmp_path, cfg):
    """text is empty; the prose lives in a content block next to a thinking
    block that must not be imported as text."""
    recs = _by_id(ai_chat.scan(claude_export(tmp_path), cfg))
    assert recs["m2"]["body"] == "That is Bled — Lake Bled, with the castle on the cliff."
    detail = json.loads(recs["m2"]["value_json"])
    assert detail["non_text_blocks"] == 1
    assert detail["role"] == "assistant"


def test_claude_subsecond_timestamp_is_truncated(tmp_path, cfg):
    """Two exports of the same message must compare equal, so sub-second
    precision is dropped rather than carried."""
    recs = _by_id(ai_chat.scan(claude_export(tmp_path), cfg))
    assert recs["m2"]["started_at"] == "2026-03-10T15:04:20Z"


def test_claude_rollover_hour_applies(tmp_path, cfg):
    """06:30 UTC is 01:30 in America/Chicago — before the 05:00 rollover, so
    it belongs to the previous journal day."""
    recs = _by_id(ai_chat.scan(claude_export(tmp_path), cfg))
    assert recs["m1"]["journal_date"] == "2026-03-10"
    assert recs["m5"]["started_at"] == "2026-03-11T06:30:00Z"
    assert recs["m5"]["journal_date"] == "2026-03-10"


def test_claude_emits_account_handle_not_a_name(tmp_path, cfg):
    scan = ai_chat.scan(claude_export(tmp_path), cfg)
    assert scan["account"] == "acct-1"
    assert scan["handles"] == [{"kind": "app_account", "value": "claude:acct-1",
                                "display_name": "Claude account"}]
    recs = _by_id(scan)
    assert recs["m1"]["participants"] == [
        {"kind": "app_account", "value": "claude:acct-1", "role": "sender"}]
    assert recs["m2"]["participants"] == [
        {"kind": "app_account", "value": "claude:acct-1", "role": "recipient"}]


def test_claude_attachments_counted_not_imported(tmp_path, cfg):
    detail = json.loads(_by_id(ai_chat.scan(claude_export(tmp_path), cfg))
                        ["m5"]["value_json"])
    assert detail["attachments"] == 1
    assert _by_id(ai_chat.scan(claude_export(tmp_path), cfg))["m5"]["media_path"] is None


def test_claude_skips_are_reported(tmp_path, cfg):
    scan = ai_chat.scan(claude_export(tmp_path), cfg)
    joined = " | ".join(scan["skipped"])
    assert "message with no text" in joined
    assert "system" in joined


# ---------------------------------------------------------------- ChatGPT

def test_chatgpt_scan_shape(tmp_path, cfg):
    scan = ai_chat.scan(chatgpt_export(tmp_path), cfg)
    assert scan["flavor"] == "chatgpt"
    recs = _by_id(scan)
    # n0 is hidden system context, n4 is a code block, root has no message.
    assert set(recs) == {"n1", "n2", "n2b", "n3"}
    assert all(r["stream"] == "chatgpt_chat" for r in scan["records"])


def test_chatgpt_keeps_abandoned_branches_but_marks_them(tmp_path, cfg):
    """The archive remembers every branch; recall needs to know which one was
    actually kept."""
    recs = _by_id(ai_chat.scan(chatgpt_export(tmp_path), cfg))
    assert json.loads(recs["n2"]["value_json"])["on_current_path"] is True
    assert json.loads(recs["n2b"]["value_json"])["on_current_path"] is False


def test_chatgpt_multimodal_keeps_prose_counts_the_rest(tmp_path, cfg):
    rec = _by_id(ai_chat.scan(chatgpt_export(tmp_path), cfg))["n3"]
    assert rec["body"] == "Here is the photo of the menu"
    assert json.loads(rec["value_json"])["non_text_parts"] == 1


def test_chatgpt_non_prose_content_types_are_reported(tmp_path, cfg):
    scan = ai_chat.scan(chatgpt_export(tmp_path), cfg)
    assert {u["content_type"] for u in scan["unmapped"]} == {"code"}


def test_chatgpt_epoch_times_and_model(tmp_path, cfg):
    recs = _by_id(ai_chat.scan(chatgpt_export(tmp_path), cfg))
    assert recs["n1"]["started_at"] == "2026-02-25T18:10:00Z"
    assert recs["n1"]["journal_date"] == "2026-02-25"
    assert json.loads(recs["n2"]["value_json"])["model"] == "gpt-5"


def test_chatgpt_user_json_becomes_an_email_handle(tmp_path, cfg):
    scan = ai_chat.scan(chatgpt_export(tmp_path), cfg)
    assert scan["account"] == "me@example.com"
    assert scan["handles"][0]["kind"] == "email"
    assert _by_id(scan)["n1"]["participants"][0]["role"] == "sender"


def test_chatgpt_without_user_json_has_no_handles(tmp_path, cfg):
    """A connector with nothing to attribute is a supported state, not an error."""
    scan = ai_chat.scan(chatgpt_export(tmp_path, with_user=False), cfg)
    assert scan["account"] is None
    assert scan["handles"] == []
    assert all(r["participants"] == [] for r in scan["records"])


def test_chatgpt_cyclic_mapping_does_not_hang(tmp_path, cfg):
    """A malformed export must fail or skip, never spin."""
    bad = tmp_path / "conversations.json"
    bad.write_text(json.dumps([{
        "title": "cycle", "conversation_id": "x", "current_node": "a",
        "create_time": 1772043000,
        "mapping": {
            "a": {"id": "a", "parent": "b", "children": [], "message": {
                "id": "a", "author": {"role": "user"}, "create_time": 1772043000,
                "content": {"content_type": "text", "parts": ["hi"]}}},
            "b": {"id": "b", "parent": "a", "children": [], "message": None},
        }}]), encoding="utf-8")
    scan = ai_chat.scan(bad, cfg)
    assert [r["external_id"] for r in scan["records"]] == ["a"]


# ---------------------------------------------------------------- determinism

def test_scan_is_deterministic(tmp_path, cfg):
    a = ai_chat.scan(claude_export(tmp_path), cfg)
    b = ai_chat.scan(claude_export(tmp_path), cfg)
    assert [r["external_id"] for r in a["records"]] == \
           [r["external_id"] for r in b["records"]]
    assert a["source_hash"] == b["source_hash"]
    assert a["records"] == b["records"]


# ---------------------------------------------------------------- bad input

def test_unknown_connector_names_the_available_ones():
    with pytest.raises(DomainError) as e:
        base.get("imessage")
    assert "ai_chat" in str(e.value)


def test_not_a_chat_export_says_what_it_saw(tmp_path, cfg):
    p = tmp_path / "conversations.json"
    p.write_text(json.dumps([{"foo": 1, "bar": 2}]), encoding="utf-8")
    with pytest.raises(DomainError) as e:
        ai_chat.scan(p, cfg)
    assert "bar, foo" in str(e.value)


def test_malformed_json_reports_position(tmp_path, cfg):
    p = tmp_path / "conversations.json"
    p.write_text('[{"uuid": ', encoding="utf-8")
    with pytest.raises(DomainError) as e:
        ai_chat.scan(p, cfg)
    assert "not valid JSON" in str(e.value)


def test_zip_without_conversations_lists_members(tmp_path, cfg):
    z = tmp_path / "wrong.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("something-else.json", "[]")
    with pytest.raises(DomainError) as e:
        ai_chat.scan(z, cfg)
    assert "something-else.json" in str(e.value)


def test_utf8_bom_export_is_accepted(tmp_path, cfg):
    """PowerShell and Notepad write BOMs; commit e61de06 fixed this for drafts
    after a live failure. New readers inherit the lesson."""
    p = tmp_path / "conversations.json"
    p.write_bytes(b"\xef\xbb\xbf" +
                  (FIXTURES / "ai_chat_claude.json").read_bytes())
    assert len(ai_chat.scan(p, cfg)["records"]) == 3


# ---------------------------------------------------------------- report

def test_report_lines_name_the_handle_to_resolve(tmp_path, cfg):
    from lifecrm.domain import observations as obs
    scan = ai_chat.scan(claude_export(tmp_path), cfg)
    plan = {"new": scan["records"], "updates": [], "refreshed": [],
            "conflicts": [], "identical": 0, "dup_incoming": 0, "invalid": [],
            "streams": ["claude_chat"]}
    text = "\n".join(ai_chat.report_lines(scan, plan))
    assert "claude_chat: 3 new" in text
    assert "2026-03-10..2026-03-10" in text
    assert "app_account:claude:acct-1" in text
    assert obs is not None


# ---------------------------------------------------------------- CLI

def _cli_setup(tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output
    return root


def test_cli_dry_run_writes_nothing(tmp_path, monkeypatch, cfg):
    _cli_setup(tmp_path, monkeypatch)
    export = claude_export(tmp_path / "exp")
    r = runner.invoke(app, ["observe", "ai_chat", str(export)])
    assert r.exit_code == 0, r.output
    assert "dry run — nothing written" in r.output
    rows = runner.invoke(app, ["sql", "SELECT COUNT(*) c FROM observations", "--json"])
    assert json.loads(rows.output)[0]["c"] == 0


def test_cli_apply_then_reimport_is_a_no_op(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    export = claude_export(tmp_path / "exp")
    r = runner.invoke(app, ["observe", "ai_chat", str(export), "--apply"])
    assert r.exit_code == 0, r.output
    assert "3 added" in r.output

    again = runner.invoke(app, ["observe", "ai_chat", str(export), "--apply"])
    assert again.exit_code == 0, again.output
    assert "0 added" in again.output
    assert "3 identical skipped" in again.output
    rows = runner.invoke(app, ["sql",
                               "SELECT COUNT(*) c FROM observations WHERE status='active'",
                               "--json"])
    assert json.loads(rows.output)[0]["c"] == 3


def test_cli_dry_run_flags_a_previously_imported_file(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    export = claude_export(tmp_path / "exp")
    runner.invoke(app, ["observe", "ai_chat", str(export), "--apply"])
    r = runner.invoke(app, ["observe", "ai_chat", str(export)])
    assert "already imported" in r.output


def test_cli_unknown_connector_exits_one(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    r = runner.invoke(app, ["observe", "nope", "whatever.json"])
    assert r.exit_code == 1
    assert "not found" in r.output or "unknown connector" in r.output


def test_cli_connectors_lists_streams(tmp_path, monkeypatch):
    _cli_setup(tmp_path, monkeypatch)
    r = runner.invoke(app, ["connectors"])
    assert r.exit_code == 0, r.output
    assert "ai_chat" in r.output
    assert "never imported" in r.output
    assert "claude_chat" in r.output


# ---------------------------------------------------------------- mirroring
# The owner's decision (2026-09-24): the archive mirrors the account. A
# conversation deleted in the app is gone from the next export, and so from
# the archive.

OLD_CHAT = {
    "title": "Old chat", "create_time": 1772000000, "update_time": 1772000100,
    "conversation_id": "cg2", "current_node": "z1",
    "mapping": {
        "z0": {"id": "z0", "parent": None, "children": ["z1"], "message": None},
        "z1": {"id": "z1", "parent": "z0", "children": [], "message": {
            "id": "z1", "author": {"role": "user"}, "create_time": 1772000050,
            "content": {"content_type": "text", "parts": ["an old question"]},
            "metadata": {}}}}}


def _claude_variant(tmp_path, drop=(), blank=()):
    data = json.loads((FIXTURES / "ai_chat_claude.json").read_text(encoding="utf-8"))
    data = [c for c in data if c["uuid"] not in drop]
    for conv in data:
        for m in conv["chat_messages"]:
            if m["uuid"] in blank:
                m["text"], m["content"] = "", []
    tmp_path.mkdir(parents=True, exist_ok=True)
    out = tmp_path / "conversations.json"
    out.write_text(json.dumps(data), encoding="utf-8")
    return out


def _chatgpt_zip(tmp_path, with_old_chat, with_user=True):
    data = json.loads((FIXTURES / "ai_chat_chatgpt.json").read_text(encoding="utf-8"))
    if with_old_chat:
        data.append(OLD_CHAT)
    tmp_path.mkdir(parents=True, exist_ok=True)
    z = tmp_path / "chatgpt-export.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("conversations.json", json.dumps(data))
        if with_user:
            zf.writestr("user.json", json.dumps(
                {"id": "user-1", "email": "me@example.com", "name": "Casey"}))
    return z


def _apply(conn, scan):
    from lifecrm.domain import observations as obs
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


def _ids(conn, stream):
    return sorted(r["external_id"] for r in conn.execute(
        "SELECT external_id FROM observations WHERE stream=?", (stream,)))


def test_a_conversation_deleted_in_claude_is_removed(tmp_path, cfg, conn):
    _apply(conn, ai_chat.scan(claude_export(tmp_path / "a"), cfg))
    assert _ids(conn, "claude_chat") == ["m1", "m2", "m5"]
    plan, result = _apply(conn, ai_chat.scan(
        _claude_variant(tmp_path / "b", drop=("c2-latenight",)), cfg))
    assert [r["external_id"] for r in plan["removed"]] == ["m5"]
    assert result["removed"] == 1
    assert _ids(conn, "claude_chat") == ["m1", "m2"]


def test_a_conversation_deleted_in_chatgpt_is_removed(tmp_path, cfg, conn):
    _apply(conn, ai_chat.scan(_chatgpt_zip(tmp_path / "a", with_old_chat=True), cfg))
    assert "z1" in _ids(conn, "chatgpt_chat")
    plan, _ = _apply(conn, ai_chat.scan(_chatgpt_zip(tmp_path / "b", with_old_chat=False), cfg))
    assert [r["external_id"] for r in plan["removed"]] == ["z1"]
    assert "z1" not in _ids(conn, "chatgpt_chat")
    assert _ids(conn, "chatgpt_chat"), "the conversation still in the export stays"


def test_a_turn_the_connector_does_not_import_still_counts_as_present(tmp_path, cfg, conn):
    """m1 loses its text in the second export, so it is not emitted — but it
    is still in the export, and must not be mistaken for a deletion."""
    _apply(conn, ai_chat.scan(claude_export(tmp_path / "a"), cfg))
    scan = ai_chat.scan(_claude_variant(tmp_path / "b", blank=("m1",)), cfg)
    assert "m1" not in _by_id(scan) and "m1" in scan["present_ids"]
    from lifecrm.domain import observations as obs
    assert obs.build_plan(conn, scan)["removed"] == []


def test_a_claude_export_never_removes_chatgpt_messages(tmp_path, cfg, conn):
    """Even under the same account name, each vendor mirrors only its own
    stream."""
    gpt = ai_chat.scan(_chatgpt_zip(tmp_path / "g", with_old_chat=True), cfg)
    gpt["account"] = "acct-1"                  # collide with the Claude account
    _apply(conn, gpt)
    claude = ai_chat.scan(claude_export(tmp_path / "c"), cfg)
    assert claude["account"] == "acct-1" and claude["mirror_streams"] == ["claude_chat"]
    from lifecrm.domain import observations as obs
    assert obs.build_plan(conn, claude)["removed"] == []


def test_an_export_that_does_not_name_its_account_removes_nothing(tmp_path, cfg, conn):
    _apply(conn, ai_chat.scan(_chatgpt_zip(tmp_path / "a", True, with_user=False), cfg))
    scan = ai_chat.scan(_chatgpt_zip(tmp_path / "b", False, with_user=False), cfg)
    assert scan["account"] is None
    from lifecrm.domain import observations as obs
    plan = obs.build_plan(conn, scan)
    assert plan["removed"] == []
    assert "does not say which account" in plan["mirror_note"]
    text = "\n".join(ai_chat.report_lines(scan, plan))
    assert "not mirroring deletions" in text


def test_cli_removes_a_deleted_conversation_with_a_snapshot_first(tmp_path, monkeypatch):
    root = cli_project(tmp_path, monkeypatch)
    assert runner.invoke(app, ["init"]).exit_code == 0
    r = runner.invoke(app, ["observe", "ai_chat", str(claude_export(tmp_path / "a")),
                            "--apply"])
    assert r.exit_code == 0, r.output
    later = _claude_variant(tmp_path / "b", drop=("c2-latenight",))

    r = runner.invoke(app, ["observe", "ai_chat", str(later)])
    assert "remove: 1 no longer in the export (deleted)" in r.output
    assert "(untitled) (1)" in r.output

    r = runner.invoke(app, ["observe", "ai_chat", str(later), "--apply"])
    assert r.exit_code == 0, r.output
    assert "1 removed" in r.output and "snapshot before removing:" in r.output
    assert list((root / "backups").glob("pre-removal-*.db"))
