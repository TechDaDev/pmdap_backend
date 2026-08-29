import os
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]


def env_list(name, default=""):
    return [
        item.strip() for item in os.getenv(name, default).split(",") if item.strip()
    ]


def env_bool(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "unsafe-development-only")
DEBUG = False
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "drf_spectacular",
    "corsheaders",
]

PROJECT_APPS = [
    # Must stay first so its admin.py replaces the default AdminSite before any
    # other app registers its models with @admin.register.
    "opsconsole.apps.OpsConsoleConfig",
    "accounts.apps.AccountsConfig",
    "registration.apps.RegistrationConfig",
    "patients.apps.PatientsConfig",
    "identities.apps.IdentitiesConfig",
    "guardians.apps.GuardiansConfig",
    "claims.apps.ClaimsConfig",
    "documents.apps.DocumentsConfig",
    "processing.apps.ProcessingConfig",
    "labs.apps.LabsConfig",
    "archive.apps.ArchiveConfig",
    "facilities.apps.FacilitiesConfig",
    "audit.apps.AuditConfig",
    "otp.apps.OtpConfig",
    "common.apps.CommonConfig",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + PROJECT_APPS

MIDDLEWARE = [
    "common.middleware.HealthcheckRedirectExemptMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "common.middleware.AuditRequestIdMiddleware",
]

# Flutter Web calls the API from a different origin than the deployed host.
# Explicit allow-list ONLY — never CORS_ALLOW_ALL_ORIGINS. Local dev web
# servers are allowed by default; production origins are injected via the
# CORS_ALLOWED_ORIGINS env var on Railway.
CORS_ALLOWED_ORIGINS = env_list(
    "CORS_ALLOWED_ORIGINS",
    "http://localhost:8080,http://127.0.0.1:8080",
)
CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_HEADERS = [
    "accept",
    "authorization",
    "content-type",
    "origin",
    "x-request-id",
]

ROOT_URLCONF = "config.urls"

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
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("POSTGRES_DB", "pmdap"),
        "USER": os.getenv("POSTGRES_USER", "pmdap"),
        "PASSWORD": os.getenv("POSTGRES_PASSWORD", ""),
        "HOST": os.getenv("POSTGRES_HOST", "localhost"),
        "PORT": os.getenv("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": 60,
    }
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": os.getenv("DJANGO_CACHE_URL", "redis://localhost:6379/2"),
    }
}

# Private file storage backend. Development/tests use the local filesystem;
# production uses the S3-compatible Railway bucket while keeping files private
# (no public URLs, authenticated streaming only).
STORAGE_BACKEND = os.getenv("STORAGE_BACKEND", "local").strip().lower()
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")
AWS_STORAGE_BUCKET_NAME = os.getenv("AWS_STORAGE_BUCKET_NAME", "")
AWS_S3_ENDPOINT_URL = os.getenv("AWS_S3_ENDPOINT_URL", "")
AWS_S3_REGION_NAME = os.getenv("AWS_S3_REGION_NAME", "") or None
AWS_S3_ADDRESSING_STYLE = os.getenv("AWS_S3_ADDRESSING_STYLE", "virtual")
AWS_S3_FILE_OVERWRITE = False
AWS_DEFAULT_ACL = None
AWS_QUERYSTRING_AUTH = False

PASSWORD_VALIDATION_MODULE = "django.contrib.auth.password_validation"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": f"{PASSWORD_VALIDATION_MODULE}.UserAttributeSimilarityValidator"},
    {"NAME": f"{PASSWORD_VALIDATION_MODULE}.MinimumLengthValidator"},
    {"NAME": f"{PASSWORD_VALIDATION_MODULE}.CommonPasswordValidator"},
    {"NAME": f"{PASSWORD_VALIDATION_MODULE}.NumericPasswordValidator"},
]

