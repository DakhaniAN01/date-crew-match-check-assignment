"""Authentication, authorisation and field-level encryption."""

import hashlib
import hmac
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app import config
from app.db import Principal, get_conn

ROLE_RANK = {"matchmaker": 1, "lead": 2, "admin": 3}

PBKDF2_ITERATIONS = 310_000

# ---------------------------------------------------------------- passwords


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


# Used to keep login timing constant when the email does not exist.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def verify_password_or_dummy(password: str, stored: str | None) -> bool:
    if stored is None:
        verify_password(password, _DUMMY_HASH)
        return False
    return verify_password(password, stored)


# ---------------------------------------------------------------- tokens


def create_access_token(user_id: str, tenant_id: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "tid": tenant_id,
        "role": role,
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=config.ACCESS_TOKEN_MINUTES),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, config.SECRET_KEY, algorithm=config.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    payload = jwt.decode(
        token,
        config.SECRET_KEY,
        algorithms=[config.JWT_ALGORITHM],
        options={"require": ["sub", "tid", "exp", "type"]},
    )
    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("wrong token type")
    return payload


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_refresh_token(conn, user_id: str) -> str:
    token = secrets.token_urlsafe(48)
    expires = datetime.now(timezone.utc) + timedelta(days=config.REFRESH_TOKEN_DAYS)
    conn.execute(
        "INSERT INTO refresh_tokens (token_hash, user_id, expires_at, revoked) VALUES (?, ?, ?, 0)",
        (_hash_token(token), user_id, expires.isoformat()),
    )
    conn.commit()
    return token


def rotate_refresh_token(conn, token: str) -> str | None:
    """Exchange a refresh token for a new one. Returns the user id, or None if invalid.

    Reusing a token that was already rotated revokes every token for that user,
    which limits the damage if a refresh token is stolen.
    """
    row = conn.execute(
        "SELECT user_id, expires_at, revoked FROM refresh_tokens WHERE token_hash = ?",
        (_hash_token(token),),
    ).fetchone()
    if row is None:
        return None
    if row["revoked"]:
        conn.execute("UPDATE refresh_tokens SET revoked = 1 WHERE user_id = ?", (row["user_id"],))
        conn.commit()
        return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        return None
    conn.execute("UPDATE refresh_tokens SET revoked = 1 WHERE token_hash = ?", (_hash_token(token),))
    conn.commit()
    return row["user_id"]


def revoke_refresh_token(conn, token: str) -> None:
    conn.execute("UPDATE refresh_tokens SET revoked = 1 WHERE token_hash = ?", (_hash_token(token),))
    conn.commit()


# ---------------------------------------------------------------- request principal

_bearer = HTTPBearer(auto_error=False)

_UNAUTHORISED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or expired token",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    conn=Depends(get_conn),
) -> Principal:
    if creds is None:
        raise _UNAUTHORISED
    try:
        payload = decode_access_token(creds.credentials)
    except jwt.PyJWTError:
        raise _UNAUTHORISED
    # Role and tenant are re-read from the database so a demoted or removed user
    # loses access immediately, not when the token expires.
    row = conn.execute(
        "SELECT id, tenant_id, role, name FROM users WHERE id = ? AND active = 1",
        (payload["sub"],),
    ).fetchone()
    if row is None or row["tenant_id"] != payload["tid"]:
        raise _UNAUTHORISED
    return Principal(user_id=row["id"], tenant_id=row["tenant_id"], role=row["role"], name=row["name"])


def require_role(minimum: str):
    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if ROLE_RANK[principal.role] < ROLE_RANK[minimum]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        return principal

    return dependency


# ---------------------------------------------------------------- field encryption


def _load_fernet() -> Fernet:
    if config.FIELD_ENCRYPTION_KEY:
        return Fernet(config.FIELD_ENCRYPTION_KEY.encode())
    key_file = config.DB_PATH.parent / ".field_key"
    key_file.parent.mkdir(parents=True, exist_ok=True)
    if not key_file.exists():
        key_file.write_bytes(Fernet.generate_key())
        key_file.chmod(0o600)
    return Fernet(key_file.read_bytes())


_fernet = _load_fernet()


def encrypt(text: str | None) -> str | None:
    return None if text is None else _fernet.encrypt(text.encode()).decode()


def decrypt(token: str | None) -> str | None:
    return None if token is None else _fernet.decrypt(token.encode()).decode()
