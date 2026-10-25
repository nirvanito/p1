"""Android calls are remembered: the phone's call log ages old calls out.

A type="full" calls file was mirrored like the texts. But Android's call log
keeps only its newest calls (500 on stock Android) and drops the oldest by
itself, so every later full backup lacked a few old calls nobody deleted —
and mirroring removed them, a few per import, never enough to trip the
mass-removal guard, until the archive held no more than the phone. Texts
still mirror; calls are kept.
"""

from test_android_sms import DANA, JAKE, ME, MOM, call, document, sms

from lifecrm.connectors import android_sms as an
from lifecrm.domain import observations as obs


def _calls(n_from: int, n_to: int) -> list[str]:
    numbers = (JAKE, MOM, DANA)
    return [call(numbers[i % 3], f"2026-09-{i:02d}T18:00:00Z", 1 + i % 2, 60 + i, "x")
            for i in range(n_from, n_to + 1)]


def _backup(folder, stamp: str, calls: list[str], texts: list[str] | None = None):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"calls-{stamp}.xml").write_text(document("calls", calls), encoding="utf-8")
    if texts is not None:
        (folder / f"sms-{stamp}.xml").write_text(document("sms", texts), encoding="utf-8")
    return folder


def _apply(conn, scan):
    plan = obs.build_plan(conn, scan)
    result = obs.apply_plan(conn, scan, plan)
    conn.commit()
    return plan, result


def _active_calls(conn) -> int:
    return conn.execute("SELECT COUNT(*) c FROM observations WHERE stream='android_call' "
                        "AND status='active'").fetchone()["c"]


def test_a_call_the_phone_aged_out_of_its_log_stays_archived(cfg, conn, tmp_path):
    cfg.connectors = {"android_sms": {"account": ME}}
    _apply(conn, an.scan(_backup(tmp_path / "a", "20261022020003", _calls(1, 20)), cfg))
    assert _active_calls(conn) == 20

    # a week later the oldest call has dropped out of the log, a new one is in
    later = an.scan(_backup(tmp_path / "b", "20261029020003", _calls(2, 21)), cfg)
    plan, result = _apply(conn, later)
    assert plan["removed"] == [] and "removed" not in result
    assert _active_calls(conn) == 21

    text = "\n".join(an.report_lines(later, obs.build_plan(conn, later)))
    assert "calls: full backup taken" in text
    assert "kept even once the phone's call log drops them" in text
    assert "deletions on the phone are mirrored" not in text
    assert "remove:" not in text


def test_texts_still_mirror_beside_remembered_calls(cfg, conn, tmp_path):
    cfg.connectors = {"android_sms": {"account": ME}}
    # the deleted text sits inside the span the phone still holds: one older
    # than the phone's oldest would read as aged out, and stay (texts.md)
    first = [sms(JAKE, "2026-09-01T17:55:00Z", 1, "running late"),
             sms(JAKE, "2026-09-01T18:00:00Z", 1, "see you at 6"),
             sms(JAKE, "2026-09-01T18:05:00Z", 2, "on my way")]
    _apply(conn, an.scan(_backup(tmp_path / "a", "20261022020003", _calls(1, 3), first),
                         cfg))
    later = an.scan(_backup(tmp_path / "b", "20261029020003", _calls(2, 3),
                            [first[0], first[2]]), cfg)
    assert later["mirror_streams"] == ["android_mms", "android_sms"]
    plan, _ = _apply(conn, later)
    assert [r["body"] for r in plan["removed"]] == ["see you at 6"]
    assert _active_calls(conn) == 3
