"""Pre-Share Match Check API."""

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Annotated

import jwt
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Path, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import config
from app.db import Principal, connect, get_conn, init_schema
from app.infra import RateLimiter, TTLCache, classify_rejection_job
from app.repository import ScopedRepo
from app.schemas import CheckRequest, LoginRequest, OutcomeRequest, PreferenceCardUpdate, RefreshRequest, ShareRequest
from app.security import (
    create_access_token,
    decode_access_token,
    get_principal,
    issue_refresh_token,
    require_role,
    revoke_refresh_token,
    rotate_refresh_token,
    verify_password_or_dummy,
)
from app.seed import seed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("matchcheck")

shortlist_cache = TTLCache(config.SHORTLIST_CACHE_TTL)
rate_limiter = RateLimiter()

IdParam = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


@asynccontextmanager
async def lifespan(_: FastAPI):
    conn = connect()
    init_schema(conn)
    started = time.perf_counter()
    seed(conn, config.SEED_CANDIDATES)
    log.info("database ready in %.1fs (%s)", time.perf_counter() - started, config.DB_PATH)
    conn.close()
    yield


app = FastAPI(
    title="Pre-Share Match Check",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if config.ENVIRONMENT != "production" else None,
    redoc_url=None,
)

if config.ALLOWED_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.ALLOWED_ORIGINS,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Authorization", "Content-Type"],
    )

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
CSP = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'"


def _rate_key(request: Request) -> tuple[str, int]:
    ip = request.client.host if request.client else "unknown"
    if request.url.path == "/api/auth/login":
        return f"login:{ip}", config.LOGIN_RATE_LIMIT_PER_MINUTE
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        try:
            return f"user:{decode_access_token(auth[7:])['sub']}", config.RATE_LIMIT_PER_MINUTE
        except jwt.PyJWTError:
            pass
    return f"ip:{ip}", config.RATE_LIMIT_PER_MINUTE


@app.middleware("http")
async def platform_middleware(request: Request, call_next):
    started = time.perf_counter()
    request_id = uuid.uuid4().hex[:12]
    if request.url.path.startswith("/api/"):
        key, limit = _rate_key(request)
        allowed, retry_after = rate_limiter.allow(key, limit)
        if not allowed:
            return JSONResponse(
                {"detail": "Too many requests"}, status_code=429, headers={"Retry-After": str(retry_after)}
            )
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if not request.url.path.startswith(("/docs", "/openapi.json")):
        response.headers["Content-Security-Policy"] = CSP
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time-Ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
    return response


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse({"detail": "Internal server error"}, status_code=500)


def repo(principal: Principal = Depends(get_principal), conn=Depends(get_conn)) -> ScopedRepo:
    return ScopedRepo(conn, principal)


def not_found(what: str = "Not found") -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=what)


# ---------------------------------------------------------------- auth


def _token_response(conn, user) -> dict:
    return {
        "access_token": create_access_token(user["id"], user["tenant_id"], user["role"]),
        "refresh_token": issue_refresh_token(conn, user["id"]),
        "token_type": "bearer",
        "expires_in": config.ACCESS_TOKEN_MINUTES * 60,
        "user": {"name": user["name"], "role": user["role"], "tenant": user["tenant_name"]},
    }


USER_SQL = """SELECT u.id, u.tenant_id, u.role, u.name, u.password_hash, t.name AS tenant_name
              FROM users u JOIN tenants t ON t.id = u.tenant_id WHERE {} AND u.active = 1"""


@app.post("/api/auth/login")
def login(body: LoginRequest, conn=Depends(get_conn)):
    user = conn.execute(USER_SQL.format("u.email = ?"), (body.email.lower(),)).fetchone()
    if not verify_password_or_dummy(body.password, user["password_hash"] if user else None):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    ScopedRepo(conn, Principal(user["id"], user["tenant_id"], user["role"], user["name"])).audit("login", "user", user["id"])
    conn.commit()
    return _token_response(conn, user)


@app.post("/api/auth/refresh")
def refresh(body: RefreshRequest, conn=Depends(get_conn)):
    user_id = rotate_refresh_token(conn, body.refresh_token)
    user = conn.execute(USER_SQL.format("u.id = ?"), (user_id,)).fetchone() if user_id else None
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
    return _token_response(conn, user)


@app.post("/api/auth/logout", status_code=204)
def logout(body: RefreshRequest, conn=Depends(get_conn), _: Principal = Depends(get_principal)):
    revoke_refresh_token(conn, body.refresh_token)