AUTH_USER_MODEL = "accounts.User"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Baghdad"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = (
    "whitenoise.storage.CompressedManifestStaticFilesStorage"
)
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
IDENTITY_FILE_ROOT = Path(
    os.getenv("IDENTITY_FILE_ROOT", BASE_DIR / "private" / "identity")
)
IDENTITY_FILE_MAX_BYTES = int(os.getenv("IDENTITY_FILE_MAX_BYTES", 10 * 1024 * 1024))
MEDICAL_FILE_ROOT = Path(
    os.getenv("MEDICAL_FILE_ROOT", BASE_DIR / "private" / "medical")
)
AVATAR_FILE_ROOT = Path(
    os.getenv("AVATAR_FILE_ROOT", BASE_DIR / "private" / "avatar")
)
MEDICAL_FILE_MAX_BYTES = int(os.getenv("MEDICAL_FILE_MAX_BYTES", 25 * 1024 * 1024))
# 64 megapixels: covers modern phone photos (e.g. 5360x7728 = 41.4MP) while
# staying well under PIL's decompression-bomb threshold (~89MP), so image-bomb
# protection is preserved.
MEDICAL_IMAGE_MAX_PIXELS = int(os.getenv("MEDICAL_IMAGE_MAX_PIXELS", 64_000_000))
PDF_TEXT_MIN_CHARS = int(os.getenv("PDF_TEXT_MIN_CHARS", "80"))
PDF_TEXT_MIN_PAGE_CHARS = int(os.getenv("PDF_TEXT_MIN_PAGE_CHARS", "40"))
PDF_TEXT_MIN_TEXT_PAGE_RATIO = float(os.getenv("PDF_TEXT_MIN_TEXT_PAGE_RATIO", "0.5"))
PDF_MAX_PAGES = int(os.getenv("PDF_MAX_PAGES", "250"))
PDF_EXTRACTION_MAX_RETRIES = int(os.getenv("PDF_EXTRACTION_MAX_RETRIES", "3"))
PDF_EXTRACTION_RETRY_BASE_SECONDS = int(
    os.getenv("PDF_EXTRACTION_RETRY_BASE_SECONDS", "5")
)
PDF_EXTRACTION_SOFT_TIME_LIMIT = int(
    os.getenv("PDF_EXTRACTION_SOFT_TIME_LIMIT", str(25 * 60))
)
PDF_EXTRACTION_TIME_LIMIT = int(os.getenv("PDF_EXTRACTION_TIME_LIMIT", str(30 * 60)))
OCR_ENGINE = os.getenv("OCR_ENGINE", "paddleocr")
OCR_TEXT_DETECTION_MODEL_NAME = os.getenv(
    "OCR_TEXT_DETECTION_MODEL_NAME", "PP-OCRv5_mobile_det"
)
OCR_TEXT_RECOGNITION_MODEL_NAME = os.getenv(
    "OCR_TEXT_RECOGNITION_MODEL_NAME", "arabic_PP-OCRv5_mobile_rec"
)
OCR_TEXT_DETECTION_MODEL_DIR = os.getenv("OCR_TEXT_DETECTION_MODEL_DIR", "")
OCR_TEXT_RECOGNITION_MODEL_DIR = os.getenv("OCR_TEXT_RECOGNITION_MODEL_DIR", "")
# Secondary Latin/multilingual OCR pipeline used for targeted region (ROI) reads
# of Latin-script values on the Iraqi National Card: blood group, printed
# dates, family number and the machine-readable zone. Uses the general
# multilingual recognizer (PP-OCRv6_medium_rec) which handles Latin/ASCII well.
OCR_LATIN_DETECTION_MODEL_NAME = os.getenv(
    "OCR_LATIN_DETECTION_MODEL_NAME", "PP-OCRv6_medium_det"
)
OCR_LATIN_RECOGNITION_MODEL_NAME = os.getenv(
    "OCR_LATIN_RECOGNITION_MODEL_NAME", "PP-OCRv6_medium_rec"
)
OCR_LATIN_DETECTION_MODEL_DIR = os.getenv("OCR_LATIN_DETECTION_MODEL_DIR", "")
OCR_LATIN_RECOGNITION_MODEL_DIR = os.getenv("OCR_LATIN_RECOGNITION_MODEL_DIR", "")
OCR_PDF_RENDER_DPI = int(os.getenv("OCR_PDF_RENDER_DPI", "300"))
OCR_PDF_RENDER_MAX_DPI = int(os.getenv("OCR_PDF_RENDER_MAX_DPI", "400"))
OCR_MAX_IMAGE_PIXELS = int(os.getenv("OCR_MAX_IMAGE_PIXELS", "20000000"))
OCR_MAX_WIDTH = int(os.getenv("OCR_MAX_WIDTH", "6000"))
OCR_MAX_HEIGHT = int(os.getenv("OCR_MAX_HEIGHT", "6000"))
OCR_MAX_TEXT_CHARS_PER_PAGE = int(os.getenv("OCR_MAX_TEXT_CHARS_PER_PAGE", "100000"))
OCR_MAX_TEXT_CHARS_PER_DOCUMENT = int(
    os.getenv("OCR_MAX_TEXT_CHARS_PER_DOCUMENT", "2000000")
)
OCR_TEXT_MIN_MEANINGFUL_CHARS = int(os.getenv("OCR_TEXT_MIN_MEANINGFUL_CHARS", "1"))
OCR_TASK_MAX_RETRIES = int(os.getenv("OCR_TASK_MAX_RETRIES", "3"))
OCR_TASK_RETRY_BASE_SECONDS = int(os.getenv("OCR_TASK_RETRY_BASE_SECONDS", "10"))
OCR_TASK_SOFT_TIME_LIMIT = int(os.getenv("OCR_TASK_SOFT_TIME_LIMIT", str(25 * 60)))
OCR_TASK_TIME_LIMIT = int(os.getenv("OCR_TASK_TIME_LIMIT", str(30 * 60)))

