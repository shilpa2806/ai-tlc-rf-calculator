"""Django settings for the TLC RF Calculator project.

Runtime configuration is driven by environment variables so the same image can
run in development and production:

    DEBUG                  Enable debug mode (default: False).
    SECRET_KEY             Optional: nothing here signs data (see below).
    ALLOWED_HOSTS          Comma-separated allowed hosts.
    ELN_ORIGIN             The ELN origin (e.g. https://eln.example.org). Sets
                           both the CSP connect-src and the fetch allowlist.

A minimal deployment needs only ALLOWED_HOSTS and (for ELN save-back)
ELN_ORIGIN. Advanced overrides — CSRF_TRUSTED_ORIGINS,
CSP_CONNECT_SRC, ELN_ALLOWED_HOSTS, MAX_IMAGE_MB, MAX_IMAGE_MEGAPIXELS,
MAX_UPLOAD_MB, MEDIA_MAX_AGE_HOURS, MEDIA_SWEEP_MINUTES, IMAGE_JOB_RESERVE_MB,
IMAGE_JOB_WAIT_SECONDS, IMAGE_CACHE_MB, SECURE_* — are all optional and default
sensibly.

See https://docs.djangoproject.com/en/5.2/ref/settings/
"""

import hashlib
import os
import tempfile
from pathlib import Path

from django.core.management.utils import get_random_secret_key

from .env import csrf_origins_from_hosts, env_bool, env_int, env_list, host_of, signs_data

# A single ELN_ORIGIN (e.g. https://eln.example.org) configures both the CSP
# connect-src (so the browser may POST results back) and the remote-fetch
# allowlist below — one variable instead of two. Either derived value can still
# be overridden explicitly (CSP_CONNECT_SRC / ELN_ALLOWED_HOSTS).
ELN_ORIGIN = os.getenv("ELN_ORIGIN", "").strip()
ELN_ORIGIN_HOST = host_of(ELN_ORIGIN)

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent


# See https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/

# DEBUG is off by default; enable it explicitly for local development.
DEBUG = env_bool("DEBUG", False)

# SECRET_KEY: this app uses none of what Django signs with it. Django signs
# login sessions, password-reset links, one-time messages and values from
# django.core.signing (signed cookies or links) -- none of which is installed
# or used here. CSRF protection does not use the key (it compares a random
# value in the cookie with the one the page sends back).
#
# Django still requires a key, so when SECRET_KEY is not set a random one is
# made at start-up. That key differs per start and per worker process. This
# is harmless only while nothing signs data: if you add logins, sessions,
# messages or signed cookies/links, set SECRET_KEY to one fixed secret in the
# environment, or a signature made by one worker fails in the other and all
# of them break at every restart. The check below (after INSTALLED_APPS and
# MIDDLEWARE) stops a production server that would get this wrong.
SECRET_KEY = os.getenv("SECRET_KEY") or get_random_secret_key()
SECRET_KEY_IS_RANDOM = not os.getenv("SECRET_KEY")

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1")

# Trusted CSRF origins default to https://<host> for each real ALLOWED_HOSTS
# entry (local / wildcard hosts are skipped), so you rarely need to set this
# separately. An explicit CSRF_TRUSTED_ORIGINS overrides the derivation.
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS") or csrf_origins_from_hosts(ALLOWED_HOSTS)

# Hosts the server may fetch ELN images from (SSRF allowlist). Empty = any
# public host. Explicit ELN_ALLOWED_HOSTS wins, else the ELN_ORIGIN host.
ELN_ALLOWED_HOSTS = env_list("ELN_ALLOWED_HOSTS") or ([ELN_ORIGIN_HOST] if ELN_ORIGIN_HOST else [])


# Application definition

# This app has no database models and no admin/auth needs; CSRF protection is
# cookie-based, so the auth / sessions / messages / admin apps are intentionally
# omitted to minimise the attack surface.
INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "tlc",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "config.middleware.ContentSecurityPolicyMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# See SECRET_KEY above: a per-start random key would break anything that signs.
if SECRET_KEY_IS_RANDOM and not DEBUG and signs_data(INSTALLED_APPS, MIDDLEWARE):
    raise RuntimeError(
        "Sessions, logins or messages are enabled, which sign data with SECRET_KEY: "
        "set SECRET_KEY to one fixed secret value in the environment."
    )

# Content-Security-Policy. The frontend is fully self-hosted with no inline
# scripts, inline styles, or inline event handlers, so scripts and styles are
# locked to 'self'. Cross-origin fetches (e.g. saving results back to an ELN)
# are blocked unless their origin is allowed via ELN_ORIGIN (or CSP_CONNECT_SRC).
_csp_extra = env_list("CSP_CONNECT_SRC") or ([ELN_ORIGIN] if ELN_ORIGIN else [])
_csp_connect = " ".join(["'self'", *_csp_extra])
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        f"connect-src {_csp_connect}",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ]
)

# Use WhiteNoise's compressed/hashed manifest storage only in production;
# in DEBUG, plain storage serves via finders so no collectstatic run is needed
# (e.g. when running the test suite).
_staticfiles_backend = (
    "django.contrib.staticfiles.storage.StaticFilesStorage"
    if DEBUG
    else "whitenoise.storage.CompressedManifestStaticFilesStorage"
)
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": _staticfiles_backend,
    },
}

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# Database: none. The app has no models; CSRF is cookie-based and there are no
# sessions/auth, so no database connection is configured. Results are written
# to MEDIA_ROOT as files, not persisted to a DB.
DATABASES: dict = {}


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = "/static/"
STATIC_ROOT = os.path.join(BASE_DIR, "staticfiles")

