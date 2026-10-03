import json

from app import matching
from app.db import connect

CARD = {
    "hard": {
        "seeking_gender": "male", "age_min": 29, "age_max": 35, "religions": ["hindu"],
        "smoking": ["never"], "wants_children": ["yes", "open"], "cities": ["Mumbai"],
    },
    "soft": [
        {"key": "min_height_cm", "value": 172, "weight": 2},
        {"key": "min_education", "value": "masters", "weight": 1},
    ],
}

BASE = {
    "id": "X-1", "gender": "male", "age": 31, "city": "Mumbai", "religion": "hindu", "diet": "vegetarian",
    "smoking": "never", "drinking": "socially", "wants_children": "yes", "open_to_relocate": 0,
    "height_cm": 178, "education": "masters", "profession": "engineer",
}


def test_pass_when_everything_matches():
    r = matching.check(CARD, BASE)
    assert r.status == "PASS" and r.fit_score == 100 and not r.violations


def test_block_on_any_deal_breaker_with_reason():
    r = matching.check(CARD, {**BASE, "smoking": "regularly"})
    assert r.status == "BLOCK"
    assert r.violations[0]["label"] == "Smoking"
    assert "regularly" in r.violations[0]["reason"]


def test_location_rule_accepts_candidates_willing_to_relocate():
    assert matching.check(CARD, {**BASE, "city": "Pune"}).status == "BLOCK"
    assert matching.check(CARD, {**BASE, "city": "Pune", "open_to_relocate": 1}).status == "PASS"


def test_warn_when_soft_preferences_mostly_missed():
    r = matching.check(CARD, {**BASE, "height_cm": 165, "education": "bachelors"})
    assert r.status == "WARN" and r.fit_score == 0 and len(r.soft_mismatches) == 2


def test_weighted_fit_score():
    r = matching.check(CARD, {**BASE, "education": "bachelors"})  # height (w2) met, education (w1) missed
    assert r.fit_score == 67 and r.status == "WARN"


def test_sql_prefilter_agrees_with_python_rules(client):
    """The fast SQL path must select exactly the candidates the Python rules allow."""
    conn = connect()
    cards = conn.execute("SELECT client_id, tenant_id, hard_json FROM preference_cards").fetchall()
    for card in cards:
        hard = json.loads(card["hard_json"])
        where, params = matching.sql_prefilter(hard)
        sql_ids = {r[0] for r in conn.execute(
            f"SELECT id FROM candidates WHERE tenant_id = ? AND {where}", [card["tenant_id"], *params])}
        all_rows = conn.execute("SELECT * FROM candidates WHERE tenant_id = ?", (card["tenant_id"],)).fetchall()
        py_ids = {r["id"] for r in all_rows if not matching.hard_violations(hard, dict(r))}
        assert sql_ids == py_ids, f"prefilter drift for client {card['client_id']}"
    conn.close()
