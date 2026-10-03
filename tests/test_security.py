import jwt

from app import config
from app.seed import DEMO_PASSWORD
from tests.conftest import login


def test_requests_without_a_token_are_rejected(client):
    assert client.get("/api/clients").status_code == 401


def test_tampered_and_forged_tokens_are_rejected(client, tokens):
    good = tokens["priya"]["access_token"]
    assert client.get("/api/clients", headers={"Authorization": f"Bearer {good[:-3]}abc"}).status_code == 401
    payload = jwt.decode(good, options={"verify_signature": False})
    payload["role"] = "admin"
    forged = jwt.encode(payload, "not-the-real-signing-key-but-32-bytes-long", algorithm="HS256")
    assert client.get("/api/clients", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    unsigned = jwt.encode(payload, None, algorithm="none")
    assert client.get("/api/clients", headers={"Authorization": f"Bearer {unsigned}"}).status_code == 401


def test_wrong_password_and_unknown_user_get_the_same_answer(client):
    a = client.post("/api/auth/login", json={"email": "priya@datecrew.test", "password": "wrong"})
    b = client.post("/api/auth/login", json={"email": "nobody@datecrew.test", "password": "wrong"})
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()


def test_login_is_rate_limited(client, monkeypatch):
    monkeypatch.setattr(config, "LOGIN_RATE_LIMIT_PER_MINUTE", 5)
    codes = [client.post("/api/auth/login", json={"email": "x@datecrew.test", "password": "x"}).status_code for _ in range(7)]
    assert codes[:5] == [401] * 5 and codes[5:] == [429, 429]


def test_refresh_tokens_rotate_and_reuse_revokes_the_family(client):
    first = login(client, "priya@datecrew.test")
    second = client.post("/api/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert second.status_code == 200
    # Replaying the old token is treated as theft: it fails and kills the new one too.
    assert client.post("/api/auth/refresh", json={"refresh_token": first["refresh_token"]}).status_code == 401
    assert client.post("/api/auth/refresh", json={"refresh_token": second.json()["refresh_token"]}).status_code == 401


def test_role_checks(client, auth):
    assert client.get("/api/audit", headers=auth["priya"]).status_code == 403
    assert client.get("/api/audit", headers=auth["meera"]).status_code == 200


def test_unknown_fields_and_bad_ids_are_rejected(client, auth):
    res = client.post("/api/match/check", headers=auth["priya"], json={"client_id": "a", "candidate_id": "b", "role": "admin"})
    assert res.status_code == 422
    res = client.post("/api/match/check", headers=auth["priya"], json={"client_id": "a' OR 1=1 --", "candidate_id": "b"})
    assert res.status_code == 422


def test_security_headers_present(client):
    res = client.get("/api/health")
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in res.headers["Content-Security-Policy"]
    assert res.headers["Cache-Control"] == "no-store"


def test_sensitive_text_is_encrypted_at_rest(client):
    from app.db import connect

    conn = connect()
    raw = conn.execute("SELECT raw_text_enc FROM rejections LIMIT 1").fetchone()[0]
    note = conn.execute("SELECT notes_enc FROM clients LIMIT 1").fetchone()[0]
    conn.close()
    assert raw.startswith("gAAAA") and note.startswith("gAAAA")  # Fernet tokens, not plain text


def test_candidate_contact_details_are_never_in_match_responses(client, auth):
    cid = client.get("/api/clients", headers=auth["priya"]).json()["data"][0]["id"]
    body = client.get(f"/api/clients/{cid}/shortlist", headers=auth["priya"]).text
    assert "@example.com" not in body and "+91" not in body


def test_email_login_is_case_insensitive(client):
    res = client.post("/api/auth/login", json={"email": "PRIYA@datecrew.test", "password": DEMO_PASSWORD})
    assert res.status_code == 200  # emails are case-insensitive