# Structured lab extraction (geometry-backed, consumes persisted OCR spans).
LAB_PIPELINE_VERSION = os.getenv("LAB_PIPELINE_VERSION", "lab-v3")
LAB_TASK_SOFT_TIME_LIMIT = int(os.getenv("LAB_TASK_SOFT_TIME_LIMIT", str(5 * 60)))
LAB_TASK_TIME_LIMIT = int(os.getenv("LAB_TASK_TIME_LIMIT", str(10 * 60)))

# Duplicate-protection content fingerprint key (HMAC-SHA256). Separate from the
# JWT/SECRET_KEY material; falls back to a SECRET_KEY-derived value when unset.
DOCUMENT_FINGERPRINT_SECRET = os.getenv(
    "DOCUMENT_FINGERPRINT_SECRET", ""
)


# Identity extraction staging lifetime. A SUCCESSFUL extraction job keeps its
# private staging images until the client finalizes (submit/replace) or this
# TTL elapses, whichever comes first. The worker schedules a delayed
# cleanup_identity_extraction_jobs task at countdown = this TTL.
IDENTITY_STAGING_TTL_SECONDS = int(
    os.getenv("IDENTITY_STAGING_TTL_SECONDS", str(30 * 60))
)
# Pre-registration identity extraction session lifetime. The public client
# uploads National Card images once, OCR runs on the worker, then the user
# reviews and completes registration. Jobs are short-lived; abandoned jobs are
# swept by a Celery cleanup task.
REGISTRATION_IDENTITY_TTL_SECONDS = int(
    os.getenv("REGISTRATION_IDENTITY_TTL_SECONDS", str(30 * 60))
)
# How long the pre-registration extraction RESULT stays in the cache. Kept in
# step with the staging TTL so a review session cannot outlive its staging.
REGISTRATION_IDENTITY_CACHE_TTL_SECONDS = int(
    os.getenv(
        "REGISTRATION_IDENTITY_CACHE_TTL_SECONDS",
        str(30 * 60),
    )
)
DATE_CONTEXT_MAX_CHARS = int(os.getenv("DATE_CONTEXT_MAX_CHARS", "160"))
DATE_SUGGESTION_MIN_SCORE = float(os.getenv("DATE_SUGGESTION_MIN_SCORE", "0.75"))
DATE_SUGGESTION_TIE_TOLERANCE = float(
    os.getenv("DATE_SUGGESTION_TIE_TOLERANCE", "0.01")
)
DATE_FUTURE_TOLERANCE_DAYS = int(os.getenv("DATE_FUTURE_TOLERANCE_DAYS", "14"))
DATE_MAX_CANDIDATES_PER_DOCUMENT = int(
    os.getenv("DATE_MAX_CANDIDATES_PER_DOCUMENT", "500")
)
DATE_PIPELINE_VERSION = os.getenv("DATE_PIPELINE_VERSION", "m9-date-v3")
DATE_TASK_MAX_RETRIES = int(os.getenv("DATE_TASK_MAX_RETRIES", "3"))
DATE_TASK_RETRY_BASE_SECONDS = int(os.getenv("DATE_TASK_RETRY_BASE_SECONDS", "5"))
DATE_TASK_SOFT_TIME_LIMIT = int(os.getenv("DATE_TASK_SOFT_TIME_LIMIT", "120"))
DATE_TASK_TIME_LIMIT = int(os.getenv("DATE_TASK_TIME_LIMIT", "180"))
ACCOUNT_CLAIM_ACTIVATION_MINUTES = int(
    os.getenv("ACCOUNT_CLAIM_ACTIVATION_MINUTES", "30")
)
SEARCH_QUERY_MAX_CHARS = int(os.getenv("SEARCH_QUERY_MAX_CHARS", "200"))

