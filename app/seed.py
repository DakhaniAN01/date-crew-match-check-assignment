"""Deterministic mock data.

The 30-day history reproduces the scenario from the brief exactly:
  1,000 profiles shared, 310 accepted, 690 rejected,
  241 of the rejections broke a preference the client had already stated,
  Matchmaker A accepts 44%, Matchmaker B accepts 21%.
"""

import json
import random
import uuid
from datetime import datetime, timedelta, timezone

from app import classifier, matching
from app.security import encrypt, hash_password

# Local demo credentials only. Every seeded user shares this password.
DEMO_PASSWORD = "MatchCheck-Demo-2026"

CITIES = ["Mumbai", "Delhi", "Bengaluru", "Pune", "Hyderabad", "Chennai", "Kolkata", "Ahmedabad", "London", "Dubai", "Singapore"]
CITY_W = [16, 16, 16, 10, 9, 8, 6, 5, 5, 5, 4]
RELIGIONS = ["hindu", "muslim", "sikh", "christian", "jain", "parsi", "none"]
RELIGION_W = [58, 14, 7, 7, 6, 2, 6]
DIETS = ["vegetarian", "non_vegetarian", "vegan", "eggetarian", "jain"]
DIET_W = [30, 52, 4, 9, 5]
SMOKING = ["never", "occasionally", "regularly"]
SMOKING_W = [70, 18, 12]
DRINKING = ["never", "socially", "regularly"]
DRINKING_W = [35, 52, 13]
CHILDREN = ["yes", "no", "open"]
CHILDREN_W = [60, 15, 25]
EDU_W = [8, 47, 38, 7]
PROFESSIONS = ["engineer", "doctor", "lawyer", "finance", "entrepreneur", "designer", "consultant", "academic", "civil_services", "marketing"]

FIRST_M = ["Aarav", "Vihaan", "Arjun", "Kabir", "Rohan", "Ishaan", "Aditya", "Karan", "Zayan", "Dev", "Nikhil", "Farhan", "Harjeet", "Neil", "Siddharth"]
FIRST_F = ["Ananya", "Diya", "Ishita", "Kavya", "Myra", "Saanvi", "Aisha", "Riya", "Tara", "Zara", "Nisha", "Meher", "Simran", "Leah", "Pooja"]
LAST = ["Sharma", "Mehta", "Iyer", "Khan", "Kapoor", "Reddy", "Singh", "D'Souza", "Shah", "Nair", "Bose", "Patel", "Gill", "Rao", "Menon"]

VIOLATION_FEEDBACK = {
    "smoking": ["They smoke. I clearly said no smokers.", "Smoking is a deal breaker for me, this profile says they smoke.", "Not comfortable with someone who smokes."],
    "wants_children": ["We are not aligned on having children.", "I want children someday, this person does not.", "Kids are important to me and we differ on that."],
    "religions": ["Different religion, my family will not agree.", "I mentioned I want someone from the same faith.", "Religion matters to me, this is not a fit."],
    "diets": ["I am vegetarian and they eat meat.", "Diet is a problem, I need a vegetarian partner.", "Non-veg is a no for me."],
    "age_range": ["The age gap is too large.", "Not the age range I asked for.", "Age is not right for me."],
    "cities": ["They live in another city and will not relocate.", "Long distance will not work for me.", "I said I want someone in my city."],
}

OTHER_FEEDBACK = [
    "Not attracted from the photos.", "The pictures did not do it for me.", "Looks are not my type.",
    "Too short for me.", "I prefer someone taller.",
    "Career is not a match.", "I was hoping for someone with a postgraduate degree.", "Not sure about their job stability.",
    "The bio felt boring.", "Did not feel a vibe.", "Our interests seem very different.",
    "I am travelling this month, not ready right now.", "Too busy at work, can we pause for two weeks?",
    "My parents did not like the family background.",
    "Just not feeling it.", "No particular reason, pass.", "Not for me.",
]

# matchmaker email -> (shares, accepted, avoidable rejections)
HISTORY_PLAN = {
    "priya@datecrew.test": (435, 191, 60),
    "rahul@datecrew.test": (565, 119, 181),
}


def _uid() -> str:
    return uuid.uuid4().hex


def _pick(rng, values, weights):
    return rng.choices(values, weights=weights, k=1)[0]


