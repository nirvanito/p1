from lifecrm.retention import prune_dir, select_retained


def _name(day: str, t: str = "120000") -> str:
    return f"lifecrm-{day}-{t}.db"


def test_recent_dailies_all_kept():
    names = [_name(f"202607{d:02d}") for d in range(14, 28)]   # 14 days
    assert select_retained(names) == set(names)


def test_same_day_keeps_only_newest():
    names = [_name("20260727", "090000"), _name("20260727", "213000")]
    assert select_retained(names) == {_name("20260727", "213000")}


def test_tiers_thin_out_history():
    # ~2.5 years of daily snapshots
    from datetime import date, timedelta
    names = []
    d = date(2024, 1, 1)
    while d <= date(2026, 7, 27):
        names.append(_name(d.strftime("%Y%m%d")))
        d += timedelta(days=1)
    kept = select_retained(names)

    # far fewer than everything, far more than a week
    assert 30 <= len(kept) <= 60
    # the newest 14 days all survive (daily tier)
    for day in ("20260727", "20260726", "20260714"):
        assert _name(day) in kept
    # something from every year survives (yearly tier, indefinitely)
    assert any(n.startswith("lifecrm-2024") for n in kept)
    assert any(n.startswith("lifecrm-2025") for n in kept)
    # a ~2-year-old mid-month weekday survives no tier: outside the daily and
    # weekly windows, not its month's newest (monthly), not its year's newest
    assert _name("20240517") not in kept


def test_unrecognized_names_never_deleted():
    names = [_name("20260727"), "pre-migration-20260728-105652.db", "notes.txt"]
    kept = select_retained(names)
    assert "pre-migration-20260728-105652.db" in kept
    assert "notes.txt" in kept


def test_prune_dir_deletes_only_expired(tmp_path):
    from datetime import date, timedelta
    d = date(2025, 1, 1)
    while d <= date(2026, 7, 27):
        (tmp_path / _name(d.strftime("%Y%m%d"))).write_bytes(b"x")
        d += timedelta(days=1)
    (tmp_path / "pre-restore-20260101-000000.db").write_bytes(b"x")

    removed = prune_dir(tmp_path)
    survivors = {p.name for p in tmp_path.glob("*.db")}
    assert removed and len(survivors) < 60
    assert _name("20260727") in survivors
    assert "pre-restore-20260101-000000.db" in survivors   # pattern excludes it
    # deterministic: pruning again removes nothing
    assert prune_dir(tmp_path) == []
