# Pre-Share Match Check

A prototype for The Date Crew product assessment. It stops matchmakers from sharing profiles that break a client's stated deal-breakers, and turns free-text rejection feedback into structured reasons that feed back into the client's preference card.

## The problem it targets

From the last 30 days of data:

| Stage | Count | Conversion |
|---|---|---|
| Profiles shared | 1,000 | |
| Profiles accepted | 310 | **31%** (the biggest drop) |
| Contact details shared | 210 | 68% |
| Conversations started | 150 | 71% |
| Meetings fixed | 75 | 50% |
| Meetings completed | 42 | 56% |

690 profiles were rejected, and about 35% of those (241) broke a preference the client had already given us. Those rejections were preventable. Removing them lifts acceptance from 310/1,000 to 310/759, about **41%**, with the same accepted profiles.

## What the prototype does

1. **Preference card per client.** Hard deal-breakers (gender, age, religion, diet, smoking, children, location) block a share. Soft preferences (height, education, profession, drinking) carry weights and produce a 0 to 100 fit score.
2. **Pre-share check.** Paste any candidate ID and get **BLOCK** (with the exact reason), **WARN** (low fit) or **PASS**. A blocked profile can only be shared with a written override reason, and every override is logged.
3. **Ranked shortlist.** All 20,000 candidates are filtered by deal-breakers in SQL, then scored and ranked. Profiles already shared with that client are excluded.
4. **Rejection feedback loop.** When a client rejects a profile, the free-text reason is classified in the background:
   - *Preference violation*: the deal-breaker was already on the card, so the rejection was avoidable.
   - *New preference*: the client mentioned a deal-breaker we never recorded. The matchmaker gets a one-click "Add deal-breaker" suggestion.
   - *Taste or timing*: appearance, height, profession, personality, timing, family or other.
5. **Backtest.** Last month's 1,000 shares are replayed through the checker. Result on the seeded data: **241 blocked, 0 of them were profiles the client accepted, acceptance 31.0% to 40.8%.** By matchmaker, A goes from 43.9% to 50.9% and B from 21.1% to 31.0%.

The mock history is generated to match the brief exactly: 1,000 shared, 310 accepted, 241 avoidable rejections, Matchmaker A at 44% and Matchmaker B at 21%.

## Run it locally