def make_candidate(rng: random.Random, tenant_id: str, cid: str) -> dict:
    gender = rng.choice(["male", "female"])
    first = rng.choice(FIRST_M if gender == "male" else FIRST_F)
    last = rng.choice(LAST)
    height = int(rng.gauss(175 if gender == "male" else 162, 7))
    return {
        "id": cid,
        "tenant_id": tenant_id,
        "name": f"{first} {last[0]}.",
        "gender": gender,
        "age": rng.randint(24, 42),
        "city": _pick(rng, CITIES, CITY_W),
        "religion": _pick(rng, RELIGIONS, RELIGION_W),
        "diet": _pick(rng, DIETS, DIET_W),
        "smoking": _pick(rng, SMOKING, SMOKING_W),
        "drinking": _pick(rng, DRINKING, DRINKING_W),
        "wants_children": _pick(rng, CHILDREN, CHILDREN_W),
        "open_to_relocate": 1 if rng.random() < 0.35 else 0,
        "height_cm": max(145, min(200, height)),
        "education": _pick(rng, matching.EDUCATION_LEVELS, EDU_W),
        "profession": rng.choice(PROFESSIONS),
        "phone": f"+91 9{rng.randint(100000000, 999999999)}",
        "email": f"{first.lower()}.{rng.randint(100, 999)}@example.com",
    }


def make_client_card(rng: random.Random, gender: str, age: int, city: str, religion: str, force_smoking: bool) -> tuple[dict, list]:
    seeking = "female" if gender == "male" else "male"
    if seeking == "male":
        age_min, age_max = age - 1, age + 6
    else:
        age_min, age_max = age - 6, age + 1
    hard: dict = {"seeking_gender": seeking, "age_min": max(22, age_min), "age_max": min(45, age_max)}
    if rng.random() < 0.7:
        hard["religions"] = [religion]
    if rng.random() < 0.4:
        hard["diets"] = ["vegetarian", "vegan", "jain", "eggetarian"]
    r = rng.random()
    if force_smoking or r < 0.6:
        hard["smoking"] = ["never"]
    elif r < 0.8:
        hard["smoking"] = ["never", "occasionally"]
    r = rng.random()
    if r < 0.6:
        hard["wants_children"] = ["yes", "open"]
    elif r < 0.75:
        hard["wants_children"] = ["no", "open"]
    if rng.random() < 0.7:
        hard["cities"] = [city]

    soft_options = [
        {"key": "min_height_cm", "value": 172 if seeking == "male" else 155, "weight": rng.randint(1, 3)},
        {"key": "min_education", "value": rng.choice(["bachelors", "masters"]), "weight": rng.randint(1, 3)},
        {"key": "professions", "value": rng.sample(PROFESSIONS, 4), "weight": rng.randint(1, 3)},
        {"key": "drinking", "value": rng.choice([["never"], ["never", "socially"]]), "weight": rng.randint(1, 2)},
    ]
    soft = rng.sample(soft_options, rng.randint(2, 4))
    return hard, soft


