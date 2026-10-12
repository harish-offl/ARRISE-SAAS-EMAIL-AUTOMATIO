"""Django settings for the email automation project."""

import os
from pathlib import Path

from backend.env_utils import BASE_DIR, DATA_DIR, load_project_env

load_project_env()

SECRET_KEY = os.getenv("SECRET_KEY", "your-dev-secret-key-change-in-production")
DEBUG = os.getenv("DEBUG", "1").lower() in {"1", "true", "yes", "on"}

allowed_hosts = os.getenv("ALLOWED_HOSTS", "127.0.0.1,localhost,0.0.0.0")
ALLOWED_HOSTS = [host.strip() for host in allowed_hosts.split(",") if host.strip()]

raw_db_name = os.getenv("DATABASE_URL", str(DATA_DIR / "leads.db"))
if raw_db_name.startswith("sqlite:///"):
    raw_db_name = raw_db_name.replace("sqlite:///", "", 1)
elif raw_db_name.startswith("sqlite://"):
    raw_db_name = raw_db_name.replace("sqlite://", "", 1)

db_name = Path(raw_db_name)
if not db_name.is_absolute():
    db_name = BASE_DIR / db_name

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(db_name),
    }
}

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.staticfiles",
    "rest_framework",
    "backend.app",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "backend.app.main"
USE_TZ = True
TIME_ZONE = "UTC"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 100,
}

STATIC_URL = "/static/"
STATIC_ROOT = str(BASE_DIR / "static")