OTP_TTL_MINUTES = int(os.getenv("OTP_TTL_MINUTES", "10"))
OTP_MAX_ATTEMPTS = int(os.getenv("OTP_MAX_ATTEMPTS", "5"))
OTP_RESEND_COOLDOWN_SECONDS = int(os.getenv("OTP_RESEND_COOLDOWN_SECONDS", "60"))
OTP_AUTHORIZATION_TTL_MINUTES = int(
    os.getenv("OTP_AUTHORIZATION_TTL_MINUTES", "5")
)
OTP_ISSUE_RATE_WINDOW_SECONDS = int(
    os.getenv("OTP_ISSUE_RATE_WINDOW_SECONDS", "3600")
)
OTP_ISSUE_LIMIT_TARGET = int(os.getenv("OTP_ISSUE_LIMIT_TARGET", "5"))
OTP_ISSUE_LIMIT_ACCOUNT = int(os.getenv("OTP_ISSUE_LIMIT_ACCOUNT", "10"))
OTP_ISSUE_LIMIT_SOURCE = int(os.getenv("OTP_ISSUE_LIMIT_SOURCE", "20"))
# Pre-registration email-verification session lifetime (M31B). Generous so a
# user can resume a registration (e.g. after leaving to read the OTP email);
# OTP freshness is enforced independently by the OTP core TTL/cooldown.
REGISTRATION_SESSION_TTL_SECONDS = int(
    os.getenv("REGISTRATION_SESSION_TTL_SECONDS", str(24 * 60 * 60))
)

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "onboarding@resend.dev")

EMAIL_BACKEND = os.getenv(
    "EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend"
)
EMAIL_HOST = os.getenv("EMAIL_HOST", "mail.privateemail.com")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
EMAIL_USE_SSL = env_bool("EMAIL_USE_SSL", False)
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "pmdap@techda.dev")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "PMDAP <pmdap@techda.dev>")
EMAIL_TIMEOUT = int(os.getenv("EMAIL_TIMEOUT", "10"))

