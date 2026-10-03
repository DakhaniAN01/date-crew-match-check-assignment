"""Runtime settings, read from environment variables with safe local defaults."""

import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

# JWT signing key. In production this must come from a secrets manager.
SECRET_KEY = os.getenv("SECRET_KEY") or secrets.token_urlsafe(48)
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_MINUTES = int(os.getenv("ACCESS_TOKEN_MINUTES", "15"))
REFRESH_TOKEN_DAYS = int(os.getenv("REFRESH_TOKEN_DAYS", "7"))

# Fernet key for field-level encryption of sensitive free text.
# Generated per process if absent, which is fine for a demo database that is reseeded on start.
FIELD_ENCRYPTION_KEY = os.getenv("FIELD_ENCRYPTION_KEY")

DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "data" / "matchcheck.db")))

# Size of the mock candidate pool. Large enough to show that matching stays fast.
SEED_CANDIDATES = int(os.getenv("SEED_CANDIDATES", "20000"))

# Rate limits (requests per minute)
RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "120"))
LOGIN_RATE_LIMIT_PER_MINUTE = int(os.getenv("LOGIN_RATE_LIMIT_PER_MINUTE", "5"))

# Shortlist cache TTL in seconds (Redis in production, in-process here)
SHORTLIST_CACHE_TTL = int(os.getenv("SHORTLIST_CACHE_TTL", "600"))

ALLOWED_ORIGINS = [o for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o]

# Optional LLM classifier. Without a key the keyword classifier is used.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLASSIFIER_MODEL = os.getenv("CLASSIFIER_MODEL", "claude-haiku-4-5")