Requires Python 3.11 or newer.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --port 8077
```

Open http://127.0.0.1:8077. The first start builds a mock database (about 2 seconds) in `data/`. Delete the `data/` folder to reset it.

**Demo accounts.** These exist only in the local mock database and all share the password defined in `app/seed.py` (`DEMO_PASSWORD`). The login screen has buttons that fill them in.

| Account | Role | Sees |
|---|---|---|
| priya@datecrew.test | Matchmaker A | Only her 6 clients |
| rahul@datecrew.test | Matchmaker B | Only his 6 clients |
| meera@datecrew.test | Lead | All 12 Date Crew clients, plus the audit log |
| sam@northstar.test | Matchmaker at a second agency | Only Northstar data, nothing from The Date Crew |

**Optional Claude classifier.** Set `ANTHROPIC_API_KEY` to classify rejection feedback with Claude Haiku using structured JSON output. Names, phone numbers and emails are removed before the text is sent. Without a key, or if the call fails, a deterministic keyword classifier is used, so the demo always works.

Run the tests:

```bash
.venv/bin/python -m pytest -q
```

## Core principles and how they are implemented

### 1. Low latency

| Technique | Where |
|---|---|
| Deal-breakers are applied as an indexed SQL filter first. 20,000 candidates become about 1,500 before any scoring. | `matching.sql_prefilter`, index `ix_candidates_filter` |
| Each rule is defined once and used for both SQL and Python, and a test proves both paths select exactly the same candidates. | `matching.HARD_RULES`, `tests/test_matching.py` |
| Soft scoring runs in-process on the filtered set. | `matching.fit_score` |
| Shortlists are cached per client and invalidated when the card changes or a profile is shared. | `infra.TTLCache` (Redis in production) |
| The LLM is never in the request path. Classification runs as a background job. | `infra.classify_rejection_job` (SQS worker in production) |
| Stateless API, so it scales by adding processes or containers. | `main.py` |

**Measured on a Mac laptop (8 cores, SQLite, load generator running on the same machine):**

| Scenario | Throughput | p50 | p95 | p99 |
|---|---|---|---|---|
| 1 user, sequential | 196 req/s | 5 ms | 6 ms | 7 ms |
| 50 users sending requests back to back, 1 process | 292 req/s | 168 ms | 214 ms | 238 ms |
| 50 users sending requests back to back, 4 processes | 523 req/s | 69 ms | 132 ms | 201 ms |

A single request is about 5 ms, and the rest is queueing under a deliberately extreme load. For scale: 1,000 active users each doing something every 10 seconds is about 100 requests per second, under 20% of one 4-process container. Reproduce with `scripts/benchmark.py`.

### 2. Data isolation

- **Single scoped repository.** Every query goes through `ScopedRepo`, which always filters by the caller's `tenant_id`, and additionally by `matchmaker_id` for matchmakers.
- **404, not 403.** Out-of-scope records return 404, so a caller cannot even learn that another agency's client exists.
- **Cache is behind the scope check.** The shortlist cache is only consulted after the scope check, so a cached result is never served to someone who cannot see that client.
- **Contact details hidden by default.** Candidate phone and email are never in match responses. They are revealed only after the client accepts, and each reveal is audited.
- **Encryption at rest.** Client notes and rejection feedback are encrypted at rest with Fernet (AES-128-CBC plus HMAC).
- **Tests.** `tests/test_isolation.py` proves all of the above across two tenants and two matchmakers.

In production, PostgreSQL row-level security enforces the same tenant rule at the database layer, so a bug in application code still cannot leak data.

### 3. Secure API

| Control | Implementation |
|---|---|
| Authentication | Short-lived JWT access tokens (15 min, HS256, algorithm pinned) |
| Refresh tokens | Rotating refresh tokens stored as hashes. Reusing an old token revokes the whole family. |
| Fresh authorisation | Role and tenant are re-read from the database on every request, so a removed user loses access immediately |
| Passwords | PBKDF2-SHA256 with 310,000 iterations. Login timing is the same for unknown emails, and the error message is identical. |
| Authorisation | Roles `matchmaker` < `lead` < `admin`, checked per endpoint, plus data scoping in the repository |
| Input validation | Pydantic models reject unknown fields and bound every value. IDs must match a strict pattern. All SQL is parameterised. |
| Rate limiting | 120 requests per minute per user, and 5 login attempts per minute per IP |
| Headers | Strict Content-Security-Policy (no inline scripts or styles), HSTS, `nosniff`, `X-Frame-Options: DENY`, `no-store` on API responses |
| Errors | Generic 500 responses with no stack traces |
| Audit log | Logins, shares, overrides, outcomes, preference changes and contact reveals |
| LLM safety | PII is scrubbed before sending. Feedback is wrapped as data, never instructions. Output is forced into a fixed JSON schema. |

`tests/test_security.py` covers forged, tampered and unsigned tokens, refresh reuse, rate limits, role checks, input rejection, headers and encryption at rest.

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/auth/login`, `/api/auth/refresh`, `/api/auth/logout` | Tokens |
| GET | `/api/clients` | Clients in scope |
| GET | `/api/clients/{id}` | Client with preference card |
| PUT | `/api/clients/{id}/preference-card` | Update deal-breakers and soft preferences |
| GET | `/api/clients/{id}/shortlist?limit=25` | Ranked candidates with timing metadata |
| POST | `/api/match/check` | BLOCK, WARN or PASS for one client and candidate |
| POST | `/api/shares` | Share a profile. Returns 409 on a block unless an override reason is given. |
| POST | `/api/shares/{id}/outcome` | Accepted or rejected. A rejection queues classification and returns 202. |
| GET | `/api/rejections/{id}` | Classified reason and any suggested deal-breaker |
| POST | `/api/shares/{id}/reveal-contact` | Only after acceptance, audited |
| GET | `/api/dashboard`, `/api/backtest` | Insight, scoped to the caller |
| GET | `/api/audit` | Lead and admin only |

Interactive docs at `/docs` when `ENVIRONMENT` is not `production`.

## Project structure

```
app/
  main.py         API routes, middleware (rate limit, security headers, timing)
  matching.py     Rules engine: hard rules, soft scoring, SQL prefilter
  classifier.py   Rejection feedback: keyword classifier and optional Claude classifier
  repository.py   Scoped data access (tenant and matchmaker isolation)
  security.py     Passwords, JWT, refresh rotation, roles, field encryption
  infra.py        Cache, rate limiter, background job (Redis and SQS stand-ins)
  db.py           Schema and connections
  seed.py         Deterministic mock data matching the brief
  schemas.py      Request validation
static/           Plain HTML, CSS and JS frontend (no build step)
tests/            Matching, isolation, security and end-to-end flow tests
scripts/          Load test
```

## From prototype to production

| Prototype | Production |
|---|---|
| SQLite | PostgreSQL (RDS) with row-level security and read replicas |
| In-process cache and rate limiter | Redis |
| FastAPI background task | SQS queue and a worker |
| Local key file | AWS Secrets Manager and KMS |
| Single container | ECS Fargate behind a load balancer and WAF, 2 or more tasks, autoscaling |
| Demo passwords | SSO (Google Workspace or Cognito) with MFA for admins |
| Manual outcome entry | Email integration that reads client replies |

## What is deliberately out of scope

- A machine-learning ranking model. Fixed rules fix the measurable 35% first, and they are explainable.
- Learning a client's true preferences from behaviour (clients who reject profiles and later accept similar ones). That needs outcome data this tool will start collecting.
