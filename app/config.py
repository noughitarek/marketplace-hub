import os

from dotenv import load_dotenv
load_dotenv()
_db = os.getenv("DATABASE_URL", "postgresql+psycopg2://hub:hub@localhost:5432/hub")
if _db.startswith("postgres://"):
    _db = _db.replace("postgres://", "postgresql+psycopg2://", 1)


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
    SQLALCHEMY_DATABASE_URI = _db
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "1") == "1"
    SESSION_COOKIE_SAMESITE = "Lax"
    MAX_CONTENT_LENGTH = 8 * 1024 * 1024