@app.get("/api/me")
def me(principal: Principal = Depends(get_principal)):
    return {"name": principal.name, "role": principal.role, "tenant_id": principal.tenant_id}


# ---------------------------------------------------------------- clients and matching


@app.get("/api/clients")
def list_clients(r: ScopedRepo = Depends(repo)):
    return {"data": r.list_clients()}


@app.get("/api/clients/{client_id}")
def get_client(client_id: IdParam, r: ScopedRepo = Depends(repo)):
    client = r.get_client(client_id)
    if client is None:
        raise not_found("Client not found")
    return client


@app.put("/api/clients/{client_id}/preference-card")
def update_card(body: PreferenceCardUpdate, client_id: IdParam, r: ScopedRepo = Depends(repo)):
    hard = body.hard.model_dump(exclude_none=True)
    card = r.update_card(client_id, hard, [s.model_dump() for s in body.soft])
    if card is None:
        raise not_found("Client not found")
    shortlist_cache.invalidate_prefix((r.p.tenant_id, client_id))
    return card


@app.get("/api/clients/{client_id}/shortlist")
def shortlist(client_id: IdParam, limit: int = Query(25, ge=1, le=100), r: ScopedRepo = Depends(repo)):
    started = time.perf_counter()
    # Scope check first, every time. The cache never answers for a client the caller cannot see.
    if r.get_client(client_id) is None:
        raise not_found("Client not found")
    key = (r.p.tenant_id, client_id, limit)
    cached = shortlist_cache.get(key)
    if cached is None:
        cached = r.shortlist(client_id, limit)
        shortlist_cache.set(key, cached)
        hit = False
    else:
        hit = True
    return {
        "data": cached["data"],
        "meta": {**cached["meta"], "cache_hit": hit, "request_ms": round((time.perf_counter() - started) * 1000, 1)},
    }


@app.get("/api/clients/{client_id}/shares")
def list_shares(client_id: IdParam, r: ScopedRepo = Depends(repo)):
    shares = r.list_shares(client_id)
    if shares is None:
        raise not_found("Client not found")
    return {"data": shares}


@app.post("/api/match/check")
def check(body: CheckRequest, r: ScopedRepo = Depends(repo)):
    result = r.check(body.client_id, body.candidate_id)
    if result is None:
        raise not_found("Client or candidate not found")
    return result


@app.post("/api/shares", status_code=201)
def create_share(body: ShareRequest, r: ScopedRepo = Depends(repo)):
    result = r.create_share(body.client_id, body.candidate_id, body.override_reason)
    if result is None:
        raise not_found("Client or candidate not found")
    if result["blocked"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": "Candidate breaks a deal-breaker. Add an override reason to share anyway.",
                    "violations": result["violations"]},
        )
    shortlist_cache.invalidate_prefix((r.p.tenant_id, body.client_id))
    return result


@app.post("/api/shares/{share_id}/outcome", status_code=202)
def record_outcome(body: OutcomeRequest, background: BackgroundTasks, share_id: IdParam, r: ScopedRepo = Depends(repo)):
    result = r.record_outcome(share_id, body.outcome, body.feedback)
    if result is None:
        raise not_found("Share not found")
    if "error" in result:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=result["error"])
    if result["rejection_id"]:
        # Classification is queued, never done inside the request.
        background.add_task(classify_rejection_job, result["rejection_id"], r.p.tenant_id)
    return result


@app.get("/api/rejections/{rejection_id}")
def get_rejection(rejection_id: IdParam, r: ScopedRepo = Depends(repo)):
    rejection = r.get_rejection(rejection_id)
    if rejection is None:
        raise not_found("Rejection not found")
    return rejection


@app.post("/api/shares/{share_id}/reveal-contact")
def reveal_contact(share_id: IdParam, r: ScopedRepo = Depends(repo)):
    result = r.reveal_contact(share_id)
    if result is None:
        raise not_found("Share not found")
    if "error" in result:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=result["error"])
    return result


# ---------------------------------------------------------------- insight


@app.get("/api/dashboard")
def dashboard(r: ScopedRepo = Depends(repo)):
    return r.dashboard()


@app.get("/api/backtest")
def backtest(r: ScopedRepo = Depends(repo)):
    return r.backtest()


@app.get("/api/audit")
def audit(principal: Principal = Depends(require_role("lead")), conn=Depends(get_conn)):
    return {"data": ScopedRepo(conn, principal).audit_log()}


@app.get("/api/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------- frontend

STATIC_DIR = config.BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")
