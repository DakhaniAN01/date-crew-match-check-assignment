"""End-to-end: backtest numbers, the share gate, and the rejection feedback loop."""

from app.classifier import classify_keyword, scrub_pii


def test_backtest_reproduces_the_brief(client, auth):
    bt = client.get("/api/backtest", headers=auth["meera"]).json()
    assert (bt["shared"], bt["accepted"], bt["blocked"]) == (1000, 310, 241)
    assert bt["blocked_accepted"] == 0  # no profile the client liked would have been blocked
    assert bt["acceptance_before"] == 31.0 and bt["acceptance_after"] == 40.8
    by = {m["matchmaker"]: m for m in bt["by_matchmaker"]}
    assert by["Priya (Matchmaker A)"]["acceptance_before"] == 43.9
    assert by["Rahul (Matchmaker B)"]["acceptance_before"] == 21.1


def test_seeded_feedback_classifies_avoidable_rejections(client, auth):
    reasons = client.get("/api/dashboard", headers=auth["meera"]).json()["reasons"]
    avoidable = sum(r["n"] for r in reasons if r["reason_code"] == "preference_violation")
    total = sum(r["n"] for r in reasons)
    assert total == 690 and avoidable == 241  # about 35%, as in the brief


def _client_without_smoking_rule(client, headers):
    for c in client.get("/api/clients", headers=headers).json()["data"]:
        detail = client.get(f"/api/clients/{c['id']}", headers=headers).json()
        if "smoking" not in detail["preference_card"]["hard"]:
            return detail
    raise AssertionError("seed should include a client without a smoking rule")


def test_blocked_share_needs_an_override_reason(client, auth):
    h = auth["meera"]
    c = client.get("/api/clients", headers=h).json()["data"][0]
    card = client.get(f"/api/clients/{c['id']}", headers=h).json()["preference_card"]["hard"]
    wrong_gender = "female" if card["seeking_gender"] == "male" else "male"
    # Find a candidate of the wrong gender: always a hard block.
    for i in range(1, 200):
        cid = f"TDC-{i:05d}"
        res = client.post("/api/match/check", headers=h, json={"client_id": c["id"], "candidate_id": cid}).json()
        if res["candidate"]["gender"] == wrong_gender:
            break
    assert res["status"] == "BLOCK"
    assert client.post("/api/shares", headers=h, json={"client_id": c["id"], "candidate_id": cid}).status_code == 409
    ok = client.post("/api/shares", headers=h, json={"client_id": c["id"], "candidate_id": cid, "override_reason": "Client asked to see this profile"})
    assert ok.status_code == 201
    actions = [a["action"] for a in client.get("/api/audit", headers=h).json()["data"]]
    assert "override_block" in actions


def test_feedback_loop_suggests_and_adds_a_new_deal_breaker(client, auth):
    h = auth["meera"]
    c = _client_without_smoking_rule(client, h)
    top = client.get(f"/api/clients/{c['id']}/shortlist?limit=1", headers=h).json()["data"][0]["candidate"]
    share = client.post("/api/shares", headers=h, json={"client_id": c["id"], "candidate_id": top["id"]}).json()

    # Shared profiles drop out of the shortlist (cache invalidated).
    ids = [r["candidate"]["id"] for r in client.get(f"/api/clients/{c['id']}/shortlist?limit=100", headers=h).json()["data"]]
    assert top["id"] not in ids

    res = client.post(f"/api/shares/{share['share_id']}/outcome", headers=h,
                      json={"outcome": "rejected", "feedback": "Honestly I can't date someone who smokes."})
    assert res.status_code == 202
    rej = client.get(f"/api/rejections/{res.json()['rejection_id']}", headers=h).json()
    assert rej["status"] == "done"  # background task runs before TestClient returns
    assert rej["reason_code"] == "new_preference" and rej["reason_detail"] == "smoking"
    assert rej["suggestion"]["proposed"] == ["never"]

    hard = {**c["preference_card"]["hard"], "smoking": ["never"]}
    upd = client.put(f"/api/clients/{c['id']}/preference-card", headers=h, json={"hard": hard, "soft": c["preference_card"]["soft"]})
    assert upd.status_code == 200 and upd.json()["version"] == c["preference_card"]["version"] + 1

    # Outcomes cannot be changed once recorded.
    again = client.post(f"/api/shares/{share['share_id']}/outcome", headers=h, json={"outcome": "accepted"})
    assert again.status_code == 409


def test_rejection_requires_feedback(client, auth):
    res = client.post("/api/shares/abc/outcome", headers=auth["meera"], json={"outcome": "rejected"})
    assert res.status_code == 422


def test_contact_reveal_only_after_acceptance(client, auth):
    h = auth["priya"]
    c = client.get("/api/clients", headers=h).json()["data"][1]
    top = client.get(f"/api/clients/{c['id']}/shortlist?limit=1", headers=h).json()["data"][0]["candidate"]
    share = client.post("/api/shares", headers=h, json={"client_id": c["id"], "candidate_id": top["id"]}).json()
    assert client.post(f"/api/shares/{share['share_id']}/reveal-contact", headers=h).status_code == 409
    client.post(f"/api/shares/{share['share_id']}/outcome", headers=h, json={"outcome": "accepted"})
    contact = client.post(f"/api/shares/{share['share_id']}/reveal-contact", headers=h).json()
    assert contact["email"].endswith("@example.com")


def test_keyword_classifier_taxonomy():
    hard = {"smoking": ["never"], "religions": ["hindu"]}
    assert classify_keyword("They smoke, I said no smokers", hard)["reason_code"] == "preference_violation"
    assert classify_keyword("Different religion, family will not agree", hard)["reason_detail"] == "religions"
    assert classify_keyword("Not attracted from the photos", hard)["reason_code"] == "appearance"
    assert classify_keyword("Travelling, not ready right now", hard)["reason_code"] == "timing"
    assert classify_keyword("Meh", hard)["reason_code"] == "other"
    new = classify_keyword("We differ on kids", hard)
    assert new["reason_code"] == "new_preference" and new["suggestion"]["proposed"] == ["yes", "open"]


def test_pii_is_scrubbed_before_any_llm_call():
    out = scrub_pii("Call Rohan Mehta on +91 98765 43210 or rohan@x.com", ["Rohan Mehta"])
    assert "Rohan" not in out and "Mehta" not in out and "98765" not in out and "@" not in out