def seed(conn, n_candidates: int, rng_seed: int = 42) -> None:
    if conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]:
        return
    rng = random.Random(rng_seed)
    now = datetime.now(timezone.utc)
    pw = hash_password(DEMO_PASSWORD)

    tenants = [("t_datecrew", "The Date Crew", "TDC", n_candidates), ("t_northstar", "Northstar Matchmaking (demo tenant)", "NSM", max(500, n_candidates // 10))]
    conn.executemany("INSERT INTO tenants (id, name) VALUES (?, ?)", [(t[0], t[1]) for t in tenants])

    users = [
        ("priya@datecrew.test", "Priya (Matchmaker A)", "matchmaker", "t_datecrew"),
        ("rahul@datecrew.test", "Rahul (Matchmaker B)", "matchmaker", "t_datecrew"),
        ("meera@datecrew.test", "Meera (Lead)", "lead", "t_datecrew"),
        ("admin@datecrew.test", "Admin", "admin", "t_datecrew"),
        ("sam@northstar.test", "Sam (Northstar)", "matchmaker", "t_northstar"),
    ]
    user_ids = {}
    for email, name, role, tid in users:
        user_ids[email] = _uid()
        conn.execute(
            "INSERT INTO users (id, tenant_id, email, name, role, password_hash) VALUES (?, ?, ?, ?, ?, ?)",
            (user_ids[email], tid, email, name, role, pw),
        )

    # Candidates
    pools: dict[str, list[dict]] = {}
    for tid, _, prefix, n in tenants:
        pool = [make_candidate(rng, tid, f"{prefix}-{i:05d}") for i in range(1, n + 1)]
        pools[tid] = pool
        conn.executemany(
            """INSERT INTO candidates (id, tenant_id, name, gender, age, city, religion, diet, smoking, drinking,
                   wants_children, open_to_relocate, height_cm, education, profession, phone, email)
               VALUES (:id, :tenant_id, :name, :gender, :age, :city, :religion, :diet, :smoking, :drinking,
                   :wants_children, :open_to_relocate, :height_cm, :education, :profession, :phone, :email)""",
            pool,
        )

    # Clients and preference cards
    client_plan = [("t_datecrew", "priya@datecrew.test", 6), ("t_datecrew", "rahul@datecrew.test", 6), ("t_northstar", "sam@northstar.test", 3)]
    clients_by_mm: dict[str, list[dict]] = {}
    notes = ["Family oriented, close to parents.", "Works long hours, values ambition.", "Loves travel and hiking.",
             "Wants a partner who enjoys cooking.", "Recently moved back from abroad.", "Prefers a calm, homely person."]
    idx = 0
    used_names: set[str] = set()
    for tid, mm_email, count in client_plan:
        for _ in range(count):
            idx += 1
            gender = "female" if idx % 2 else "male"
            first = rng.choice(FIRST_F if gender == "female" else FIRST_M)
            age = rng.randint(27, 36)
            city = _pick(rng, CITIES[:8], CITY_W[:8])
            religion = _pick(rng, RELIGIONS[:5], RELIGION_W[:5])
            # A couple of clients have no smoking rule, so the feedback loop can propose one.
            hard, soft = make_client_card(rng, gender, age, city, religion, force_smoking=idx % 5 != 0)
            if idx % 5 == 0:
                hard.pop("smoking", None)
            # Relax over-narrow mock cards so every client has a workable shortlist.
            for loosen in ("cities", "diets", "religions"):
                passing = sum(1 for k in pools[tid] if not matching.hard_violations(hard, k))
                if passing >= len(pools[tid]) // 100:
                    break
                hard.pop(loosen, None)
            name = f"{first} {rng.choice(LAST)}"
            while name in used_names:
                name = f"{rng.choice(FIRST_F if gender == 'female' else FIRST_M)} {rng.choice(LAST)}"
            used_names.add(name)
            client = {
                "id": _uid(), "tenant_id": tid, "matchmaker_id": user_ids[mm_email],
                "name": name, "gender": gender, "age": age, "city": city,
                "hard": hard, "soft": soft,
            }
            clients_by_mm.setdefault(mm_email, []).append(client)
            conn.execute(
                "INSERT INTO clients (id, tenant_id, matchmaker_id, name, gender, age, city, notes_enc) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (client["id"], tid, client["matchmaker_id"], client["name"], gender, age, city, encrypt(rng.choice(notes))),
            )
            conn.execute(
                "INSERT INTO preference_cards (client_id, tenant_id, hard_json, soft_json, version, updated_at) VALUES (?, ?, ?, ?, 1, ?)",
                (client["id"], tid, json.dumps(hard), json.dumps(soft), now.isoformat()),
            )

    # 30 days of history for The Date Crew
    pool = pools["t_datecrew"]
    for mm_email, (n_shares, n_accepted, n_avoidable) in HISTORY_PLAN.items():
        clients = clients_by_mm[mm_email]
        passing: dict[str, list[dict]] = {}
        violating: dict[str, list[dict]] = {}
        for c in clients:
            same_gender = [k for k in pool if k["gender"] == c["hard"]["seeking_gender"]]
            passing[c["id"]] = [k for k in same_gender if not matching.hard_violations(c["hard"], k)]
            violating[c["id"]] = [k for k in same_gender if len(matching.hard_violations(c["hard"], k)) == 1]
        kinds = ["accepted"] * n_accepted + ["avoidable"] * n_avoidable + ["other"] * (n_shares - n_accepted - n_avoidable)
        rng.shuffle(kinds)
        # Draw without replacement so no client is shown the same profile twice.
        for lists in (passing, violating):
            for cands in lists.values():
                rng.shuffle(cands)
        for kind in kinds:
            source = violating if kind == "avoidable" else passing
            client = rng.choice([c for c in clients if source[c["id"]]])
            cand = source[client["id"]].pop()
            result = matching.check({"hard": client["hard"], "soft": client["soft"]}, cand)
            share_id = _uid()
            shared_at = (now - timedelta(days=rng.uniform(1, 30))).isoformat()
            outcome = "accepted" if kind == "accepted" else "rejected"
            conn.execute(
                """INSERT INTO shares (id, tenant_id, client_id, candidate_id, matchmaker_id, outcome, check_status,
                                       fit_score, is_historical, shared_at)
                   VALUES (?, 't_datecrew', ?, ?, ?, ?, ?, ?, 1, ?)""",
                (share_id, client["id"], cand["id"], client["matchmaker_id"], outcome, result.status, result.fit_score, shared_at),
            )
            if outcome == "rejected":
                if kind == "avoidable":
                    rule = matching.hard_violations(client["hard"], cand)[0]["rule"]
                    text = rng.choice(VIOLATION_FEEDBACK[rule])
                else:
                    text = rng.choice(OTHER_FEEDBACK)
                c = classifier.classify_keyword(text, client["hard"])
                conn.execute(
                    """INSERT INTO rejections (id, tenant_id, share_id, raw_text_enc, status, reason_code, reason_detail,
                                               confidence, classifier, suggestion_json, created_at)
                       VALUES (?, 't_datecrew', ?, ?, 'done', ?, ?, ?, ?, ?, ?)""",
                    (_uid(), share_id, encrypt(text), c["reason_code"], c["reason_detail"], c["confidence"],
                     c["classifier"], json.dumps(c["suggestion"]) if c["suggestion"] else None, shared_at),
                )
    conn.commit()