MEDIA_URL = "/media/"
MEDIA_ROOT = os.path.join(BASE_DIR, "media")

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Image limits. Phone photos of plates reach 13 MB and 50 MP (6120x8160), so
# the defaults admit them. MAX_IMAGE_MB caps a file (local upload or ELN
# fetch) and is also enforced in the browser; MAX_IMAGE_MEGAPIXELS caps the
# decoded size, bounding the ~3 bytes/pixel the OpenCV paths hold per request.
MAX_IMAGE_MB = env_int("MAX_IMAGE_MB", 25)
MAX_IMAGE_MEGAPIXELS = env_int("MAX_IMAGE_MEGAPIXELS", 64)

# Local uploads travel as a base64 data URL inside the JSON body, inflating
# them by a third, so the body cap must exceed MAX_IMAGE_MB by that much.
DATA_UPLOAD_MAX_MEMORY_SIZE = env_int("MAX_UPLOAD_MB", (MAX_IMAGE_MB * 4) // 3 + 2) * 1024 * 1024

# Files in MEDIA_ROOT (uploaded and ELN images, annotated images, CSVs) are
# deleted once unused for MEDIA_MAX_AGE_HOURS; the app checks after a write, at
# most every MEDIA_SWEEP_MINUTES per worker (tlc/media.py). 0 turns automatic
# sweeps off, e.g. when cron runs `manage.py cleanup_media` instead.
MEDIA_MAX_AGE_HOURS = env_int("MEDIA_MAX_AGE_HOURS", 24)
MEDIA_SWEEP_MINUTES = env_int("MEDIA_SWEEP_MINUTES", 15)

# Image jobs (decode + detection or drawing) start only when the memory they
# need -- ~14 bytes per pixel, ~680 MB for a 50 MP photo -- is free, keeping
# IMAGE_JOB_RESERVE_MB spare; otherwise they wait up to IMAGE_JOB_WAIT_SECONDS
# and then get 503 "busy" (tlc/admission.py). Each worker keeps recently
# decoded images, up to IMAGE_CACHE_MB, so the steps on one plate decode it
# once (tlc/imagecache.py); 0 turns the cache off.
IMAGE_JOB_RESERVE_MB = env_int("IMAGE_JOB_RESERVE_MB", 300)
IMAGE_JOB_WAIT_SECONDS = env_int("IMAGE_JOB_WAIT_SECONDS", 30)
IMAGE_CACHE_MB = env_int("IMAGE_CACHE_MB", 256)
# Where workers share their memory claims (one small file per deployment).
IMAGE_JOB_CLAIMS_FILE = os.getenv(
    "IMAGE_JOB_CLAIMS_FILE",
    os.path.join(
        tempfile.gettempdir(),
        "tlc-image-jobs-" + hashlib.sha256(str(BASE_DIR).encode()).hexdigest()[:12] + ".json",
    ),
)


# Candidate detection: TLC-SpotDet and TLC-LineDet (SI section 6). The models
# are versioned artefacts; their SHA-256 is reported with every detection.
# Input sizes come from the models themselves (1280 and 1024 px).
SPOT_MODEL_PATH = BASE_DIR / "tlc" / "ml_models" / "spot_best.onnx"
LINE_MODEL_PATH = BASE_DIR / "tlc" / "ml_models" / "line_best.onnx"
# spot_best.onnx was exported with its own confidence cutoff of 0.25: it
# returns nothing scoring lower (checked on 223 plates; the lowest score seen
# is 0.252). A lower value here therefore finds no more spots -- version 1
# used 0.15 with the same result -- so the setting states the cutoff
# that actually applies. Going lower would need a re-export from the .pt
# weights.
SPOT_CONF_DEFAULT = 0.25
LINE_CONF_DEFAULT = 0.05
# Spot inference runs only on the strip between baseline and solvent front,
# widened by this many pixels on each side (SI section 4).
TLC_BAND_MARGIN = 12
# Threads ONNX Runtime may use per inference. Each Gunicorn thread can run one
# inference at a time, so workers x threads x this should not exceed the cores.
ONNX_INTRA_OP_THREADS = env_int("ONNX_INTRA_OP_THREADS", 2)
# ONNX Runtime's CPU memory arena keeps the largest tensors it has seen: on
# 50 MP plates ~500 MB per worker, held for good. Off, results are identical
# and the speed the same within noise (measured on 40 plates).
ONNX_CPU_MEM_ARENA = env_bool("ONNX_CPU_MEM_ARENA", False)


# Security hardening. These are enforced whenever DEBUG is off so that local
# development over plain HTTP keeps working, while deployments are secure.
# https://docs.djangoproject.com/en/5.2/howto/deployment/checklist/
# Health probes come over plain HTTP from inside the cluster or host, so the
# health check answers there instead of redirecting to HTTPS.
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]

if not DEBUG:
    SECURE_SSL_REDIRECT = os.getenv("SECURE_SSL_REDIRECT", "True").lower() in ("true", "1", "t")
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.getenv("SECURE_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

SECURE_CONTENT_TYPE_NOSNIFF = True
# The tool is opened as its own page (not embedded by the ELN), so deny all
# framing to fully block clickjacking.
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_HTTPONLY = True
