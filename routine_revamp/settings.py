"""
Django settings for Routine Revamp.

Site-specific values (database location, report folder, lab name, ...) live in
config.ini next to manage.py so they can be changed without touching code.
See config.example.ini for every option. Set the ROUTINE_CONFIG environment
variable to use a config file somewhere else.
"""

import configparser
import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

_config = configparser.ConfigParser()
_config.read(os.environ.get("ROUTINE_CONFIG", BASE_DIR / "config.ini"))


def _cfg(key, default=None):
    return _config.get("routine", key, fallback=default)


def _cfg_bool(key, default=False):
    return _config.getboolean("routine", key, fallback=default)


def _secret_key():
    configured = _cfg("secret_key")
    if configured:
        return configured
    key_file = BASE_DIR / ".secret_key"
    if not key_file.exists():
        key_file.write_text(secrets.token_urlsafe(50))
    return key_file.read_text().strip()


SECRET_KEY = _secret_key()

DEBUG = _cfg_bool("debug", False)

# The app is served to the user's own browser on their own computer.
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "lab",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "routine_revamp.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "lab.context_processors.lab_settings",
            ],
        },
    },
]

WSGI_APPLICATION = "routine_revamp.wsgi.application"

# The database file can sit on the shared network drive so every user sees the
# same sample log. WAL mode is deliberately not used: it does not work on
# network shares.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": Path(_cfg("database_path", str(BASE_DIR / "routine.sqlite3"))),
        "OPTIONS": {
            "timeout": 30,
            "transaction_mode": "IMMEDIATE",
        },
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = _cfg("time_zone", "America/Chicago")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "lab:sample_log"
LOGOUT_REDIRECT_URL = "login"

# --- Lab settings -----------------------------------------------------------

LAB_NAME = _cfg("lab_name", "Analytical Laboratory")

# Generated PDF reports are saved here (normally a folder on the network drive).
REPORTS_DIR = Path(_cfg("reports_dir", str(BASE_DIR / "reports")))

# Calibrations are valid for this many months from the calibration date.
CALIBRATION_VALID_MONTHS = int(_cfg("calibration_valid_months", "6"))

# A verification passes when every component's calculated % is within this many
# percentage points of the theoretical %.
VERIFICATION_TOLERANCE = float(_cfg("verification_tolerance", "0.03"))

# Text appended to every report email.
EMAIL_SIGNATURE = _cfg("email_signature", "").replace("\\n", "\n")
