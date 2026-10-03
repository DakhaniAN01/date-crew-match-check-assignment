"""Data isolation: tenants never see each other, matchmakers only see their own clients."""


def clients_of(client, headers):
    return client.get("/api/clients", headers=headers).json()["data"]


def test_matchmakers_only_see_their_own_clients(client, auth):
    priya = clients_of(client, auth["priya"])
    rahul = clients_of(client, auth["rahul"])
    assert len(priya) == 6 and len(rahul) == 6
    assert not {c["id"] for c in priya} & {c["id"] for c in rahul}


def test_lead_sees_whole_agency_but_not_other_tenants(client, auth):
    assert len(clients_of(client, auth["meera"])) == 12
    assert len(clients_of(client, auth["sam"])) == 3


def test_cross_matchmaker_access_returns_404(client, auth):
    rahul_client = clients_of(client, auth["rahul"])[0]["id"]
    for path in (f"/api/clients/{rahul_client}", f"/api/clients/{rahul_client}/shortlist", f"/api/clients/{rahul_client}/shares"):
        assert client.get(path, headers=auth["priya"]).status_code == 404


def test_cross_tenant_access_returns_404(client, auth):
    tdc_client = clients_of(client, auth["meera"])[0]["id"]
    sam_client = clients_of(client, auth["sam"])[0]["id"]
    assert client.get(f"/api/clients/{tdc_client}", headers=auth["sam"]).status_code == 404
    # A Northstar matchmaker cannot check a Date Crew candidate, even against their own client.
    res = client.post("/api/match/check", headers=auth["sam"], json={"client_id": sam_client, "candidate_id": "TDC-00001"})
    assert res.status_code == 404
    res = client.post("/api/shares", headers=auth["sam"], json={"client_id": sam_client, "candidate_id": "TDC-00001"})
    assert res.status_code == 404


def test_cache_never_serves_out_of_scope_data(client, auth):
    rahul_client = clients_of(client, auth["rahul"])[0]["id"]
    assert client.get(f"/api/clients/{rahul_client}/shortlist", headers=auth["meera"]).status_code == 200  # warms the cache
    assert client.get(f"/api/clients/{rahul_client}/shortlist", headers=auth["priya"]).status_code == 404


def test_dashboard_and_backtest_are_scoped(client, auth):
    priya = client.get("/api/backtest", headers=auth["priya"]).json()
    assert [m["matchmaker"] for m in priya["by_matchmaker"]] == ["Priya (Matchmaker A)"]
    assert client.get("/api/backtest", headers=auth["sam"]).json()["shared"] == 0
    names = [m["matchmaker"] for m in client.get("/api/dashboard", headers=auth["priya"]).json()["by_matchmaker"]]
    assert names == ["Priya (Matchmaker A)"]


def test_other_matchmakers_rejections_are_hidden(client, auth):
    rahul_client = clients_of(client, auth["rahul"])[0]["id"]
    shares = client.get(f"/api/clients/{rahul_client}/shares", headers=auth["rahul"]).json()["data"]
    rejection = next(s["rejection_id"] for s in shares if s["rejection_id"])
    assert client.get(f"/api/rejections/{rejection}", headers=auth["rahul"]).status_code == 200
    assert client.get(f"/api/rejections/{rejection}", headers=auth["priya"]).status_code == 404
    assert client.get(f"/api/rejections/{rejection}", headers=auth["sam"]).status_code == 404
