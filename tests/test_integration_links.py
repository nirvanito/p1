"""Cross-feature seams found while integrating: a relationship that cites an
observed record keeps that record from being deleted by source mirroring."""

from lifecrm.connectors import base
from lifecrm.domain import entities, entity_links
from lifecrm.domain import observations as obs
from lifecrm.doctor import run_checks


def _scan(ids):
    s = base.empty_scan("synthetic", account="me@example.com",
                        source_file="cards.vcf", source_hash="h-" + "-".join(ids))
    for i in ids:
        s["records"].append({
            "stream": "contact_card", "external_id": i, "journal_date": "2026-07-27",
            "started_at": "2026-07-27T15:00:00Z", "title": f"Card {i}",
            "body": "Works at Acme", "participants": []})
    s["present_ids"] = set(ids)
    s["mirror_streams"] = ["contact_card"]
    return s


def test_mirroring_retracts_an_observation_a_relationship_cites(conn, cfg):
    first = _scan(["a", "b"])
    obs.apply_plan(conn, first, obs.build_plan(conn, first))
    conn.commit()
    card = conn.execute("SELECT uuid FROM observations WHERE external_id='a'").fetchone()
    jake = entities.create(conn, "person", "Jake Morales")
    acme = entities.create(conn, "organization", "Acme")
    entity_links.add(conn, jake["id"], acme["id"], "works_at",
                     observation_uuid=card["uuid"])
    conn.commit()

    second = _scan(["b"])                         # card 'a' was deleted at the source
    result = obs.apply_plan(conn, second, obs.build_plan(conn, second))
    conn.commit()

    assert result.get("retracted_cited") == 1
    row = conn.execute("SELECT status FROM observations WHERE external_id='a'").fetchone()
    assert row["status"] == "retracted"           # kept: a confirmed fact cites it
    assert run_checks(conn, cfg)["failures"] == 0