if EMAIL_USE_TLS and EMAIL_USE_SSL:
    raise ValueError("EMAIL_USE_TLS and EMAIL_USE_SSL cannot both be enabled.")

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "accounts.authentication.ActiveAccountJWTAuthentication"
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_RATES": {
        "auth_register": "5/hour",
        "auth_login": "10/minute",
        # Public pre-registration OCR runs expensive PaddleOCR: aggressive,
        # dedicated anonymous scopes.
        "registration_identity_extract": "10/minute",
        "registration_identity_poll": "60/minute",
        # M31B anonymous pre-registration email verification.
        "registration_email_start": "5/hour",
        "registration_email_resend": "5/minute",
        "registration_email_verify": "10/minute",
        "registration_email_status": "60/minute",
        "account_claim_submit": "5/hour",
        "account_claim_activation": "10/hour",
        "medical_document_upload": "20/hour",
        "medical_search": "600/minute",
    },
    "EXCEPTION_HANDLER": "common.exceptions.api_exception_handler",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=5),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "USER_ID_FIELD": "uuid",
    "USER_ID_CLAIM": "user_id",
    "AUTH_HEADER_TYPES": ("Bearer",),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Patient Medical Document Archiving Platform API",
    "DESCRIPTION": "Versioned patient medical archive API.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "ENUM_NAME_OVERRIDES": {
        "AccountClaimStatusEnum": "claims.models.PatientAccountClaim.Status",
        "AccountClaimComparisonEnum": "claims.models.PatientAccountClaim.Comparison",
        "ClaimEvidenceDocumentTypeEnum": (
            "claims.models.ClaimIdentityEvidence.DocumentType"
        ),
        "IdentityDocumentTypeEnum": "identities.models.IdentityDocument.DocumentType",
        "RegistrationIdentityDocumentTypeEnum": (
            ("UNIFIED_NATIONAL_CARD", "UNIFIED_NATIONAL_CARD"),
        ),
        "MedicalDocumentTypeEnum": "documents.models.MedicalDocument.DocumentType",
        "IdentityDocumentLifecycleStatusEnum": (
            "identities.models.IdentityDocument.LifecycleStatus"
        ),
    },
}

CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/1")
# Application polling NEVER reads the Celery result backend: every async flow
# writes durable state to PostgreSQL (processing_status / job.status / lab
# extraction rows) and identity/registration extraction results live in the
# Django cache. Celery result keys in Redis are therefore pure overhead.
#
# Default ALL tasks to ignore_result (a per-task override still wins) so the
# result backend cannot accumulate millions of dead keys again, and bound any
# kept result TTL tightly. 6h is far above any polling window because nothing
# polls the result backend at all.
CELERY_TASK_IGNORE_RESULT = True
CELERY_RESULT_EXPIRES = 6 * 60 * 60  # 6 hours
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60
CELERY_TIMEZONE = "Asia/Baghdad"
CELERY_ENABLE_UTC = True

# Privacy-safe app logger config. ONLY identities + registration emit INFO
# summaries (job UUID, timings, confidence buckets — never OCR text, names,
# DOB, identifiers, storage keys or tokens). Everything else stays at Django
# defaults so no sensitive request/response logging is enabled.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "loggers": {
        "identities": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "registration": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

# Belt-and-suspenders expiry sweep for abandoned identity extraction jobs.
# The worker also schedules per-job delayed cleanup (countdown = staging TTL),
# so this beat entry is only needed if a `celery beat` process is deployed.
CELERY_BEAT_SCHEDULE = {
    "cleanup-identity-extraction-jobs": {
        "task": "identities.cleanup_identity_extraction_jobs",
    # The Railway metrics collector is self-rescheduling (no beat process
    # required); this entry only fires if a `celery beat` is deployed.
    "ops-railway-collect-metrics": {
        "task": "ops.railway.collect_metrics",
        "schedule": max(
            30, int(os.getenv("RAILWAY_METRICS_SAMPLE_SECONDS", "30"))
        ),
        "args": [],
    },
        "schedule": int(os.getenv("IDENTITY_STAGING_SWEEP_SECONDS", str(15 * 60))),
        "args": [],
    },
}
